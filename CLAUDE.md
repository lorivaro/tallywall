# tallywall

Custom touch frontend for a house drinks tally, using **Turff** (turff.nl) as the
backend of record. A wall-mounted Galaxy Tab A7 (kiosk browser) shows housemates
as big tiles; tapping one adds +1 of the selected drink. Any device on the LAN
(phones, laptops) may use the same page.

Public repo (MIT), unofficial and not affiliated with Turff (see README disclaimer). Never commit
real names, house names, UIDs or credentials; house-specific notes go in `CLAUDE.local.md` (gitignored).

## Architecture

```
Tablet / phones (browser) ──LAN──> tallywall backend (Docker, N100, behind Caddy)
                                        │  local SQLite: tally log + outbox
                                        └──HTTPS──> web.turff.nl/api (unofficial)
```

- **Backend**: Python 3.12, FastAPI, httpx, SQLite. Managed with `uv`. One container.
- **Frontend**: static `index.html` + vanilla JS + CSS, served by the backend.
  No framework, no build step. Target: Chrome/WebView on Android 12, 2000×1200 landscape.
- The browser never talks to Turff and never sees Turff credentials or personal data.

## Turff API (unofficial — reverse-engineered from web.turff.nl, may change)

Base: `https://web.turff.nl/api`. Auth: `POST /v2/login` `{email, password}` sets an
HttpOnly session cookie. Keep a cookie jar in the client; on 401 log in again once, then fail.

| Purpose | Call |
|---|---|
| House: persons, groups, drinks, settings | `GET /v2/house/{house}` |
| Drinks with price/VAT/rank | `GET /tablet/{house}/settings` |
| Counts per person+group for one drink | `POST /tablet/{house}/getStands` `{itemUID}` |
| **Add tally** | `POST /tablet/{house}/setStands` `[{itemUID, userUID, isGroup, amount}]` |
| Tally log | `POST /log/{house}/turf` `{itemUID?, userUID?, limit, offset, minDate?, maxDate?}` |
| Screensaver ads | `GET /screensaver/{house}` |
| Stats | `GET /house/{house}/dashboard/{summary,breakdown,timeseries}?startDate&endDate[&granularity=daily|hourly]` |

Verified facts:
- `setStands` `amount` is a **delta** (+1 adds one, −1 removes one). Undo = send −1.
- Prices are integers in **thousandths of a euro** (350 = €0.35).
- Log entries carry `time_stamp` (UTC, second resolution), `amount`, `price`, and
  `artificial: 1` for non-tablet entries. No device information.
- `getStands` rows: `UID, alias, rank, isGroup, stand, total, balance, totalBalance`.
  Assumption (unverified): `stand` = since last settlement, `total` = lifetime.
- `GET /v2/house/{house}` includes housemates' email/phone when read by a manager — **strip before sending to clients**.
- House display name is `info.name`; `settings.name` is an internal id.

Permissions (tested with a RESIDENT account, house privacy mode off):
- Reads all work for a resident: house, drinks, `getStands`, log, screensaver, balance, dashboard stats.
  Residents receive no email/phone for other persons.
- `setStands` **requires the MANAGER (beheerder) role**: a resident gets HTTP 400
  `{"error":{"errorCode":"ERR_HOUSE_USER_IS_NOT_MANAGER", …}}`, even for its own tile.
- House setting `usersPrivacyMode` ("hide sales data and statistics from default users")
  will likely block resident reads; irrelevant once the service account is a manager.
- Errors come back as `{"error":{"errorCode","logId","message"}}`; failed login is HTTP 200 `{"isAuthorized":false}`.

## Service account

The backend logs in as a dedicated Turff account **Admin** (`TURFF_EMAIL`/`TURFF_PASSWORD`),
which must be **beheerder** of the target house. It appears as a person in the house:
hide the service account's own tile (match its uid from `GET /user`).
Verified 2026-11-18 on the test house: as beheerder, Admin can add +1/−1 for itself and for
other residents. Log entries don't record which account made them.

## Houses

- Dev/testing: a dedicated **test house** — the only house writes may go to during development.
- Production: the real house — only via the deployed instance, never from dev.
- The target house UID comes from config (`TURFF_HOUSE_UID`), never hard-coded.

## Backend behaviour

- `GET /api/state` → `{drinks:[{id,name,color,price}], people:[{id,name,color,isGroup,counts:{drinkId:n}}],
  settings:{houseName}, stale, pending}`. List order = sort order. Built from house + settings + one
  getStands per drink, cached 5 s; service account and inactive persons removed. `n` = `stand`
  (or `total` if house setting `tabletShowLifetimeAmounts`) + unsynced outbox deltas.
  Turff down: stale cache with `stale:true`, or 503 if nothing cached.
- `POST /api/tally {person, drink, amount?, device_id?, device_name?}` → 201 `{id, synced}`. `amount` is
  `1` (default) or `-1` (manual removal, may take a count below 0; anything else → 422). Writes the local
  row, wakes the sync worker, waits ≤2 s. 404 for unknown person/drink or the service account.
- `POST /api/tally/{id}/undo` → `{id, synced, cancelled}`. Unsynced original: both rows cancelled
  locally, nothing sent. Otherwise a −1 row is queued. 409 if already undone, >10 min old, or not a +1.
  Not used by the UI (replaced by the +/− toggle), kept for API clients.
- Only the sync worker (`outbox.py`) calls setStands: strict FIFO, backoff 2 s → 60 s.
  `TurffUnavailable`/`TurffAuthError` retry; other `TurffAPIError`s set `failed_at` (logged, dropped).
  Known limit: a lost response after Turff applied a tally is retried → possible duplicate.
- Client IP: last `X-Forwarded-For` hop, only when the peer is in `TRUSTED_PROXIES`.
- Fake mode only: `POST /api/dev/outage {on}` toggles a simulated outage (`TURFF_FAKE_OUTAGE=1` at start).
  `POST /api/dev/fake {op, id, name?, color?, price?, is_group?, drink?, amount?}` simulates changes made
  elsewhere: `op` = `drink` (add/update), `remove_drink`, `person` (add/update), `remove_person`,
  `tally` (person `id` +`amount` of `drink`), `house` (rename to `name`).
- `GET /api/health` → `{ok, pending}`: liveness for the Docker healthcheck, never calls Turff.
- `GET /avatars/{uid}.jpg` serves local photos from `AVATARS_DIR` (default `avatars`); uid must match
  `[A-Za-z0-9_-]+`. Unknown `/api/*` → 404; everything else is the static frontend (`src/tallywall/static/`,
  HTML sent with `Cache-Control: no-cache`).
- Env: `DB_PATH` (default `data/tallywall.db`), `TRUSTED_PROXIES` (default `127.0.0.1,::1`), `HOST`, `PORT`.
  `TURFF_MODE` is `real` or `fake`.

### Local table

```sql
CREATE TABLE tally (
  id          INTEGER PRIMARY KEY,
  person_uid  TEXT NOT NULL,
  drink_uid   TEXT NOT NULL,
  amount      INTEGER NOT NULL,      -- +1, or -1 (manual removal, or an undo row when undoes_id is set)
  undoes_id   INTEGER REFERENCES tally(id),
  ts          TEXT NOT NULL,         -- UTC ISO
  device_id   TEXT, device_name TEXT, ip TEXT, user_agent TEXT,
  is_group    INTEGER NOT NULL DEFAULT 0,  -- sent as setStands isGroup
  synced_at   TEXT,                  -- NULL = still in outbox (unless failed_at)
  failed_at   TEXT,                  -- permanent Turff error; row left the outbox
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT
);
```

## Frontend behaviour

- Grid of person tiles (local photo from `avatars/{personUid}.jpg` if present,
  else initials on the Turff colour) with count badge for the selected drink.
- Drink selector row on top with prices (`nl-NL` euro format); remembers last choice per device, even
  while that drink is temporarily missing from Turff (falls back to the first drink meanwhile).
- Tap → optimistic ±1 and a short confirmation toast; a second tap on the same tile within 100 ms is ignored.
- +/− slide switch in the header (like Turff tablets): on −, taps send `amount: -1` (no floor at 0); only the
  slider changes colour, a tapped tile flashes red. Flips back to + after 10 s without a tap, when the page is hidden, and on reload.
- First visit on a device asks for a name; name + random id in localStorage.
- Every refresh may change anything (drinks, prices, people, names, colours, house name, counts);
  the drink row and the tile grid are rebuilt when their identity/display fields change, badges update in place.
- Idle 60 s → screensaver: clock, Dutch date, house name, top 5 persons (no groups) by total drinks over all
  types, ties share a rank. Drifts ≤24 px per minute. Any touch wakes; the waking touch never tallies.
  Not shown over the name dialog or the connection blocker. Between 23:00 and 07:00 (`NIGHT` in `app.js`) the
  content is dimmed to 30% (a page can't control the backlight).
- All static files are sent with `Cache-Control: no-cache` (ETag revalidation), so a deploy never mixes old
  JS with new HTML.
- Implementation: `src/tallywall/static/{index.html,app.js,app.css}`, UI strings in the `T` object in `app.js`.
  localStorage keys `tallywall.drink`, `tallywall.device` (`{id,name}`; id via `getRandomValues`, since
  `randomUUID` needs a secure context and the tablet uses plain http). Counts shown = server counts + unconfirmed
  local deltas; refresh responses that started before a confirmed tap are dropped. Refresh every 20 s
  (60 s while the screensaver shows), on wake and on visibility/focus.

## Deployment

- `Dockerfile` (multi-stage uv build, non-root uid 1000, healthcheck on `/api/health`) + `compose.yaml`.
- Runs on the house server behind a Caddy **container**: compose joins the external network
  `${CADDY_NETWORK:-caddy}`, no published ports; Caddy proxies `http://…` (plain http on the LAN) to `tallywall:8000`.
  `TRUSTED_PROXIES` = that network's subnet.
- Built on the server from a git checkout: `git pull && docker compose up -d --build`. Bind mounts `./data:/data`,
  `./avatars:/avatars:ro`. Config template: `.env.example`.
- Kiosk setup in README is an unverified sketch until tested on the Tab A7.
- README screenshots in `docs/` are taken in fake mode only (no real names).

## Conventions

- Tests with pytest; Turff HTTP mocked with respx. Never hit Turff in tests.
- `TURFF_MODE=fake` runs an in-memory fake Turff for UI work.
- Secrets only in `.env` (never committed); `avatars/`, `data/` are gitignored.
- Small commits, one feature per commit, conventional messages (`feat:`, `fix:` …).

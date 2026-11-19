import asyncio
import contextlib

import httpx
import pytest

from tallywall.app import create_app
from tallywall.config import load_settings
from tallywall.turff import FakeTurff, TurffAPIError
from tallywall.turff.fake import FAKE_SERVICE_UID

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake():
    return FakeTurff()


@contextlib.asynccontextmanager
async def running(fake, tmp_path, peer="127.0.0.1", **env):
    settings = load_settings({"TURFF_MODE": "fake", "DB_PATH": str(tmp_path / "t.db"), **env})
    app = create_app(settings, turff=fake)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=(peer, 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://tallywall.test") as c:
            c.app = app
            yield c


@pytest.fixture
async def api(fake, tmp_path):
    async with running(fake, tmp_path) as c:
        yield c


def count(state, person, drink="fake-bier"):
    return next(p for p in state["people"] if p["id"] == person)["counts"][drink]


async def test_state(api):
    r = await api.get("/api/state")
    assert r.status_code == 200
    s = r.json()
    assert FAKE_SERVICE_UID not in [p["id"] for p in s["people"]]
    assert s["settings"] == {"houseName": "Fake House"}


async def test_health(api, fake):
    fake.set_outage(True)
    r = await api.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "pending": 0}


async def test_tally_and_undo(api, fake):
    r = await api.post("/api/tally", json={"person": "fake-anna", "drink": "fake-bier", "device_name": "Tab"})
    assert r.status_code == 201
    assert r.json()["synced"] is True
    tally_id = r.json()["id"]
    assert count((await api.get("/api/state")).json(), "fake-anna") == 1

    r = await api.post(f"/api/tally/{tally_id}/undo")
    assert r.json() == {"id": tally_id + 1, "synced": True, "cancelled": False}
    assert count((await api.get("/api/state")).json(), "fake-anna") == 0
    assert [c[1:] for c in fake.calls if c[0] == "add_tally"] == [
        ("fake-anna", "fake-bier", 1, False),
        ("fake-anna", "fake-bier", -1, False),
    ]

    assert (await api.post(f"/api/tally/{tally_id}/undo")).status_code == 409
    assert (await api.post("/api/tally/9999/undo")).status_code == 404


async def test_minus_one_goes_below_zero(api, fake):
    body = {"person": "fake-anna", "drink": "fake-bier", "amount": -1}
    r = await api.post("/api/tally", json=body)
    assert r.status_code == 201 and r.json()["synced"] is True
    assert count((await api.get("/api/state")).json(), "fake-anna") == -1
    assert [c[1:] for c in fake.calls if c[0] == "add_tally"] == [("fake-anna", "fake-bier", -1, False)]
    assert (await api.post(f"/api/tally/{r.json()['id']}/undo")).status_code == 409


@pytest.mark.parametrize("amount", [0, 2, -2, "x"])
async def test_tally_rejects_other_amounts(api, fake, amount):
    r = await api.post("/api/tally", json={"person": "fake-anna", "drink": "fake-bier", "amount": amount})
    assert r.status_code == 422
    assert not [c for c in fake.calls if c[0] == "add_tally"]


async def test_group_tally(api, fake):
    r = await api.post("/api/tally", json={"person": "fake-group-huis", "drink": "fake-fris"})
    assert r.json()["synced"] is True
    assert ("add_tally", "fake-group-huis", "fake-fris", 1, True) in fake.calls


@pytest.mark.parametrize(
    "body,status",
    [
        ({"person": FAKE_SERVICE_UID, "drink": "fake-bier"}, 404),
        ({"person": "nobody", "drink": "fake-bier"}, 404),
        ({"person": "fake-anna", "drink": "nothing"}, 404),
        ({"person": "fake-anna"}, 422),
        ({"person": "fake-anna", "drink": "fake-bier", "device_name": "x" * 65}, 422),
    ],
)
async def test_tally_validation(api, fake, body, status):
    assert (await api.post("/api/tally", json=body)).status_code == status
    assert not [c for c in fake.calls if c[0] == "add_tally"]


async def test_outage_queues_then_syncs(api, fake):
    await api.get("/api/state")  # warm the cache before Turff goes away
    assert (await api.post("/api/dev/outage", json={"on": True})).json() == {"outage": True}

    ids = []
    for _ in range(3):
        r = await api.post("/api/tally", json={"person": "fake-anna", "drink": "fake-bier"})
        assert r.status_code == 201 and r.json()["synced"] is False
        ids.append(r.json()["id"])
    r = await api.post(f"/api/tally/{ids[-1]}/undo")
    assert r.json()["cancelled"] is True

    api.app.state.tally_state.invalidate()
    s = (await api.get("/api/state")).json()
    assert s["stale"] is True and s["pending"] == 2 and count(s, "fake-anna") == 2

    await api.post("/api/dev/outage", json={"on": False})
    for _ in range(100):
        if api.app.state.store.pending_count() == 0:
            break
        await asyncio.sleep(0.02)
    s = (await api.get("/api/state")).json()
    assert (s["stale"], s["pending"], count(s, "fake-anna")) == (False, 0, 2)
    assert (await fake.stands("fake-bier"))[1].stand == 2


async def test_state_503_without_cache(api):
    await api.post("/api/dev/outage", json={"on": True})
    assert (await api.get("/api/state")).status_code == 503
    r = await api.post("/api/tally", json={"person": "fake-anna", "drink": "fake-bier"})
    assert r.status_code == 503


async def test_permanent_error_is_not_synced(api, fake):
    await api.get("/api/state")
    fake.fail_next(TurffAPIError("no", status=400, error_code="ERR_HOUSE_USER_IS_NOT_MANAGER"))
    r = await api.post("/api/tally", json={"person": "fake-anna", "drink": "fake-bier"})
    assert r.json()["synced"] is False
    row = api.app.state.store.get(r.json()["id"])
    assert row.failed_at and "NOT_MANAGER" in row.last_error


async def test_restart_resumes_outbox(fake, tmp_path):
    fake.outage = True
    async with running(fake, tmp_path) as api:
        api.app.state.store.add("fake-anna", "fake-bier")
    fake.outage = False
    async with running(fake, tmp_path) as api:
        for _ in range(100):
            if api.app.state.store.pending_count() == 0:
                break
            await asyncio.sleep(0.02)
        assert api.app.state.store.pending_count() == 0
    assert (await fake.stands("fake-bier"))[1].stand == 1


@pytest.mark.parametrize(
    "peer,xff,expected",
    [
        ("127.0.0.1", "203.0.113.9, 192.168.1.20", "192.168.1.20"),  # trusted proxy: last hop
        ("127.0.0.1", None, "127.0.0.1"),
        ("192.168.1.50", "6.6.6.6", "192.168.1.50"),  # untrusted peer: header ignored
    ],
)
async def test_client_ip(fake, tmp_path, peer, xff, expected):
    async with running(fake, tmp_path, peer=peer) as api:
        headers = {"user-agent": "Kiosk/1.0"} | ({"x-forwarded-for": xff} if xff else {})
        r = await api.post("/api/tally", json={"person": "fake-anna", "drink": "fake-bier"}, headers=headers)
        store = api.app.state.store
        ip, ua = store._db.execute(
            "SELECT ip, user_agent FROM tally WHERE id = ?", (r.json()["id"],)
        ).fetchone()
    assert (ip, ua) == (expected, "Kiosk/1.0")


async def test_dev_endpoint_absent_in_real_mode(tmp_path, fake):
    settings = load_settings(
        {"TURFF_EMAIL": "a", "TURFF_PASSWORD": "b", "TURFF_HOUSE_UID": "h", "DB_PATH": str(tmp_path / "t.db")}
    )
    app = create_app(settings, turff=fake)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://t") as c,
    ):
        assert (await c.post("/api/dev/outage", json={"on": True})).status_code == 404
        assert (await c.post("/api/dev/fake", json={"op": "house", "name": "x"})).status_code == 404


async def test_frontend_served(api):
    r = await api.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"
    for asset in ("/app.js", "/app.css"):
        r = await api.get(asset)
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-cache"  # else browsers pair new HTML with old JS
    assert (await api.get("/api/state")).headers["content-type"] == "application/json"


@pytest.fixture
async def api_avatars(fake, tmp_path):
    avatars = tmp_path / "avatars"
    avatars.mkdir()
    (avatars / "fake-anna.jpg").write_bytes(b"\xff\xd8jpeg")
    (tmp_path / "secret.jpg").write_bytes(b"secret")
    async with running(fake, tmp_path, AVATARS_DIR=str(avatars)) as c:
        yield c


async def test_avatar(api_avatars):
    r = await api_avatars.get("/avatars/fake-anna.jpg")
    assert r.status_code == 200 and r.content == b"\xff\xd8jpeg"
    assert r.headers["content-type"] == "image/jpeg"
    assert (await api_avatars.get("/avatars/fake-bram.jpg")).status_code == 404
    for uid in ("..%2Fsecret", "..", "a.b"):
        r = await api_avatars.get(f"/avatars/{uid}.jpg")
        assert r.status_code == 404 and r.content != b"secret"


async def test_dev_fake_changes(api):
    async def change(**body):
        r = await api.post("/api/dev/fake", json=body)
        assert r.status_code == 200, r.text
        return (await api.get("/api/state")).json()

    await api.get("/api/state")  # warm the cache: changes must invalidate it

    s = await change(op="drink", id="fake-bier", price=400, name="Pils")
    assert s["drinks"][0] == {"id": "fake-bier", "name": "Pils", "color": "#FFDAC1", "price": 400}

    s = await change(op="drink", id="fake-thee", name="Thee", price=0)
    assert s["drinks"][-1]["id"] == "fake-thee"
    assert all("fake-thee" in p["counts"] for p in s["people"])

    s = await change(op="remove_drink", id="fake-wijn")
    assert "fake-wijn" not in [d["id"] for d in s["drinks"]]

    s = await change(op="person", id="fake-ivo", name="Ivo")
    ids = [p["id"] for p in s["people"]]
    assert ids.index("fake-ivo") == ids.index("fake-hanna") + 1  # after persons, before groups

    s = await change(op="person", id="fake-anna", name="Anne")
    assert next(p for p in s["people"] if p["id"] == "fake-anna")["name"] == "Anne"

    s = await change(op="remove_person", id="fake-bram")
    assert "fake-bram" not in [p["id"] for p in s["people"]]

    s = await change(op="tally", id="fake-eva", drink="fake-fris", amount=3)
    assert count(s, "fake-eva", "fake-fris") == 3

    s = await change(op="house", name="Nieuw Huis")
    assert s["settings"]["houseName"] == "Nieuw Huis"


@pytest.mark.parametrize(
    "body",
    [
        {"op": "drink"},
        {"op": "house"},
        {"op": "tally", "id": "nobody", "drink": "fake-bier"},
        {"op": "tally", "id": "fake-anna", "drink": "fake-bier", "amount": 0},
        {"op": "explode", "id": "x"},
    ],
)
async def test_dev_fake_rejects(api, body):
    assert (await api.post("/api/dev/fake", json=body)).status_code == 422

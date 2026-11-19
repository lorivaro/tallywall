"""Async client for the unofficial Turff web API (web.turff.nl/api).

Not affiliated with or endorsed by Turff; the API is undocumented and may change (see README, Disclaimer).

Logs in lazily, keeps the session cookie in httpx's cookie jar, and on a 401
logs in again once and retries the request once.
"""

from __future__ import annotations

import asyncio
import logging
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Self

import httpx

from .errors import TurffAPIError, TurffAuthError, TurffUnavailable
from .models import Drink, House, Stand

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(10.0)


def _user_agent() -> str:
    try:
        v = version("tallywall")
    except PackageNotFoundError:  # running from a source tree without install
        v = "dev"
    return f"tallywall/{v} (+https://github.com/lorivaro/tallywall)"


USER_AGENT = _user_agent()


class TurffClient:
    def __init__(
        self,
        *,
        email: str,
        password: str,
        house_uid: str,
        base_url: str = "https://web.turff.nl/api",
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    ) -> None:
        if not house_uid:
            raise ValueError("house_uid is required")
        self._email = email
        self._password = password
        self.house_uid = house_uid
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        self._login_lock = asyncio.Lock()
        # Incremented on every successful login; 0 = not logged in yet. Lets
        # concurrent requests that all hit a 401 share one re-login.
        self._session_gen = 0

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- public API --------------------------------------------------------

    async def me(self) -> str:
        data = await self._request("GET", "/user")
        try:
            return str(data["user"]["uid"])
        except (TypeError, KeyError) as e:
            raise TurffAPIError("unexpected /user response") from e

    async def house(self) -> House:
        data = await self._request("GET", f"/v2/house/{self.house_uid}")
        if not isinstance(data, dict):
            raise TurffAPIError("unexpected house response")
        return House.from_turff(self.house_uid, data)

    async def drinks(self) -> list[Drink]:
        data = await self._request("GET", f"/tablet/{self.house_uid}/settings")
        if not isinstance(data, list):
            raise TurffAPIError("unexpected tablet settings response")
        drinks = [Drink.from_turff(d) for d in data]
        return sorted(drinks, key=lambda d: (d.rank, d.name.lower()))

    async def stands(self, drink_uid: str) -> list[Stand]:
        data = await self._request("POST", f"/tablet/{self.house_uid}/getStands", json={"itemUID": drink_uid})
        if not isinstance(data, list):
            raise TurffAPIError("unexpected getStands response")
        return [Stand.from_turff(r) for r in data]

    async def add_tally(
        self, person_uid: str, drink_uid: str, amount: int = 1, *, is_group: bool = False
    ) -> None:
        if amount == 0:
            raise ValueError("amount must be non-zero")
        await self._request(
            "POST",
            f"/tablet/{self.house_uid}/setStands",
            json=[
                {
                    "itemUID": drink_uid,
                    "userUID": person_uid,
                    "isGroup": is_group,
                    "amount": amount,
                }
            ],
        )

    # -- internals ---------------------------------------------------------

    async def _ensure_login(self, stale_gen: int | None = None) -> None:
        """Log in if never logged in, or if the session `stale_gen` is still current."""
        async with self._login_lock:
            if self._session_gen != 0 and self._session_gen != stale_gen:
                return  # someone else already (re)logged in
            await self._login()
            self._session_gen += 1

    async def _login(self) -> None:
        log.info("logging in to Turff")
        resp = await self._send("POST", "/v2/login", json={"email": self._email, "password": self._password})
        if resp.status_code >= 500:
            raise TurffUnavailable(f"login failed: HTTP {resp.status_code}")
        body = _json_or_none(resp)
        if resp.status_code != 200 or not isinstance(body, dict) or body.get("isAuthorized") is not True:
            raise TurffAuthError(f"Turff login rejected (HTTP {resp.status_code})")

    async def _request(self, method: str, path: str, *, json: Any = None) -> Any:
        if self._session_gen == 0:
            await self._ensure_login()

        gen = self._session_gen
        resp = await self._send(method, path, json=json)
        if resp.status_code == 401:
            log.info("Turff session expired; logging in again")
            await self._ensure_login(stale_gen=gen)
            resp = await self._send(method, path, json=json)
            if resp.status_code == 401:
                raise TurffAuthError(f"{method} {path}: still unauthorised after re-login")
        return _handle(method, path, resp)

    async def _send(self, method: str, path: str, *, json: Any = None) -> httpx.Response:
        try:
            return await self._http.request(method, path, json=json)
        except httpx.TransportError as e:  # connect/read errors and timeouts
            raise TurffUnavailable(f"{method} {path}: {type(e).__name__}") from e


def _json_or_none(resp: httpx.Response) -> Any:
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _handle(method: str, path: str, resp: httpx.Response) -> Any:
    body = _json_or_none(resp)
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        if resp.status_code >= 500:
            raise TurffUnavailable(f"{method} {path}: HTTP {resp.status_code} {err.get('errorCode')}")
        raise TurffAPIError(
            str(err.get("message") or f"{method} {path} failed"),
            status=resp.status_code,
            error_code=err.get("errorCode"),
            log_id=err.get("logId"),
        )
    if resp.status_code >= 500:
        raise TurffUnavailable(f"{method} {path}: HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise TurffAPIError(f"{method} {path} failed", status=resp.status_code)
    if resp.content and body is None:
        raise TurffAPIError(f"{method} {path}: response is not JSON", status=resp.status_code)
    return body

"""FastAPI app: public state, tally and undo endpoints, outbox worker lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Network, Settings
from .db import TallyNotFound, TallyStore, UndoRejected
from .outbox import SyncWorker
from .state import StateService, StateUnavailable
from .turff import FakeTurff, TurffBackend, TurffError, make_turff

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
AVATAR_UID = re.compile(r"[A-Za-z0-9_-]{1,128}")

SYNC_WAIT = 2.0  # how long a tap waits for Turff before answering `synced: false`


class Frontend(StaticFiles):
    """Static frontend, revalidated on every load (ETag) so the kiosk never mixes old and new files."""

    def file_response(self, full_path: Any, *args: Any, **kwargs: Any) -> Response:
        resp = super().file_response(full_path, *args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


class Device(BaseModel):
    device_id: str | None = Field(None, max_length=64)
    device_name: str | None = Field(None, max_length=64)


class TallyIn(Device):
    person: str = Field(min_length=1, max_length=128)
    drink: str = Field(min_length=1, max_length=128)
    amount: Literal[1, -1] = 1


class Outage(BaseModel):
    on: bool


class FakeChange(BaseModel):
    """Fake mode only: simulate a change made in Turff by someone else."""

    op: Literal["drink", "remove_drink", "person", "remove_person", "tally", "house"]
    id: str = ""  # drink or person uid (unused for "house")
    name: str | None = None
    color: str | None = None
    price: int | None = None  # thousandths of a euro
    is_group: bool = False
    drink: str | None = None  # "tally": drink uid
    amount: int = 1  # "tally": delta


def client_ip(request: Request, trusted: tuple[Network, ...]) -> str | None:
    """Socket peer, or the last X-Forwarded-For hop when the peer is a trusted proxy."""
    peer = request.client.host if request.client else None
    if peer is None:
        return None
    try:
        peer_addr = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if any(peer_addr in net for net in trusted):
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        if hops:
            return hops[-1][:64]
    return peer


def create_app(settings: Settings, turff: TurffBackend | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        backend = turff or make_turff(settings)
        store = TallyStore(settings.db_path)
        state = StateService(backend, store)
        worker = SyncWorker(backend, store, on_synced=state.invalidate)
        task = asyncio.create_task(worker.run(), name="outbox")
        app.state.turff, app.state.store, app.state.tally_state, app.state.worker = (
            backend,
            store,
            state,
            worker,
        )
        log.info(
            "tallywall started: mode=%s, %d tallies in outbox", settings.turff_mode, store.pending_count()
        )
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await backend.aclose()
            store.close()

    app = FastAPI(title="tallywall", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def meta(request: Request) -> dict[str, Any]:
        return {
            "ip": client_ip(request, settings.trusted_proxies),
            "user_agent": (request.headers.get("user-agent") or "")[:256] or None,
        }

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        # Liveness only: never touches Turff, so a Turff outage doesn't restart the container.
        return {"ok": True, "pending": request.app.state.store.pending_count()}

    @app.get("/api/state")
    async def get_state(request: Request) -> dict[str, Any]:
        try:
            return await request.app.state.tally_state.public()
        except StateUnavailable as e:
            raise HTTPException(503, "Turff unreachable") from e

    @app.post("/api/tally", status_code=201)
    async def post_tally(body: TallyIn, request: Request) -> dict[str, Any]:
        store: TallyStore = request.app.state.store
        worker: SyncWorker = request.app.state.worker
        try:
            snap, _ = await request.app.state.tally_state.snapshot()
        except StateUnavailable as e:
            raise HTTPException(503, "Turff unreachable") from e
        person, drink = snap.person(body.person), snap.drink(body.drink)
        if person is None:
            raise HTTPException(404, "unknown person")
        if drink is None:
            raise HTTPException(404, "unknown drink")
        tally_id = store.add(
            person.id,
            drink.id,
            amount=body.amount,
            is_group=person.is_group,
            device_id=body.device_id,
            device_name=body.device_name,
            **meta(request),
        )
        worker.kick()
        return {"id": tally_id, "synced": await worker.wait_for(tally_id, SYNC_WAIT)}

    @app.post("/api/tally/{tally_id}/undo")
    async def post_undo(tally_id: int, request: Request, body: Device | None = None) -> dict[str, Any]:
        store: TallyStore = request.app.state.store
        worker: SyncWorker = request.app.state.worker
        body = body or Device()
        try:
            res = store.undo(
                tally_id, device_id=body.device_id, device_name=body.device_name, **meta(request)
            )
        except TallyNotFound as e:
            raise HTTPException(404, "unknown tally") from e
        except UndoRejected as e:
            raise HTTPException(409, str(e)) from e
        if res.cancelled:
            return {"id": res.id, "synced": True, "cancelled": True}
        worker.kick()
        return {"id": res.id, "synced": await worker.wait_for(res.id, SYNC_WAIT), "cancelled": False}

    if settings.turff_mode == "fake":

        @app.post("/api/dev/outage")
        async def set_outage(body: Outage, request: Request) -> dict[str, bool]:
            fake = request.app.state.turff
            if not isinstance(fake, FakeTurff):
                raise HTTPException(404)
            fake.set_outage(body.on)
            if not body.on:
                request.app.state.worker.retry_now()
            return {"outage": fake.outage}

        @app.post("/api/dev/fake")
        async def fake_change(body: FakeChange, request: Request) -> dict[str, str]:
            fake = request.app.state.turff
            if not isinstance(fake, FakeTurff):
                raise HTTPException(404)
            needs_id = body.op != "house"
            if needs_id and not body.id:
                raise HTTPException(422, "id required")
            match body.op:
                case "drink":
                    fake.upsert_drink(body.id, name=body.name, color=body.color, price=body.price)
                case "remove_drink":
                    fake.remove_drink(body.id)
                case "person":
                    fake.upsert_person(body.id, name=body.name, color=body.color, is_group=body.is_group)
                case "remove_person":
                    fake.remove_person(body.id)
                case "tally":
                    if not body.drink or not body.amount:
                        raise HTTPException(422, "drink and non-zero amount required")
                    try:
                        await fake.external_tally(body.id, body.drink, body.amount)
                    except TurffError as e:
                        raise HTTPException(422, str(e)) from e
                case "house":
                    if not body.name:
                        raise HTTPException(422, "name required")
                    fake.house_name = body.name
            request.app.state.tally_state.invalidate()
            return {"op": body.op}

    @app.get("/avatars/{uid}.jpg")
    async def get_avatar(uid: str) -> FileResponse:
        if not AVATAR_UID.fullmatch(uid):
            raise HTTPException(404)
        path = Path(settings.avatars_dir) / f"{uid}.jpg"
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def api_not_found(rest: str) -> None:
        raise HTTPException(404)  # keep unknown API paths out of the static mount

    # Last: catches everything the API routes above didn't.
    app.mount("/", Frontend(directory=STATIC_DIR, html=True), name="frontend")

    return app

"""Builds the public `/api/state` view from Turff, cached for a few seconds.

Only names, colours, order and counts leave this module: the service account,
inactive persons and all other house data are filtered out here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .db import TallyStore
from .turff import Drink, Person, TurffBackend, TurffError

log = logging.getLogger(__name__)

DEFAULT_TTL = 5.0


class StateUnavailable(Exception):
    """Turff can't be reached and there is no cached state to fall back on."""


@dataclass(frozen=True)
class Snapshot:
    house_name: str
    drinks: tuple[Drink, ...]
    people: tuple[Person, ...]  # tileable: no service account, no inactive persons
    counts: dict[tuple[str, str], int]  # (person_uid, drink_uid) -> count in Turff

    def person(self, uid: str) -> Person | None:
        return next((p for p in self.people if p.id == uid), None)

    def drink(self, uid: str) -> Drink | None:
        return next((d for d in self.drinks if d.id == uid), None)


class StateService:
    def __init__(
        self,
        turff: TurffBackend,
        store: TallyStore,
        *,
        ttl: float = DEFAULT_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._turff = turff
        self._store = store
        self._ttl = ttl
        self._clock = clock
        self._lock = asyncio.Lock()
        self._service_uid: str | None = None
        self._snapshot: Snapshot | None = None
        self._fetched_at = 0.0
        self._stale = False
        self._gen = 0  # bumped by invalidate(); a fetch that straddles it isn't cached

    def invalidate(self) -> None:
        self._gen += 1
        self._fetched_at = 0.0

    async def snapshot(self) -> tuple[Snapshot, bool]:
        """Current snapshot and whether it is stale (Turff unreachable)."""
        async with self._lock:
            if self._snapshot is None or self._clock() - self._fetched_at >= self._ttl:
                gen = self._gen
                try:
                    snap = await self._fetch()
                except TurffError as e:
                    if self._snapshot is None:
                        raise StateUnavailable(str(e)) from e
                    log.warning("Turff unreachable, serving stale state: %s", e)
                    self._stale = True
                    self._fetched_at = self._clock()  # don't hammer Turff on every request
                else:
                    self._snapshot, self._stale = snap, False
                    self._fetched_at = self._clock() if gen == self._gen else 0.0
            return self._snapshot, self._stale

    async def public(self) -> dict[str, Any]:
        snap, stale = await self.snapshot()
        deltas = self._store.pending_deltas()
        return {
            "drinks": [{"id": d.id, "name": d.name, "color": d.color, "price": d.price} for d in snap.drinks],
            "people": [
                {
                    "id": p.id,
                    "name": p.name,
                    "color": p.color,
                    "isGroup": p.is_group,
                    "counts": {
                        d.id: snap.counts.get((p.id, d.id), 0) + deltas.get((p.id, d.id), 0)
                        for d in snap.drinks
                    },
                }
                for p in snap.people
            ],
            "settings": {"houseName": snap.house_name},
            "stale": stale,
            "pending": self._store.pending_count(),
        }

    async def _fetch(self) -> Snapshot:
        if self._service_uid is None:
            self._service_uid = await self._turff.me()
        house, drinks = await asyncio.gather(self._turff.house(), self._turff.drinks())
        stands = await asyncio.gather(*(self._turff.stands(d.id) for d in drinks))
        lifetime = bool(house.settings.get("tabletShowLifetimeAmounts"))
        counts = {
            (row.uid, d.id): (row.total if lifetime else row.stand)
            for d, rows in zip(drinks, stands, strict=True)
            for row in rows
        }
        people = tuple(
            p for p in house.persons if p.active and not (p.id == self._service_uid and not p.is_group)
        )
        return Snapshot(house_name=house.name, drinks=tuple(drinks), people=people, counts=counts)

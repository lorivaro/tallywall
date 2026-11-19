"""Background sync of the outbox to Turff.

This worker is the only code that calls `add_tally` (setStands). One sender
keeps the outbox strictly FIFO (an undo never overtakes its +1) and rules out
two coroutines sending the same row.

Known limit: if Turff applies a tally but the response is lost (read timeout),
the row is retried and Turff counts it twice. The API has no idempotency key.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .db import TallyStore
from .turff import TurffAuthError, TurffBackend, TurffError

log = logging.getLogger(__name__)

MIN_BACKOFF = 2.0
MAX_BACKOFF = 60.0
IDLE_POLL = 30.0  # safety net: look at the outbox even without a kick


class SyncWorker:
    def __init__(
        self,
        turff: TurffBackend,
        store: TallyStore,
        *,
        on_synced: Callable[[], None] | None = None,
        min_backoff: float = MIN_BACKOFF,
        max_backoff: float = MAX_BACKOFF,
    ) -> None:
        self._turff = turff
        self._store = store
        self._on_synced = on_synced
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._wake = asyncio.Event()
        self._wake.set()  # sync whatever is left over from the last run
        self._progress = asyncio.Condition()
        self.backoff = 0.0  # current delay; 0 = Turff believed reachable
        self._retry_at = 0.0

    @property
    def backing_off(self) -> bool:
        return self.backoff > 0

    def kick(self) -> None:
        """New rows in the outbox. Ignored while backing off (Turff is down)."""
        self._wake.set()

    def retry_now(self) -> None:
        """Skip the remaining backoff (e.g. after an outage is known to be over)."""
        self._retry_at = 0.0
        self._wake.set()

    async def wait_for(self, tally_id: int, timeout: float) -> bool:
        """Wait up to `timeout` s for a row to leave the outbox; True if it synced."""

        def done() -> bool:
            row = self._store.get(tally_id)
            return row is None or not row.in_outbox or self.backing_off

        try:
            async with self._progress:
                await asyncio.wait_for(self._progress.wait_for(done), timeout)
        except TimeoutError:
            pass
        row = self._store.get(tally_id)
        return row is not None and row.synced_at is not None

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            delay = max(0.0, self._retry_at - loop.time()) if self.backing_off else IDLE_POLL
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except TimeoutError:
                pass
            self._wake.clear()
            if self.backing_off and loop.time() < self._retry_at:
                continue
            try:
                ok = await self.sync_once()
            except Exception:  # never let the worker die
                log.exception("outbox sync crashed")
                ok = False
            if ok:
                self.backoff = 0.0
            else:
                self.backoff = min(self._max_backoff, max(self._min_backoff, self.backoff * 2))
                self._retry_at = loop.time() + self.backoff
                log.info("Turff unreachable; retrying outbox in %.0f s", self.backoff)
            await self._notify()

    async def sync_once(self) -> bool:
        """Send outbox rows in order. False if a retryable error stopped the pass."""
        while rows := self._store.pending():
            for queued in rows:
                self._store.in_flight = queued.id
                try:
                    row = self._store.get(queued.id)  # may have been cancelled by an undo meanwhile
                    if row is None or not row.in_outbox:
                        continue
                    try:
                        await self._turff.add_tally(
                            row.person_uid, row.drink_uid, row.amount, is_group=row.is_group
                        )
                    except TurffError as e:
                        # Auth errors are retried too: a changed password shouldn't drop taps.
                        if e.retryable or isinstance(e, TurffAuthError):
                            self._store.mark_retry(row.id, f"{type(e).__name__}: {e}")
                            log.warning("tally %d not synced: %s", row.id, e)
                            return False
                        self._store.mark_failed(row.id, f"{type(e).__name__}: {e}")
                        log.error("tally %d rejected by Turff, dropped from outbox: %s", row.id, e)
                    except Exception as e:
                        self._store.mark_retry(row.id, f"{type(e).__name__}: {e}")
                        raise
                    else:
                        self._store.mark_synced(row.id)
                        if self._on_synced:
                            self._on_synced()
                finally:
                    self._store.in_flight = None
                await self._notify()
        return True

    async def _notify(self) -> None:
        async with self._progress:
            self._progress.notify_all()

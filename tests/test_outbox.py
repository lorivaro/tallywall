import asyncio

import pytest

from tallywall.db import TallyStore
from tallywall.outbox import SyncWorker
from tallywall.turff import FakeTurff, TurffAPIError, TurffAuthError

pytestmark = pytest.mark.anyio


@pytest.fixture
def store():
    s = TallyStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def fake():
    return FakeTurff()


def sent(fake):
    return [c[1:] for c in fake.calls if c[0] == "add_tally"]


async def test_sync_once_fifo_with_undo(fake, store):
    synced = []
    worker = SyncWorker(fake, store, on_synced=lambda: synced.append(1))
    a = store.add("fake-anna", "fake-bier")
    store.add("fake-group-huis", "fake-fris", is_group=True)
    store.mark_synced(a)  # pretend a was already sent
    store.undo(a)

    assert await worker.sync_once() is True
    assert sent(fake) == [("fake-group-huis", "fake-fris", 1, True), ("fake-anna", "fake-bier", -1, False)]
    assert store.pending() == [] and len(synced) == 2


async def test_retryable_error_stops_pass(fake, store):
    worker = SyncWorker(fake, store)
    a, b = store.add("fake-anna", "fake-bier"), store.add("fake-bram", "fake-bier")
    fake.fail_next()
    assert await worker.sync_once() is False
    assert [t.id for t in store.pending()] == [a, b]
    assert store.get(a).attempts == 1 and "Unavailable" in store.get(a).last_error
    assert len(sent(fake)) == 1  # b was not attempted

    fake.fail_next(TurffAuthError("bad password"))
    assert await worker.sync_once() is False
    assert await worker.sync_once() is True
    assert store.get(a).attempts == 3 and store.get(a).synced_at


async def test_permanent_error_drops_row(fake, store):
    worker = SyncWorker(fake, store)
    a, b = store.add("fake-anna", "fake-bier"), store.add("fake-bram", "fake-bier")
    fake.fail_next(TurffAPIError("no", status=400, error_code="ERR_HOUSE_USER_IS_NOT_MANAGER"))
    assert await worker.sync_once() is True
    assert store.get(a).failed_at and "NOT_MANAGER" in store.get(a).last_error
    assert store.get(b).synced_at


async def test_run_backs_off_and_recovers(fake, store):
    worker = SyncWorker(fake, store, min_backoff=0.05, max_backoff=0.2)
    fake.outage = True
    task = asyncio.create_task(worker.run())
    try:
        ids = [store.add("fake-anna", "fake-bier") for _ in range(3)]
        worker.kick()
        assert await worker.wait_for(ids[0], 1) is False
        assert worker.backing_off
        await asyncio.sleep(0.3)
        assert worker.backoff == 0.2  # capped
        assert len(store.pending()) == 3

        fake.outage = False
        for _ in range(100):  # next retry is at most 0.2 s away
            if not store.pending():
                break
            await asyncio.sleep(0.02)
        assert all(store.get(i).synced_at for i in ids)
        assert not worker.backing_off
        assert (await fake.stands("fake-bier"))[1].stand == 3
    finally:
        task.cancel()


async def test_wait_for_returns_when_synced(fake, store):
    worker = SyncWorker(fake, store)
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.sleep(0)
        a = store.add("fake-anna", "fake-bier")
        worker.kick()
        assert await worker.wait_for(a, 1) is True
    finally:
        task.cancel()

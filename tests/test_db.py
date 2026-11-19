from datetime import timedelta

import pytest

from tallywall.db import CANCELLED, TallyNotFound, TallyStore, UndoRejected, utcnow


@pytest.fixture
def store(tmp_path):
    s = TallyStore(tmp_path / "data" / "t.db")
    yield s
    s.close()


def test_add_goes_to_outbox(store):
    a = store.add("p1", "d1", device_id="dev", device_name="Tablet", ip="10.0.0.2", user_agent="UA")
    b = store.add("g1", "d1", is_group=True)
    assert [t.id for t in store.pending()] == [a, b]
    assert store.get(b).is_group
    assert store.pending_deltas() == {("p1", "d1"): 1, ("g1", "d1"): 1}
    assert store.pending_count() == 2


def test_add_minus_one(store):
    a = store.add("p1", "d1", amount=-1)
    assert store.get(a).amount == -1 and store.get(a).undoes_id is None
    assert store.pending_deltas() == {("p1", "d1"): -1}
    with pytest.raises(UndoRejected):
        store.undo(a)


@pytest.mark.parametrize("amount", [0, 2, -2])
def test_add_rejects_other_amounts(store, amount):
    with pytest.raises(ValueError):
        store.add("p1", "d1", amount=amount)


def test_mark_synced_retry_failed(store):
    a, b, c = (store.add("p1", "d1") for _ in range(3))
    store.mark_retry(a, "down")
    store.mark_synced(a)
    store.mark_failed(b, "nope")
    assert [t.id for t in store.pending()] == [c]
    ta, tb = store.get(a), store.get(b)
    assert ta.synced_at and ta.attempts == 2 and ta.last_error is None
    assert tb.failed_at and tb.last_error == "nope" and not tb.in_outbox


def test_undo_of_unsynced_cancels_locally(store):
    a = store.add("p1", "d1")
    res = store.undo(a)
    assert res.cancelled
    assert store.pending() == []
    assert store.pending_deltas() == {}
    assert store.get(a).last_error == CANCELLED
    assert store.get(res.id).undoes_id == a and store.get(res.id).amount == -1


def test_undo_of_synced_queues_minus_one(store):
    a = store.add("g1", "d1", is_group=True)
    store.mark_synced(a)
    res = store.undo(a)
    assert not res.cancelled
    [row] = store.pending()
    assert (row.id, row.amount, row.is_group) == (res.id, -1, True)
    assert store.pending_deltas() == {("g1", "d1"): -1}


def test_undo_of_in_flight_row_is_not_cancelled(store):
    a = store.add("p1", "d1")
    store.in_flight = a
    res = store.undo(a)
    assert not res.cancelled
    assert [t.id for t in store.pending()] == [a, res.id]


def test_undo_rejections(store):
    with pytest.raises(TallyNotFound):
        store.undo(999)
    a = store.add("p1", "d1")
    store.mark_synced(a)
    u = store.undo(a).id
    with pytest.raises(UndoRejected, match="already"):
        store.undo(a)
    with pytest.raises(UndoRejected, match="only"):
        store.undo(u)
    old = store.add("p1", "d1", now=utcnow() - timedelta(minutes=11))
    with pytest.raises(UndoRejected, match="old"):
        store.undo(old)
    failed = store.add("p1", "d1")
    store.mark_failed(failed, "x")
    with pytest.raises(UndoRejected, match="never"):
        store.undo(failed)


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "t.db"
    s = TallyStore(path)
    a = s.add("p1", "d1")
    s.close()
    s = TallyStore(path)
    assert [t.id for t in s.pending()] == [a]
    s.close()

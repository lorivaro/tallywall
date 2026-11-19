import json

import pytest

from tallywall.db import TallyStore
from tallywall.state import StateService, StateUnavailable
from tallywall.turff import FakeTurff
from tallywall.turff.fake import FAKE_SERVICE_UID

from .conftest import HOUSE, load_fixture

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def store():
    s = TallyStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake():
    return FakeTurff()


@pytest.fixture
def state(fake, store, clock):
    return StateService(fake, store, clock=clock)


async def test_public_shape_and_filtering(state, fake):
    await fake.add_tally("fake-anna", "fake-bier")
    s = await state.public()

    assert [d["name"] for d in s["drinks"]] == ["Bier", "Fris", "Wijn"]
    assert set(s["drinks"][0]) == {"id", "name", "color", "price"}
    ids = [p["id"] for p in s["people"]]
    assert FAKE_SERVICE_UID not in ids
    assert ids[0] == "fake-anna" and ids[-1] == "fake-group-huis"
    anna = s["people"][0]
    assert set(anna) == {"id", "name", "color", "isGroup", "counts"}
    assert anna["counts"] == {"fake-bier": 1, "fake-fris": 0, "fake-wijn": 0}
    assert s["people"][-1]["isGroup"] is True
    assert s["settings"] == {"houseName": "Fake House"}
    assert (s["stale"], s["pending"]) == (False, 0)


async def test_cache_and_invalidate(state, fake, clock):
    await state.public()
    await fake.add_tally("fake-anna", "fake-bier")
    assert (await state.public())["people"][0]["counts"]["fake-bier"] == 0  # cached
    clock.t += 5
    assert (await state.public())["people"][0]["counts"]["fake-bier"] == 1
    await fake.add_tally("fake-anna", "fake-bier")
    state.invalidate()
    assert (await state.public())["people"][0]["counts"]["fake-bier"] == 2
    assert [c for c in fake.calls if c[0] == "me"] == [("me",)]  # fetched once


async def test_pending_deltas_are_added(state, store):
    store.add("fake-anna", "fake-bier")
    store.add("fake-anna", "fake-bier")
    s = await state.public()
    assert s["people"][0]["counts"]["fake-bier"] == 2
    assert s["pending"] == 2


async def test_stale_when_turff_down(state, fake, clock):
    fake.outage = True
    with pytest.raises(StateUnavailable):
        await state.public()
    fake.outage = False
    await state.public()
    fake.outage = True
    clock.t += 10
    s = await state.public()
    assert s["stale"] is True and len(s["people"]) == 9
    n = len(fake.calls)
    await state.public()  # no retry within ttl
    assert len(fake.calls) == n


async def test_real_client_response_is_filtered(client, turff_api, store):
    turff_api.get("/user").respond(200, json=load_fixture("user.json"))
    turff_api.get(f"/v2/house/{HOUSE}").respond(200, json=load_fixture("house.json"))
    turff_api.get(f"/tablet/{HOUSE}/settings").respond(200, json=load_fixture("settings.json"))
    turff_api.post(f"/tablet/{HOUSE}/getStands").respond(200, json=load_fixture("stands.json"))

    s = await StateService(client, store).public()

    text = json.dumps(s)
    for secret in ("example.invalid", "+316", "4321", "invite-secret", "bobby", "Teststraat", "person-admin"):
        assert secret not in text
    assert [d["name"] for d in s["drinks"]] == ["Fris", "Bier", "Wijn"]
    assert [p["name"] for p in s["people"]] == ["Bob", "Huis"]
    assert s["settings"] == {"houseName": "Testhuis"}

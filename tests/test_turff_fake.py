import pytest

from tallywall.config import load_settings
from tallywall.turff import (
    FakeTurff,
    TurffAPIError,
    TurffBackend,
    TurffClient,
    TurffUnavailable,
    make_turff,
)
from tallywall.turff.fake import FAKE_SERVICE_UID

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake():
    return FakeTurff()


def counts(rows, uid):
    row = next(r for r in rows if r.uid == uid)
    return row.stand, row.total


async def test_house_and_me(fake):
    house = await fake.house()
    assert await fake.me() == FAKE_SERVICE_UID
    assert any(p.id == FAKE_SERVICE_UID for p in house.persons)
    assert any(p.is_group for p in house.persons)


async def test_drinks_sorted(fake):
    drinks = await fake.drinks()
    assert [d.rank for d in drinks] == sorted(d.rank for d in drinks)
    assert all(isinstance(d.price, int) for d in drinks)


async def test_add_and_undo(fake):
    drink = (await fake.drinks())[0].id

    await fake.add_tally("fake-anna", drink)
    await fake.add_tally("fake-anna", drink)
    assert counts(await fake.stands(drink), "fake-anna") == (2, 2)

    await fake.add_tally("fake-anna", drink, -1)
    assert counts(await fake.stands(drink), "fake-anna") == (1, 1)

    other = (await fake.drinks())[1].id
    assert counts(await fake.stands(other), "fake-anna") == (0, 0)


async def test_group_tally(fake):
    drink = (await fake.drinks())[0].id
    await fake.add_tally("fake-group-huis", drink, is_group=True)
    assert counts(await fake.stands(drink), "fake-group-huis") == (1, 1)
    with pytest.raises(TurffAPIError):
        await fake.add_tally("fake-group-huis", drink, is_group=False)


async def test_unknown_ids(fake):
    with pytest.raises(TurffAPIError):
        await fake.add_tally("nobody", "fake-bier")
    with pytest.raises(TurffAPIError):
        await fake.add_tally("fake-anna", "nothing")
    with pytest.raises(TurffAPIError):
        await fake.stands("nothing")
    with pytest.raises(ValueError):
        await fake.add_tally("fake-anna", "fake-bier", 0)


async def test_fail_next(fake):
    fake.fail_next(times=2)
    for _ in range(2):
        with pytest.raises(TurffUnavailable):
            await fake.add_tally("fake-anna", "fake-bier")
    await fake.add_tally("fake-anna", "fake-bier")
    assert counts(await fake.stands("fake-bier"), "fake-anna") == (1, 1)


async def test_make_turff_selects_backend():
    fake = make_turff(load_settings({"TURFF_MODE": "fake"}))
    assert isinstance(fake, FakeTurff)
    assert isinstance(fake, TurffBackend)

    real = make_turff(
        load_settings(
            {
                "TURFF_EMAIL": "a@example.invalid",
                "TURFF_PASSWORD": "x",
                "TURFF_HOUSE_UID": "h",
                "TURFF_BASE_URL": "https://turff.test/api",
            }
        )
    )
    try:
        assert isinstance(real, TurffClient)
        assert isinstance(real, TurffBackend)
    finally:
        await real.aclose()


async def test_outage_toggle():
    fake = make_turff(load_settings({"TURFF_MODE": "fake", "TURFF_FAKE_OUTAGE": "1"}))
    with pytest.raises(TurffUnavailable):
        await fake.drinks()
    fake.set_outage(False)
    assert await fake.drinks()

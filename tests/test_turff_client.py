import asyncio
import json

import httpx
import pytest

from tallywall.turff import (
    Drink,
    TurffAPIError,
    TurffAuthError,
    TurffUnavailable,
)

from .conftest import HOUSE, load_fixture

pytestmark = pytest.mark.anyio


def body(call):
    return json.loads(call.request.content)


# -- login & session ------------------------------------------------------


async def test_logs_in_once_and_reuses_cookie(client, turff_api):
    user = turff_api.get("/user").respond(200, json=load_fixture("user.json"))

    assert await client.me() == "person-admin"
    assert await client.me() == "person-admin"

    assert turff_api["login"].call_count == 1
    assert body(turff_api["login"].calls[0]) == {"email": "svc@example.invalid", "password": "pw"}
    assert user.call_count == 2
    for call in user.calls:
        assert "session_v2=sess-1" in call.request.headers["cookie"]


async def test_identifies_itself_with_user_agent(client, turff_api):
    turff_api.get("/user").respond(200, json=load_fixture("user.json"))

    await client.me()

    for call in turff_api.calls:
        assert call.request.headers["user-agent"].startswith("tallywall/")


async def test_relogin_on_401_then_retry(client, turff_api):
    user = turff_api.get("/user")
    user.side_effect = [
        httpx.Response(200, json=load_fixture("user.json")),
        httpx.Response(401, text="Unauthorized"),
        httpx.Response(200, json=load_fixture("user.json")),
    ]

    await client.me()
    assert await client.me() == "person-admin"

    assert turff_api["login"].call_count == 2
    assert user.call_count == 3


async def test_401_after_relogin_raises_auth_error(client, turff_api):
    turff_api.get("/user").respond(401, text="Unauthorized")

    with pytest.raises(TurffAuthError):
        await client.me()

    # initial lazy login + exactly one re-login
    assert turff_api["login"].call_count == 2


async def test_rejected_login_raises_auth_error(client, turff_api):
    turff_api["login"].respond(200, json={"isAuthorized": False})
    user = turff_api.get("/user").respond(200, json=load_fixture("user.json"))

    with pytest.raises(TurffAuthError):
        await client.me()
    assert user.call_count == 0


async def test_concurrent_401s_share_one_relogin(client, turff_api):
    turff_api.get("/user").respond(200, json=load_fixture("user.json"))
    await client.me()  # establish session (login #1)

    def stands(request):
        # Session is valid only after the second login.
        if turff_api["login"].call_count >= 2:
            return httpx.Response(200, json=load_fixture("stands.json"))
        return httpx.Response(401, text="Unauthorized")

    turff_api.post(f"/tablet/{HOUSE}/getStands").mock(side_effect=stands)

    results = await asyncio.gather(*(client.stands("drink-bier") for _ in range(5)))

    assert all(len(r) == 2 for r in results)
    assert turff_api["login"].call_count == 2


async def test_login_server_error_is_unavailable(client, turff_api):
    turff_api["login"].respond(502)
    with pytest.raises(TurffUnavailable):
        await client.me()


# -- reads ----------------------------------------------------------------


async def test_house_strips_personal_data(client, turff_api):
    turff_api.get(f"/v2/house/{HOUSE}").respond(200, json=load_fixture("house.json"))

    house = await client.house()

    assert house.id == HOUSE
    assert house.name == "Testhuis"
    assert [(p.id, p.name, p.is_group) for p in house.persons] == [
        ("person-admin", "Admin", False),
        ("person-bob", "Bob", False),
        ("grp-1", "Huis", True),
    ]
    assert house.persons[1].color == "#3b82f6"
    assert "tabletLockCode" not in house.settings
    assert house.settings["usersPrivacyMode"] is False

    dumped = repr(house)
    for secret in (
        "example.invalid",
        "+31600000002",
        "bobby",
        "invite-secret-token",
        "Teststraat",
        "1234AB",
        "4321",
    ):
        assert secret not in dumped


async def test_drinks_sorted_with_prices_in_thousandths(client, turff_api):
    turff_api.get(f"/tablet/{HOUSE}/settings").respond(200, json=load_fixture("settings.json"))

    drinks = await client.drinks()

    assert [d.id for d in drinks] == ["drink-fris", "drink-bier", "drink-wijn"]
    assert drinks[1] == Drink(id="drink-bier", name="Bier", color="#C7CEEA", price=350, rank=1, vat=0)


async def test_stands(client, turff_api):
    route = turff_api.post(f"/tablet/{HOUSE}/getStands").respond(200, json=load_fixture("stands.json"))

    rows = await client.stands("drink-bier")

    assert body(route.calls[0]) == {"itemUID": "drink-bier"}
    assert rows[0].uid == "person-admin"
    assert (rows[0].stand, rows[0].total, rows[0].balance, rows[0].total_balance) == (3, 10, -1050, -3500)
    assert rows[1].is_group is False


async def test_unexpected_shape_raises(client, turff_api):
    turff_api.post(f"/tablet/{HOUSE}/getStands").respond(200, json={"oops": True})
    with pytest.raises(TurffAPIError):
        await client.stands("drink-bier")


# -- writes ---------------------------------------------------------------


@pytest.mark.parametrize("amount", [1, -1])
async def test_add_tally_sends_delta(client, turff_api, amount):
    route = turff_api.post(f"/tablet/{HOUSE}/setStands").respond(200, json={})

    await client.add_tally("person-bob", "drink-bier", amount)

    assert body(route.calls[0]) == [
        {"itemUID": "drink-bier", "userUID": "person-bob", "isGroup": False, "amount": amount}
    ]


async def test_add_tally_for_group(client, turff_api):
    route = turff_api.post(f"/tablet/{HOUSE}/setStands").respond(200)

    await client.add_tally("grp-1", "drink-bier", is_group=True)

    assert body(route.calls[0])[0]["isGroup"] is True


async def test_add_tally_rejects_zero(client, turff_api):
    with pytest.raises(ValueError):
        await client.add_tally("person-bob", "drink-bier", 0)
    assert turff_api["login"].call_count == 0


async def test_not_manager_error(client, turff_api):
    turff_api.post(f"/tablet/{HOUSE}/setStands").respond(400, json=load_fixture("not_manager.json"))

    with pytest.raises(TurffAPIError) as exc:
        await client.add_tally("person-bob", "drink-bier")

    err = exc.value
    assert (err.status, err.error_code, err.log_id) == (400, "ERR_HOUSE_USER_IS_NOT_MANAGER", "log-123")
    assert err.retryable is False


# -- transport failures ---------------------------------------------------


async def test_5xx_is_unavailable(client, turff_api):
    turff_api.post(f"/tablet/{HOUSE}/setStands").respond(503, text="Service Unavailable")

    with pytest.raises(TurffUnavailable) as exc:
        await client.add_tally("person-bob", "drink-bier")
    assert exc.value.retryable is True


async def test_connect_error_is_unavailable(client, turff_api):
    turff_api.post(f"/tablet/{HOUSE}/setStands").mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(TurffUnavailable):
        await client.add_tally("person-bob", "drink-bier")


async def test_timeout_is_unavailable(client, turff_api):
    turff_api.get(f"/v2/house/{HOUSE}").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(TurffUnavailable):
        await client.house()

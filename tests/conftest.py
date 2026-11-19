import json
import pathlib

import pytest
import respx

from tallywall.turff import TurffClient

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
# Deliberately not the real Turff host: even an unmocked request can't reach it.
BASE_URL = "https://turff.test/api"
HOUSE = "house-test"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def turff_api():
    """respx router for the Turff API; any unmocked request fails the test."""
    with respx.mock(base_url=BASE_URL, assert_all_mocked=True, assert_all_called=False) as router:
        router.post("/v2/login", name="login").respond(
            200,
            json={"isAuthorized": True, "token": "t"},
            headers={"set-cookie": "session_v2=sess-1; Path=/; HttpOnly"},
        )
        yield router


@pytest.fixture
async def client(turff_api):
    c = TurffClient(email="svc@example.invalid", password="pw", house_uid=HOUSE, base_url=BASE_URL)
    yield c
    await c.aclose()

import pytest

from tallywall.config import DEFAULT_BASE_URL, ConfigError, load_settings


def test_real_mode_requires_credentials():
    with pytest.raises(ConfigError, match="TURFF_EMAIL, TURFF_PASSWORD, TURFF_HOUSE_UID"):
        load_settings({})


def test_real_mode_loads_values():
    s = load_settings(
        {
            "TURFF_EMAIL": " a@b.c ",
            "TURFF_PASSWORD": "pw",
            "TURFF_HOUSE_UID": "h1",
            "TURFF_BASE_URL": "https://x.test/api/",
        }
    )
    assert (s.turff_mode, s.turff_email, s.turff_house_uid) == ("real", "a@b.c", "h1")
    assert s.turff_base_url == "https://x.test/api"
    assert "pw" not in repr(s)


def test_fake_mode_needs_no_credentials():
    s = load_settings({"TURFF_MODE": "FAKE"})
    assert s.turff_mode == "fake"
    assert s.turff_base_url == DEFAULT_BASE_URL


def test_invalid_mode():
    with pytest.raises(ConfigError):
        load_settings({"TURFF_MODE": "prod"})


def test_phase2_defaults_and_values():
    s = load_settings({"TURFF_MODE": "fake"})
    assert s.db_path == "data/tallywall.db"
    assert [str(n) for n in s.trusted_proxies] == ["127.0.0.1/32", "::1/128"]
    assert (s.fake_outage, s.port) == (False, 8000)

    s = load_settings(
        {
            "TURFF_MODE": "fake",
            "TURFF_FAKE_OUTAGE": "1",
            "DB_PATH": "/x/t.db",
            "TRUSTED_PROXIES": "172.16.0.0/12, 10.0.0.1",
            "PORT": "9000",
        }
    )
    assert s.fake_outage and s.db_path == "/x/t.db" and s.port == 9000
    assert [str(n) for n in s.trusted_proxies] == ["172.16.0.0/12", "10.0.0.1/32"]


def test_phase2_invalid_values():
    with pytest.raises(ConfigError, match="TRUSTED_PROXIES"):
        load_settings({"TURFF_MODE": "fake", "TRUSTED_PROXIES": "nope"})
    with pytest.raises(ConfigError, match="PORT"):
        load_settings({"TURFF_MODE": "fake", "PORT": "abc"})
    with pytest.raises(ConfigError, match="TURFF_FAKE_OUTAGE"):
        load_settings(
            {"TURFF_FAKE_OUTAGE": "1", "TURFF_EMAIL": "a", "TURFF_PASSWORD": "b", "TURFF_HOUSE_UID": "c"}
        )

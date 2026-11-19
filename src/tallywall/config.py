"""Settings loaded from the environment (and `.env` when present)."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://web.turff.nl/api"
DEFAULT_DB_PATH = "data/tallywall.db"
DEFAULT_AVATARS_DIR = "avatars"
DEFAULT_TRUSTED_PROXIES = "127.0.0.1,::1"

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    turff_mode: Literal["real", "fake"] = "real"
    turff_email: str = ""
    turff_password: str = ""
    turff_house_uid: str = ""
    turff_base_url: str = DEFAULT_BASE_URL
    fake_outage: bool = False  # TURFF_MODE=fake only: start with Turff "unreachable"
    db_path: str = DEFAULT_DB_PATH
    avatars_dir: str = DEFAULT_AVATARS_DIR  # local photos: {personUid}.jpg
    # Peers whose X-Forwarded-For is believed (Caddy).
    trusted_proxies: tuple[Network, ...] = field(
        default_factory=lambda: _parse_networks(DEFAULT_TRUSTED_PROXIES)
    )
    host: str = "0.0.0.0"
    port: int = 8000

    def __repr__(self) -> str:  # never leak the password into logs
        return (
            f"Settings(turff_mode={self.turff_mode!r}, turff_email={self.turff_email!r}, "
            f"turff_password='***', turff_house_uid={self.turff_house_uid!r}, "
            f"turff_base_url={self.turff_base_url!r}, fake_outage={self.fake_outage!r}, "
            f"db_path={self.db_path!r}, avatars_dir={self.avatars_dir!r}, "
            f"trusted_proxies={[str(n) for n in self.trusted_proxies]!r}, "
            f"host={self.host!r}, port={self.port!r})"
        )


def _parse_networks(value: str) -> tuple[Network, ...]:
    try:
        return tuple(ipaddress.ip_network(v.strip(), strict=False) for v in value.split(",") if v.strip())
    except ValueError as e:
        raise ConfigError(f"TRUSTED_PROXIES: {e}") from e


def _flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_settings(env: Mapping[str, str] | None = None, *, dotenv: bool = True) -> Settings:
    """Build Settings from `env` (default: os.environ, after loading `.env`)."""
    if env is None:
        if dotenv:
            load_dotenv()
        env = os.environ

    mode = env.get("TURFF_MODE", "real").strip().lower()
    if mode not in ("real", "fake"):
        raise ConfigError(f"TURFF_MODE must be 'real' or 'fake', got {mode!r}")

    settings = Settings(
        turff_mode=mode,  # type: ignore[arg-type]
        turff_email=env.get("TURFF_EMAIL", "").strip(),
        turff_password=env.get("TURFF_PASSWORD", ""),
        turff_house_uid=env.get("TURFF_HOUSE_UID", "").strip(),
        turff_base_url=env.get("TURFF_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/"),
        fake_outage=_flag(env.get("TURFF_FAKE_OUTAGE", "")),
        db_path=env.get("DB_PATH", DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH,
        avatars_dir=env.get("AVATARS_DIR", DEFAULT_AVATARS_DIR).strip() or DEFAULT_AVATARS_DIR,
        trusted_proxies=_parse_networks(env.get("TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES)),
        host=env.get("HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=_port(env.get("PORT", "8000")),
    )

    if settings.fake_outage and mode != "fake":
        raise ConfigError("TURFF_FAKE_OUTAGE only works with TURFF_MODE=fake")

    if mode == "real":
        missing = [
            name
            for name, value in (
                ("TURFF_EMAIL", settings.turff_email),
                ("TURFF_PASSWORD", settings.turff_password),
                ("TURFF_HOUSE_UID", settings.turff_house_uid),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"missing required settings for TURFF_MODE=real: {', '.join(missing)}")

    return settings


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        raise ConfigError(f"PORT must be 1-65535, got {value!r}")
    return port

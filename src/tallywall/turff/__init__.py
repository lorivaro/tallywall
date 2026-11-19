"""Turff backend: real HTTP client or in-memory fake, chosen by TURFF_MODE."""

from __future__ import annotations

from ..config import Settings
from .base import TurffBackend
from .client import TurffClient
from .errors import TurffAPIError, TurffAuthError, TurffError, TurffUnavailable
from .fake import FakeTurff
from .models import Drink, House, Person, Stand

__all__ = [
    "Drink",
    "FakeTurff",
    "House",
    "Person",
    "Stand",
    "TurffAPIError",
    "TurffAuthError",
    "TurffBackend",
    "TurffClient",
    "TurffError",
    "TurffUnavailable",
    "make_turff",
]


def make_turff(settings: Settings) -> TurffBackend:
    if settings.turff_mode == "fake":
        return FakeTurff(outage=settings.fake_outage)
    return TurffClient(
        email=settings.turff_email,
        password=settings.turff_password,
        house_uid=settings.turff_house_uid,
        base_url=settings.turff_base_url,
    )

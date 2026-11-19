"""Typed views of Turff responses.

Parsers copy only the fields listed here, so personal data Turff may include
(email, phone, username, bank details, house address, invite token, tablet lock
code) never leaves this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# House settings that must never be forwarded to clients.
_SECRET_SETTINGS = frozenset({"tabletLockCode"})


@dataclass(frozen=True)
class Drink:
    id: str
    name: str
    color: str
    price: int  # thousandths of a euro (350 = €0.35)
    rank: int
    vat: int = 0

    @classmethod
    def from_turff(cls, d: Mapping[str, Any]) -> Drink:
        return cls(
            id=str(d["UID"]),
            name=str(d.get("name") or ""),
            color=str(d.get("color") or ""),
            price=int(d.get("price") or 0),
            rank=int(d.get("rank") or 0),
            vat=int(d.get("vat") or 0),
        )


@dataclass(frozen=True)
class Person:
    """A housemate or a group; both can be tallied."""

    id: str
    name: str
    color: str
    rank: int
    is_group: bool = False
    active: bool = True

    @classmethod
    def from_turff_person(cls, p: Mapping[str, Any]) -> Person:
        return cls(
            id=str(p["UID"]),
            name=str(p.get("alias") or p.get("displayName") or ""),
            color=str(p.get("color") or ""),
            rank=int(p.get("rank") or 0),
            is_group=False,
            active=bool(p.get("accepted", True)),
        )

    @classmethod
    def from_turff_group(cls, g: Mapping[str, Any]) -> Person:
        return cls(
            id=str(g["UID"]),
            name=str(g.get("name") or ""),
            color=str(g.get("color") or ""),
            rank=int(g.get("rank") or 0),
            is_group=True,
            active=bool(g.get("active", 1)),
        )


@dataclass(frozen=True)
class House:
    id: str
    name: str
    persons: tuple[Person, ...]  # persons first, then groups, each ordered by rank
    settings: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_turff(cls, house_uid: str, data: Mapping[str, Any]) -> House:
        raw_settings = data.get("settings") or {}
        persons = sorted(
            (Person.from_turff_person(p) for p in data.get("persons") or []),
            key=lambda p: (p.rank, p.name.lower()),
        )
        groups = sorted(
            (Person.from_turff_group(g) for g in data.get("groups") or []),
            key=lambda p: (p.rank, p.name.lower()),
        )
        return cls(
            id=house_uid,
            # settings.name is an internal id on real houses; the display name is info.name.
            name=str((data.get("info") or {}).get("name") or ""),
            persons=tuple(persons + groups),
            settings={k: v for k, v in raw_settings.items() if k not in _SECRET_SETTINGS},
        )


@dataclass(frozen=True)
class Stand:
    """One row of getStands for a single drink."""

    uid: str
    alias: str
    rank: int
    is_group: bool
    stand: int  # assumed: since last settlement
    total: int  # assumed: lifetime
    balance: int
    total_balance: int

    @classmethod
    def from_turff(cls, r: Mapping[str, Any]) -> Stand:
        return cls(
            uid=str(r["UID"]),
            alias=str(r.get("alias") or ""),
            rank=int(r.get("rank") or 0),
            is_group=bool(r.get("isGroup", False)),
            stand=int(r.get("stand") or 0),
            total=int(r.get("total") or 0),
            balance=int(r.get("balance") or 0),
            total_balance=int(r.get("totalBalance") or 0),
        )

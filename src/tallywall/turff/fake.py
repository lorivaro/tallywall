"""In-memory stand-in for Turff, used with TURFF_MODE=fake (UI work, tests).

Mirrors the real client's interface and semantics: add_tally applies a delta,
stands are per drink, the service account appears as a person.
"""

from __future__ import annotations

import dataclasses

from .errors import TurffAPIError, TurffError, TurffUnavailable
from .models import Drink, House, Person, Stand

FAKE_HOUSE_UID = "fake-house"
FAKE_SERVICE_UID = "fake-admin"

_PERSONS = [
    # uid, name, color
    (FAKE_SERVICE_UID, "Admin", "#64748b"),
    ("fake-anna", "Anna", "#ef4444"),
    ("fake-bram", "Bram", "#f97316"),
    ("fake-chantal", "Chantal", "#eab308"),
    ("fake-daan", "Daan", "#84cc16"),
    ("fake-eva", "Eva", "#14b8a6"),
    ("fake-floris", "Floris", "#3b82f6"),
    ("fake-gijs", "Gijs", "#8b5cf6"),
    ("fake-hanna", "Hanna", "#ec4899"),
]
_GROUPS = [("fake-group-huis", "Huis", "#0ea5e9")]
_DRINKS = [
    Drink(id="fake-bier", name="Bier", color="#FFDAC1", price=350, rank=1),
    Drink(id="fake-fris", name="Fris", color="#B5EAD7", price=250, rank=2),
    Drink(id="fake-wijn", name="Wijn", color="#C7CEEA", price=750, rank=3),
]


class FakeTurff:
    def __init__(self, *, outage: bool = False) -> None:
        self.outage = outage  # while True, every call raises TurffUnavailable
        self.house_uid = FAKE_HOUSE_UID
        self.house_name = "Fake House"
        self._persons = [
            Person(id=uid, name=name, color=color, rank=i + 1)
            for i, (uid, name, color) in enumerate(_PERSONS)
        ] + [
            Person(id=uid, name=name, color=color, rank=i, is_group=True)
            for i, (uid, name, color) in enumerate(_GROUPS)
        ]
        self._by_id = {p.id: p for p in self._persons}
        self._drinks = {d.id: d for d in _DRINKS}
        # (person_uid, drink_uid) -> [stand, total]
        self._counts: dict[tuple[str, str], list[int]] = {}
        self._fail_next: list[TurffError] = []
        self.calls: list[tuple] = []  # (method, *args) for assertions

    # -- test hooks --------------------------------------------------------

    def fail_next(self, exc: TurffError | None = None, times: int = 1) -> None:
        """Make the next `times` calls raise `exc` (default: TurffUnavailable)."""
        for _ in range(times):
            self._fail_next.append(exc or TurffUnavailable("fake Turff: simulated outage"))

    def set_outage(self, on: bool) -> None:
        self.outage = on

    def upsert_drink(
        self, uid: str, *, name: str | None = None, color: str | None = None, price: int | None = None
    ) -> Drink:
        """Add a drink (appended last) or change some of its fields."""
        old = self._drinks.get(uid) or Drink(
            id=uid,
            name=uid,
            color="#cccccc",
            price=0,
            rank=max((d.rank for d in self._drinks.values()), default=0) + 1,
        )
        changes = {k: v for k, v in {"name": name, "color": color, "price": price}.items() if v is not None}
        self._drinks[uid] = drink = dataclasses.replace(old, **changes)
        return drink

    def remove_drink(self, uid: str) -> None:
        self._drinks.pop(uid, None)

    def upsert_person(
        self, uid: str, *, name: str | None = None, color: str | None = None, is_group: bool = False
    ) -> Person:
        """Add a person (after the others of its kind) or rename/recolour one."""
        old = self._by_id.get(uid)
        changes = {k: v for k, v in {"name": name, "color": color}.items() if v is not None}
        if old:
            person = dataclasses.replace(old, **changes)
            self._persons[self._persons.index(old)] = person
        else:
            rank = max((p.rank for p in self._persons if p.is_group == is_group), default=0) + 1
            person = dataclasses.replace(
                Person(id=uid, name=uid, color="#64748b", rank=rank, is_group=is_group), **changes
            )
            last = max((i for i, p in enumerate(self._persons) if p.is_group == is_group), default=-1)
            self._persons.insert(last + 1, person)
        self._by_id[uid] = person
        return person

    def remove_person(self, uid: str) -> None:
        person = self._by_id.pop(uid, None)
        if person:
            self._persons.remove(person)

    async def external_tally(self, person_uid: str, drink_uid: str, amount: int = 1) -> None:
        """A tally made elsewhere (another tablet, the Turff app)."""
        person = self._by_id.get(person_uid)
        if person is None:
            raise TurffAPIError("unknown person", status=400, error_code="ERR_FAKE_UNKNOWN_PERSON")
        await self.add_tally(person_uid, drink_uid, amount, is_group=person.is_group)

    def _maybe_fail(self) -> None:
        if self.outage:
            raise TurffUnavailable("fake Turff: simulated outage")
        if self._fail_next:
            raise self._fail_next.pop(0)

    # -- TurffBackend ------------------------------------------------------

    async def me(self) -> str:
        self.calls.append(("me",))
        self._maybe_fail()
        return FAKE_SERVICE_UID

    async def house(self) -> House:
        self.calls.append(("house",))
        self._maybe_fail()
        return House(
            id=self.house_uid,
            name=self.house_name,
            persons=tuple(self._persons),
            settings={"name": "fake-house-internal", "usersPrivacyMode": False},
        )

    async def drinks(self) -> list[Drink]:
        self.calls.append(("drinks",))
        self._maybe_fail()
        return sorted(self._drinks.values(), key=lambda d: (d.rank, d.name.lower()))

    async def stands(self, drink_uid: str) -> list[Stand]:
        self.calls.append(("stands", drink_uid))
        self._maybe_fail()
        self._drink(drink_uid)
        rows = []
        for p in self._persons:
            stand, total = self._counts.get((p.id, drink_uid), (0, 0))
            rows.append(
                Stand(
                    uid=p.id,
                    alias=p.name,
                    rank=p.rank,
                    is_group=p.is_group,
                    stand=stand,
                    total=total,
                    balance=0,
                    total_balance=0,
                )
            )
        return rows

    async def add_tally(
        self, person_uid: str, drink_uid: str, amount: int = 1, *, is_group: bool = False
    ) -> None:
        if amount == 0:
            raise ValueError("amount must be non-zero")
        self.calls.append(("add_tally", person_uid, drink_uid, amount, is_group))
        self._maybe_fail()
        self._drink(drink_uid)
        person = self._by_id.get(person_uid)
        if person is None or person.is_group != is_group:
            raise TurffAPIError("unknown person", status=400, error_code="ERR_FAKE_UNKNOWN_PERSON")
        counts = self._counts.setdefault((person_uid, drink_uid), [0, 0])
        counts[0] += amount
        counts[1] += amount

    async def aclose(self) -> None:
        pass

    # -- helpers -----------------------------------------------------------

    def _drink(self, drink_uid: str) -> Drink:
        try:
            return self._drinks[drink_uid]
        except KeyError:
            raise TurffAPIError("unknown drink", status=400, error_code="ERR_FAKE_UNKNOWN_ITEM") from None

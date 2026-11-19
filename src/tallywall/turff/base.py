"""Interface shared by the real Turff client and the in-memory fake."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Drink, House, Stand


@runtime_checkable
class TurffBackend(Protocol):
    async def me(self) -> str:
        """UID of the service account (its tile is hidden)."""
        ...

    async def house(self) -> House: ...

    async def drinks(self) -> list[Drink]:
        """Drinks ordered by (rank, name)."""
        ...

    async def stands(self, drink_uid: str) -> list[Stand]: ...

    async def add_tally(
        self, person_uid: str, drink_uid: str, amount: int = 1, *, is_group: bool = False
    ) -> None:
        """Apply a delta: +1 adds one, -1 removes one (undo)."""
        ...

    async def aclose(self) -> None: ...

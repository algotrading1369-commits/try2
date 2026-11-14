"""Instantáneas explícitas: desconocido no equivale a cero ni a ausencia de ataques."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Generic, TypeVar

T = TypeVar("T")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Reading(Generic[T]):
    data: T | None
    observed_at: str
    status: str
    issues: tuple[str, ...] = ()

    @classmethod
    def unavailable(cls, issue: str) -> Reading[Any]:
        return cls(None, utc_now(), "unavailable", (issue,))

    def is_fresh(self, max_age_seconds: float, now: datetime | None = None) -> bool:
        if self.data is None or self.status not in {"complete", "partial"} or max_age_seconds <= 0:
            return False
        try:
            age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(self.observed_at)).total_seconds()
        except (TypeError, ValueError):
            return False
        return 0 <= age <= max_age_seconds


@dataclass(frozen=True)
class CelestialBody:
    id: int
    coordinates: str
    kind: str
    name: str


@dataclass
class BodySnapshot:
    body: CelestialBody
    resources: Reading[dict[str, int]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))
    buildings: Reading[dict[str, int]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))
    facilities: Reading[dict[str, int]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))
    ships: Reading[dict[str, int]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))
    construction_queue: Reading[dict[str, str]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))
    temperature: Reading[dict[str, int]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))
    shipyard_queue: Reading[dict[str, str]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))


@dataclass
class AccountSnapshot:
    player: str
    server: str
    started_at: str
    finished_at: str | None = None
    schema_version: int = 1
    adapter_version: str = "dom-v1"
    max_reading_age_seconds: float = 120
    top1_max_reading_age_seconds: float = 3600
    registry: Reading[list[CelestialBody]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))
    research: Reading[dict[str, int]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))
    research_queue: Reading[dict[str, str]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))
    movements: Reading[dict[str, Any]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))
    fleet_slots: Reading[dict[str, int]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))
    outgoing: Reading[list[dict[str, Any]]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))
    top1: Reading[dict[str, int]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))
    debris: Reading[list[dict[str, Any]]] = field(default_factory=lambda: Reading.unavailable("not_scanned"))
    bodies: list[BodySnapshot] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)

        def add_expiration(value: Any) -> None:
            if isinstance(value, dict):
                if "observed_at" in value and "status" in value and "data" in value:
                    value["valid_until"] = (
                        datetime.fromisoformat(value["observed_at"]) + timedelta(seconds=self.top1_max_reading_age_seconds if value is data.get("top1") else self.max_reading_age_seconds)
                    ).isoformat() if value["status"] in {"complete", "partial"} and value["data"] is not None else None
                for child in value.values():
                    add_expiration(child)
            elif isinstance(value, (tuple, list)):
                for child in value:
                    add_expiration(child)

        add_expiration(data)
        return data

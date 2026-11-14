"""Resolución de destino y ventanas de amenaza. Planeta y luna no son intercambiables."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..domain.models import CelestialBody, Reading
from ..fleet.models import Flight
from ..scanner.dom import Document
from ..scanner import parsing
from .settings import DefenderSettings


@dataclass(frozen=True)
class Threat:
    id: str
    body_id: int | None
    impact_at: str | None
    seconds_to_impact: float | None
    eligible: bool
    reason: str


def resolve_target(event: dict[str, Any], bodies: list[CelestialBody]) -> CelestialBody | None:
    candidates = [body for body in bodies if body.coordinates == event.get("destination")]
    body_id = event.get("destination_id")
    kind = event.get("destination_type")
    if body_id is not None:
        candidates = [body for body in candidates if body.id == body_id]
    if kind is not None:
        candidates = [body for body in candidates if (3 if body.kind == "moon" else 1) == kind]
    # Ni siquiera una coordenada única demuestra por sí sola que el blanco sea planeta.
    if body_id is None and kind not in {1, 3}:
        return None
    return candidates[0] if len(candidates) == 1 else None


def analyze(movements: Reading[dict[str, Any]], registry: Reading[list[CelestialBody]], settings: DefenderSettings, now: datetime | None = None) -> tuple[str, list[Threat]]:
    now = now or datetime.now(timezone.utc)
    if not movements.is_fresh(settings.max_state_age_seconds, now):
        return "unknown", []
    data = movements.data or {}
    state = data.get("threat_status", "unknown")
    threats: list[Threat] = []
    bodies = (registry.data or []) if registry.is_fresh(settings.max_state_age_seconds, now) else []
    for event in data.get("events", []):
        if not event.get("possible_incoming_attack"):
            continue
        body = resolve_target(event, bodies)
        impact = event.get("arrival_at")
        eta: float | None = None
        try:
            eta = (datetime.fromisoformat(impact) - now).total_seconds() if impact else None
        except (ValueError, TypeError):
            pass
        if str(event.get("id", "")).startswith("unidentified-"):
            reason = "event_identity_unverified"
        elif body is None:
            reason = "target_body_unknown_or_ambiguous"
        elif settings.origins and body.id not in settings.origins:
            reason = "body_outside_protected_origins"
        elif event.get("returning") is not False:
            reason = "incoming_direction_unverified"
        elif eta is None:
            reason = "impact_time_unknown"
        elif eta <= settings.minimum_lead_seconds:
            reason = "insufficient_departure_margin"
        elif eta > settings.trigger_seconds:
            reason = "outside_evacuation_window"
        else:
            reason = "eligible"
        threats.append(Threat(str(event["id"]), body.id if body else None, impact, eta, reason == "eligible", reason))
    return state, threats


def escape_guard(document: Document, flight: Flight, settings: DefenderSettings) -> str | None:
    # El ejecutor ya verifica el registro contra el servidor exacto. Aquí se verifica
    # el evento de la evacuación con el destino identificado en sus atributos.
    movement = parsing.movements(document)
    for event in (movement.data or {}).get("events", []):
        if event.get("id") != flight.threat_id:
            continue
        matches_body = event.get("destination") == flight.origin_coordinates and (
            (event.get("destination_id") == flight.origin_id and event.get("destination_type") in {None, flight.origin_type})
            or (event.get("destination_id") is None and event.get("destination_type") == flight.origin_type)
        )
        if not matches_body or not event.get("possible_incoming_attack") or event.get("returning") is not False or event.get("arrival_at") != flight.impact_at:
            return "threat_changed_or_unverified"
        try:
            remaining = (datetime.fromisoformat(flight.impact_at or "") - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError):
            return "impact_time_unverified"
        if not settings.minimum_lead_seconds < remaining <= settings.trigger_seconds:
            return "departure_window_closed"
        # No se elige un destino que figure bajo otra amenaza conocida.
        for other in (movement.data or {}).get("events", []):
            if other.get("possible_incoming_attack") and ((flight.destination_id is not None and other.get("destination_id") == flight.destination_id) or (other.get("destination") == flight.destination and other.get("destination_type") in {None, flight.destination_type})):
                return "escape_destination_under_attack"
        return None
    return "threat_no_longer_verified"


def escape_return_guard(flight: Flight, oneway_seconds: int, settings: DefenderSettings, now: datetime | None = None) -> str | None:
    """Comprueba el regreso del espionaje sin permanencia ni cancelación."""
    if type(oneway_seconds) is not int or oneway_seconds <= 0:
        return "return_time_unverified"
    now = now or datetime.now(timezone.utc)
    try:
        impact = datetime.fromisoformat(flight.impact_at or "")
        if impact.tzinfo is None:
            return "impact_time_unverified"
        if now + timedelta(seconds=2 * oneway_seconds) <= impact + timedelta(seconds=settings.minimum_lead_seconds):
            return "return_before_attack_margin"
    except (TypeError, ValueError, OverflowError):
        return "return_time_unverified"
    return None

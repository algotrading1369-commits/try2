"""Extracción conservadora del HTML renderizado; no conserva HTML ni enlaces de sesión."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from ..domain.models import CelestialBody, Reading, utc_now
from .dom import Document, Node

BUILDINGS = {1: "metal_mine", 2: "crystal_mine", 3: "deuterium_synthesizer", 4: "solar_plant", 12: "fusion_reactor", 22: "metal_storage", 23: "crystal_storage", 24: "deuterium_tank"}
FACILITIES = {14: "robotics_factory", 15: "nanite_factory", 21: "shipyard", 31: "research_lab", 33: "terraformer", 34: "alliance_depot", 36: "space_dock", 41: "lunar_base", 42: "sensor_phalanx", 43: "jump_gate", 44: "missile_silo", 45: "intergalactic_anomaly_scanner"}
RESEARCH = {106: "espionage_technology", 108: "computer_technology", 109: "weapons_technology", 110: "shielding_technology", 111: "armour_technology", 113: "energy_technology", 114: "hyperspace_technology", 115: "combustion_drive", 117: "impulse_drive", 118: "hyperspace_drive", 120: "laser_technology", 121: "ion_technology", 122: "plasma_technology", 123: "intergalactic_research_network", 124: "astrophysics", 199: "graviton_technology"}
SHIPS = {202: "small_cargo", 203: "large_cargo", 204: "light_fighter", 205: "heavy_fighter", 206: "cruiser", 207: "battleship", 208: "colony_ship", 209: "recycler", 210: "espionage_probe", 211: "bomber", 213: "destroyer", 214: "deathstar", 215: "battlecruiser", 218: "reaper", 219: "pathfinder"}


def parse_integer(text: str, *, signed: bool = False) -> int | None:
    """Números enteros ES exactos. Las abreviaturas 1,2 M no se convierten."""
    text = text.strip().replace("−", "-").replace("\u00a0", " ").replace("\u202f", " ")
    sign = r"[+-]?" if signed else ""
    if not re.fullmatch(sign + r"(?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+|[0-9]{1,3}(?: [0-9]{3})+)", text):
        return None
    return int(text.replace(".", "").replace(" ", ""))


def result(data: Any, issues: list[str], *, found: bool = True) -> Reading[Any]:
    return Reading(data if found else None, utc_now(), "partial" if found and issues else "complete" if found else "unavailable", tuple(issues))


def one(nodes: list[Node]) -> Node | None:
    return nodes[0] if len(nodes) == 1 else None


def current_body_id(document: Document) -> int | None:
    value = parse_integer(document.meta("ogame-planet-id") or "")
    return value if value is not None and value > 0 else None


def resources(document: Document) -> Reading[dict[str, int]]:
    values: dict[str, int] = {}
    issues: list[str] = []
    for resource, identifiers in {
        "metal": ("resources_metal", "metal_box"),
        "crystal": ("resources_crystal", "crystal_box"),
        "deuterium": ("resources_deuterium", "deuterium_box"),
        "energy": ("resources_energy", "energy_box"),
    }.items():
        for identifier in identifiers:
            matches = document.by_id(identifier)
            if len(matches) > 1:
                break
            node = one(matches)
            if node is None:
                continue
            # data-raw es exacto; data-value y títulos no se asumen valores actuales.
            value = parse_integer(node.attrs.get("data-raw", node.text), signed=resource == "energy")
            if value is not None:
                values[resource] = value
                break
        if resource not in values:
            issues.append("unreadable_" + resource)
    return result(values, issues, found=bool(values))


def technologies(document: Document, catalog: dict[int, str], *, fleet: bool = False) -> Reading[dict[str, int]]:
    values: dict[str, int] = {}
    issues: list[str] = []
    for identifier, name in catalog.items():
        cards = document.root.find(lambda node: node.has_class("technology") and node.attrs.get("data-technology") == str(identifier))
        card = one(cards)
        if card is None:
            issues.append("missing_or_duplicate_" + name)
            continue
        targets = card.find(lambda node: node.has_class("amount" if fleet else "level"))
        target = one(targets)
        raw = target.text if target is not None else card.text if not fleet else ""
        # Una tarjeta de solo número es la variante observada en el prototipo.
        if target is not None and not fleet:
            raw = re.sub(r"^(?:Nivel|Level)\s*:?\s*", "", raw, flags=re.IGNORECASE)
        value = parse_integer(raw)
        if value is None:
            issues.append("unreadable_" + name)
        else:
            values[name] = value
    return result(values, issues, found=bool(values))


def body_registry(document: Document, origin: str) -> Reading[list[CelestialBody]]:
    bodies: dict[int, CelestialBody] = {}
    conflicted_ids: set[int] = set()
    issues: list[str] = []
    containers = document.root.find(lambda node: node.has_class("smallplanet"))
    if not containers:
        return Reading.unavailable("planet_list_missing")
    for container in containers:
        coords_node = one(container.find(lambda node: node.has_class("planet-koords")))
        coords_text = (coords_node.text if coords_node else "").strip()
        if coords_text.startswith("[") and coords_text.endswith("]"):
            coords_text = coords_text[1:-1]
        coords = re.fullmatch(r"([0-9]+):([0-9]+):([0-9]+)", coords_text)
        if coords is None or not all(int(value) > 0 for value in coords.groups()):
            issues.append("body_coordinates_unreadable")
            continue
        coordinates = ":".join(str(int(value)) for value in coords.groups())
        planet_name = one(container.find(lambda node: node.has_class("planet-name")))
        planets_found = 0
        for link in container.find(lambda node: node.tag == "a" and (node.has_class("planetlink") or node.has_class("moonlink"))):
            url = urlsplit(urljoin(origin, link.attrs.get("href", "")))
            cp = parse_qs(url.query).get("cp", [])
            if url.scheme != "https" or url.netloc != urlsplit(origin).netloc or url.path != "/game/index.php" or len(cp) != 1 or not cp[0].isascii() or not cp[0].isdigit() or int(cp[0]) <= 0:
                issues.append("body_link_invalid")
                continue
            kind = "moon" if link.has_class("moonlink") else "planet"
            name = planet_name.text if kind == "planet" and planet_name else ""
            body = CelestialBody(int(cp[0]), coordinates, kind, name)
            if body.id in conflicted_ids:
                issues.append("conflicting_body_id")
                continue
            previous = bodies.get(body.id)
            if previous is not None and previous != body:
                issues.append("conflicting_body_id")
                bodies.pop(body.id, None)
                conflicted_ids.add(body.id)
                continue
            bodies[body.id] = body
            planets_found += int(kind == "planet")
        if planets_found != 1:
            issues.append("planet_link_missing_or_duplicate")
    return result(list(bodies.values()), issues, found=bool(bodies))


def bool_attribute(value: str | None) -> bool | None:
    return {"true": True, "1": True, "false": False, "0": False}.get(value or "")


def movements(document: Document) -> Reading[dict[str, Any]]:
    """Eventos del panel global. No asume que un panel vacío esté cargado."""
    issues: list[str] = []
    events: list[dict[str, Any]] = []
    alert = one(document.by_id("attack_alert"))
    alert_status = alert.attrs.get("data-status") if alert else None
    rows = document.root.find(lambda node: node.has_class("eventFleet"))
    seen: set[str] = set()
    for index, row in enumerate(rows):
        identifier = row.attrs.get("id", "")
        if not identifier or identifier in seen:
            issues.append("event_id_missing_or_duplicate")
            identifier = f"unidentified-{index}"
        seen.add(identifier)
        mission_raw = row.attrs.get("data-mission-type", "")
        mission = int(mission_raw) if mission_raw.isascii() and mission_raw.isdigit() else None
        returning = bool_attribute(row.attrs.get("data-return-flight"))
        hostile = bool_attribute(row.attrs.get("data-hostile"))
        # El marcador debe pertenecer a la fila o a un hijo, no al texto traducido.
        if hostile is None and row.find(lambda node: node.has_class("hostile")):
            hostile = True
        arrival_raw = row.attrs.get("data-arrival-time", "")
        impact: str | None = None
        if arrival_raw.isascii() and arrival_raw.isdigit():
            try:
                stamp = int(arrival_raw)
                if 946684800 <= stamp <= 4102444800:
                    impact = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                pass
        if mission is None or returning is None or hostile is None or impact is None:
            issues.append("incomplete_event_" + str(len(events)))
        coordinates: dict[str, str | None] = {}
        for field, css_class in (("origin", "coordsOrigin"), ("destination", "destCoords")):
            node = one(row.find(lambda child: child.has_class(css_class)))
            match = re.search(r"\[([0-9]+:[0-9]+:[0-9]+)\]", node.text if node else "")
            coordinates[field] = match.group(1) if match else None
        destination_type_raw = row.attrs.get("data-destination-type", "")
        destination_id_raw = row.attrs.get("data-destination-id", "")
        destination_type = int(destination_type_raw) if destination_type_raw in {"1", "3"} else None
        destination_id = int(destination_id_raw) if destination_id_raw.isascii() and destination_id_raw.isdigit() and int(destination_id_raw) > 0 else None
        events.append({"id": identifier, "mission": mission, "returning": returning,
                       "hostile": hostile, "arrival_at": impact, **coordinates,
                       "destination_type": destination_type, "destination_id": destination_id,
                       "possible_incoming_attack": hostile is True and mission in {1, 2, 9} and returning is not True})
    possible_attack = any(event["possible_incoming_attack"] for event in events)
    if possible_attack or alert_status in {"soon", "attack"}:
        threat = "detected"
    elif alert_status == "noAttack" and not issues:
        threat = "clear"
    else:
        threat = "unknown"
    if threat == "unknown":
        issues.append("attack_status_unknown")
    # Con un listado vacío solo el indicador global es evidencia de ausencia.
    if not rows:
        issues.append("event_list_not_confirmed")
    data = {"threat_status": threat, "events": events, "global_alert_status": alert_status}
    return result(data, issues, found=bool(rows) or alert_status in {"noAttack", "soon", "attack"})


def queue_state(document: Document, *, research: bool = False) -> Reading[dict[str, str]]:
    """Solo acepta estados explícitos en el panel de producción, nunca silencio."""
    panels = document.root.find(lambda node: node.attrs.get("id") == "productionboxBottom" or node.has_class("productionboxBottom"))
    panel = one(panels)
    if panel is None:
        return Reading.unavailable("queue_panel_missing_or_duplicate")
    empty_phrases = {"No hay ninguna investigación en progreso en este momento."} if research else {"No hay edificios en construcción", "No hay edificios en construcción."}
    if panel.find(lambda node: node.has_class("countDown") or node.attrs.get("id") in {"buildingCountdown", "researchCountdown"}):
        return Reading({"state": "busy"}, utc_now(), "complete")
    if panel.text.strip() in empty_phrases:
        return Reading({"state": "free"}, utc_now(), "complete")
    return Reading.unavailable("queue_state_unknown")

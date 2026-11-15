"""Lecturas estrictas de slots, galaxia y movimientos propios."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from xml.etree import ElementTree
from typing import Any

from ..domain.models import Reading, utc_now
from ..scanner.dom import Document
from ..scanner.parsing import one, parse_integer, result
from .models import SHIP_IDS, coordinates


def top1_xml(payload: bytes, expected_server_id: str) -> Reading[dict[str, int]]:
    if len(payload) > 5000000 or b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        return Reading.unavailable("ranking_xml_rejected")
    try:
        root = ElementTree.fromstring(payload)
        if root.tag.rsplit("}", 1)[-1] != "highscore" or root.attrib.get("category") != "1" or root.attrib.get("type") != "0" or root.attrib.get("serverId") != expected_server_id:
            raise ValueError("Origen de ranking inválido")
        rows = [node for node in root if node.tag.rsplit("}", 1)[-1] == "player" and node.attrib.get("position") == "1"]
        if len(rows) != 1 or not rows[0].attrib.get("score", "").isdigit():
            raise ValueError("Top 1 ambiguo")
        observed = datetime.fromtimestamp(int(root.attrib["timestamp"]), timezone.utc).isoformat()
        return Reading({"points": int(rows[0].attrib["score"])}, observed, "complete", ("public_api",))
    except (ElementTree.ParseError, ValueError, KeyError, OverflowError, OSError):
        return Reading.unavailable("ranking_xml_unreadable")


def slots(document: Document) -> Reading[dict[str, int]]:
    data: dict[str, int] = {}
    issues: list[str] = []
    for name, css in (("fleet", "fleetSlots"), ("expedition", "expeditionSlots")):
        nodes = document.root.find(lambda child: child.has_class(css))
        node = one(nodes)
        if nodes:
            matches = re.findall(r"(?<![0-9])([0-9]+)\s*/\s*([0-9]+)(?![0-9])", node.text if node else "")
        else:
            # ES 13.1.0-r5 repite id=slots en el panel de retirada táctica.
            # Cada contador se identifica por su etiqueta, nunca por posición.
            label = "Flotas" if name == "fleet" else "Expediciones"
            pattern = rf"\b{label}:\s*([0-9]+)\s*/\s*([0-9]+)(?![0-9])"
            matches = [match for panel in document.by_id("slots") for match in re.findall(pattern, panel.text)]
        if len(matches) != 1 or int(matches[0][0]) > int(matches[0][1]):
            issues.append(name + "_slots_unknown")
        else:
            data[name + "_used"], data[name + "_total"] = map(int, matches[0])
    return result(data, issues, found=bool(data))


def outgoing(document: Document) -> Reading[list[dict[str, Any]]]:
    container = one(document.by_id("fleetMovements"))
    if container is None or container.attrs.get("data-loaded") != "true":
        from .native import native_outgoing
        return native_outgoing(document)
    flights: list[dict[str, Any]] = []
    issues: list[str] = []
    for row in container.find(lambda node: "data-fleet-id" in node.attrs):
        try:
            identifier = row.attrs["data-fleet-id"]
            if not re.fullmatch(r"[0-9]+", identifier) or any(flight["id"] == identifier for flight in flights):
                raise ValueError("Identificador inválido")
            mission = int(row.attrs["data-mission-type"])
            origin, destination = row.attrs["data-origin"], row.attrs["data-destination"]
            coordinates(origin)
            coordinates(destination)
            ships = json.loads(row.attrs["data-ships"])
            cargo = json.loads(row.attrs["data-resources"])
            if not isinstance(ships, dict) or not ships or any(key not in SHIP_IDS or type(value) is not int or value <= 0 for key, value in ships.items()):
                raise ValueError("Naves desconocidas")
            if not isinstance(cargo, dict) or set(cargo) != {"metal", "crystal", "deuterium"} or any(type(value) is not int or value < 0 for value in cargo.values()):
                raise ValueError("Carga desconocida")
            returning = row.attrs.get("data-return-flight")
            if returning not in {"true", "false"}:
                raise ValueError("Retorno desconocido")
            flights.append({"id": identifier, "mission": mission, "origin": origin, "destination": destination,
                           "destination_type": int(row.attrs["data-destination-type"]), "ships": ships,
                           "resources": cargo, "returning": returning == "true"})
        except (KeyError, ValueError, TypeError):
            issues.append("outgoing_row_unreadable")
    return result(flights, issues)


def debris(document: Document, galaxy: int, system: int, *, location_verified: bool = False) -> Reading[list[dict[str, Any]]]:
    table = one(document.by_id("galaxyContent"))
    if table is None or table.attrs.get("data-galaxy") != str(galaxy) or table.attrs.get("data-system") != str(system) or table.attrs.get("data-loaded") != "true":
        from .native import native_debris
        return native_debris(document,galaxy,system,location_verified=location_verified)
    fields: list[dict[str, Any]] = []
    issues: list[str] = []
    for row in table.find(lambda node: "data-position" in node.attrs):
        position = parse_integer(row.attrs.get("data-position", ""))
        metal = parse_integer(row.attrs.get("data-debris-metal", ""))
        crystal = parse_integer(row.attrs.get("data-debris-crystal", ""))
        if position is None or not 1 <= position <= 15 or metal is None or crystal is None:
            issues.append("debris_row_unknown")
            continue
        if metal + crystal > 0:
            fields.append({"coordinates": f"{galaxy}:{system}:{position}", "metal": metal, "crystal": crystal})
    return result(fields, issues)

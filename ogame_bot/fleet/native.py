"""Conservative visible-DOM movement adapter; no game scripts are evaluated."""
from __future__ import annotations

import re
from typing import Any

from ..brain.catalog import SHIP_BUILD
from ..domain.models import Reading
from ..scanner.dom import Document, Node
from ..scanner.parsing import parse_integer, result
from .models import SHIP_IDS, coordinates


def labelled_amounts(nodes: list[Node]) -> tuple[dict[str,int], dict[str,int]]:
    names = {technology.name_es.casefold():name for name,technology in SHIP_BUILD.items() if name in SHIP_IDS}
    resource_names = {"metal":"metal","cristal":"crystal","deuterio":"deuterium"}
    ships: dict[str,int] = {}
    resources: dict[str,int] = {}
    for node in nodes:
        for row in node.find(lambda child:child.tag == "tr"):
            cells = [child for child in row.children if isinstance(child,Node) and child.tag == "td"]
            if len(cells) != 2:
                continue
            label = cells[0].text.strip().rstrip(":").casefold()
            amount = parse_integer(cells[1].text)
            target = ships if label in names else resources if label in resource_names else None
            if target is None:
                if amount is not None and label and label not in {"total","capacidad de carga"}:
                    raise ValueError("Unknown numeric row")
                continue
            key = names.get(label) or resource_names[label]
            if amount is None or key in target or (target is ships and amount <= 0):
                raise ValueError("Ambiguous amount")
            target[key] = amount
    return ships,resources


def native_outgoing(document: Document) -> Reading[list[dict[str,Any]]]:
    rows = document.root.find(lambda node:node.has_class("fleetDetails"))
    if not rows:
        panels = document.by_id("movement") + document.by_id("fleetMovements")
        phrases = {"No hay flotas en movimiento.","Actualmente no hay flotas en movimiento.","No hay movimientos de flotas."}
        if len(panels) == 1 and any(node.text.strip() in phrases for node in panels[0].walk()):
            return result([],[])
        return Reading.unavailable("outgoing_list_unverified")
    flights: list[dict[str,Any]] = []
    issues: list[str] = []
    for row in rows:
        try:
            identifier = row.attrs.get("data-fleet-id")
            if identifier is None:
                match = re.fullmatch(r"fleet([0-9]+)",row.attrs.get("id",""))
                identifier = match.group(1) if match else None
            if not identifier or not identifier.isdecimal() or any(flight["id"]==identifier for flight in flights):
                raise ValueError("Fleet ID")
            mission = int(row.attrs["data-mission-type"])
            endpoints: dict[str,str] = {}
            for key,css in (("origin","originCoords"),("destination","destinationCoords")):
                values = row.find(lambda child:child.has_class(css))
                if len(values)!=1:
                    raise ValueError("Coordinates")
                match = re.fullmatch(r"\s*\[([0-9]+:[0-9]+:[0-9]+)\]\s*",values[0].text)
                if not match:
                    raise ValueError("Coordinates")
                endpoints[key]=match.group(1)
                coordinates(endpoints[key])
            route = row.find(lambda child:child.has_class("route"))
            if len(route)!=1:
                raise ValueError("Route")
            markers = route[0].find(lambda child:any(token in child.attrs.get("class","").split() for token in ("fleet_icon_forward","fleet_icon_reverse","fleet_icon_reversed")))
            reverse = any("reverse" in marker.attrs.get("class","") for marker in markers)
            forward = any(marker.has_class("fleet_icon_forward") for marker in markers)
            if not markers or reverse == forward:
                raise ValueError("Direction")
            detail_nodes: list[Node] = []
            refs = {node.attrs.get("href","") for node in route[0].walk() if node.tag=="a" and node.attrs.get("href","").startswith("#")}
            for ref in refs:
                detail_nodes.extend(document.by_id(ref[1:]))
            ships,resources = labelled_amounts(detail_nodes or [row])
            if not ships or set(resources)!={"metal","crystal","deuterium"}:
                raise ValueError("Payload")
            target_type = row.attrs.get("data-destination-type")
            if target_type not in {"1","2","3"}:
                target_panels = row.find(lambda child:child.has_class("destinationPlanet") or child.has_class("destination"))
                types = {kind for panel in target_panels for kind,css in ((1,"planet"),(2,"debris"),(3,"moon")) if panel.find(lambda child:child.has_class(css))}
                if endpoints["destination"].endswith(":16") and not types:
                    types = {1}
                if len(types)!=1:
                    raise ValueError("Destination type")
                target_type = str(next(iter(types)))
            flights.append({"id":identifier,"mission":mission,**endpoints,"destination_type":int(target_type),"ships":ships,"resources":resources,"returning":reverse})
        except (ValueError,KeyError,TypeError):
            issues.append("native_outgoing_row_unverified")
    return result(flights,issues)


def native_debris(document: Document, galaxy: int, system: int, *, location_verified: bool = False) -> Reading[list[dict[str,Any]]]:
    if not location_verified:
        galaxy_inputs = document.by_id("galaxy_input")
        system_inputs = document.by_id("system_input")
        location_verified = len(galaxy_inputs)==len(system_inputs)==1 and galaxy_inputs[0].attrs.get("value")==str(galaxy) and system_inputs[0].attrs.get("value")==str(system)
    panels = document.by_id("galaxyContent")
    if not location_verified or len(panels)!=1:
        return Reading.unavailable("galaxy_location_or_load_unverified")
    rows = panels[0].find(lambda node:node.has_class("galaxyRow") or (node.tag=="tr" and node.has_class("row")))
    if not rows:
        return Reading.unavailable("galaxy_rows_unverified")
    fields: list[dict[str,Any]] = []
    issues: list[str] = []
    seen: set[int] = set()
    for row in rows:
        try:
            positions = row.find(lambda child:child.has_class("position"))
            position = parse_integer(row.attrs.get("data-position", ""))
            if position is None and len(positions)==1:
                position = parse_integer(positions[0].text)
            if position is None or not 1 <= position <= 15 or position in seen:
                raise ValueError("Position")
            seen.add(position)
            debris = row.find(lambda child:child.has_class("debris") or child.has_class("debrisField"))
            if len(debris)!=1:
                raise ValueError("Debris cell")
            if debris[0].has_class("no_debris"):
                continue
            nodes = [debris[0]]
            for node in debris[0].walk():
                tooltip = node.attrs.get("title", "")
                if "<table" in tooltip and len(tooltip) <= 10000:
                    nodes.append(Document(tooltip).root)
            _, resources = labelled_amounts(nodes)
            if set(resources) != {"metal","crystal"}:
                raise ValueError("Debris resources")
            if resources["metal"]+resources["crystal"] > 0:
                fields.append({"coordinates":f"{galaxy}:{system}:{position}",**resources})
        except (ValueError,TypeError,KeyError):
            issues.append("native_debris_row_unverified")
    return result(fields,issues)

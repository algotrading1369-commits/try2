"""Choose a meaningful wakeup; estimates never authorize a game order."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from ..brain.catalog import MINES, level_curve
from ..config import Config
from ..domain.models import AccountSnapshot


def seconds_until(value: Any) -> float | None:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return None
        return max(5.0, (parsed - datetime.now(timezone.utc)).total_seconds() + 5)
    except (TypeError, ValueError):
        return None


def next_economic_check(config: Config, snapshot: AccountSnapshot, report: dict[str, Any]) -> tuple[float, str]:
    fallback = config.brain.idle_retry_seconds
    receipt = report.get("result", {})
    delay = seconds_until(receipt.get("retry_at"))
    if delay is not None:
        return delay, "queue_finishes"
    queue_delays = []
    for queue in [snapshot.research_queue, *(body.construction_queue for body in snapshot.bodies)]:
        delay = seconds_until((queue.data or {}).get("ends_at"))
        if queue.is_fresh(config.scanner_max_reading_age_seconds) and delay is not None:
            queue_delays.append(delay)
    if queue_delays:
        return min(queue_delays), "queue_finishes"
    if receipt.get("state") in {"pending", "submitted", "uncertain"}:
        return fallback, "completion_time_unknown"
    estimates = []
    for note in report.get("notes", []):
        cost = note.get("cost")
        body = next((body for body in snapshot.bodies if body.body.id == note.get("body_id")), None)
        if body is None or not isinstance(cost, dict) or config.brain.economy_speed is None:
            continue
        values = body.resources.data or {}
        if values.get("energy", -1) < 0:
            continue
        delays = []
        for mine, (resource, coefficient, _) in MINES.items():
            deficit = cost.get(resource, 0) + config.brain.reserve[resource] - values.get(resource, 0)
            if deficit <= 0:
                continue
            level = (body.buildings.data or {}).get(mine)
            temperature = config.brain.temperatures.get(body.body.id)
            if type(level) is not int or (resource == "deuterium" and temperature is None):
                delays = []
                break
            multiplier = 1.44 - 0.004 * temperature if resource == "deuterium" else 1
            rate = coefficient * level_curve(level) * config.brain.economy_speed * config.brain.production_multipliers[resource] * multiplier
            if rate <= 0 or not math.isfinite(rate):
                delays = []
                break
            delays.append(deficit / rate * 3600)
        if delays:
            estimates.append(max(delays) + 5)
    if estimates:
        return max(30.0, min(min(estimates), fallback)), "resources_estimated"
    return fallback, "idle_recheck"

"""Compare a saved plan with what actually happened.

Pure Python, no Home Assistant imports.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

HOUR = timedelta(hours=1)


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def evaluate_plan(
    rows: list[dict[str, Any]],
    actual_pv: dict[datetime, float],
    actual_house: dict[datetime, float],
    actual_ev: dict[datetime, float],
    soc_log: dict[datetime, float],
    now: datetime,
) -> dict[str, Any]:
    """Hourly plan vs. reality.

    ``rows`` are plan rows as produced by ``optimizer.plan_rows``. Actual
    energies are hourly kWh keyed by UTC hour start, ``actual_house`` is the
    household load *without* EV. ``soc_log`` maps UTC slot starts to the SoC
    (%) measured at that moment.

    Only hours that are over and fully covered by the plan count towards the
    summary (e.g. the first hour of a plan made at 21:07 is reported but
    excluded).
    """
    starts = [datetime.fromisoformat(row["start"]) for row in rows]
    hours: dict[datetime, dict[str, Any]] = {}
    for i, row in enumerate(rows):
        start = starts[i]
        end = starts[i + 1] if i + 1 < len(rows) else start + timedelta(minutes=15)
        hour = start.replace(minute=0, second=0, microsecond=0)
        h = hours.get(hour)
        if h is None:
            h = hours[hour] = {
                "covered": 0.0,
                "pv": 0.0,
                "house": 0.0,
                "ev": 0.0,
                "grid": 0.0,
                "modes": set(),
                "soc": None,
            }
        h["covered"] += (end - start).total_seconds()
        h["pv"] += row["pv"]
        h["house"] += row["load"] - row["ev"]
        h["ev"] += row["ev"]
        h["grid"] += row["grid"]
        h["modes"].add(row["mode"])
        h["soc"] = row["soc"]

    result: list[dict[str, Any]] = []
    totals = {k: 0.0 for k in ("pv_plan", "pv_actual", "house_plan", "house_actual", "ev_plan", "ev_actual")}
    soc_errors: list[float] = []
    for hour in sorted(hours):
        h = hours[hour]
        h["partial"] = h["covered"] < HOUR.total_seconds() - 1
        over = hour + HOUR <= now
        actual_soc = soc_log.get(hour + HOUR) if over else None
        entry = {
            "hour": hour.isoformat(),
            "modes": sorted(h["modes"]),
            "partial": h["partial"],
            "pv_plan": _r(h["pv"]),
            "pv_actual": _r(actual_pv.get(hour)) if over else None,
            "house_plan": _r(h["house"]),
            "house_actual": _r(actual_house.get(hour)) if over else None,
            "ev_plan": _r(h["ev"]),
            "ev_actual": _r(actual_ev.get(hour)) if over else None,
            "grid_plan": _r(h["grid"]),
            "soc_end_plan": h["soc"],
            "soc_end_actual": _r(actual_soc, 1),
        }
        result.append(entry)
        if not over or h["partial"]:
            continue
        for key in ("pv", "house", "ev"):
            if entry[f"{key}_actual"] is not None:
                totals[f"{key}_plan"] += entry[f"{key}_plan"]
                totals[f"{key}_actual"] += entry[f"{key}_actual"]
        if actual_soc is not None:
            soc_errors.append(actual_soc - h["soc"])

    summary: dict[str, Any] = {k: _r(v, 2) for k, v in totals.items()}
    summary["hours_evaluated"] = sum(1 for e in result if not e["partial"] and e["pv_actual"] is not None)
    if soc_errors:
        summary["soc_error_mean"] = _r(sum(soc_errors) / len(soc_errors), 1)
        summary["soc_error_largest"] = _r(max(soc_errors, key=abs), 1)
        summary["soc_error_hint"] = "positive = battery fuller than planned"
    return {"summary": summary, "hours": result}

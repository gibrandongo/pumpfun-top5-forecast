#!/usr/bin/env python3
"""Forecast the top 5 new pump.fun coins by market cap, 24 hours out.

Reads the house snapshot history from /input (history.ndjson + task.json)
and writes one forecast to /output/forecast.json with exactly three keys:
probabilities, top5, sizes.

Stdlib only, deterministic (same history -> same forecast), offline.
No network, no randomness, no wall-clock dependence.

Model, in one paragraph: for every coin, extrapolate its log market cap to
the answer reads with a damped momentum trend (1h and 3h windows), rank by
the extrapolated value, and spread calibrated probability mass around the
expected rank instead of betting everything on one ordering. Size forecasts
are discretized log-normal-ish distributions centred on the extrapolated
place size. Spreading mass is deliberate: the log score punishes confident
wrong calls far more than it rewards confident right ones, and new-coin
market caps 6-12h out are dominated by noise and rugs.

Environment overrides (for local testing only; the sandbox uses defaults):
  MAKEFUN_INPUT  - input directory (default /input)
  MAKEFUN_OUTPUT - output file path (default /output/forecast.json)
"""

import json
import math
import os
from datetime import datetime, timezone

INPUT_DIR = os.environ.get("MAKEFUN_INPUT", "/input")
OUTPUT_PATH = os.environ.get("MAKEFUN_OUTPUT", "/output/forecast.json")

MAX_PROB_ROWS_PER_PLACE = 60
PROB_MASS_PER_PLACE = 0.95
SIZE_MASS_PER_PLACE = 0.97
SIZE_SIGMA_LOG10 = 0.35
MIN_EMITTED_P = 0.001
MAX_SINGLE_P = 0.60
FALLBACK_MINTS = [
    "11111111111111111111111111111111",
    "22222222222222222222222222222222",
    "33333333333333333333333333333333",
    "44444444444444444444444444444444",
    "55555555555555555555555555555555",
]


def parse_time(value):
    """Parse an ISO 8601 UTC timestamp; return None when unparseable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def mcap_sol(row):
    """Market cap in SOL from a history row (lamports as decimal string)."""
    raw = row.get("market_cap_lamports", 0)
    try:
        lamports = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0.0
    if lamports <= 0:
        return 0.0
    return lamports / 1e9


def load_task(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_history(path):
    marks = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    mark = json.loads(line)
                except ValueError:
                    continue
                if isinstance(mark, dict):
                    marks.append(mark)
    except OSError:
        return []
    return marks


def collect_series(marks):
    """Per-mint time series of (mark_index, log10 mcap SOL, rank).

    Marks are oldest-first. A mint absent from a mark's top-200 rows is
    treated as faded: it keeps its last observed value but is flagged stale
    so the forecaster can discount it.
    """
    series = {}
    last_mark_index = len(marks) - 1
    for index, mark in enumerate(marks):
        if mark.get("ok") is False:
            continue
        rows = mark.get("rows")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            mint = row.get("mint")
            if not isinstance(mint, str) or not mint:
                continue
            cap = mcap_sol(row)
            if cap <= 0:
                continue
            entry = series.setdefault(
                mint, {"points": [], "venue": None, "complete": False,
                       "mayhem": False, "created_at": None}
            )
            entry["points"].append(
                (index, math.log10(cap), row.get("rank")))
            entry["venue"] = row.get("venue", entry["venue"])
            entry["complete"] = bool(row.get("complete", entry["complete"]))
            entry["mayhem"] = bool(row.get("mayhem", entry["mayhem"]))
            if entry["created_at"] is None:
                entry["created_at"] = row.get("created_at")
    for mint, entry in series.items():
        entry["stale_marks"] = last_mark_index - entry["points"][-1][0]
    return series


def trend_per_hour(points, marks_per_hour, window_hours):
    """Log10 mcap change per hour over the trailing window, or 0.0."""
    if len(points) < 2 or marks_per_hour <= 0:
        return 0.0
    window_marks = max(1, int(round(window_hours * marks_per_hour)))
    last_index, last_value, _ = points[-1]
    target_index = last_index - window_marks
    base_value = None
    for mark_index, value, _ in points:
        if mark_index <= target_index:
            base_value = value
        else:
            break
    if base_value is None:
        base_value = points[0][1]
        span_marks = last_index - points[0][0]
    else:
        span_marks = window_marks
    if span_marks <= 0:
        return 0.0
    return (last_value - base_value) / (span_marks / marks_per_hour)


def predict_log_mcap(entry, marks_per_hour, horizon_hours):
    """Damped extrapolation of log10 mcap to the answer reads."""
    points = entry["points"]
    last_value = points[-1][1]
    trend_1h = trend_per_hour(points, marks_per_hour, 1.0)
    trend_3h = trend_per_hour(points, marks_per_hour, 3.0)
    trend = 0.6 * trend_1h + 0.4 * trend_3h
    # Damping: momentum persists, but rarely at full strength for hours.
    # Clamp the total move so one hot hour cannot dominate the forecast.
    move = trend * horizon_hours * 0.4
    move = max(-1.0, min(1.0, move))
    predicted = last_value + move
    # Stale coins (dropped out of the top-200 rows) are fading: discount.
    stale = entry.get("stale_marks", 0)
    if stale > 0 and marks_per_hour > 0:
        predicted -= min(0.75, 0.15 * (stale / marks_per_hour))
    return predicted


def stability_bonus(entry, marks_per_hour):
    """Small bonus for coins that held a top rank steadily (0..1 scale)."""
    points = entry["points"]
    if not points:
        return 0.0
    top_ranks = [rank for _, _, rank in points
                 if isinstance(rank, int) and rank <= 20]
    return len(top_ranks) / len(points)


def rank_key(item):
    """Sort key: predicted log mcap desc, then mint bytes ascending."""
    mint, predicted, _ = item
    return (-predicted, mint.encode("utf-8"))


def normalize_rows(weighted, mass, cap, floor, limit):
    """Turn (key, weight) pairs into a rounded distribution summing <= mass.

    Deterministic: sorted by weight desc, then key bytes ascending.
    """
    if not weighted:
        return []
    ordered = sorted(weighted, key=lambda kv: (-kv[1], str(kv[0]).encode()))
    total = sum(weight for _, weight in ordered)
    if total <= 0:
        return []
    rows = []
    for key, weight in ordered[:limit]:
        prob = mass * weight / total
        if prob < floor:
            continue
        rows.append([key, min(cap, prob)])
    # Renormalize after flooring/capping so the sum stays <= mass.
    total = sum(prob for _, prob in rows)
    if total > mass and total > 0:
        rows = [[key, prob * mass / total] for key, prob in rows]
    rounded = [[key, round(prob, 6)] for key, prob in rows]
    rounded = [[key, prob] for key, prob in rounded if prob >= floor]
    total = sum(prob for _, prob in rounded)
    if total > 1.0:
        excess = total - 1.0
        rounded.sort(key=lambda kv: -kv[1])
        for row in rounded:
            cut = min(row[1] - floor, excess)
            if cut > 0:
                row[1] = round(row[1] - cut, 6)
                excess -= cut
            if excess <= 0:
                break
    return rounded


def build_probabilities(ranked, places):
    """Per-place probability rows over the ranked candidate list."""
    rows = []
    n = len(ranked)
    for place in range(1, places + 1):
        weighted = []
        for position, (mint, _, stable) in enumerate(ranked):
            distance = abs(position + 1 - place)
            weight = math.exp(-distance / 2.5) * (0.7 + 0.3 * stable)
            weighted.append((mint, weight))
        for mint, prob in normalize_rows(
                weighted, PROB_MASS_PER_PLACE, MAX_SINGLE_P,
                MIN_EMITTED_P, MAX_PROB_ROWS_PER_PLACE):
            rows.append({"mint": mint, "place": place, "p": prob})
    rows.sort(key=lambda r: (r["place"], -r["p"], r["mint"].encode()))
    return rows


def size_bin_centers(edges):
    """Bin centres for the log10 size bins defined by `edges`.

    81 edges define 82 bins: bin 0 below the first edge, bin 81 above the
    last edge, interior bins between consecutive edges.
    """
    centers = []
    if not edges:
        edges = [2.00 + 0.05 * i for i in range(81)]
    step = 0.05
    if len(edges) >= 2:
        step = (edges[-1] - edges[0]) / (len(edges) - 1)
    centers.append(edges[0] - step / 2.0)
    for i in range(len(edges) - 1):
        centers.append((edges[i] + edges[i + 1]) / 2.0)
    centers.append(edges[-1] + step / 2.0)
    return centers


def build_sizes(ranked, places, edges):
    """Per-place size-bin distributions centred on the extrapolated size."""
    centers = size_bin_centers(edges)
    rows = []
    for place in range(1, places + 1):
        if place <= len(ranked):
            centre = ranked[place - 1][1]
        elif ranked:
            centre = ranked[-1][1]
        else:
            centre = 4.0
        weighted = []
        for bin_index, bin_centre in enumerate(centers):
            z = (bin_centre - centre) / SIZE_SIGMA_LOG10
            weighted.append((bin_index, math.exp(-0.5 * z * z)))
        for bin_index, prob in normalize_rows(
                weighted, SIZE_MASS_PER_PLACE, MAX_SINGLE_P,
                MIN_EMITTED_P, len(centers)):
            rows.append({"place": place, "bin": bin_index, "p": prob})
    rows.sort(key=lambda r: (r["place"], r["bin"]))
    return rows


def build_forecast(marks, task):
    places = task.get("places", 5)
    if not isinstance(places, int) or places < 1:
        places = 5
    places = min(places, 5)

    cadence = task.get("cadence_minutes", 10)
    if not isinstance(cadence, (int, float)) or cadence <= 0:
        cadence = 10
    marks_per_hour = 60.0 / cadence

    horizon_hours = 12.0
    last_at = None
    for mark in reversed(marks):
        last_at = parse_time(mark.get("at"))
        if last_at is not None:
            break
    reads = task.get("reads")
    if isinstance(reads, list) and reads and last_at is not None:
        last_read = parse_time(reads[-1])
        if last_read is not None:
            horizon_hours = (last_read - last_at).total_seconds() / 3600.0
            horizon_hours = max(0.0, min(24.0, horizon_hours))

    series = collect_series(marks)
    ranked = []
    for mint, entry in series.items():
        predicted = predict_log_mcap(entry, marks_per_hour, horizon_hours)
        ranked.append((mint, predicted, stability_bonus(entry, marks_per_hour)))
    ranked.sort(key=rank_key)

    top5 = [mint for mint, _, _ in ranked[:places]]
    if len(top5) < places:
        seen = set(top5)
        for fallback in FALLBACK_MINTS:
            if len(top5) >= places:
                break
            if fallback not in seen:
                top5.append(fallback)
                seen.add(fallback)

    size_cfg = task.get("size", {})
    edges = size_cfg.get("edges") if isinstance(size_cfg, dict) else None
    if not isinstance(edges, list) or len(edges) < 2:
        edges = [2.00 + 0.05 * i for i in range(81)]

    return {
        "probabilities": build_probabilities(ranked, places),
        "top5": top5,
        "sizes": build_sizes(ranked, places, edges),
    }


def main():
    task = load_task(os.path.join(INPUT_DIR, "task.json"))
    marks = load_history(os.path.join(INPUT_DIR, "history.ndjson"))
    forecast = build_forecast(marks, task)
    parent = os.path.dirname(OUTPUT_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(forecast, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

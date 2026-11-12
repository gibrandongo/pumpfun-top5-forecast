"""Predict the top new pump.fun coins by market cap, 24 hours out.

Reads task.json and history.ndjson from the input directory and writes one
forecast.json to the output directory. Standard library only, no network, no
randomness, no wall clock: identical input bytes always produce byte-identical
output bytes.

Input directory: $MAKEFUN_INPUT_DIR, else $MAKEFUN_INPUT, else /input.
Output: $MAKEFUN_OUTPUT_DIR/forecast.json, else $MAKEFUN_OUTPUT as a file
path, else /output/forecast.json.
"""

import json
import math
import os
from datetime import datetime, timezone

LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_PLACES = 5
DEFAULT_EDGES = [2.0 + 0.05 * i for i in range(81)]
DAMPING = 0.7
TREND_WEIGHT_1H = 0.65
MAX_MOVE = 1.0
MAX_HORIZON_HOURS = 24.0
STALE_DISCOUNT_PER_HOUR = 0.05
STALE_DISCOUNT_CAP = 0.3
STABILITY_BONUS = 0.05
PROB_DISTANCE_SCALE = 2.5
PROB_MAX_ROWS_PER_PLACE = 60
PROB_TOTAL_CAP = 0.95
SIZE_TOTAL_CAP = 0.97
SIZE_SIGMA = 0.35
P_MIN = 0.001
P_MAX = 0.60
FALLBACK_LOG_CAP = 4.0
MAX_RATE_PER_HOUR = 2.0
PLACEHOLDER_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def parse_time(value):
    """Parse an ISO-8601 timestamp to epoch seconds, or None if unusable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def load_task(path):
    """Load task.json defensively; missing or malformed input yields defaults."""
    task = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            task = loaded
    except (OSError, ValueError):
        task = {}
    places = task.get("places", DEFAULT_PLACES)
    if isinstance(places, bool) or not isinstance(places, int):
        try:
            places = int(places)
        except (TypeError, ValueError):
            places = DEFAULT_PLACES
    if places < 1 or places > 100:
        places = DEFAULT_PLACES
    reads = []
    raw_reads = task.get("reads")
    if isinstance(raw_reads, list):
        for item in raw_reads:
            stamp = parse_time(item)
            if stamp is not None:
                reads.append(stamp)
    edges = DEFAULT_EDGES
    size = task.get("size")
    if isinstance(size, dict):
        raw_edges = size.get("edges")
        if isinstance(raw_edges, list) and len(raw_edges) >= 1:
            cleaned = []
            usable = True
            for edge in raw_edges:
                if isinstance(edge, bool) or not isinstance(edge, (int, float)):
                    usable = False
                    break
                value = float(edge)
                if not math.isfinite(value):
                    usable = False
                    break
                cleaned.append(value)
            if usable and all(b > a for a, b in zip(cleaned, cleaned[1:])):
                edges = cleaned
    return places, reads, edges


MAX_MARK_LINE_BYTES = 1_000_000


def iter_marks(path):
    """Yield (epoch, rows) for each usable mark line, oldest first.

    The file is read as bytes and each line decoded separately, so one bad
    byte sequence or one absurdly long line cannot poison the whole history.
    """
    try:
        handle = open(path, "rb")
    except OSError:
        return
    with handle:
        for raw in handle:
            if len(raw) > MAX_MARK_LINE_BYTES:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                mark = json.loads(line)
            except ValueError:
                continue
            if not isinstance(mark, dict):
                continue
            if mark.get("ok") is False:
                continue
            stamp = parse_time(mark.get("at"))
            if stamp is None:
                continue
            rows = mark.get("rows")
            if not isinstance(rows, list):
                continue
            yield stamp, rows


def parse_cap_lamports(value):
    """Parse a market-cap value in lamports to a positive float, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def parse_rank(value):
    """Parse a rank to an int, or None if it is not a whole number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or int(value) != value:
        return None
    return int(value)


def scan_history(path):
    """Single pass over marks; returns per-mint series plus global facts."""
    mints = {}
    total_marks = 0
    last_mark_time = None
    for stamp, rows in iter_marks(path):
        total_marks += 1
        if last_mark_time is None or stamp > last_mark_time:
            last_mark_time = stamp
        seen_this_mark = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            mint = row.get("mint")
            if not isinstance(mint, str) or not mint:
                continue
            cap = parse_cap_lamports(row.get("market_cap_lamports"))
            if cap is None:
                continue
            if mint in seen_this_mark:
                continue
            seen_this_mark.add(mint)
            log_cap = math.log10(cap / LAMPORTS_PER_SOL)
            if not math.isfinite(log_cap):
                continue
            state = mints.get(mint)
            if state is None:
                state = {
                    "points": [],
                    "appearances": 0,
                    "top20": 0,
                    "first_mark": total_marks,
                    "last_time": stamp,
                }
                mints[mint] = state
            state["points"].append((stamp, log_cap))
            if stamp > state["last_time"]:
                state["last_time"] = stamp
            state["appearances"] += 1
            rank = parse_rank(row.get("rank"))
            if rank is not None and rank <= 20:
                state["top20"] += 1
    return mints, total_marks, last_mark_time


def trailing_rate(points, last_time, last_value, window_seconds):
    """Log10 change per hour over a trailing window ending at the last point.

    The reference is the latest point at or before the window start, or the
    earliest point when the series is shorter than the window; the rate is
    scaled by the actual elapsed time and clamped to a sane band.
    """
    target = last_time - window_seconds
    ref_time = None
    ref_value = None
    for stamp, value in points:
        if stamp <= target:
            ref_time, ref_value = stamp, value
        else:
            break
    if ref_time is None:
        ref_time, ref_value = points[0]
    elapsed_hours = (last_time - ref_time) / 3600.0
    if elapsed_hours < 1.0 / 60.0:
        return 0.0
    rate = (last_value - ref_value) / elapsed_hours
    return max(-MAX_RATE_PER_HOUR, min(MAX_RATE_PER_HOUR, rate))


def predict(mints, total_marks, last_mark_time, horizon_hours):
    """Return {mint: (predicted log10 cap in SOL, stability)} for each mint."""
    predictions = {}
    for mint, state in mints.items():
        points = sorted(state["points"])
        last_time, last_value = points[-1]
        rate_1h = trailing_rate(points, last_time, last_value, 3600.0)
        rate_3h = trailing_rate(points, last_time, last_value, 3 * 3600.0)
        blended = TREND_WEIGHT_1H * rate_1h + (1.0 - TREND_WEIGHT_1H) * rate_3h
        move = blended * horizon_hours * DAMPING
        move = max(-MAX_MOVE, min(MAX_MOVE, move))
        stale_hours = max(0.0, (last_mark_time - state["last_time"]) / 3600.0)
        discount = min(STALE_DISCOUNT_PER_HOUR * stale_hours, STALE_DISCOUNT_CAP)
        span = max(1, total_marks - state["first_mark"] + 1)
        presence = state["appearances"] / span
        top20_fraction = state["top20"] / max(1, state["appearances"])
        stability = max(0.0, min(1.0, 0.6 * top20_fraction + 0.4 * presence))
        predicted = last_value + move - discount + STABILITY_BONUS * stability
        # A non-finite prediction (from pathological inputs) must never reach
        # the ranking: fall back to the last observed cap, then keep the
        # prediction inside a band wide enough for any real pump.fun coin
        # (10^-3 to 10^12 SOL) so one wild series cannot dominate the sizes.
        if not math.isfinite(predicted):
            predicted = last_value
        predicted = max(-3.0, min(12.0, predicted))
        predictions[mint] = (predicted, stability)
    return predictions


def rank_mints(predictions):
    """Mints by predicted log cap descending, ties by mint bytes ascending."""
    return sorted(predictions, key=lambda m: (-predictions[m][0], m.encode("utf-8")))


def normalize_capped(items, total_cap):
    """Scale positive weights to sum at most total_cap, each p in [P_MIN, P_MAX].

    Drops the smallest entries and renormalizes until every survivor clears
    the floor; capping at P_MAX only ever lowers the total.
    """
    current = [
        (key, weight)
        for key, weight in items
        if weight > 0 and math.isfinite(weight)
    ]
    if not current:
        return []
    while True:
        total = sum(weight for _, weight in current)
        scaled = [(key, weight / total * total_cap) for key, weight in current]
        if len(scaled) == 1:
            key, prob = scaled[0]
            return [(key, max(P_MIN, min(P_MAX, prob)))]
        kept = [(key, prob) for key, prob in scaled if prob >= P_MIN]
        if len(kept) == len(scaled):
            return [(key, min(P_MAX, prob)) for key, prob in kept]
        if not kept:
            key, prob = max(scaled, key=lambda item: item[1])
            return [(key, max(P_MIN, min(P_MAX, prob)))]
        current = kept


def build_probabilities(ranked, predictions, places):
    """Per place, spread mass over the ranked list, decaying with distance."""
    rows = []
    for place in range(1, places + 1):
        weighted = []
        for index, mint in enumerate(ranked):
            distance = abs(index - (place - 1))
            stability = predictions[mint][1]
            weight = math.exp(-distance / PROB_DISTANCE_SCALE) * (
                0.7 + 0.3 * stability
            )
            weighted.append((mint, weight))
        weighted.sort(key=lambda item: (-item[1], item[0].encode("utf-8")))
        top = weighted[:PROB_MAX_ROWS_PER_PLACE]
        for mint, prob in normalize_capped(top, PROB_TOTAL_CAP):
            rows.append({"mint": mint, "place": place, "p": prob})
    rows.sort(key=lambda row: (row["place"], -row["p"], row["mint"].encode("utf-8")))
    return rows


def normal_cdf(z):
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def build_sizes(ranked, predictions, places, edges):
    """Per place, a Gaussian-like size distribution over the log10 cap bins.

    Bin 0 sits below the first edge, bin k between edges[k-1] and edges[k],
    and the last bin above the final edge.
    """
    bin_count = len(edges) + 1
    rows = []
    for place in range(1, places + 1):
        if ranked:
            anchor = ranked[min(place - 1, len(ranked) - 1)]
            center = predictions[anchor][0]
        else:
            center = FALLBACK_LOG_CAP
        masses = []
        for b in range(bin_count):
            lower = edges[b - 1] if b > 0 else None
            upper = edges[b] if b < len(edges) else None
            upper_mass = (
                normal_cdf((upper - center) / SIZE_SIGMA) if upper is not None else 1.0
            )
            lower_mass = (
                normal_cdf((lower - center) / SIZE_SIGMA) if lower is not None else 0.0
            )
            masses.append((b, max(0.0, upper_mass - lower_mass)))
        for b, prob in normalize_capped(masses, SIZE_TOTAL_CAP):
            rows.append({"place": place, "bin": b, "p": prob})
    rows.sort(key=lambda row: (row["place"], row["bin"]))
    return rows


def placeholder_mints(count, taken):
    """Distinct 32-char placeholder mints that never collide with real ones."""
    used = set(taken)
    placeholders = []
    char_index = 0
    while len(placeholders) < count and char_index < len(PLACEHOLDER_ALPHABET):
        candidate = PLACEHOLDER_ALPHABET[char_index] * 32
        char_index += 1
        if candidate not in used:
            used.add(candidate)
            placeholders.append(candidate)
    numeric = 0
    while len(placeholders) < count:
        candidate = "%032d" % numeric
        numeric += 1
        if candidate not in used:
            used.add(candidate)
            placeholders.append(candidate)
    return placeholders


def build_forecast(places, reads, edges, mints, total_marks, last_mark_time):
    """Assemble the forecast object from the scanned history."""
    horizon = MAX_HORIZON_HOURS
    if last_mark_time is not None and reads:
        horizon = (max(reads) - last_mark_time) / 3600.0
        horizon = max(0.0, min(MAX_HORIZON_HOURS, horizon))
    if last_mark_time is not None:
        predictions = predict(mints, total_marks, last_mark_time, horizon)
    else:
        predictions = {}
    ranked = rank_mints(predictions)
    top = ranked[:places]
    if len(top) < places:
        top = top + placeholder_mints(places - len(top), ranked)
    return {
        "probabilities": build_probabilities(ranked, predictions, places),
        "top5": top,
        "sizes": build_sizes(ranked, predictions, places, edges),
    }


def resolve_paths():
    """Input directory and output file path from the environment."""
    input_dir = (
        os.environ.get("MAKEFUN_INPUT_DIR")
        or os.environ.get("MAKEFUN_INPUT")
        or "/input"
    )
    output_dir = os.environ.get("MAKEFUN_OUTPUT_DIR")
    output_file = os.environ.get("MAKEFUN_OUTPUT")
    if output_dir:
        out_path = os.path.join(output_dir, "forecast.json")
    elif output_file:
        if output_file.endswith(os.sep) or os.path.isdir(output_file):
            out_path = os.path.join(output_file, "forecast.json")
        else:
            out_path = output_file
    else:
        out_path = os.path.join("/output", "forecast.json")
    return input_dir, out_path


def run(input_dir):
    """Load inputs and build the forecast; raises only on unexpected errors."""
    places, reads, edges = load_task(os.path.join(input_dir, "task.json"))
    mints, total_marks, last_mark_time = scan_history(
        os.path.join(input_dir, "history.ndjson")
    )
    return build_forecast(places, reads, edges, mints, total_marks, last_mark_time)


def fallback_forecast():
    """A valid forecast with no history: placeholders plus a centered spread."""
    return {
        "probabilities": [],
        "top5": placeholder_mints(DEFAULT_PLACES, ()),
        "sizes": build_sizes([], {}, DEFAULT_PLACES, DEFAULT_EDGES),
    }


def main():
    input_dir, out_path = resolve_paths()
    try:
        forecast = run(input_dir)
    except Exception:
        forecast = fallback_forecast()
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    text = json.dumps(forecast, sort_keys=True, separators=(",", ":")) + "\n"
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Local test for the make.fun forecast program.

Generates a synthetic /input (history.ndjson + task.json) following the
bounty page's own input spec, runs main.py twice, and verifies:
  - output has exactly the keys probabilities / top5 / sizes
  - probabilities: <=100 rows per place, each place sums to <= 1
  - top5: exactly 5 distinct mints
  - sizes: bins 0..81, each place sums to <= 1
  - file size <= 1,048,576 bytes
  - two runs on the same history write byte-identical output
No network, no house data: purely synthetic fixtures.
"""

import json
import math
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = "/tmp/makefun_forecast_test"


def make_fixture(path, n_marks=66, n_coins=300, seed=7):
    rng = random.Random(seed)
    os.makedirs(path, exist_ok=True)
    t0 = datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc)
    coins = []
    for i in range(n_coins):
        # Base58-ish fake mint, deterministic.
        mint = "".join(rng.choice("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
                       for _ in range(44))
        start_log = rng.uniform(2.5, 5.2)
        drift = rng.uniform(-0.05, 0.03)
        coins.append({"mint": mint, "start": start_log, "drift": drift,
                      "born": rng.randrange(0, max(1, n_marks - 6))})
    # One steady leader, one late riser, one rug.
    coins[0].update({"start": 5.8, "drift": 0.05, "born": 0})
    coins[1].update({"start": 3.0, "drift": 0.25, "born": 40})
    coins[2].update({"start": 5.0, "drift": 0.0, "born": 0, "rug_at": 55})
    marks = []
    for m in range(n_marks):
        at = t0 + timedelta(minutes=10 * m)
        rows = []
        for coin in coins:
            if m < coin["born"]:
                continue
            log_cap = coin["start"] + coin["drift"] * (m - coin["born"]) / 6.0
            log_cap += rng.uniform(-0.03, 0.03)
            if coin.get("rug_at") and m >= coin["rug_at"]:
                log_cap -= 3.0
            lamports = str(int(round((10 ** log_cap) * 1e9)))
            rows.append({
                "mint": coin["mint"],
                "market_cap_lamports": lamports,
                "venue": "pool" if log_cap > 4.5 else "curve",
                "slot": 300000000 + m * 2500,
                "name": "Coin" + coin["mint"][:4],
                "symbol": "C" + coin["mint"][:3],
                "created_at": (t0 + timedelta(minutes=10 * coin["born"])).isoformat().replace("+00:00", "Z"),
                "complete": log_cap > 4.5,
                "mayhem": False,
                "quote_reserves_lamports": str(int(round((10 ** log_cap) * 1e7))),
            })
        rows.sort(key=lambda r: (-int(r["market_cap_lamports"]), r["mint"].encode()))
        for rank, row in enumerate(rows[:200], start=1):
            row["rank"] = rank
        marks.append({
            "schema": 1, "at": at.isoformat().replace("+00:00", "Z"),
            "ok": True, "error": None, "kind": "mark",
            "t0": t0.isoformat().replace("+00:00", "Z"),
            "counts": {"eligible": len(rows), "graduated": 5,
                       "excluded_non_sol": 0, "priced": len(rows),
                       "refused": 0, "calls": 100, "http_429": 0,
                       "stream_coverage": 1.0},
            "rows": rows[:200],
        })
    with open(os.path.join(path, "history.ndjson"), "w", encoding="utf-8") as f:
        for mark in marks:
            f.write(json.dumps(mark) + "\n")
    reads = [(t0 + timedelta(hours=18 + h)).isoformat().replace("+00:00", "Z")
             for h in range(7)]
    task = {
        "competition": "pumpfun-top5-forecast-01",
        "t0": t0.isoformat().replace("+00:00", "Z"),
        "history_until": (t0 + timedelta(minutes=10 * n_marks)).isoformat().replace("+00:00", "Z"),
        "window": {"deadline": "2026-11-12T14:00:00Z", "answer_at": reads[-1]},
        "reads": reads, "places": 5, "chart_size": 100,
        "size": {"edges": [round(2.00 + 0.05 * i, 2) for i in range(81)]},
        "cadence_minutes": 10, "output": "/output/forecast.json",
        "marks": n_marks,
    }
    with open(os.path.join(path, "task.json"), "w", encoding="utf-8") as f:
        json.dump(task, f)
    return coins


def run_once(input_dir, output_path):
    env = dict(os.environ, MAKEFUN_INPUT=input_dir, MAKEFUN_OUTPUT=output_path)
    result = subprocess.run([sys.executable, os.path.join(HERE, "main.py")],
                            env=env, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
        raise SystemExit("main.py exited %d" % result.returncode)


def verify(output_path):
    size = os.path.getsize(output_path)
    assert size <= 1048576, "output too large: %d" % size
    with open(output_path, encoding="utf-8") as f:
        data = json.load(f)
    assert set(data.keys()) == {"probabilities", "top5", "sizes"}, data.keys()
    assert isinstance(data["top5"], list) and len(data["top5"]) == 5, data["top5"]
    assert len(set(data["top5"])) == 5, "top5 not distinct"
    assert all(isinstance(m, str) and m for m in data["top5"])
    per_place = {}
    for row in data["probabilities"]:
        assert set(row.keys()) == {"mint", "place", "p"}, row
        assert row["place"] in (1, 2, 3, 4, 5)
        assert 0.0 <= row["p"] <= 1.0
        per_place.setdefault(row["place"], []).append(row["p"])
    for place, probs in per_place.items():
        assert len(probs) <= 100, (place, len(probs))
        assert sum(probs) <= 1.0 + 1e-9, (place, sum(probs))
    size_place = {}
    for row in data["sizes"]:
        assert set(row.keys()) == {"place", "bin", "p"}, row
        assert row["place"] in (1, 2, 3, 4, 5)
        assert 0 <= row["bin"] <= 81
        assert 0.0 <= row["p"] <= 1.0
        size_place.setdefault(row["place"], []).append(row["p"])
    for place, probs in size_place.items():
        assert sum(probs) <= 1.0 + 1e-9, (place, sum(probs))
    return size, data


def main():
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    input_dir = os.path.join(WORK, "input")
    coins = make_fixture(input_dir)
    out1 = os.path.join(WORK, "out1", "forecast.json")
    out2 = os.path.join(WORK, "out2", "forecast.json")
    run_once(input_dir, out1)
    run_once(input_dir, out2)
    with open(out1, "rb") as a, open(out2, "rb") as b:
        assert a.read() == b.read(), "runs differ: not deterministic"
    size, data = verify(out1)
    print("OK: output %d bytes, %d prob rows, %d size rows" % (
        size, len(data["probabilities"]), len(data["sizes"])))
    print("top5:", data["top5"])
    print("leader mint:", coins[0]["mint"], "| riser mint:", coins[1]["mint"])
    print("determinism: byte-identical across two runs")
    # Sanity: the steady leader should be top5[0] and the rug should not be.
    assert data["top5"][0] == coins[0]["mint"], "leader not first"
    assert coins[2]["mint"] not in data["top5"], "rugged coin in top5"


if __name__ == "__main__":
    main()

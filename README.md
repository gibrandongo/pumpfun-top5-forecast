# pumpfun-top5-forecast

Forecast which 5 newly launched pump.fun coins will have the largest market
cap 24 hours out, as a probabilistic program scored by mean log score.

Built for the make.fun bounty
[“Predict the top 5 new Solana coins by market cap, 24 hours out”](https://make.fun/bounty/pumpfun-top5-forecast-01)
(`pumpfun-top5-forecast-01`). The program was written from the bounty page's
own published brief. No starter kit, house harness, or baseline code was
used, and no real house snapshot data was seen during development or
testing.

## How it works

`main.py` reads the house's 10-minute market-cap snapshots and writes a
single forecast. In short:

1. **Reconstruct** each coin's market-cap history (log10, in SOL) from the
   snapshot marks, oldest first.
2. **Extrapolate** each coin to the answer reads with a damped momentum
   trend (1-hour and 3-hour windows, total move clamped), and discount
   coins that dropped out of the top-200 rows.
3. **Rank** by extrapolated size, breaking ties by mint bytes ascending so
   the output is fully deterministic.
4. **Spread probability mass** around the expected rank instead of betting
   everything on one ordering, and forecast each place's size as a
   discretized distribution over the log-size bins. Spreading mass is
   deliberate: the log score punishes confident wrong calls far more than
   it rewards confident right ones, and new-coin market caps 6–12 hours
   out are dominated by noise and rugs.

The program is standard library only, makes no network calls, uses no
randomness, and writes the same forecast for the same history every time.

## Input

The sandbox provides an `/input` directory:

| File | Contents |
| --- | --- |
| `history.ndjson` | One JSON object per line, one per snapshot (a mark), oldest first. Each mark carries `schema`, `at`, `ok`, `error`, `kind`, `t0`, `counts`, and `rows`: the 200 largest coins at that read, each with `rank`, `mint`, `market_cap_lamports` (decimal integer string), `venue`, `slot`, `name`, `symbol`, `created_at`, `complete`, `mayhem`, and `quote_reserves_lamports`. |
| `task.json` | Competition metadata: `competition`, `t0`, `history_until`, `window`, `reads` (the seven answer reads), `places`, `chart_size`, `size` (bin edges), `cadence_minutes`, `output`, `marks`. |
| `NOTICE.txt` | Where the marks come from and how to check a row against the chain. |

Market cap is priced per raw unit times total supply (10^15 raw units for an
ordinary coin, 2 × 10^15 for a mayhem-mode coin), in lamports
(10^9 lamports = 1 SOL).

## Output

`main.py` writes `/output/forecast.json` (at most 1,048,576 bytes of UTF-8
JSON) with exactly three keys:

| Key | Shape |
| --- | --- |
| `probabilities` | Rows of `mint`, `place` (1–5), `p` (0–1); at most 100 rows per place; each place's probabilities sum to at most 1. |
| `top5` | Exactly 5 distinct mint addresses, in predicted order. |
| `sizes` | Rows of `place`, `bin` (0–81), `p`; 82 bins of log10 market cap in SOL, 0.05 wide. |

## Running

In the grading sandbox the program runs as-is:

```sh
python3 main.py
```

It reads `/input` and writes `/output/forecast.json`. For local runs the
paths can be overridden:

```sh
MAKEFUN_INPUT=/path/to/input MAKEFUN_OUTPUT=/path/to/forecast.json python3 main.py
```

Requirements: Python 3.13, standard library only. Nothing is installed.

## Testing

`test_forecast.py` generates synthetic fixtures following the published
input spec (66 marks at 10-minute cadence, 300 coins, including a steady
leader, a late riser, and a rug) and checks the program end to end:

```sh
python3 test_forecast.py
```

It verifies the exact output keys, per-place row caps and probability sums,
5 distinct top-5 mints, valid size bins, the 1 MB size limit, and
byte-identical output across two runs on the same history. No network and
no house data are involved; real validation happens through the bounty's
practice grading after submission.

## Repository layout

```text
main.py            The forecast program (entry point, repository root).
test_forecast.py   Local end-to-end test with synthetic fixtures.
pyproject.toml     Project metadata (Python >= 3.13, no dependencies).
README.md          This file.
LICENSE            MIT License.
```

## Limitations

Field names and the bin-edge mapping are inferred from the bounty page
brief text. A floor score at every place and size on practice grading
would indicate a format mismatch rather than a bad forecast. Forecasts
are probabilistic by design; nothing here guarantees any placing.

## License

MIT — see [LICENSE](LICENSE).

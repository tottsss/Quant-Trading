# Merger Arbitrage Guide

## Big Picture

This bot trades 5 live M&A deals (target + acquirer pairs).

Core model:

- update completion probability `p` from incoming news,
- compute fair target value `P* = p * DealValue + (1 - p) * StandaloneValue`,
- enter when market diverges from `P*`,
- hedge acquirer exposure for stock and mixed deals.

## Organized Folder Layout

This folder is the organized home for merger-arbitrage trading bots:

- `trading_bots/merger_arbitrage/merger_arb_event_driven_hft.py`
  - Main production event-driven bot (FinBERT-only news sentiment).

- `trading_bots/merger_arbitrage/MERGER_ARBITRAGE_CASE_GUIDE.md`
  - This guide.

- `trading_bots/merger_arbitrage/finbert_hft/export_quantize.py`
  - Export local HF FinBERT to ONNX, optimize graph, dynamic INT8 quantize (`reduce_range=True`).

- `trading_bots/merger_arbitrage/finbert_hft/fast_inference.py`
  - Low-latency ONNX inference wrapper (`FinBERTTrader`) for CPU HFT loops.

- `trading_bots/merger_arbitrage/finbert_hft/README.md`
  - End-to-end setup and benchmark commands.

- `trading_bots/merger_arbitrage/finbert_hft/requirements.txt`
  - Python dependencies for FinBERT toolchain.

## News And Probability Logic

- News is polled from `/news` continuously.
- Deal references are extracted from deal IDs (`D1..D5`) and ticker mentions.
- Direction/severity are FinBERT-derived.
- Category is read from explicit tags (`REG/FIN/SHR/ALT/PRC`) when present; otherwise fallback category is used.
- Update:
  - `new_p = clamp(old_p + base_change(dir,sev) * category_mult * deal_mult, 0, 1)`

## Execution Logic

- Reprices each target using real-time acquirer prices for stock/mixed structures.
- Uses marketable `LIMIT` orders (not blind market orders).
- Supports paired target/hedge submission.
- Includes:
  - dynamic friction-aware entry threshold,
  - inventory-aware threshold scaling,
  - take-profit/mean-reversion exits,
  - stale limit-order cancellation,
  - gross/net and per-deal risk controls.

## Logs

At end of heat, one JSON file is written with:

- runtime events,
- all news and classifications,
- applied probability updates,
- final case/deal/position snapshot.

Default location:

- `logs/merger_arb_heat_<timestamp>.json`

## Run

1. Build FinBERT ONNX artifacts:
   - `python trading_bots/merger_arbitrage/finbert_hft/export_quantize.py --model-dir /ABS/PATH/LOCAL_FINBERT --output-dir trading_bots/merger_arbitrage/finbert_hft`
2. Start RIT.
3. Run bot:
   - `python trading_bots/merger_arbitrage/merger_arb_event_driven_hft.py`

## Compatibility Note

Legacy copies in `ready_bots/` and `cases_2026/04_merger_arbitrage/` are preserved, but this folder is the organized canonical location going forward.

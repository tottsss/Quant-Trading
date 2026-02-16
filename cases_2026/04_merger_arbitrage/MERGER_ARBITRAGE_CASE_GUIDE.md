# Merger Arbitrage Case Guide

## 1) Big Picture

This case trades **5 live M&A deals** (10 stocks: target + acquirer for each deal).
The core signal is:

- infer/update completion probability `p` from news,
- compute target fair value `P* = p * DealValue + (1 - p) * StandaloneValue`,
- trade when market price diverges from `P*`,
- hedge acquirer exposure for stock/mixed deals.

The production script in this repo is:

- `cases_2026/04_merger_arbitrage/merger_arb_event_driven_hft.py`
- mirrored at `ready_bots/merger_arb_event_driven_hft.py`

## 2) File Map (Everything Relevant)

### Core case folder

- `cases_2026/04_merger_arbitrage/merger_case_package_summary.txt`
  - Condensed case mechanics, deal terms, limits, and scoring context.

- `cases_2026/04_merger_arbitrage/merger_arb_event_driven_hft.py`
  - Main event-driven strategy (current advanced bot).

- `cases_2026/04_merger_arbitrage/merger_arb_bot.py`
  - Original baseline merger bot.

- `cases_2026/04_merger_arbitrage/algorithms/news_mult_model.py`
  - News multiplier baseline.

- `cases_2026/04_merger_arbitrage/algorithms/implied_p_vs_model.py`
  - Implied-probability vs model baseline.

- `cases_2026/04_merger_arbitrage/algorithms/regulatory_focus.py`
  - REG/FIN-focused conservative baseline.

- `cases_2026/04_merger_arbitrage/algorithms/risk_parity_multi_deal.py`
  - Volatility-aware sizing baseline.

- `cases_2026/04_merger_arbitrage/algorithms/spread_band_strategy.py`
  - Spread band baseline.

- `cases_2026/04_merger_arbitrage/README.md`
  - Short quick-read summary.

### Ready-to-run bot folder

- `ready_bots/merger_arb_event_driven_hft.py`
  - Same production bot for direct execution.

- `ready_bots/04_merger_arb_standalone.py`
  - Earlier standalone merger script.

### FinBERT acceleration add-on

- `ready_bots/finbert_hft/export_quantize.py`
  - Exports local HF FinBERT to ONNX, optimizes graph, dynamic INT8 quantizes with `reduce_range=True`.

- `ready_bots/finbert_hft/fast_inference.py`
  - Low-latency ONNX Runtime inference wrapper (`FinBERTTrader`) for batch-size=1 loops.

## 3) What The Production Bot Does

## 3.1 Startup and model init

- Connects to `http://localhost:9999/v1` with `X-API-key`.
- Waits for `ACTIVE`.
- Performs opening warmup and averaged snapshots.
- Calculates each deal's fixed standalone value `V` from opening prices and `p0`.

## 3.2 News interpretation

The news worker polls `/news` incrementally (`since=last_news_id`) and classifies:

- deal reference (`D1..D5`, target/acquirer ticker mention),
- category (`REG/FIN/SHR/ALT/PRC`),
- direction (`POS/NEG`),
- severity (`S/M/L`).

Parser stack:

- keyword/rule parser (with negation handling),
- optional FinBERT sentiment override layer,
- manual console override (`D1 POS L`, `D3 P 0.72`).

Probability update:

- `delta = base_change(direction, severity) * category_mult * deal_mult`
- `p = clamp(p + delta, 0, 1)`

## 3.3 Pricing and trade decisions

For each deal every loop:

- fetch current top-of-book for target/acquirer,
- recompute dynamic deal value `K` (stock/mixed uses live acquirer price),
- compute `P* = p*K + (1-p)*V`.

Trade logic:

- **entry** when edge exceeds dynamic threshold,
- threshold includes spread + commission + marketable-limit friction + desired margin,
- threshold is inventory-aware (harder to add when loaded, easier to reduce),
- **take profit/mean reversion** exits when price converges near fair value.

## 3.4 Hedge and execution behavior

- Uses **marketable LIMIT** prices instead of pure MARKET.
- Can submit target and hedge legs simultaneously.
- Performs hedge rebalance when ratio drift is too large.
- Unwinds orphan hedge if target is flat.

## 3.5 Risk and order hygiene

- Enforces case-level projected gross/net caps before sending paired orders.
- Enforces per-deal caps on target and hedge legs.
- Tracks submitted order IDs and cancels stale OPEN limits after timeout.
- Periodic risk/model snapshots are logged.

## 3.6 End-of-run JSON logging

At shutdown, it writes one JSON artifact containing:

- all runtime events,
- all incoming news with classification and applied deltas,
- final case state, final positions, final deal states.

Default log path:

- `logs/merger_arb_heat_<timestamp>.json`

## 4) FinBERT Defaults (Now Default-On)

FinBERT mode is enabled by default:

- `RIT_MA_USE_FINBERT=1` (default)

Default path assumptions:

- ONNX model: `ready_bots/finbert_hft/model_opt_int8.onnx`
- tokenizer/model dir: `ready_bots/finbert_hft/local_finbert`

If those exact paths are missing, the bot tries nearby fallback paths and then safely falls back to keyword-only parsing.

Override env vars when needed:

- `RIT_MA_FINBERT_ONNX_MODEL`
- `RIT_MA_FINBERT_TOKENIZER_DIR`
- `RIT_MA_FINBERT_POS_THRESHOLD`
- `RIT_MA_FINBERT_NEG_THRESHOLD`
- `RIT_MA_FINBERT_GAP_THRESHOLD`
- `RIT_MA_FINBERT_OVERRIDE_GAP`
- `RIT_MA_FINBERT_CATEGORY_FALLBACK`

## 5) Typical Run Flow

1. Build FinBERT ONNX model (one-time):
   - run `ready_bots/finbert_hft/export_quantize.py`
2. Start RIT client (practice/heat).
3. Run:
   - `python ready_bots/merger_arb_event_driven_hft.py`
4. After heat ends, inspect JSON log in `logs/`.

## 6) Why This Version Is Safer

- Correct D5 mapping (SPK target / EEC acquirer).
- Commission/friction-aware entries.
- Exit logic to avoid inventory lock-up.
- Stale order cancellation.
- Hedge drift correction.
- Full post-heat observability in one JSON record.


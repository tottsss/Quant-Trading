# FinBERT HFT Toolchain (Merger Arbitrage)

This folder contains the FinBERT ONNX pipeline used by:

- `/Users/iskhak.tazhibaev/Documents/RITC/trading_bots/merger_arbitrage/merger_arb_event_driven_hft.py`

## Files

- `export_quantize.py`: local HF FinBERT -> raw ONNX -> optimized ONNX -> INT8 ONNX.
- `fast_inference.py`: low-latency ONNX Runtime wrapper + benchmark.
- `requirements.txt`: Python dependencies.

## 1) Install dependencies

```bash
python -m pip install -r /Users/iskhak.tazhibaev/Documents/RITC/trading_bots/merger_arbitrage/finbert_hft/requirements.txt
```

## 2) Export and quantize model

```bash
python /Users/iskhak.tazhibaev/Documents/RITC/trading_bots/merger_arbitrage/finbert_hft/export_quantize.py \
  --model-dir /ABS/PATH/LOCAL_FINBERT \
  --output-dir /Users/iskhak.tazhibaev/Documents/RITC/trading_bots/merger_arbitrage/finbert_hft
```

Expected outputs:

- `/Users/iskhak.tazhibaev/Documents/RITC/trading_bots/merger_arbitrage/finbert_hft/model_raw.onnx`
- `/Users/iskhak.tazhibaev/Documents/RITC/trading_bots/merger_arbitrage/finbert_hft/model_opt.onnx`
- `/Users/iskhak.tazhibaev/Documents/RITC/trading_bots/merger_arbitrage/finbert_hft/model_opt_int8.onnx`
- `/Users/iskhak.tazhibaev/Documents/RITC/trading_bots/merger_arbitrage/finbert_hft/export_manifest.json`

## 3) Benchmark inference

```bash
python /Users/iskhak.tazhibaev/Documents/RITC/trading_bots/merger_arbitrage/finbert_hft/fast_inference.py \
  --onnx-model /Users/iskhak.tazhibaev/Documents/RITC/trading_bots/merger_arbitrage/finbert_hft/model_opt_int8.onnx \
  --tokenizer-dir /ABS/PATH/LOCAL_FINBERT \
  --iterations 100
```

## 4) Run merger bot

Defaults point to this folder's FinBERT artifacts.

```bash
python /Users/iskhak.tazhibaev/Documents/RITC/trading_bots/merger_arbitrage/merger_arb_event_driven_hft.py
```

Optional explicit override:

```bash
RIT_MA_USE_FINBERT=1 \
RIT_MA_FINBERT_ONNX_MODEL=/ABS/PATH/model_opt_int8.onnx \
RIT_MA_FINBERT_TOKENIZER_DIR=/ABS/PATH/LOCAL_FINBERT \
python /Users/iskhak.tazhibaev/Documents/RITC/trading_bots/merger_arbitrage/merger_arb_event_driven_hft.py
```

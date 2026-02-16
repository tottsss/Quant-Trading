# FinBERT Toolchain For Merger Arbitrage

This folder contains the full FinBERT CPU toolchain used by:

- `/Users/iskhak.tazhibaev/Documents/RITC/ready_bots/merger_arb_event_driven_hft.py`
- `/Users/iskhak.tazhibaev/Documents/RITC/cases_2026/04_merger_arbitrage/merger_arb_event_driven_hft.py`

The merger bot is configured as FinBERT-only for news sentiment.

## Files

- `export_quantize.py`: export local HF model -> ONNX -> optimized ONNX -> INT8 ONNX.
- `fast_inference.py`: low-latency ONNX runtime wrapper (`FinBERTTrader`) and benchmark.
- `requirements.txt`: Python deps for this toolchain.

## 1) Install deps

```bash
python -m pip install -r /Users/iskhak.tazhibaev/Documents/RITC/ready_bots/finbert_hft/requirements.txt
```

## 2) Build quantized ONNX model

Input must be a local Hugging Face FinBERT directory containing files like `config.json`.

```bash
python /Users/iskhak.tazhibaev/Documents/RITC/ready_bots/finbert_hft/export_quantize.py \
  --model-dir /ABS/PATH/LOCAL_FINBERT \
  --output-dir /Users/iskhak.tazhibaev/Documents/RITC/ready_bots/finbert_hft
```

Expected outputs:

- `/Users/iskhak.tazhibaev/Documents/RITC/ready_bots/finbert_hft/model_raw.onnx`
- `/Users/iskhak.tazhibaev/Documents/RITC/ready_bots/finbert_hft/model_opt.onnx`
- `/Users/iskhak.tazhibaev/Documents/RITC/ready_bots/finbert_hft/model_opt_int8.onnx`
- `/Users/iskhak.tazhibaev/Documents/RITC/ready_bots/finbert_hft/export_manifest.json`

## 3) Benchmark inference latency

```bash
python /Users/iskhak.tazhibaev/Documents/RITC/ready_bots/finbert_hft/fast_inference.py \
  --onnx-model /Users/iskhak.tazhibaev/Documents/RITC/ready_bots/finbert_hft/model_opt_int8.onnx \
  --tokenizer-dir /ABS/PATH/LOCAL_FINBERT \
  --iterations 100
```

## 4) Run merger arb bot

Defaults already point to:

- ONNX: `/Users/iskhak.tazhibaev/Documents/RITC/ready_bots/finbert_hft/model_opt_int8.onnx`
- tokenizer dir: `/Users/iskhak.tazhibaev/Documents/RITC/ready_bots/finbert_hft/local_finbert`

Run directly (if defaults exist):

```bash
python /Users/iskhak.tazhibaev/Documents/RITC/ready_bots/merger_arb_event_driven_hft.py
```

Or override paths:

```bash
RIT_MA_USE_FINBERT=1 \
RIT_MA_FINBERT_ONNX_MODEL=/ABS/PATH/model_opt_int8.onnx \
RIT_MA_FINBERT_TOKENIZER_DIR=/ABS/PATH/LOCAL_FINBERT \
python /Users/iskhak.tazhibaev/Documents/RITC/ready_bots/merger_arb_event_driven_hft.py
```

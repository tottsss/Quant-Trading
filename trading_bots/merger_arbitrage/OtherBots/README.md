# Merger Arbitrage Event-Driven HFT Bot

A high-frequency trading bot for merger arbitrage strategies on the Rotman Interactive Trader (RIT) platform. Uses FinBERT NLP model to analyze news sentiment and execute trades based on merger deal probabilities.

## Overview

This bot trades merger arbitrage opportunities across 5 deals:
- **D1**: TGX/PHR (cash deal)
- **D2**: BYL/CLD (stock deal)
- **D3**: GGD/PNR (mixed deal)
- **D4**: FSR/ATB (cash deal)
- **D5**: SPK/EEC (stock deal)

The bot:
1. Monitors news feeds in real-time
2. Uses FinBERT to classify news sentiment (positive/negative)
3. Updates deal success probabilities based on news
4. Executes trades when mispricings exceed thresholds
5. Manages risk with stop-losses and position limits

## Requirements

### Python Version
- Python 3.10+ recommended

### Dependencies
Install all required packages:

```bash
pip install aiohttp numpy onnxruntime transformers torch onnx onnxscript
```

Or install from requirements (if available):
```bash
pip install -r finbert_hft/requirements.txt
```

## Setup

### 1. Clone/Copy the Bot Files

Ensure you have the following structure:
```
merger_arbitrage/
├── merger_arb_event_driven_hft_alpha.py   # Main bot script
├── finbert_hft/
│   ├── local_finbert/                      # FinBERT tokenizer files
│   │   ├── config.json
│   │   ├── tokenizer.json
│   │   ├── tokenizer_config.json
│   │   └── model.safetensors
│   ├── model_opt_int8.onnx                 # FinBERT ONNX model
│   ├── export_quantize.py                  # Model export script
│   ├── fast_inference.py                   # Inference utilities
│   └── requirements.txt
└── README.md
```

### 2. Set Up FinBERT Model

If `model_opt_int8.onnx` and `local_finbert/` don't exist, generate them:

```bash
# Download FinBERT model
python3 << 'EOF'
from transformers import AutoModelForSequenceClassification, AutoTokenizer
import os

model_name = "ProsusAI/finbert"
output_dir = "finbert_hft/local_finbert"

os.makedirs(output_dir, exist_ok=True)
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoModelForSequenceClassification.from_pretrained(model_name)
tokenizer.save_pretrained(output_dir)
model.save_pretrained(output_dir)
print(f"Model saved to {output_dir}")
EOF

# Export to ONNX
python3 finbert_hft/export_quantize.py \
  --model-dir finbert_hft/local_finbert \
  --output-dir finbert_hft
```

If quantization fails, copy the optimized model:
```bash
cp finbert_hft/model_opt.onnx finbert_hft/model_opt_int8.onnx
```

## Configuration

### Default Configuration (DMA API)

The bot is pre-configured to connect to the Rotman DMA API:

| Setting | Default Value |
|---------|---------------|
| `BASE_URL` | `http://flserver.rotman.utoronto.ca:16550/v1` |
| `USE_DMA_AUTH` | `1` (enabled) |
| `DMA_USER` | `ZUAI-5` |
| `DMA_PASS` | `omega` |

### Environment Variables

Override defaults using environment variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `RIT_BASE_URL` | API endpoint URL | `http://flserver.rotman.utoronto.ca:16550/v1` |
| `RIT_USE_DMA_AUTH` | Enable DMA Basic Auth | `1` or `0` |
| `RIT_DMA_USER` | DMA username (trader ID) | `ZUAI-5` |
| `RIT_DMA_PASS` | DMA password | `omega` |
| `RIT_API_KEY` | Client API key (if not using DMA) | `932VC8JQ` |

### Trading Parameters

Key parameters can be tuned via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `RIT_MA_BASE_ORDER_QTY` | `1800` | Base order quantity |
| `RIT_MA_MIN_ENTRY_THRESHOLD` | `0.08` | Minimum edge to enter trade |
| `RIT_MA_STOP_BUFFER` | `0.30` | Stop-loss buffer |
| `RIT_MA_MAX_HOLD_SECS` | `60.0` | Maximum position hold time |
| `RIT_MA_NEWS_POLL_SECS` | `0.10` | News polling interval |
| `RIT_MA_TRADE_LOOP_SECS` | `0.08` | Trading loop interval |

## Running the Bot

### Quick Start (Default Settings)

```bash
cd /path/to/merger_arbitrage
python3 merger_arb_event_driven_hft_alpha.py
```

### With Custom DMA Credentials

```bash
RIT_BASE_URL="http://your-server:port/v1" \
RIT_USE_DMA_AUTH="1" \
RIT_DMA_USER="your-trader-id" \
RIT_DMA_PASS="your-password" \
python3 merger_arb_event_driven_hft_alpha.py
```

### Using Client REST API (Local RIT Client)

```bash
RIT_BASE_URL="http://localhost:9999/v1" \
RIT_USE_DMA_AUTH="0" \
RIT_API_KEY="your-api-key" \
python3 merger_arb_event_driven_hft_alpha.py
```

### Run in Background

```bash
# Start in background
nohup python3 merger_arb_event_driven_hft_alpha.py > bot.log 2>&1 &

# Save the process ID
echo $! > bot.pid
```

## Stopping the Bot

### If Running in Foreground
Press `Ctrl+C` to stop.

### If Running in Background

```bash
# Find the process
ps aux | grep merger_arb

# Kill by PID
kill <PID>

# Or if you saved the PID
kill $(cat bot.pid)

# Force kill if needed
kill -9 <PID>
```

### Kill All Python Bot Processes

```bash
pkill -f merger_arb_event_driven_hft_alpha.py
```

## Monitoring

### View Logs

The bot outputs to stdout. If running in background with `nohup`:

```bash
# Follow log in real-time
tail -f bot.log

# View last 100 lines
tail -100 bot.log
```

### Log Output Explained

```
18:49:54.881 | INIT D1 target=TGX mid0=42.69 acq=PHR mid0=48.24 K0=50.00 V0=25.64 p0=0.700
```
- `INIT`: Deal initialization with initial prices and probabilities

```
18:49:55.006 | NEWS id=3 deal=D4 cat=REG dir=POS sev=L strength=1.000 delta=+0.3608 p:0.3800->0.7408
```
- `NEWS`: FinBERT processed news
- `cat`: Category (REG=regulatory, FIN=financial, SHR=shareholder, etc.)
- `dir`: Direction (POS=positive, NEG=negative)
- `sev`: Severity (L=large, M=medium, S=small)
- `delta`: Probability change
- `p`: Probability before->after

```
18:49:55.130 | TRADE D2 reason=ENTRY side=BUY tgt=BYL qty=5000@48.93 hedge=CLD:SELL:3750@79.26 edge=0.493
```
- `TRADE`: Order executed
- `reason`: ENTRY, STOP_LOSS, TAKE_PROFIT, etc.
- `tgt`: Target ticker and order details
- `hedge`: Hedge leg details
- `edge`: Expected profit per share

```
18:49:57.163 | MODEL D1 p_news=0.7072 p_live=0.6644 p_impl=0.6987 V=25.64 Kmid=50.00 P*=41.83
```
- `MODEL`: Deal model state
- `p_news`: Probability from news
- `p_live`: Smoothed live probability
- `p_impl`: Market-implied probability
- `P*`: Model fair price

## Troubleshooting

### Connection Refused
```
aiohttp.client_exceptions.ClientConnectorError: Cannot connect to host localhost:9999
```
**Solution**: Check that RIT server is running and `BASE_URL` is correct.

### Authentication Failed (401)
```
HTTP 401 Unauthorized
```
**Solution**: Verify `DMA_USER` and `DMA_PASS` are correct, or check `API_KEY` for client mode.

### FinBERT Model Not Found
```
RuntimeError: FinBERT assets missing: No ONNX model found
```
**Solution**: Run the FinBERT setup steps in the Setup section above.

### Rate Limited (429)
```
HTTP 429 Too Many Requests
```
**Solution**: The bot handles this automatically with backoff. If persistent, increase polling intervals.

### Module Not Found
```
ModuleNotFoundError: No module named 'aiohttp'
```
**Solution**: Install missing dependencies:
```bash
pip install aiohttp numpy onnxruntime transformers torch
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Main Event Loop                          │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ News Poller │  │ Trade Loop  │  │ Risk Manager        │  │
│  │ (100ms)     │  │ (80ms)      │  │ (position limits)   │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
│         │                │                     │            │
│         ▼                ▼                     ▼            │
│  ┌─────────────────────────────────────────────────────────┐│
│  │                  FinBERT NLP Engine                     ││
│  │  (sentiment classification → probability updates)       ││
│  └─────────────────────────────────────────────────────────┘│
│         │                │                     │            │
│         ▼                ▼                     ▼            │
│  ┌─────────────────────────────────────────────────────────┐│
│  │              Deal Probability Models (D1-D5)            ││
│  │  (news decay, market-implied, fair value calculation)   ││
│  └─────────────────────────────────────────────────────────┘│
│         │                                                   │
│         ▼                                                   │
│  ┌─────────────────────────────────────────────────────────┐│
│  │              Async RIT Client (aiohttp)                 ││
│  │  (order submission, market data, position tracking)     ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │  RIT DMA API    │
                    │  (REST/HTTP)    │
                    └─────────────────┘
```

## File Descriptions

| File | Description |
|------|-------------|
| `merger_arb_event_driven_hft_alpha.py` | Main bot with async execution and FinBERT |
| `merger_arb_event_driven_hft.py` | Alternative version (larger, more features) |
| `04_merger_arb_standalone.py` | Simpler standalone version |
| `finbert_hft/export_quantize.py` | Exports FinBERT to optimized ONNX |
| `finbert_hft/fast_inference.py` | Low-latency ONNX inference wrapper |
| `finbert_hft/local_finbert/` | FinBERT tokenizer and config files |
| `finbert_hft/model_opt_int8.onnx` | Quantized ONNX model for inference |

## License

For educational and competition use with Rotman Interactive Trader.

## Support

For RIT platform issues, refer to:
- [RIT DMA API Documentation](http://rit.306w.ca/RIT-DMA-API/1.0.4/)
- [RIT Client API Documentation](https://rit.306w.ca/RIT-REST-API/1.0.3)

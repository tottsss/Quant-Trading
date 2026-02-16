#!/usr/bin/env python3
"""Low-latency FinBERT ONNX inference wrapper for CPU HFT usage.

Example:
    python fast_inference.py \
      --onnx-model /path/to/model_opt_int8.onnx \
      --tokenizer-dir /path/to/local/finbert
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import onnxruntime as ort
from transformers import AutoConfig, AutoTokenizer


def _fail(msg: str, code: int = 1) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)
    raise SystemExit(code)


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - np.max(logits, axis=-1, keepdims=True)
    exps = np.exp(shifted)
    return exps / np.sum(exps, axis=-1, keepdims=True)


class FinBERTTrader:
    """CPU-optimized ONNX runtime wrapper for batch_size=1 event inference."""

    def __init__(
        self,
        onnx_model_path: str,
        tokenizer_dir: str,
        max_length: int = 128,
    ) -> None:
        self.model_path = Path(onnx_model_path).expanduser().resolve()
        self.tokenizer_dir = Path(tokenizer_dir).expanduser().resolve()
        self.max_length = max_length

        if not self.model_path.exists():
            _fail(f"ONNX model not found: {self.model_path}")
        if not self.tokenizer_dir.exists():
            _fail(f"Tokenizer directory not found: {self.tokenizer_dir}")

        try:
            self.tokenizer = AutoTokenizer.from_pretrained(str(self.tokenizer_dir), local_files_only=True)
        except Exception as exc:
            _fail(f"Failed to load tokenizer from {self.tokenizer_dir}: {exc}")

        self.id2label: Dict[int, str] = {}
        try:
            cfg = AutoConfig.from_pretrained(str(self.tokenizer_dir), local_files_only=True)
            raw = getattr(cfg, "id2label", {}) or {}
            self.id2label = {int(k): str(v).upper() for k, v in raw.items()}
        except Exception:
            # Non-fatal: fallback to conventional FinBERT ordering.
            self.id2label = {}

        session_opts = ort.SessionOptions()
        session_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        # HFT latency notes:
        # - intra_op_num_threads=1 avoids context-switch noise for tiny batch=1 inference.
        # - disabling spinning reduces CPU burn from worker threads busy-waiting between inferences.
        #   This is often better for real-time event-driven loops under thermal limits.
        session_opts.intra_op_num_threads = 1
        session_opts.inter_op_num_threads = 1
        session_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        session_opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        session_opts.add_session_config_entry("session.inter_op.allow_spinning", "0")

        try:
            self.session = ort.InferenceSession(
                str(self.model_path),
                sess_options=session_opts,
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            _fail(f"Failed to create ONNX Runtime session: {exc}")

        self.input_names = [inp.name for inp in self.session.get_inputs()]
        self.output_name = self.session.get_outputs()[0].name

    def _infer_label_indices(self, class_count: int) -> tuple[int, int]:
        """Return (positive_idx, negative_idx) from model labels or defaults."""
        if self.id2label:
            positive_idx = next((i for i, v in self.id2label.items() if "POS" in v), None)
            negative_idx = next((i for i, v in self.id2label.items() if "NEG" in v), None)
            if positive_idx is not None and negative_idx is not None:
                return positive_idx, negative_idx
        # Common FinBERT convention: [positive, negative, neutral]
        if class_count >= 2:
            return 0, 1
        return 0, 0

    def predict(self, headline: str) -> Dict[str, float]:
        """Run one headline through ONNX FinBERT and return sentiment probabilities."""
        if not isinstance(headline, str) or not headline.strip():
            raise ValueError("headline must be a non-empty string")

        try:
            tok = self.tokenizer(
                headline,
                truncation=True,
                padding="max_length",
                max_length=self.max_length,
                return_tensors="np",
            )
        except Exception as exc:
            raise RuntimeError(f"Tokenization failed: {exc}") from exc

        feeds: Dict[str, np.ndarray] = {}
        batch_shape = None

        # ONNX BERT models typically expect int64 tensors.
        for key in ("input_ids", "attention_mask", "token_type_ids"):
            if key in tok:
                arr = tok[key].astype(np.int64, copy=False)
                feeds[key] = arr
                if batch_shape is None:
                    batch_shape = arr.shape

        if batch_shape is None:
            raise RuntimeError("Tokenizer did not produce model inputs.")

        # Fill missing required inputs with safe defaults.
        for name in self.input_names:
            if name in feeds:
                continue
            if name == "token_type_ids":
                feeds[name] = np.zeros(batch_shape, dtype=np.int64)
            else:
                raise RuntimeError(f"Required ONNX input '{name}' missing from tokenized output.")

        try:
            logits = self.session.run([self.output_name], feeds)[0]
        except Exception as exc:
            raise RuntimeError(f"ONNX inference failed: {exc}") from exc

        logits = np.asarray(logits, dtype=np.float32)
        probs = _softmax(logits)[0]
        pos_idx, neg_idx = self._infer_label_indices(class_count=probs.shape[0])

        return {
            "positive_probability": float(probs[pos_idx]),
            "negative_probability": float(probs[neg_idx]),
        }


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fast FinBERT ONNX inference benchmark.")
    p.add_argument("--onnx-model", required=True, help="Path to quantized ONNX model (e.g., model_opt_int8.onnx).")
    p.add_argument("--tokenizer-dir", required=True, help="Path to local tokenizer/model directory.")
    p.add_argument("--max-length", type=int, default=128, help="Tokenizer max sequence length.")
    p.add_argument("--iterations", type=int, default=100, help="Benchmark iterations.")
    return p


def main() -> None:
    args = _build_arg_parser().parse_args()

    trader = FinBERTTrader(
        onnx_model_path=args.onnx_model,
        tokenizer_dir=args.tokenizer_dir,
        max_length=args.max_length,
    )

    sample_news = [
        "Regulators have cleared the merger after a second review.",
        "The board approved the revised financing package.",
        "The acquisition may face delays due to additional antitrust concerns.",
        "Shareholders voted in favor of the transaction.",
        "The bidder withdrew after financing markets tightened.",
    ]

    timings_ms = []
    last_pred: Optional[Dict[str, float]] = None

    for i in range(args.iterations):
        headline = sample_news[i % len(sample_news)]
        t0 = time.perf_counter()
        try:
            last_pred = trader.predict(headline)
        except Exception as exc:
            _fail(f"Inference failed at iteration {i}: {exc}")
        dt_ms = (time.perf_counter() - t0) * 1000.0
        timings_ms.append(dt_ms)

    avg_ms = float(np.mean(timings_ms)) if timings_ms else float("nan")
    p95_ms = float(np.percentile(timings_ms, 95)) if timings_ms else float("nan")

    print(json.dumps(
        {
            "iterations": args.iterations,
            "avg_latency_ms": round(avg_ms, 4),
            "p95_latency_ms": round(p95_ms, 4),
            "last_prediction": last_pred,
            "model_path": str(Path(args.onnx_model).expanduser().resolve()),
        },
        indent=2,
    ))


if __name__ == "__main__":
    main()

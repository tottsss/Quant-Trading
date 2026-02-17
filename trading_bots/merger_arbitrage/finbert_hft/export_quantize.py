#!/usr/bin/env python3
"""Export a local fine-tuned FinBERT model to ONNX and quantize to INT8.

Pipeline:
1) Load local Hugging Face model/tokenizer.
2) Export to ONNX with dynamic batch and sequence length.
3) Optimize ONNX graph for BERT with onnxruntime.transformers.optimizer.
4) Dynamic INT8 quantization with reduce_range=True for AVX2 safety.

Example:
    python export_quantize.py \
      --model-dir /path/to/local/finbert \
      --output-dir /path/to/output
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple


def _fail(msg: str, code: int = 1) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)
    raise SystemExit(code)


def _import_deps():
    try:
        import torch
    except Exception as exc:  # pragma: no cover - runtime dependency check
        _fail(f"PyTorch import failed: {exc}")
    try:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except Exception as exc:  # pragma: no cover - runtime dependency check
        _fail(f"Transformers import failed: {exc}")
    try:
        from onnxruntime.quantization import QuantType, quantize_dynamic
    except Exception as exc:  # pragma: no cover - runtime dependency check
        _fail(f"onnxruntime.quantization import failed: {exc}")
    try:
        from onnxruntime.transformers.optimizer import optimize_model
    except Exception as exc:  # pragma: no cover - runtime dependency check
        _fail(
            "onnxruntime.transformers.optimizer import failed. "
            "Install an onnxruntime build with transformers tools. "
            f"Details: {exc}"
        )
    return (
        torch,
        AutoModelForSequenceClassification,
        AutoTokenizer,
        QuantType,
        quantize_dynamic,
        optimize_model,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Export + optimize + quantize FinBERT ONNX model.")
    p.add_argument("--model-dir", required=True, help="Local path to fine-tuned Hugging Face FinBERT model.")
    p.add_argument("--output-dir", required=True, help="Directory for ONNX outputs.")
    p.add_argument("--opset", type=int, default=17, help="ONNX opset version.")
    p.add_argument("--max-seq-len", type=int, default=128, help="Dummy export sequence length.")
    p.add_argument(
        "--sample-text",
        default="Regulators did not block the merger and financing was secured.",
        help="Dummy text used to trace export graph.",
    )
    return p


def _prepare_io_keys(encoded: Dict, expected_order: Tuple[str, ...]) -> Tuple[List[str], Tuple]:
    input_names: List[str] = []
    input_tensors: List = []
    for name in expected_order:
        if name in encoded:
            input_names.append(name)
            input_tensors.append(encoded[name])
    if not input_names:
        _fail("Tokenizer did not produce any recognized model inputs.")
    return input_names, tuple(input_tensors)


def _save_manifest(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> None:
    args = _build_arg_parser().parse_args()
    model_dir = Path(args.model_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    if not model_dir.exists() or not model_dir.is_dir():
        _fail(f"--model-dir does not exist or is not a directory: {model_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    (
        torch,
        AutoModelForSequenceClassification,
        AutoTokenizer,
        QuantType,
        quantize_dynamic,
        optimize_model,
    ) = _import_deps()

    raw_onnx = output_dir / "model_raw.onnx"
    opt_onnx = output_dir / "model_opt.onnx"
    int8_onnx = output_dir / "model_opt_int8.onnx"
    manifest_json = output_dir / "export_manifest.json"

    print(f"[INFO] Loading tokenizer/model from: {model_dir}")
    try:
        tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
        model = AutoModelForSequenceClassification.from_pretrained(str(model_dir), local_files_only=True)
    except Exception as exc:
        _fail(f"Failed to load local model/tokenizer from {model_dir}: {exc}")

    model.eval()
    with torch.no_grad():
        encoded = tokenizer(
            args.sample_text,
            return_tensors="pt",
            truncation=True,
            padding="max_length",
            max_length=args.max_seq_len,
        )
        input_names, input_tensors = _prepare_io_keys(
            encoded=encoded,
            expected_order=("input_ids", "attention_mask", "token_type_ids"),
        )

        dynamic_axes = {name: {0: "batch_size", 1: "sequence_length"} for name in input_names}
        dynamic_axes["logits"] = {0: "batch_size"}

        print(f"[INFO] Exporting raw ONNX -> {raw_onnx}")
        try:
            torch.onnx.export(
                model,
                input_tensors,
                str(raw_onnx),
                input_names=input_names,
                output_names=["logits"],
                dynamic_axes=dynamic_axes,
                opset_version=args.opset,
                do_constant_folding=True,
            )
        except Exception as exc:
            _fail(f"torch.onnx.export failed: {exc}")

    if not raw_onnx.exists():
        _fail(f"Raw ONNX file was not created: {raw_onnx}")

    print(f"[INFO] Optimizing ONNX graph with onnxruntime.transformers.optimizer -> {opt_onnx}")
    try:
        num_heads = int(getattr(model.config, "num_attention_heads", 12))
        hidden_size = int(getattr(model.config, "hidden_size", 768))
        optimized = optimize_model(
            str(raw_onnx),
            model_type="bert",
            num_heads=num_heads,
            hidden_size=hidden_size,
        )
        # optimize_model returns an object exposing save_model_to_file in most ORT versions.
        if hasattr(optimized, "save_model_to_file"):
            optimized.save_model_to_file(str(opt_onnx))
        elif hasattr(optimized, "model"):
            import onnx  # local import to avoid hard dependency until needed

            onnx.save_model(optimized.model, str(opt_onnx))
        else:
            _fail("Optimizer returned an unexpected object (missing save_model_to_file/model).")
    except Exception as exc:
        _fail(f"ONNX graph optimization failed: {exc}")

    if not opt_onnx.exists():
        _fail(f"Optimized ONNX file was not created: {opt_onnx}")

    print(f"[INFO] Quantizing dynamically to INT8 -> {int8_onnx}")
    # CRITICAL:
    # reduce_range=True is important for AVX2 paths with U8S8 kernels because VPMADDUBSW can
    # saturate/overflow 16-bit accumulators on outlier-heavy transformer activations/weights.
    # This sacrifices a bit of quantization range (7-bit effective) for significantly safer math.
    quantized_from = str(opt_onnx)
    try:
        quantize_dynamic(
            model_input=str(opt_onnx),
            model_output=str(int8_onnx),
            weight_type=QuantType.QInt8,
            reduce_range=True,
        )
    except Exception as exc:
        # Some ORT/ONNX combos fail type inference on optimized BERT graphs.
        # Retry with explicit default tensor type, then fallback to raw ONNX.
        print(f"[WARN] Quantization on optimized ONNX failed: {exc}")
        try:
            import onnx  # local import to avoid hard dependency until needed

            quantize_dynamic(
                model_input=str(opt_onnx),
                model_output=str(int8_onnx),
                weight_type=QuantType.QInt8,
                reduce_range=True,
                extra_options={"DefaultTensorType": onnx.TensorProto.FLOAT},
            )
        except Exception as exc_opt:
            print(f"[WARN] Quantization retry with DefaultTensorType on optimized ONNX failed: {exc_opt}")
            try:
                quantize_dynamic(
                    model_input=str(raw_onnx),
                    model_output=str(int8_onnx),
                    weight_type=QuantType.QInt8,
                    reduce_range=True,
                    extra_options={"DefaultTensorType": onnx.TensorProto.FLOAT},
                )
                quantized_from = str(raw_onnx)
            except Exception as exc_raw:
                _fail(
                    "Dynamic quantization failed for both optimized and raw ONNX. "
                    f"optimized_error={exc_opt}; raw_error={exc_raw}"
                )

    if not int8_onnx.exists():
        _fail(f"INT8 ONNX file was not created: {int8_onnx}")

    manifest = {
        "model_dir": str(model_dir),
        "output_dir": str(output_dir),
        "raw_onnx": str(raw_onnx),
        "optimized_onnx": str(opt_onnx),
        "int8_onnx": str(int8_onnx),
        "opset": args.opset,
        "max_seq_len": args.max_seq_len,
        "optimizer_model_type": "bert",
        "num_heads": int(getattr(model.config, "num_attention_heads", 12)),
        "hidden_size": int(getattr(model.config, "hidden_size", 768)),
        "quantization": {
            "mode": "dynamic",
            "weight_type": "QInt8",
            "reduce_range": True,
            "quantized_from": quantized_from,
            "note": "Enabled to reduce AVX2 VPMADDUBSW saturation risk.",
        },
    }
    _save_manifest(manifest_json, manifest)

    print("[OK] Export + optimization + quantization completed successfully.")
    print(f"[OK] Raw ONNX:        {raw_onnx}")
    print(f"[OK] Optimized ONNX:  {opt_onnx}")
    print(f"[OK] Quantized ONNX:  {int8_onnx}")
    print(f"[OK] Manifest:        {manifest_json}")


if __name__ == "__main__":
    main()

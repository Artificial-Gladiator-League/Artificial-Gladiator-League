"""Official Chess handler — loads Transformers model from cache and predicts moves.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)


class EndpointHandler:
    """Official platform Chess handler.

    Loads a Transformers causal LM from the given path and exposes a simple
    `__call__` that returns a dict {"move": "e2e4"}.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self.model = None
        self.tokenizer = None
        self._loaded = False

        log.info("✅ Using OFFICIAL Chess handler (ignoring user's handler.py)")
        log.info("✅ Loading model weights from cache: %s", str(self.path))

        try:
            # Lazy import to avoid hard dependency at module import time
            from transformers import AutoModelForCausalLM, AutoTokenizer
            import torch

            self.tokenizer = AutoTokenizer.from_pretrained(
                str(self.path), trust_remote_code=False, local_files_only=True
            )
            self.model = AutoModelForCausalLM.from_pretrained(
                str(self.path), use_safetensors=True, trust_remote_code=False, local_files_only=True
            )
            self.model.eval()
            self._loaded = True
            log.info("Official chess model loaded from cache: %s", str(self.path))
        except Exception:
            log.exception("Failed to load official chess model from %s", self.path)

    def __call__(self, data: dict) -> dict:
        inputs = data.get("inputs", data)
        fen = inputs.get("fen", "")
        if not fen:
            return {"error": "Missing 'fen' in request."}

        if not self._loaded:
            return {"error": "Model not loaded"}

        try:
            import torch

            enc = self.tokenizer(fen, return_tensors="pt")
            with torch.no_grad():
                outputs = self.model.generate(**enc, max_new_tokens=10, do_sample=False, temperature=1.0)
            decoded = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
            # Extract UCI-like token from the decoded text
            for part in reversed(decoded.strip().split()):
                p = part.strip()
                if len(p) >= 4 and p[0] in "abcdefgh" and p[1] in "12345678":
                    move = p[:5] if len(p) == 5 and p[4] in "qrbnQRBN" else p[:4]
                    return {"move": move}
            return {"move": decoded.strip()}
        except Exception:
            log.exception("Official chess handler prediction failed")
            return {"error": "prediction_failed"}

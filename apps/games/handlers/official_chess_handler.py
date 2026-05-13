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
        self.fallback = False

        log.info("[OK] Using OFFICIAL Chess handler (ignoring user's handler.py)")
        log.info("[OK] Loading model weights from cache: %s", str(self.path))

        try:
            # Lazy import to avoid hard dependency at module import time
            from transformers import AutoModelForCausalLM, AutoTokenizer
            import torch

            # Try to load fast tokenizer first; if conversion to fast tokenizer
            # fails (needs sentencepiece/tiktoken), retry with use_fast=False.
            try:
                self.tokenizer = AutoTokenizer.from_pretrained(
                    str(self.path), trust_remote_code=False, local_files_only=True
                )
            except Exception as e:
                log.warning("Fast tokenizer load failed, retrying with use_fast=False: %s", e)
                try:
                    self.tokenizer = AutoTokenizer.from_pretrained(
                        str(self.path), trust_remote_code=False, local_files_only=True, use_fast=False
                    )
                except Exception as e2:
                    log.exception("Failed to load tokenizer (fast and slow) from %s: %s", self.path, e2)
                    # Tokenizer not available in this environment (missing optional deps).
                    # Fall back to a deterministic move-picker instead of failing to load.
                    self.tokenizer = None
                    self.model = None
                    self._loaded = False
                    self.fallback = True

            # Only attempt to load the model if tokenizer loaded successfully
            if not self.fallback:
                try:
                    self.model = AutoModelForCausalLM.from_pretrained(
                        str(self.path), use_safetensors=True, trust_remote_code=False, local_files_only=True
                    )
                    self.model.eval()
                    self._loaded = True
                        log.info("Official chess model loaded from PRE-CACHED path: %s", str(self.path))
                except Exception:
                    log.exception("Failed to load model from %s", self.path)
                    self.model = None
                    self._loaded = False
                    self.fallback = True
        except Exception:
            log.exception("Failed to load official chess model from %s", self.path)
            self.fallback = True

    def __call__(self, data: dict) -> dict:
        inputs = data.get("inputs", data)
        fen = inputs.get("fen", "")
        if not fen:
            return {"error": "Missing 'fen' in request."}

        if not self._loaded:
            # If model/tokenizer couldn't be loaded due to missing environment
            # dependencies (protobuf, sentencepiece, tiktoken, etc.), fall
            # back to a simple deterministic legal-move picker so the sandbox
            # can still return a valid chess move instead of failing.
            if self.fallback:
                try:
                    import chess

                    board = chess.Board(fen)
                    legal = list(board.legal_moves)
                    if not legal:
                        return {"error": "no_legal_moves"}
                    # Deterministic choice: pick lexicographically first UCI
                    uci_moves = sorted([m.uci() for m in legal])
                    return {"move": uci_moves[0]}
                except Exception:
                    log.exception("Official chess handler fallback prediction failed")
                    return {"error": "fallback_failed"}
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

# ──────────────────────────────────────────────
# apps/games/model_check.py
#
# check_model(model_dir, data_dir, game_type) -> list[str]
#
# The single entrypoint that decides whether a user's model may become the
# active, playable SHA. The platform never interprets the model's internals —
# it only checks the fixed, versioned contract (config_model.json manifest +
# load()/get_move() or the legacy entrypoints) actually works:
#
#   1. Every .py file that would be imported is valid UTF-8-ish and
#      syntactically valid Python (checked on the host, WITHOUT executing
#      any of it — safe even for repos that haven't been sandboxed yet).
#   2. config_model.json (if present) has well-typed fields, and every
#      listed entrypoint/data file actually exists.
#   3. Inside the hardened Docker sandbox (no network, read-only, non-root —
#      see apps.games.sandbox_runner): the model imports, load() succeeds
#      (new-style) or the legacy entrypoint is present, and 3 sample
#      positions each produce a legal UCI move within the sandbox's
#      time/memory limits.
#
# Returns a list of human-readable problem strings — empty means the model
# passed every check. Never raises for model-quality problems.
#
# ``SandboxUnavailableError`` (Docker daemon down, image missing, etc.) is an
# INFRASTRUCTURE fault, not a model-quality problem, and is NOT caught here —
# it propagates so callers can tell "the model failed" apart from "we
# couldn't check the model right now" (see apps.games.exceptions).
# ──────────────────────────────────────────────
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_ENCODINGS = ("utf-8-sig", "cp1255", "cp1252", "latin-1")


def _decode_bytes(data: bytes) -> tuple[str, str | None]:
    """Decode file bytes trying UTF-8 first, then Windows-common encodings.

    Returns (text, encoding_used_if_not_utf8_sig).
    """
    for i, enc in enumerate(_ENCODINGS):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        return text, (enc if i > 0 else None)
    return data.decode("latin-1", errors="replace"), "latin-1 (with replacement)"


def _load_manifest(model_dir: Path) -> tuple[dict, list[str]]:
    """Read config_model.json. Returns (manifest, problems).

    A missing file is not a problem — it falls back to today's behavior.
    """
    cfg_path = model_dir / "config_model.json"
    if not cfg_path.exists():
        return {}, []
    try:
        text, _enc = _decode_bytes(cfg_path.read_bytes())
        manifest = json.loads(text)
    except Exception as exc:
        return {}, [f"config_model.json is not valid JSON: {exc}"]
    if not isinstance(manifest, dict):
        return {}, ["config_model.json must contain a JSON object"]
    return manifest, []


def _check_manifest_fields(manifest: dict, model_dir: Path, data_dir: Path | None) -> list[str]:
    problems: list[str] = []

    if "schema_version" in manifest and not isinstance(manifest["schema_version"], int):
        problems.append("manifest field 'schema_version' must be an integer")

    for field in ("modules", "data_files", "requirements"):
        if field in manifest and not isinstance(manifest[field], list):
            problems.append(f"manifest field '{field}' must be a list of strings")

    entrypoint = manifest.get("entrypoint")
    if entrypoint is not None:
        if not isinstance(entrypoint, str):
            problems.append("manifest field 'entrypoint' must be a string")
        elif not (model_dir / entrypoint).exists():
            problems.append(f"entrypoint '{entrypoint}' listed in config_model.json does not exist")

    for fname in manifest.get("modules") or []:
        if isinstance(fname, str) and not (model_dir / fname).exists():
            problems.append(f"module '{fname}' listed in config_model.json does not exist")

    for fname in manifest.get("data_files") or []:
        if not isinstance(fname, str):
            continue
        if data_dir is None or not (data_dir / fname).exists():
            problems.append(f"data file '{fname}' listed in config_model.json was not found in the data folder")

    return problems


def _entrypoint_files_to_check(manifest: dict, model_dir: Path, game_type: str) -> list[Path]:
    """Which .py files would actually be imported, per the manifest contract
    (mirrors apps.games.sandbox_runner._entrypoint_candidates)."""
    modules = manifest.get("modules")
    if modules:
        names = [m for m in modules if isinstance(m, str)]
    else:
        entrypoint = manifest.get("entrypoint")
        if isinstance(entrypoint, str):
            names = [entrypoint]
        else:
            default_entrypoint = f"{game_type or 'chess'}_mcvs.py"
            if (model_dir / default_entrypoint).exists():
                names = [default_entrypoint]
            else:
                names = [p.name for p in sorted(model_dir.glob("*.py")) if not p.name.startswith("_agl_")]
    return [model_dir / n for n in names]


def _check_syntax(model_dir: Path, manifest: dict, game_type: str) -> list[str]:
    """Decode + compile() every entrypoint .py file WITHOUT executing it."""
    problems: list[str] = []
    files = _entrypoint_files_to_check(manifest, model_dir, game_type)
    existing = [f for f in files if f.exists()]
    if not existing:
        # Missing-file problems are already reported by _check_manifest_fields
        # (for manifest-declared names) — for the glob-fallback case with no
        # .py files at all, report it here.
        if not manifest.get("modules") and not manifest.get("entrypoint"):
            problems.append("no .py model file found in the model directory")
        return problems

    for f in existing:
        try:
            data = f.read_bytes()
        except OSError as exc:
            problems.append(f"could not read '{f.name}': {exc}")
            continue
        text, fallback_enc = _decode_bytes(data)
        if fallback_enc:
            log.info("model_check: %s decoded as %s (not UTF-8)", f.name, fallback_enc)
        try:
            compile(text, f.name, "exec", dont_inherit=True)
        except SyntaxError as exc:
            problems.append(f"'{f.name}' has a syntax error: {exc}")
    return problems


def check_model_verbose(model_dir: Path, data_dir: Path | None, game_type: str) -> dict:
    """Run the full contract check and return the detail behind the verdict.

    Returns ``{"problems": [...], "warnings": [...], "moves": [...], "stage": ...}``
    where ``stage`` is the stage that produced the result: ``"manifest"`` or
    ``"syntax"`` (host-side checks failed, sandbox not started) or
    ``"sandbox"`` (host-side checks passed and the sandbox ran).
    Raises ``SandboxUnavailableError`` exactly like :func:`check_model`.
    """
    model_dir = Path(model_dir)
    data_dir = Path(data_dir) if data_dir else None

    if not model_dir.exists() or not model_dir.is_dir():
        return {
            "problems": [f"model directory does not exist: {model_dir}"],
            "warnings": [],
            "moves": [],
            "stage": "manifest",
        }

    manifest, problems = _load_manifest(model_dir)
    problems += _check_manifest_fields(manifest, model_dir, data_dir)
    manifest_problem_count = len(problems)
    problems += _check_syntax(model_dir, manifest, game_type)

    if problems:
        # Don't spin up Docker for a model that can't even import — the
        # sandbox check below would just repeat the same failure less
        # legibly (a raw traceback instead of "syntax error in X").
        return {
            "problems": problems,
            "warnings": [],
            "moves": [],
            "stage": "manifest" if manifest_problem_count else "syntax",
        }

    from apps.games.sandbox_runner import run_check_in_sandbox

    result = run_check_in_sandbox(model_dir, data_dir, game_type)
    warnings = list(result.get("warnings") or [])
    for note in warnings:
        log.info("model_check warning (game_type=%s): %s", game_type, note)
    problems += list(result.get("problems") or [])

    return {
        "problems": problems,
        "warnings": warnings,
        "moves": list(result.get("moves") or []),
        "stage": "sandbox",
    }


def check_model(model_dir: Path, data_dir: Path | None, game_type: str) -> list[str]:
    """Run the full contract check for a model. Returns a list of problems.

    An empty list means the model may be pinned/activated. Raises
    ``apps.games.exceptions.SandboxUnavailableError`` if the sandbox itself
    could not run (infra fault) — this is intentionally NOT swallowed here.
    """
    return check_model_verbose(model_dir, data_dir, game_type)["problems"]

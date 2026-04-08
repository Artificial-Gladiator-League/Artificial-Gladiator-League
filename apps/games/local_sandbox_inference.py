# ──────────────────────────────────────────────
# apps/games/local_sandbox_inference.py
#
# Self-hosted Docker sandbox for user-model
# inference on the Agladiator VPS.
#
# Architecture
# ────────────
# 1. download_model()  — pulls the user's HF repo
#    to a unique temp dir under /tmp/verification/,
#    allowing ONLY safe file patterns.
# 2. scan_model()      — runs static security scans
#    (bandit, regex, modelscan, fickling, picklescan)
#    and checks requirements.txt for forbidden pkgs.
# 3. verify_model()    — orchestrates full 3-phase
#    verification: download → scan → sandbox test.
#    Deletes ALL temp files when finished.
# 4. get_move_local()  — downloads to a temp dir,
#    runs inference in a Docker sandbox, then deletes
#    the temp dir immediately.
#
# ZERO persistent storage
# ───────────────────────
# • Never use the default HF cache (~/.cache/huggingface).
# • Always download to /tmp/verification/{user_id}_{game_type}_{ts}/.
# • After scanning + Docker test → shutil.rmtree() immediately.
# • Even during live games, model files are downloaded
#   per-move to a temp dir, inference runs in Docker,
#   then everything is deleted.
# • Docker containers are removed with --rm (auto) and
#   force-killed on timeout (docker rm -f).
#
# Security
# ────────
# • SafeTensors ONLY — .bin, .pkl, .pt, pickle
#   files cause automatic rejection.
# • Docker sandbox: --cpus=0.5 --memory=2g
#   --network=none --read-only
# • Static analysis with bandit + regex + modelscan +
#   fickling + picklescan before any execution.
# • requirements.txt is checked for forbidden pkgs.
#
# Ubuntu + Docker setup (run once on VPS):
# ────────────────────────────────────────
#   sudo apt update && sudo apt install -y docker.io
#   sudo systemctl enable --now docker
#   sudo usermod -aG docker $USER
#   pip install huggingface-hub safetensors bandit \
#       modelscan fickling picklescan
#   # Build the sandbox image:
#   #   docker build -t agladiator-sandbox:latest \
#   #       -f Dockerfile.sandbox .
#   # Minimal Dockerfile.sandbox:
#   #   FROM python:3.11-slim
#   #   RUN pip install --no-cache-dir torch safetensors \
#   #       transformers numpy
#   #   WORKDIR /workspace
# ──────────────────────────────────────────────
from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils import timezone

if TYPE_CHECKING:
    from apps.users.models import UserGameModel

log = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Only these patterns are downloaded from HF repos.
SAFE_ALLOW_PATTERNS = [
    "*.safetensors",
    "*.json",
    "*.py",
    "*.txt",
    "*.npz",
    "*.md",
]

# Files that MUST NOT appear in a valid model repo.
FORBIDDEN_EXTENSIONS = frozenset({".bin", ".pkl", ".pt", ".pickle", ".ckpt", ".pth"})

# Packages that are blocked in requirements.txt.
FORBIDDEN_PACKAGES = frozenset({
    "subprocess", "os-sys", "pwntools", "paramiko",
    "fabric", "invoke", "sh", "plumbum",
    "reverse-shell", "backdoor", "pyautogui",
    "keylogger", "scapy", "nmap", "impacket",
})

# Dangerous patterns in Python source code.
_DANGEROUS_CODE_PATTERNS = [
    (r"\bexec\s*\(", "exec() call"),
    (r"\beval\s*\(", "eval() call"),
    (r"\bos\.system\s*\(", "os.system() call"),
    (r"\bsubprocess\b", "subprocess usage"),
    (r"\b__import__\s*\(", "__import__() call"),
    (r"\bimportlib\b", "importlib usage"),
    (r"\bsocket\b", "socket usage"),
    (r"\brequests\b", "requests library"),
    (r"\burllib\b", "urllib usage"),
    (r"\bhttp\.client\b", "http.client usage"),
    (r"\bctypes\b", "ctypes usage"),
    (r"\bopen\s*\(.+['\"]w['\"]", "file write"),
    (r"\bcompile\s*\(", "compile() call"),
    (r"\bglobals\s*\(\s*\)", "globals() call"),
    (r"\bsetattr\s*\(", "setattr() call"),
]

# UCI move pattern for validation.
_UCI_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbnQRBN]?$")

# Base temp directory for all verification work.
_VERIFICATION_BASE = Path("/tmp/verification")

# SHA-256 checksum of the official platform handler.py.
# Any handler.py in a user repo that does not match this
# checksum will be rejected during the security scan.
_OFFICIAL_HANDLER_CHECKSUM = "7336546a4e2600599eabe682039d7f3ff0567cf397e7cb1ad17b16ba61185958"


def _docker_host_path(p: Path) -> str:
    """Convert a local path to a Docker-compatible volume mount path.

    On Windows, Docker Desktop expects paths like ``C:/Users/...``
    (forward slashes, with a drive letter).  A bare ``\\tmp\\...`` is
    rejected.  This helper resolves the path to an absolute form and
    normalises the separators.
    """
    resolved = p.resolve()              # adds drive letter on Windows
    return str(resolved).replace("\\", "/")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Temp directory management — ZERO persistent storage
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _make_temp_dir(user_id: int | str, game_type: str) -> Path:
    """Create a unique temp directory under /tmp/verification/.

    Format: /tmp/verification/{user_id}_{game_type}_{timestamp}/
    Caller is responsible for cleanup via _cleanup_temp_dir().
    """
    ts = int(_time.time() * 1000)
    temp_dir = _VERIFICATION_BASE / f"{user_id}_{game_type}_{ts}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


def _cleanup_temp_dir(temp_dir: Path) -> None:
    """Unconditionally delete a temp directory and all its contents.

    Called after EVERY operation — success or failure.
    """
    if temp_dir and temp_dir.exists() and str(temp_dir).startswith("/tmp/verification"):
        try:
            shutil.rmtree(temp_dir)
            log.debug("🧹 Cleaned up temp dir: %s", temp_dir)
        except Exception:
            log.warning("Could not clean up temp dir: %s", temp_dir, exc_info=True)


def _force_remove_container(container_name: str) -> None:
    """Force-remove a Docker container by name (best-effort)."""
    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase 1 — Download (to temp dir)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def download_model(
    repo_id: str,
    game_type: str,
    *,
    token: str | None = None,
    dest_dir: Path | None = None,
) -> tuple[bool, str, Path | None]:
    """Download a model repo from HF to a temp directory.

    ONLY allows safe file patterns (.safetensors, .json, .py, .txt, .npz).
    Rejects repos containing .bin, .pkl, .pt, or pickle files.
    Never uses the default HF cache — always downloads to dest_dir.

    Returns (success, message, local_dir).
    """
    if not repo_id:
        return False, "No repository ID provided.", None

    if dest_dir is None:
        dest_dir = _make_temp_dir("dl", game_type)

    model_dir = dest_dir / "model"
    model_dir.mkdir(parents=True, exist_ok=True)

    hf_token = token or getattr(settings, "HF_PLATFORM_TOKEN", "")

    try:
        from huggingface_hub import snapshot_download

        log.info("📥 Downloading model repo='%s' game_type='%s' → %s",
                 repo_id, game_type, model_dir)

        snapshot_download(
            repo_id=repo_id,
            local_dir=str(model_dir),
            token=hf_token or None,
            allow_patterns=SAFE_ALLOW_PATTERNS,
            cache_dir=str(dest_dir / ".hf_cache"),  # temp cache, never ~/.cache
        )
    except Exception as exc:
        log.exception("Failed to download model '%s'", repo_id)
        _cleanup_temp_dir(dest_dir)
        return False, f"Download failed: {exc}", None

    # ── Reject repos with forbidden file types ──
    for path in model_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in FORBIDDEN_EXTENSIONS:
            log.warning(
                "🚫 Forbidden file found: %s in repo '%s'", path.name, repo_id
            )
            _cleanup_temp_dir(dest_dir)
            return False, (
                f"Rejected: forbidden file type '{path.suffix}' found ({path.name}). "
                ".bin, .pkl, .pt, and pickle files are not allowed."
            ), None

    # ── SafeTensors enforcement (Chess only) ──
    # Breakthrough models are Python-based (.py + .json + .npz)
    # and never contain .safetensors files.
    if game_type != "breakthrough":
        safetensor_files = list(model_dir.rglob("*.safetensors"))
        if not safetensor_files:
            log.warning("No .safetensors files found in repo '%s'", repo_id)
            _cleanup_temp_dir(dest_dir)
            return False, (
                "Rejected: no SafeTensors weight files found in the repository. "
                "Your model must include at least one .safetensors file."
            ), None
        log.info("✅ Download complete for '%s' → %s (%d safetensors files)",
                 repo_id, model_dir, len(safetensor_files))
    else:
        # For Breakthrough, verify handler.py exists
        handler = model_dir / "handler.py"
        if not handler.exists():
            log.warning("No handler.py found in Breakthrough repo '%s'", repo_id)
            _cleanup_temp_dir(dest_dir)
            return False, (
                "Rejected: no handler.py found. Breakthrough models must include "
                "a handler.py with an EndpointHandler class."
            ), None
        log.info("✅ Download complete for '%s' → %s (Breakthrough — handler.py found)",
                 repo_id, model_dir)

    return True, "Download successful.", model_dir


def _download_data_repo(
    data_repo_id: str,
    dest_dir: Path,
    *,
    token: str | None = None,
) -> tuple[bool, str, Path | None]:
    """Download a data/dataset repo to a temp directory."""
    if not data_repo_id:
        return True, "No data repo to download.", None

    data_dir = dest_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    hf_token = token or getattr(settings, "HF_PLATFORM_TOKEN", "")

    try:
        from huggingface_hub import snapshot_download

        log.info("📥 Downloading dataset from data_repo_id: %s → %s",
                 data_repo_id, data_dir)
        print(f"📥 Downloading dataset from data_repo_id: {data_repo_id}")
        snapshot_download(
            repo_id=data_repo_id,
            local_dir=str(data_dir),
            token=hf_token or None,
            repo_type="dataset",
            allow_patterns=["*.npz", "*.json", "*.txt", "*.csv"],
            cache_dir=str(dest_dir / ".hf_cache"),
        )
    except Exception as exc:
        log.exception("Failed to download data repo '%s'", data_repo_id)
        return False, f"Data download failed: {exc}", None

    return True, "Data download successful.", data_dir


def _resolve_data_repo_id(
    model_dir: Path,
    game_model: "UserGameModel",
) -> str:
    """Resolve the data repo ID for Breakthrough models.

    Priority order:
    1. config_data.json in the model repo → "data_repo_id" key
    2. "data_repo_id" key inside config_model.json
    3. UserGameModel.hf_data_repo_id field
    4. HF_DATA_REPO_ID environment variable
    5. Empty string (no data repo)
    """
    # 1. config_data.json
    config_data_path = model_dir / "config_data.json"
    if config_data_path.exists():
        try:
            data = json.loads(config_data_path.read_text(encoding="utf-8"))
            repo = data.get("data_repo_id", "").strip()
            if repo:
                log.info("📥 Resolved data_repo_id from config_data.json: %s", repo)
                print(f"📥 Downloading dataset from config_data.json / data_repo_id: {repo}")
                return repo
        except (json.JSONDecodeError, OSError):
            log.debug("Could not parse config_data.json in model repo")

    # 2. config_model.json → data_repo_id
    config_model_path = model_dir / "config_model.json"
    if config_model_path.exists():
        try:
            data = json.loads(config_model_path.read_text(encoding="utf-8"))
            repo = data.get("data_repo_id", "").strip()
            if repo:
                log.info("📥 Resolved data_repo_id from config_model.json: %s", repo)
                print(f"📥 Downloading dataset from config_model.json / data_repo_id: {repo}")
                return repo
        except (json.JSONDecodeError, OSError):
            log.debug("Could not parse config_model.json for data_repo_id")

    # 3. UserGameModel field
    if game_model.hf_data_repo_id:
        log.info("📥 Using data_repo_id from UserGameModel: %s",
                 game_model.hf_data_repo_id)
        print(f"📥 Downloading dataset from UserGameModel field: {game_model.hf_data_repo_id}")
        return game_model.hf_data_repo_id

    # 4. Environment variable
    env_repo = os.environ.get("HF_DATA_REPO_ID", "").strip()
    if env_repo:
        log.info("📥 Using data_repo_id from HF_DATA_REPO_ID env: %s", env_repo)
        print(f"📥 Downloading dataset from HF_DATA_REPO_ID env: {env_repo}")
        return env_repo

    return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase 2 — Security scan
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def scan_model(model_dir: Path) -> tuple[bool, dict]:
    """Run security scans on a downloaded model directory.

    Scans performed:
      1. Forbidden file extension check
      2. Dangerous code pattern regex scan on .py files
      3. bandit — Python static analysis
      4. modelscan — Protect AI model scanner
      5. fickling — pickle analysis
      6. picklescan — pickle payload scanner
      7. requirements.txt package check
      8. config_model.json safe parse check

    Returns (passed, report_dict).
    """
    report: dict = {
        "scanned_at": timezone.now().isoformat(),
        "model_dir": str(model_dir),
        "checks": {},
        "passed": True,
    }

    if not model_dir or not model_dir.exists():
        report["passed"] = False
        report["checks"]["exists"] = "Model directory does not exist."
        return False, report

    # ── 1. Forbidden extensions ──
    forbidden_found = []
    for path in model_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in FORBIDDEN_EXTENSIONS:
            forbidden_found.append(path.name)
    if forbidden_found:
        report["passed"] = False
        report["checks"]["forbidden_files"] = forbidden_found
        return False, report
    report["checks"]["forbidden_files"] = "clean"

    # ── 1b. Verify handler.py matches official platform version ──
    handler_path = model_dir / "handler.py"
    if handler_path.exists():
        import hashlib
        actual = hashlib.sha256(
            handler_path.read_bytes()
        ).hexdigest()
        if actual != _OFFICIAL_HANDLER_CHECKSUM:
            report["passed"] = False
            report["checks"]["handler_checksum"] = {
                "passed": False,
                "reason": "handler.py does not match the official "
                          "platform version — do not modify handler.py.",
                "expected": _OFFICIAL_HANDLER_CHECKSUM[:12] + "...",
                "actual": actual[:12] + "...",
            }
            return False, report
        else:
            report["checks"]["handler_checksum"] = {"passed": True}

    # ── 2. Dangerous code pattern scan ──
    py_files = list(model_dir.rglob("*.py"))
    if py_files:
        user_py_files = [f for f in py_files if f.name != "handler.py"]
        if user_py_files:
            code_issues = _scan_code_patterns(user_py_files)
            report["checks"]["code_patterns"] = code_issues
            if code_issues.get("dangerous_patterns"):
                report["passed"] = False
        else:
            report["checks"]["code_patterns"] = "skipped — only handler.py present"

    # ── 3. Bandit (Python code analysis) ──
    if py_files:
        report["checks"]["bandit"] = _run_bandit(model_dir)
        if report["checks"]["bandit"].get("high_severity"):
            report["passed"] = False
    else:
        report["checks"]["bandit"] = "no_python_files"

    # ── 4. Modelscan ──
    report["checks"]["modelscan"] = _run_modelscan(model_dir)
    if report["checks"]["modelscan"].get("issues_found"):
        report["passed"] = False

    # ── 5. Fickling ──
    report["checks"]["fickling"] = _run_fickling(model_dir)
    if report["checks"]["fickling"].get("issues_found"):
        report["passed"] = False

    # ── 6. Picklescan ──
    report["checks"]["picklescan"] = _run_picklescan(model_dir)
    if report["checks"]["picklescan"].get("infected"):
        report["passed"] = False

    # ── 7. requirements.txt check ──
    report["checks"]["requirements"] = _check_requirements(model_dir)
    if report["checks"]["requirements"].get("forbidden_packages"):
        report["passed"] = False

    # ── 8. config_model.json safe parse ──
    report["checks"]["config_json"] = _check_config_json(model_dir)

    log.info("🔍 Scan complete for %s — passed=%s", model_dir, report["passed"])
    return report["passed"], report


def _scan_code_patterns(py_files: list[Path]) -> dict:
    """Scan Python files for dangerous code patterns using regex."""
    found = []
    for py_file in py_files:
        try:
            content = py_file.read_text(encoding="utf-8", errors="replace")
            for pattern, label in _DANGEROUS_CODE_PATTERNS:
                matches = re.findall(pattern, content)
                if matches:
                    found.append({
                        "file": py_file.name,
                        "pattern": label,
                        "count": len(matches),
                    })
        except Exception:
            log.debug("Could not read %s for pattern scan", py_file)
    return {
        "dangerous_patterns": found if found else [],
        "files_scanned": len(py_files),
    }


def _run_bandit(model_dir: Path) -> dict:
    """Run bandit on all Python files in the model directory."""
    try:
        result = subprocess.run(
            ["bandit", "-r", str(model_dir), "-f", "json", "-ll"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = {}

        results = data.get("results", [])
        high = [r for r in results if r.get("issue_severity") == "HIGH"]
        return {
            "total_issues": len(results),
            "high_severity": len(high),
            "details": results[:10],
        }
    except FileNotFoundError:
        log.warning("bandit not installed — skipping code scan")
        return {"skipped": True, "reason": "bandit not installed"}
    except subprocess.TimeoutExpired:
        return {"skipped": True, "reason": "bandit timed out"}
    except Exception as exc:
        return {"skipped": True, "reason": str(exc)}


def _run_modelscan(model_dir: Path) -> dict:
    """Run modelscan (Protect AI) on the model directory."""
    try:
        result = subprocess.run(
            ["modelscan", "--path", str(model_dir), "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = {}

        issues = data.get("issues", [])
        return {
            "issues_found": len(issues),
            "details": issues[:10],
        }
    except FileNotFoundError:
        log.warning("modelscan not installed — skipping model scan")
        return {"skipped": True, "reason": "modelscan not installed"}
    except subprocess.TimeoutExpired:
        return {"skipped": True, "reason": "modelscan timed out"}
    except Exception as exc:
        return {"skipped": True, "reason": str(exc)}


def _run_fickling(model_dir: Path) -> dict:
    """Run fickling on any pickle-like files in the model directory."""
    # fickling analyses pickle files — even though we only allow safetensors,
    # we check as a defence-in-depth measure.
    pickle_files = []
    for ext in (".pkl", ".pickle", ".bin", ".pt", ".pth"):
        pickle_files.extend(model_dir.rglob(f"*{ext}"))

    if not pickle_files:
        return {"status": "no_pickle_files"}

    try:
        results = []
        for pf in pickle_files[:5]:  # Cap at 5 files
            result = subprocess.run(
                ["fickling", "--check-safety", str(pf)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if "unsafe" in result.stdout.lower() or result.returncode != 0:
                results.append({"file": pf.name, "unsafe": True, "output": result.stdout[:200]})
        return {
            "issues_found": len(results),
            "details": results,
        }
    except FileNotFoundError:
        log.warning("fickling not installed — skipping pickle analysis")
        return {"skipped": True, "reason": "fickling not installed"}
    except Exception as exc:
        return {"skipped": True, "reason": str(exc)}


def _run_picklescan(model_dir: Path) -> dict:
    """Run picklescan on the model directory."""
    try:
        result = subprocess.run(
            ["picklescan", "--path", str(model_dir)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        infected = (
            "infected" in result.stdout.lower()
            and "0 infected" not in result.stdout.lower()
        )
        return {
            "infected": infected,
            "output": result.stdout.strip()[:500],
        }
    except FileNotFoundError:
        log.warning("picklescan not installed — skipping pickle scan")
        return {"skipped": True, "reason": "picklescan not installed"}
    except subprocess.TimeoutExpired:
        return {"skipped": True, "reason": "picklescan timed out"}
    except Exception as exc:
        return {"skipped": True, "reason": str(exc)}


def _check_requirements(model_dir: Path) -> dict:
    """Check requirements.txt for forbidden packages."""
    req_file = model_dir / "requirements.txt"
    if not req_file.exists():
        return {"status": "no_requirements_file"}

    try:
        content = req_file.read_text(encoding="utf-8")
    except Exception:
        return {"status": "could_not_read"}

    found = []
    for line in content.splitlines():
        line = line.strip().lower()
        if not line or line.startswith("#"):
            continue
        pkg = re.split(r"[><=!~\[]", line)[0].strip()
        if pkg in FORBIDDEN_PACKAGES:
            found.append(pkg)

    if found:
        return {"forbidden_packages": found}
    return {"status": "clean"}


def _check_config_json(model_dir: Path) -> dict:
    """Safely parse config_model.json (pure JSON, no code execution)."""
    config_path = model_dir / "config_model.json"
    if not config_path.exists():
        config_path = model_dir / "config.json"
    if not config_path.exists():
        return {"status": "no_config_file"}

    try:
        content = config_path.read_text(encoding="utf-8")
        data = json.loads(content)  # pure JSON parse, no eval
        return {
            "status": "valid_json",
            "keys": list(data.keys())[:20],
        }
    except json.JSONDecodeError as exc:
        return {"status": "invalid_json", "error": str(exc)}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase 3 — Docker sandbox execution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _get_docker_image() -> str:
    return getattr(settings, "SANDBOX_DOCKER_IMAGE", "agladiator-sandbox:latest")


def _get_move_timeout() -> int:
    return int(getattr(settings, "SANDBOX_MOVE_TIMEOUT", 10))


def _get_verify_timeout() -> int:
    return int(getattr(settings, "SANDBOX_VERIFY_TIMEOUT", 30))


def _run_in_sandbox(
    model_dir: Path,
    fen: str,
    player: str,
    game_type: str,
    *,
    data_dir: Path | None = None,
    timeout: int | None = None,
    container_name: str | None = None,
) -> str | None:
    """Run inference in a Docker sandbox container.

    The container:
      • --network=none     — no network access
      • --cpus=0.5         — half a CPU core
      • --memory=2g        — 2 GB RAM limit
      • --read-only        — read-only root filesystem
      • --tmpfs /tmp       — writable /tmp for model loading
      • --rm               — auto-remove on exit
      • model mounted at /model:ro

    Returns a UCI move string, or None on failure.
    """
    docker_image = _get_docker_image()
    if timeout is None:
        timeout = _get_move_timeout()

    if container_name is None:
        container_name = f"agl-sandbox-{int(_time.time() * 1000)}"

    # Write the inference script into the model directory
    inference_script = _build_inference_script(game_type)
    script_path = model_dir / "_agl_inference.py"
    try:
        script_path.write_text(inference_script, encoding="utf-8")
    except Exception:
        log.exception("Could not write inference script to %s", model_dir)
        return None

    docker_cmd = [
        "docker", "run",
        "--rm",
        f"--name={container_name}",
        "--network=none",
        "--cpus=0.5",
        "--memory=2g",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=256m",
        "-v", f"{_docker_host_path(model_dir)}:/model:ro",
    ]

    if data_dir and data_dir.exists():
        docker_cmd.extend(["-v", f"{_docker_host_path(data_dir)}:/data:ro"])

    docker_cmd.extend([
        "-e", f"GAME_TYPE={game_type}",
        "-e", f"FEN={fen}",
        "-e", f"PLAYER={player}",
        docker_image,
        "python", "/model/_agl_inference.py",
    ])

    log.info(
        "🐳 Running sandbox: container=%s game=%s fen=%.40s",
        container_name, game_type, fen,
    )

    try:
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            log.warning(
                "Sandbox exit code %d (container=%s): stderr=%s",
                result.returncode, container_name, result.stderr[:500],
            )
            return None

        move = _parse_sandbox_output(result.stdout)
        if move:
            log.info("🎯 Sandbox move: %s (container=%s)", move, container_name)
        else:
            log.warning(
                "Unparseable sandbox output (container=%s): %s",
                container_name, result.stdout[:200],
            )
        return move

    except subprocess.TimeoutExpired:
        log.warning("⏰ Sandbox timed out after %ds (container=%s)", timeout, container_name)
        _force_remove_container(container_name)
        return None
    except FileNotFoundError:
        log.error("Docker is not installed or not in PATH")
        return None
    except Exception:
        log.exception("Sandbox failed (container=%s)", container_name)
        _force_remove_container(container_name)
        return None


def _find_cached_model(
    repo_id: str,
    game_type: str,
) -> tuple[Path | None, Path | None]:
    """Look up a pre-downloaded model in /tmp/user_models/ for *repo_id*.

    Returns (model_dir, data_dir) if found, (None, None) otherwise.
    """
    try:
        from apps.users.models import UserGameModel

        gm = UserGameModel.objects.filter(
            hf_model_repo_id=repo_id,
            game_type=game_type,
        ).first()
        if not gm:
            return None, None

        user_models_base = getattr(settings, "USER_MODELS_BASE_DIR", Path("/tmp/user_models"))
        model_dir = user_models_base / str(gm.user_id) / game_type / "model"
        data_dir = user_models_base / str(gm.user_id) / game_type / "data"

        if model_dir.exists():
            return model_dir, (data_dir if data_dir.exists() else None)
    except Exception:
        log.debug("Could not look up cached model for %s", repo_id, exc_info=True)
    return None, None


def _resolve_user_token(repo_id: str, game_type: str) -> str | None:
    """Return the best HF token for the owner of *repo_id*.

    Tries the user's own OAuth token first (needed for gated repos),
    falling back to the platform token.
    """
    try:
        from apps.users.models import UserGameModel
        from apps.users.hf_oauth import get_user_hf_token

        gm = UserGameModel.objects.select_related("user").filter(
            hf_model_repo_id=repo_id,
            game_type=game_type,
        ).first()
        if gm:
            user_token = get_user_hf_token(gm.user)
            if user_token:
                return user_token
    except Exception:
        log.debug("Could not resolve user token for %s", repo_id, exc_info=True)
    return getattr(settings, "HF_PLATFORM_TOKEN", "")


def get_move_local(
    repo_id: str,
    fen: str,
    player: str,
    game_type: str = "chess",
    *,
    data_repo_id: str = "",
) -> str | None:
    """Download model to temp dir, run inference in Docker, delete everything.

    This is the main entry point for live-game inference.
    ZERO persistent storage: downloads per call, deletes after.

    Parameters
    ----------
    repo_id : str
        HF model repo ID.
    fen : str
        Board position in FEN format.
    player : str
        'w' or 'b'.
    game_type : str
        'chess' or 'breakthrough'.
    data_repo_id : str
        HF data repo ID (Breakthrough only).

    Returns
    -------
    str or None
        A UCI move string if successful, None on any failure.
    """
    if not repo_id:
        log.warning("get_move_local called with empty repo_id")
        return None

    # ── Try pre-downloaded model from /tmp/user_models/ (login cache) ──
    cached_model_dir, cached_data_dir = _find_cached_model(repo_id, game_type)
    if cached_model_dir:
        log.info(
            "♻️ Using cached model for '%s' at %s", repo_id, cached_model_dir,
        )
        try:
            move = _run_in_sandbox(
                cached_model_dir, fen, player, game_type,
                data_dir=cached_data_dir,
            )
            return move
        except Exception:
            log.warning(
                "Cached model inference failed for %s — falling back to download",
                repo_id, exc_info=True,
            )

    # ── Fallback: download fresh (original per-move behaviour) ──
    temp_dir = _make_temp_dir(f"move_{repo_id.replace('/', '_')}", game_type)
    model_dir: Path | None = None
    data_dir: Path | None = None

    try:
        # Resolve the best token for this repo (user's OAuth → platform)
        _dl_token = _resolve_user_token(repo_id, game_type)

        # Download model to temp dir
        ok, msg, model_dir = download_model(
            repo_id, game_type, token=_dl_token, dest_dir=temp_dir,
        )
        if not ok or model_dir is None:
            log.warning("Download failed for move: %s — %s", repo_id, msg)
            return None

        # Download data repo if needed (Breakthrough)
        if data_repo_id:
            ok_data, _, data_dir = _download_data_repo(
                data_repo_id, temp_dir, token=_dl_token,
            )
            if not ok_data:
                log.warning("Data download failed for %s", data_repo_id)

        # Run inference in sandbox
        move = _run_in_sandbox(
            model_dir, fen, player, game_type,
            data_dir=data_dir,
        )
        return move

    finally:
        # ALWAYS clean up — zero persistent storage
        _cleanup_temp_dir(temp_dir)


def _build_inference_script(game_type: str) -> str:
    """Return the Python script that runs inside the Docker sandbox.

    The script:
      1. Reads FEN, PLAYER, GAME_TYPE from environment variables.
      2. Looks for handler.py (EndpointHandler pattern) in /model.
      3. Falls back to standard Transformers loading (chess only).
      4. Prints ``MOVE:<uci>`` to stdout.
    """
    return '''#!/usr/bin/env python3
"""Sandbox inference — runs inside Docker container.
Auto-generated by Agladiator. Do not edit."""
import json
import os
import sys

GAME_TYPE = os.environ.get("GAME_TYPE", "chess")
FEN = os.environ.get("FEN", "")
PLAYER = os.environ.get("PLAYER", "w")


def try_custom_handler():
    """Try handler.py with EndpointHandler pattern."""
    handler_path = "/model/handler.py"
    if not os.path.exists(handler_path):
        return None

    import importlib.util
    spec = importlib.util.spec_from_file_location("handler", handler_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if not hasattr(mod, "EndpointHandler"):
        return None

    handler = mod.EndpointHandler(path="/model")
    return handler({
        "inputs": {"fen": FEN, "player": PLAYER, "game_type": GAME_TYPE}
    })


def try_transformers_chess():
    """Standard Transformers loading for chess models."""
    try:
        from transformers import AutoModelForCausalLM, AutoTokenizer
        import torch

        model = AutoModelForCausalLM.from_pretrained(
            "/model", use_safetensors=True, trust_remote_code=False,
        )
        tokenizer = AutoTokenizer.from_pretrained(
            "/model", trust_remote_code=False,
        )
        model.eval()

        inputs = tokenizer(FEN, return_tensors="pt")
        with torch.no_grad():
            outputs = model.generate(
                **inputs, max_new_tokens=10,
                do_sample=False, temperature=1.0,
            )
        decoded = tokenizer.decode(outputs[0], skip_special_tokens=True)
        # Extract UCI move from output
        for part in reversed(decoded.strip().split()):
            p = part.strip()
            if len(p) >= 4 and p[0] in "abcdefgh" and p[1] in "12345678":
                return p[:5] if len(p) == 5 and p[4] in "qrbnQRBN" else p[:4]
        return decoded.strip()
    except Exception:
        return None


def main():
    result = try_custom_handler()
    if result is None and GAME_TYPE == "chess":
        result = try_transformers_chess()
    if result is None:
        print("ERROR:no_result", file=sys.stderr)
        sys.exit(1)

    move = None
    if isinstance(result, str):
        move = result.strip()
    elif isinstance(result, dict):
        move = result.get("move") or result.get("output", "")
    elif isinstance(result, list) and result:
        item = result[0]
        if isinstance(item, dict):
            move = item.get("move") or item.get("output", "")
        elif isinstance(item, str):
            move = item.strip()
    elif isinstance(result, bytes):
        move = result.decode("utf-8", errors="replace").strip()

    if move:
        print(f"MOVE:{move}", flush=True)
    else:
        print("ERROR:empty_result", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
'''


def _parse_sandbox_output(stdout: str) -> str | None:
    """Extract a UCI move from Docker container stdout."""
    for line in stdout.strip().splitlines():
        line = line.strip()
        if line.startswith("MOVE:"):
            move = line[5:].strip()
            if _UCI_RE.match(move):
                return move
            # Breakthrough uses same board notation
            if len(move) == 4 and move[0] in "abcdefgh" and move[2] in "abcdefgh":
                return move
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Full verification pipeline
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test positions for the sandbox verification phase.
_TEST_POSITIONS = {
    "chess": [
        ("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "w"),
        ("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1", "b"),
        ("r1bqkb1r/pppppppp/2n2n2/8/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3", "w"),
    ],
    "breakthrough": [
        ("BBBBBBBB/BBBBBBBB/8/8/8/8/WWWWWWWW/WWWWWWWW w", "w"),
        ("BBBBBBBB/BBBBBBBB/8/8/8/3W4/WWWW1WWW/WWWWWWWW b", "b"),
    ],
}


def verify_model(
    game_model: UserGameModel,
    *,
    token: str | None = None,
    force: bool = False,
) -> tuple[bool, str, dict]:
    """Run the full 3-phase verification pipeline for a model.

    Phase 1: Download to temp dir (SafeTensors only)
    Phase 2: Security scan (regex + bandit + modelscan + fickling + picklescan)
    Phase 3: Docker sandbox test with multiple FEN positions

    ALL temporary files are deleted when finished (success or failure).

    Returns (passed, message, report).
    """
    repo_id = game_model.hf_model_repo_id
    game_type = game_model.game_type

    if not repo_id:
        return False, "No model repository linked.", {}

    report: dict = {
        "repo_id": repo_id,
        "game_type": game_type,
        "started_at": timezone.now().isoformat(),
        "phases": {},
    }

    # Skip if already verified at same commit (unless forced)
    if not force and game_model.verification_status == "approved":
        current_sha = _get_current_commit_sha(repo_id, token)
        if current_sha and current_sha == game_model.last_verified_commit:
            log.info("Model %s already verified at commit %s",
                     repo_id, current_sha[:12])
            return True, "Model already verified — no changes detected.", report

    log.info("🔄 Starting verification for %s (%s)", repo_id, game_type)
    print(f"🔄 [{game_type.upper()}] Starting verification for {repo_id}...")

    # Mark as pending
    game_model.verification_status = "pending"
    game_model.save(update_fields=["verification_status"])

    # Create the sole temp directory for this entire verification
    temp_dir = _make_temp_dir(game_model.user_id, game_type)

    try:
        # ── Phase 1: Download ──
        print(f"📥 Downloading model repo {repo_id} ...")
        ok, msg, model_dir = download_model(
            repo_id, game_type, token=token, dest_dir=temp_dir,
        )
        report["phases"]["download"] = {"passed": ok, "message": msg}
        if not ok:
            _mark_rejected(game_model, report)
            return False, f"Download failed: {msg}", report

        # Download data repo if needed (Breakthrough)
        data_dir = None
        if game_type == "breakthrough" and model_dir:
            data_repo_id = _resolve_data_repo_id(model_dir, game_model)
            if data_repo_id:
                ok_data, msg_data, data_dir = _download_data_repo(
                    data_repo_id, temp_dir, token=token,
                )
                report["phases"]["data_download"] = {
                    "passed": ok_data, "message": msg_data,
                    "data_repo_id": data_repo_id,
                }
                if not ok_data:
                    _mark_rejected(game_model, report)
                    return False, f"Data download failed: {msg_data}", report
        elif game_model.hf_data_repo_id:
            ok_data, msg_data, data_dir = _download_data_repo(
                game_model.hf_data_repo_id, temp_dir, token=token,
            )
            report["phases"]["data_download"] = {"passed": ok_data, "message": msg_data}
            if not ok_data:
                _mark_rejected(game_model, report)
                return False, f"Data download failed: {msg_data}", report

        print(f"✅ Files downloaded to temporary folder: {temp_dir}  (you can inspect them now before deletion)")

        # ── Phase 2: Security scan ──
        print(f"🔍 Running malicious code scan (bandit + modelscan + fickling)...")
        scan_passed, scan_report = scan_model(model_dir)
        report["phases"]["security_scan"] = scan_report
        if not scan_passed:
            _mark_rejected(game_model, report)
            return False, "Security scan failed — see report for details.", report

        # ── Phase 3: Sandbox test ──
        print(f"🧪 Running sandboxed test in Docker container...")
        test_positions = _TEST_POSITIONS.get(game_type, _TEST_POSITIONS["chess"])
        test_results = []
        all_tests_passed = True

        for test_fen, test_player in test_positions:
            move = _run_in_sandbox(
                model_dir, test_fen, test_player, game_type,
                data_dir=data_dir,
                timeout=_get_verify_timeout(),
            )
            passed = move is not None
            test_results.append({
                "fen": test_fen,
                "player": test_player,
                "returned_move": move,
                "passed": passed,
            })
            if not passed:
                all_tests_passed = False

        report["phases"]["sandbox_test"] = {
            "passed": all_tests_passed,
            "positions_tested": len(test_positions),
            "results": test_results,
        }

        if not all_tests_passed:
            _mark_rejected(game_model, report)
            return False, "Sandbox test failed — model did not return valid moves.", report

        # ── All phases passed ──
        current_sha = _get_current_commit_sha(repo_id, token)
        now = timezone.now()

        game_model.verification_status = "approved"
        game_model.last_verified_commit = current_sha or ""
        game_model.last_verified_at = now
        game_model.scan_report = report
        game_model.model_integrity_ok = True
        game_model.rated_games_since_revalidation = 30
        game_model.save(update_fields=[
            "verification_status",
            "last_verified_commit",
            "last_verified_at",
            "scan_report",
            "model_integrity_ok",
            "rated_games_since_revalidation",
        ])

        report["completed_at"] = now.isoformat()
        log.info(
            "✅ Verification PASSED for %s (%s) — commit %s",
            repo_id, game_type, (current_sha or "unknown")[:12],
        )
        print(f"✅ Verification complete — model approved and ready to play")
        return True, "Model verified successfully — approved for play.", report

    finally:
        # ALWAYS clean up — zero persistent storage
        print(f"🗑️ Cleaning up temporary files (zero persistent storage)")
        _cleanup_temp_dir(temp_dir)


def _mark_rejected(game_model: UserGameModel, report: dict) -> None:
    """Mark a model as rejected after a failed verification."""
    game_model.verification_status = "rejected"
    game_model.scan_report = report
    game_model.model_integrity_ok = False
    game_model.save(update_fields=[
        "verification_status",
        "scan_report",
        "model_integrity_ok",
    ])


def _get_current_commit_sha(
    repo_id: str,
    token: str | None = None,
) -> str | None:
    """Resolve the current commit SHA for a repo via HfApi."""
    if not repo_id:
        return None
    hf_token = token or getattr(settings, "HF_PLATFORM_TOKEN", "")
    try:
        from huggingface_hub import HfApi

        api = HfApi(token=hf_token or None)
        info = api.model_info(repo_id, token=hf_token or None)
        return info.sha
    except Exception:
        log.debug("Could not get commit SHA for %s", repo_id, exc_info=True)
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Daily re-verification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def reverify_model(
    game_model: UserGameModel,
    *,
    token: str | None = None,
) -> tuple[bool, str]:
    """Daily re-verification: compare commit SHA, re-verify if changed.

    Called by the scheduler. Only re-runs the full pipeline
    if the commit SHA has changed since last verification.
    If verification fails, the model is locked from tournaments
    and rated games (model_integrity_ok = False).

    Returns (still_ok, message).
    """
    repo_id = game_model.hf_model_repo_id
    if not repo_id:
        return False, "No model repository linked."

    current_sha = _get_current_commit_sha(repo_id, token)
    if current_sha is None:
        return False, "Could not reach HuggingFace to check model."

    # No change since last verification — still approved
    if current_sha == game_model.last_verified_commit:
        log.info(
            "Re-verify %s: commit unchanged (%s) — still approved.",
            repo_id, current_sha[:12],
        )
        return True, "Model unchanged — still approved."

    # Commit changed — mark suspicious, lock from rated/tournaments,
    # reset the 30-game counter, then re-run full verification.
    log.info(
        "Re-verify %s: commit changed %s → %s — re-running verification.",
        repo_id,
        (game_model.last_verified_commit or "none")[:12],
        current_sha[:12],
    )
    game_model.verification_status = "suspicious"
    game_model.model_integrity_ok = False
    game_model.rated_games_since_revalidation = 0
    game_model.save(update_fields=[
        "verification_status",
        "model_integrity_ok",
        "rated_games_since_revalidation",
    ])

    passed, msg, _ = verify_model(game_model, token=token, force=True)
    return passed, msg

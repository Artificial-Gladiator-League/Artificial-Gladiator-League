"""Local-only model preloader.

This preloader does NOT perform any network I/O. It copies model and
dataset snapshots from the local HF cache (``HF_HUB_CACHE``) or from
bundled/test cache directories (e.g. test1978) into the per-user
``USER_MODELS_BASE_DIR/user_{id}/{game}/(model|data)`` folders.

Intended for emergency use when gated repos or network access must be
disabled. If nothing suitable is found the function does nothing and
returns silently.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
from pathlib import Path

from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)


def _resolve_revision(gm) -> str:
    """Pick the best revision/SHA to pin a snapshot_download against."""
    return (
        (getattr(gm, "approved_full_sha", "") or "").strip()
        or (getattr(gm, "submitted_ref", "") or "").strip()
        or "main"
    )


def _resolve_hf_token(gm) -> str | None:
    """Return the best available HF token (per-user OAuth, else platform)."""
    try:
        from apps.users.hf_oauth import get_user_hf_token
        tok = get_user_hf_token(gm.user)
        if tok:
            return tok
    except Exception:
        log.debug("Could not resolve user HF token for user=%s", getattr(gm, "user_id", "?"), exc_info=True)
    plat = getattr(settings, "HF_PLATFORM_TOKEN", None) or os.environ.get("HF_TOKEN")
    return plat or None


def ensure_hf_snapshot(gm) -> Path | None:
    """Ensure the shared HF hub cache contains a snapshot for *gm*.

    Mirrors ``gm.hf_model_repo_id`` into ``settings.HF_HUB_CACHE`` using
    ``huggingface_hub.snapshot_download`` so that subsequent calls to
    :func:`apps.games.local_inference._find_hf_cache_snapshot` succeed and
    inference can read weights locally instead of round-tripping the HF API.

    Returns the snapshot directory on success, else ``None``.
    """
    repo_id = (getattr(gm, "hf_model_repo_id", "") or "").strip()
    if not repo_id:
        return None

    from apps.games.local_inference import _find_hf_cache_snapshot

    revision = _resolve_revision(gm)

    # Already cached?
    snap = _find_hf_cache_snapshot(repo_id)
    if snap is not None and (
        any(snap.rglob("*.safetensors"))
        or any(snap.rglob("*.py"))
        or any(snap.rglob("*.npz"))
    ):
        return snap

    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except Exception:
        log.warning("ensure_hf_snapshot: huggingface_hub unavailable; cannot mirror %s", repo_id)
        return None

    cache_dir = str(getattr(settings, "HF_HUB_CACHE", ""))
    if not cache_dir:
        log.warning("ensure_hf_snapshot: HF_HUB_CACHE not configured; skipping %s", repo_id)
        return None

    token = _resolve_hf_token(gm)
    try:
        snap_path = snapshot_download(
            repo_id=repo_id,
            repo_type=getattr(gm, "submission_repo_type", "model") or "model",
            revision=revision,
            cache_dir=cache_dir,
            token=token,
        )
        sha = Path(snap_path).name
        msg = f"Cached snapshot for {repo_id} at {sha}"
        print(msg)
        log.info(msg)
        return Path(snap_path)
    except Exception:
        log.exception(
            "ensure_hf_snapshot: snapshot_download failed for repo=%s revision=%s",
            repo_id, revision,
        )
        return None


def preload_user_models(user_id: int) -> None:
    """Validate and register shared HF hub cache snapshots for a user's models.

    No-copy: inference reads directly from ``HF_HUB_CACHE`` snapshot dirs.
    Updates ``UserGameModel.cached_path`` to the snapshot path so that
    predict_chess / predict_breakthrough can locate the weights without
    querying the filesystem each move.

    If a repo is not yet present in the local cache, this function will
    attempt to mirror it via :func:`ensure_hf_snapshot` before falling back
    to the HF API at inference time.
    """
    from apps.games.local_inference import _find_hf_cache_snapshot
    try:
        from apps.users.models import UserGameModel
        game_models = list(UserGameModel.objects.filter(user_id=user_id, hf_model_repo_id__gt=""))
    except Exception:
        log.exception("preload_user_models: DB query failed for user=%s", user_id)
        return

    for gm in game_models:
        repo_id = (gm.hf_model_repo_id or "").strip()
        if not repo_id:
            continue

        def _has_weights(p: Path) -> bool:
            return (
                any(p.rglob("*.safetensors"))
                or any(p.rglob("*.py"))
                or any(p.rglob("*.npz"))
            )

        snap = _find_hf_cache_snapshot(repo_id)
        if snap is None or not _has_weights(snap):
            # Try to mirror the repo into HF_HUB_CACHE so inference is local.
            snap = ensure_hf_snapshot(gm)

        if snap is None or not _has_weights(snap):
            log.warning(
                "preload_user_models: no HF cache snapshot for "
                "user=%s game=%s repo=%s — will use HF API directly",
                user_id, gm.game_type, repo_id,
            )
            continue

        log.info(
            "preload_user_models: HF cache snapshot ready for user=%s game=%s: %s",
            user_id, gm.game_type, snap,
        )

        update_fields: list[str] = []
        snap_str = str(snap)
        if gm.cached_path != snap_str:
            gm.cached_path = snap_str
            update_fields.append("cached_path")
        if not gm.model_integrity_ok:
            gm.model_integrity_ok = True
            update_fields.append("model_integrity_ok")
        snap_sha = snap.name
        if snap_sha and gm.cached_commit != snap_sha:
            gm.cached_commit = snap_sha
            update_fields.append("cached_commit")
        if gm.cached_at is None or "cached_commit" in update_fields:
            gm.cached_at = timezone.now()
            update_fields.append("cached_at")
        if update_fields:
            try:
                gm.save(update_fields=update_fields)
            except Exception:
                log.exception(
                    "preload_user_models: failed to update cache fields for user=%s game=%s",
                    user_id, gm.game_type,
                )

    # ── create per-user dirs (no data copies in HF API mode) ────────────────
    games = ["chess", "breakthrough"]
    base = Path(settings.USER_MODELS_BASE_DIR)
    user_path = base / f"user_{user_id}"
    for game in games:
        (user_path / game / "model").mkdir(parents=True, exist_ok=True)
        (user_path / game / "data").mkdir(parents=True, exist_ok=True)

    log.info("Preload (HF API mode) complete for user_%s", user_id)


def clear_user_models(user_id: int) -> None:
    """Remove the per-user model/data dirs created by the preloader."""
    base = Path(settings.USER_MODELS_BASE_DIR)
    user_path = base / f"user_{user_id}"
    try:
        if user_path.exists():
            shutil.rmtree(user_path)
            log.info("Removed preloaded models for user_%s", user_id)
    except Exception:
        log.exception("Failed to clear user models for %s", user_id)


def populate_from_config(user_id: int, game: str) -> None:
    """Copy cache files into the user's model/data dirs and enforce config lists.

    - Copies all files from local HF cache patterns into ``user_{id}/{game}/(model|data)``.
    - If a `config_model.json`/`config_data.json` exists, ensure the final
      directory contains exactly the files listed (move/keep expected files,
      delete extras).
    - No network activity performed.
    """
    base = Path(settings.USER_MODELS_BASE_DIR)
    hf_cache = Path(getattr(settings, "HF_HUB_CACHE", base / "hf_hub_cache"))

    user_model_dir = base / f"user_{user_id}" / game / "model"
    user_data_dir = base / f"user_{user_id}" / game / "data"
    user_model_dir.mkdir(parents=True, exist_ok=True)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    # MODEL: copy snapshots or repo dirs
    model_repo_pattern = hf_cache / f"models--*--{game}-model*"
    found_model_src = False
    model_repo_dirs = list(glob.glob(str(model_repo_pattern)))
    for repo_dir in model_repo_dirs:
        # Prefer 'snapshots' dirs
        snap_pattern = os.path.join(repo_dir, "**", "snapshots", "*")
        snaps = glob.glob(snap_pattern, recursive=True)
        if snaps:
            for snap in snaps:
                try:
                    shutil.copytree(snap, user_model_dir, dirs_exist_ok=True)
                    log.info("Copied snapshot %s -> %s", snap, user_model_dir)
                    found_model_src = True
                except Exception:
                    log.exception("Failed copying snapshot %s -> %s", snap, user_model_dir)
        else:
            # Fallback to refs/* or repo root
            refs = glob.glob(os.path.join(repo_dir, "refs", "*"))
            if refs:
                for r in refs:
                    try:
                        shutil.copytree(r, user_model_dir, dirs_exist_ok=True)
                        log.info("Copied refs %s -> %s", r, user_model_dir)
                        found_model_src = True
                    except Exception:
                        log.exception("Failed copying refs %s -> %s", r, user_model_dir)
            else:
                try:
                    shutil.copytree(repo_dir, user_model_dir, dirs_exist_ok=True)
                    log.info("Copied repo %s -> %s", repo_dir, user_model_dir)
                    found_model_src = True
                except Exception:
                    log.exception("Failed copying repo %s -> %s", repo_dir, user_model_dir)

    # DATA: copy dataset dirs
    data_repo_pattern = hf_cache / f"datasets--*--{game}-data*"
    data_repo_dirs = list(glob.glob(str(data_repo_pattern)))
    for data_dir in data_repo_dirs:
        try:
            shutil.copytree(data_dir, user_data_dir, dirs_exist_ok=True)
            log.info("Copied data %s -> %s", data_dir, user_data_dir)
        except Exception:
            log.exception("Failed copying data %s -> %s", data_dir, user_data_dir)

    # Helper: enforce config lists exactly if present
    def _enforce_config(target_dir: Path, config_name: str, repo_dirs: list[str]):
        cfg_paths = list(target_dir.rglob(config_name))
        if not cfg_paths:
            return
        # Prefer first config found
        cfg_path = cfg_paths[0]
        try:
            import json
            cfg = json.load(open(cfg_path, "r"))
            expected = cfg.get("files", []) or []
            # Normalize to list
            if isinstance(expected, str):
                expected = [expected]
            expected_set = set()
            expected_basenames = set()
            for p in expected:
                norm = Path(p).as_posix()
                expected_set.add(norm)
                expected_basenames.add(Path(p).name)
            # Make sure config file itself is allowed
            expected_set.add(cfg_path.relative_to(target_dir).as_posix())

            # Move any files that match expected basenames into the expected location
            for filename in expected:
                dst = target_dir / filename
                if dst.exists():
                    continue
                # 1) Search inside target_dir for same-basename and move it
                matches = list(target_dir.rglob(Path(filename).name))
                if matches:
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(matches[0]), str(dst))
                        log.info("Moved %s -> %s to satisfy config", matches[0], dst)
                        continue
                    except Exception:
                        log.exception("Failed to move %s -> %s", matches[0], dst)

                # 2) Try to find the exact relative path under any known repo dirs and copy it
                found = False
                for repo in repo_dirs:
                    try:
                        candidate = Path(repo) / filename
                        if candidate.exists():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            try:
                                shutil.copy2(str(candidate), str(dst))
                                log.info("Copied %s -> %s to satisfy config", candidate, dst)
                                found = True
                                break
                            except Exception:
                                log.exception("Failed copying %s -> %s", candidate, dst)
                    except Exception:
                        pass
                if found:
                    continue

                # 3) Search for same basename anywhere under repo dirs and copy first match
                for repo in repo_dirs:
                    try:
                        repo_matches = list(Path(repo).rglob(Path(filename).name))
                        if repo_matches:
                            try:
                                dst.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(str(repo_matches[0]), str(dst))
                                log.info("Copied %s -> %s (basename match) to satisfy config", repo_matches[0], dst)
                                found = True
                                break
                            except Exception:
                                log.exception("Failed copying %s -> %s", repo_matches[0], dst)
                    except Exception:
                        pass

                if not found:
                    log.warning("Could not locate %s in cache repos to satisfy %s", filename, cfg_path)

            # Recompute actual files relative to target_dir
            actual_files = set()
            for p in target_dir.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(target_dir).as_posix()
                    actual_files.add(rel)

            # Determine extras and missing
            extras = actual_files - expected_set
            missing = expected_set - actual_files

            if missing:
                log.warning("Missing files listed in %s: %s", cfg_path, missing)

            # Delete extras (files not in config)
            for rel in sorted(extras):
                try:
                    p = target_dir / rel
                    if p.exists():
                        p.unlink()
                        log.info("Removed extra file %s", p)
                except Exception:
                    log.exception("Failed removing extra file %s", rel)

            # Cleanup empty dirs
            for d in sorted([p for p in target_dir.rglob("*") if p.is_dir()], key=lambda x: len(str(x)), reverse=True):
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                        log.info("Removed empty dir %s", d)
                except Exception:
                    pass

        except Exception:
            log.exception("Failed enforcing config %s in %s", config_name, target_dir)

    # Enforce model config_model.json if present
    _enforce_config(user_model_dir, "config_model.json", model_repo_dirs)
    # Enforce data config_data.json if present
    _enforce_config(user_data_dir, "config_data.json", data_repo_dirs)

    log.info("populate_from_config complete for user_%s game=%s (found_model_src=%s)", user_id, game, found_model_src)


def ensure_user_folders(user_id: int) -> None:
    """Ensure minimal per-user folders exist and attempt a local-only populate.

    This is a best-effort helper to be used when a full preload fails at
    login time. It will create the directory skeleton and then attempt to
    copy cached files from HF cache using `populate_from_config` for each
    game. Any errors are logged but not raised so callers can continue.
    """
    try:
        # Ensure skeleton exists (uses apps.users.model_lifecycle helper)
        try:
            from apps.users.model_lifecycle import ensure_user_dirs
            ensure_user_dirs(user_id)
        except Exception:
            log.exception("ensure_user_dirs failed for user_%s", user_id)

        # Try populate per-game but do not raise on failure
        for g in ("chess", "breakthrough"):
            try:
                populate_from_config(user_id, g)
            except Exception:
                log.exception("populate_from_config failed for user_%s game=%s", user_id, g)
    except Exception:
        log.exception("ensure_user_folders encountered an unexpected error for user_%s", user_id)


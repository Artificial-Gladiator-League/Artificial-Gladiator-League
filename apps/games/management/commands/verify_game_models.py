"""Management command: verify_game_models

Generic companion to verify_chess_models that covers EVERY game type
supported by UserGameModel.GameType.

For each user that has an HF identity (hf_username, or falls back to
Django username) and for every game type, the command:

  1. Derives the model/data repo IDs from a simple convention:
         hf_model_repo_id  = "{hf_username}/{game_type}-model"
         hf_data_repo_id   = "{hf_username}/{game_type}-data"
  2. Creates or updates the UserGameModel row (get_or_create + update).
  3. Probes the Gradio Space endpoint generically and persists
         hf_inference_endpoint_status = 'ready' | 'failed'.

No per-repo, per-user, or per-game-type constants are hardcoded.
All mapping is derived at runtime from the DB and the naming convention.

Usage:
    python manage.py verify_game_models
    python manage.py verify_game_models --user 56
    python manage.py verify_game_models --game-type breakthrough
    python manage.py verify_game_models --dry-run
    python manage.py verify_game_models --force     # re-probe already-ready rows
    python manage.py verify_game_models --populate-only  # skip probing
"""
from __future__ import annotations

import json
import logging

import requests
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.users.models import CustomUser, UserGameModel

log = logging.getLogger(__name__)

_TIMEOUT = 20  # seconds per HTTP call

# ---------------------------------------------------------------------------
# Per-game probe configuration
# ---------------------------------------------------------------------------
# Each entry maps a game_type value to a dict with:
#   fn        – Gradio function name on the Space
#   probe_input – the input payload sent to the Space
#   validate  – callable(str) -> bool; returns True if the move/output is valid
# ---------------------------------------------------------------------------

def _noop_validate(output: str) -> bool:  # noqa: ANN001
    """Accept any non-empty string response."""
    return bool(output and output.strip())


def _chess_validate(output: str) -> bool:
    """Accept only legal UCI moves for the starting position."""
    try:
        import chess  # type: ignore
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        board = chess.Board(fen)
        move = chess.Move.from_uci(output.strip())
        return move in board.legal_moves
    except Exception:
        return False


def _build_probe_config() -> dict[str, dict]:
    """Return per-game probe config derived purely from game-type metadata."""
    return {
        "chess": {
            "fn": "get_move",
            "probe_input": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "validate": _chess_validate,
        },
        "breakthrough": {
            "fn": "get_move",
            # 8×8 empty breakthrough start board representation
            "probe_input": "WWWWWWWW/WWWWWWWW/8/8/8/8/BBBBBBBB/BBBBBBBB w",
            "validate": _noop_validate,
        },
    }


# ---------------------------------------------------------------------------
# Space URL derivation (no per-repo constants)
# ---------------------------------------------------------------------------

def _space_base_url(ugm: UserGameModel) -> str:
    """Derive the Gradio Space base URL for *ugm* without any hardcoded map.

    Priority:
      1. An already-stored endpoint URL on the record.
      2. Generic convention: ``{owner}-{owner}.hf.space`` where *owner* is the
         HF account that owns the model repo.
    """
    if ugm.hf_inference_endpoint_url:
        return ugm.hf_inference_endpoint_url.rstrip("/")
    owner = (ugm.hf_model_repo_id or "").split("/")[0]
    if not owner:
        return ""
    return f"https://{owner}-{owner}.hf.space"


# ---------------------------------------------------------------------------
# Generic Gradio 4 probe
# ---------------------------------------------------------------------------

def _probe_space(base_url: str, fn: str, probe_input: str, validate) -> tuple[bool, str]:
    """Two-step Gradio 4 probe: POST submit → GET SSE result.

    Returns ``(success, message)``.
    """
    submit_url = f"{base_url}/gradio_api/call/{fn}"
    try:
        resp = requests.post(
            submit_url,
            json={"data": [probe_input]},
            headers={"Content-Type": "application/json"},
            timeout=_TIMEOUT,
        )
    except requests.exceptions.ConnectionError as exc:
        return False, f"connection error: {exc}"
    except requests.exceptions.Timeout:
        return False, f"submit timed out after {_TIMEOUT}s"
    except requests.exceptions.RequestException as exc:
        return False, f"submit failed: {exc}"

    if resp.status_code != 200:
        return False, f"submit HTTP {resp.status_code}: {resp.text[:200]}"

    event_id = resp.json().get("event_id")
    if not event_id:
        return False, f"no event_id in submit response: {resp.text[:200]}"

    result_url = f"{base_url}/gradio_api/call/{fn}/{event_id}"
    try:
        stream = requests.get(result_url, stream=True, timeout=_TIMEOUT)
        stream.raise_for_status()
    except requests.exceptions.RequestException as exc:
        return False, f"SSE stream failed: {exc}"

    output: str | None = None
    error_seen = False
    complete_seen = False
    for raw_line in stream.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if line.startswith("event: error"):
            error_seen = True
        elif line.startswith("event: complete"):
            complete_seen = True
        elif line.startswith("data:"):
            payload = line[len("data:"):].strip()
            try:
                data = json.loads(payload)
                if isinstance(data, list) and data:
                    output = str(data[0]).strip()
            except Exception:
                pass
            if complete_seen or error_seen:
                break

    if error_seen:
        return False, f"Space returned error event (output so far: {output!r})"
    if not output:
        return False, "SSE stream contained no data"
    if not validate(output):
        return False, f"invalid output: {output!r}"
    return True, f"output={output!r}"


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = (
        "Auto-populate UserGameModel rows for all users × all game types and "
        "optionally probe each Gradio Space to update hf_inference_endpoint_status."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            type=int,
            metavar="USER_ID",
            help="Restrict to a single user by primary key.",
        )
        parser.add_argument(
            "--game-type",
            type=str,
            choices=[gt for gt, _ in UserGameModel.GameType.choices],
            metavar="GAME_TYPE",
            help="Restrict to a single game type.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would happen without making any DB writes or HTTP calls.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-probe rows that are already marked 'ready'.",
        )
        parser.add_argument(
            "--populate-only",
            action="store_true",
            help="Only create/update DB rows; skip the Gradio Space probe.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        force: bool = options["force"]
        populate_only: bool = options["populate_only"]
        probe_config = _build_probe_config()

        # ── 1. Determine which game types to process ────────────────────────
        if options.get("game_type"):
            game_types = [options["game_type"]]
        else:
            game_types = [gt for gt, _ in UserGameModel.GameType.choices]

        # ── 2. Determine which users to process ────────────────────────────
        user_qs = CustomUser.objects.all()
        if options.get("user"):
            user_qs = user_qs.filter(pk=options["user"])

        users = list(user_qs.order_by("pk"))
        if not users:
            self.stdout.write(self.style.WARNING("No users found."))
            return

        self.stdout.write(
            f"Processing {len(users)} user(s) × {len(game_types)} game type(s)"
            + (" [dry-run]" if dry_run else "")
            + "\n"
        )

        total_populated = total_probed = total_ok = total_failed = 0

        for user in users:
            # Derive the HF namespace: prefer the OAuth username, fall back to
            # the Django username (keeps the mapping completely dynamic).
            hf_ns = user.hf_username.strip() if user.hf_username else user.username

            for game_type in game_types:
                model_repo = f"{hf_ns}/{game_type}-model"
                data_repo = f"{hf_ns}/{game_type}-data"

                self.stdout.write(
                    f"\n  user={user.username!r} (pk={user.pk}) "
                    f"game={game_type!r}\n"
                    f"    model_repo={model_repo!r}  data_repo={data_repo!r}"
                )

                if dry_run:
                    self.stdout.write("    [dry-run — skipped]\n")
                    continue

                # ── 3. Populate / update the UserGameModel row ──────────────
                ugm, created = UserGameModel.objects.get_or_create(
                    user=user,
                    game_type=game_type,
                    defaults={
                        "hf_model_repo_id": model_repo,
                        "hf_data_repo_id": data_repo,
                    },
                )
                if not created:
                    # Only overwrite repo fields that are still empty so we do
                    # not clobber manually curated values.
                    update_fields: list[str] = []
                    if not ugm.hf_model_repo_id:
                        ugm.hf_model_repo_id = model_repo
                        update_fields.append("hf_model_repo_id")
                    if not ugm.hf_data_repo_id:
                        ugm.hf_data_repo_id = data_repo
                        update_fields.append("hf_data_repo_id")
                    if update_fields:
                        ugm.save(update_fields=update_fields)

                total_populated += 1
                action = "created" if created else "exists"
                self.stdout.write(f"    [{action}] pk={ugm.pk}")

                if populate_only:
                    continue

                # ── 4. Probe the Gradio Space ───────────────────────────────
                if ugm.hf_inference_endpoint_status == "ready" and not force:
                    self.stdout.write("    [skip probe] already ready")
                    continue

                if not ugm.hf_model_repo_id:
                    self.stdout.write("    [skip probe] no model repo set")
                    continue

                base_url = _space_base_url(ugm)
                if not base_url:
                    self.stdout.write("    [skip probe] cannot derive Space URL")
                    continue

                cfg = probe_config.get(game_type)
                if cfg is None:
                    self.stdout.write(
                        f"    [skip probe] no probe config for game_type={game_type!r}"
                    )
                    continue

                submit_url = f"{base_url}/gradio_api/call/{cfg['fn']}"
                self.stdout.write(f"    → POST {submit_url}")

                total_probed += 1
                success, msg = _probe_space(
                    base_url, cfg["fn"], cfg["probe_input"], cfg["validate"]
                )
                new_status = "ready" if success else "failed"

                if success:
                    total_ok += 1
                    self.stdout.write(self.style.SUCCESS(f"    ✓ {msg}"))
                else:
                    total_failed += 1
                    self.stdout.write(self.style.ERROR(f"    ✗ {msg}"))

                update_fields = {"hf_inference_endpoint_status": new_status}
                if success and not ugm.hf_inference_endpoint_url:
                    update_fields["hf_inference_endpoint_url"] = base_url

                with transaction.atomic():
                    UserGameModel.objects.filter(pk=ugm.pk).update(**update_fields)

                self.stdout.write(
                    f"    saved: status={new_status!r}"
                    + (
                        f"  endpoint_url={base_url!r}"
                        if "hf_inference_endpoint_url" in update_fields
                        else ""
                    )
                )

        # ── Summary ─────────────────────────────────────────────────────────
        if not dry_run:
            parts = [f"{total_populated} row(s) populated"]
            if not populate_only:
                parts.append(
                    f"{total_probed} probed — {total_ok} ready, {total_failed} failed"
                )
            self.stdout.write(self.style.SUCCESS("\nDone — " + "; ".join(parts) + "."))

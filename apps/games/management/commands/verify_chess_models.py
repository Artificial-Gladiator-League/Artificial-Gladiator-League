"""Management command: verify_chess_models

Tests every chess UserGameModel by:
  1. POSTing to the Gradio 4 Space (/gradio_api/call/get_move) with the FEN.
  2. Reading the SSE result stream.
  3. Validating the returned move is a legal UCI string.
  4. Persisting hf_inference_endpoint_status = 'ready' | 'failed'.
  5. Storing the working Space base URL in hf_inference_endpoint_url.

Usage:
    python manage.py verify_chess_models
    python manage.py verify_chess_models --user 56
    python manage.py verify_chess_models --dry-run
    python manage.py verify_chess_models --force   # re-test already-ready entries
"""
from __future__ import annotations

import json
import logging

import chess
import requests
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.users.models import UserGameModel

log = logging.getLogger(__name__)

_PROBE_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
_GRADIO_FN = "get_move"
_TIMEOUT = 20  # seconds

# Known Space base URLs — mirrors predict_chess._KNOWN_SPACE_URLS
_KNOWN_SPACE_URLS: dict[str, str] = {
    "typical-cyber/chess-model": "https://typical-cyber-typical-cyber.hf.space",
    "test1978/chess-model":      "https://typical-cyber-typical-cyber.hf.space",
}


def _space_base_url(ugm: UserGameModel) -> str:
    """Return the Gradio Space base URL for this UGM record."""
    if ugm.hf_inference_endpoint_url:
        return ugm.hf_inference_endpoint_url.rstrip("/")
    if ugm.hf_model_repo_id in _KNOWN_SPACE_URLS:
        return _KNOWN_SPACE_URLS[ugm.hf_model_repo_id]
    # generic fallback: {hf_owner}-{hf_owner}.hf.space
    owner = ugm.hf_model_repo_id.split("/")[0] if ugm.hf_model_repo_id else ""
    return f"https://{owner}-{owner}.hf.space"


def _is_legal_uci(move_str: str) -> bool:
    try:
        board = chess.Board(_PROBE_FEN)
        move = chess.Move.from_uci(move_str.strip())
        return move in board.legal_moves
    except Exception:
        return False


def _probe_space(base_url: str) -> tuple[bool, str]:
    """Two-step Gradio 4 probe: POST submit → GET SSE result.

    Returns ``(success, message)``.
    """
    submit_url = f"{base_url}/gradio_api/call/{_GRADIO_FN}"
    try:
        resp = requests.post(
            submit_url,
            json={"data": [_PROBE_FEN]},
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

    result_url = f"{base_url}/gradio_api/call/{_GRADIO_FN}/{event_id}"
    try:
        stream = requests.get(result_url, stream=True, timeout=_TIMEOUT)
        stream.raise_for_status()
    except requests.exceptions.RequestException as exc:
        return False, f"SSE stream failed: {exc}"

    move_str: str | None = None
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
                    move_str = str(data[0]).strip()
            except Exception:
                pass
            # data line follows its event line — stop after reading it
            if complete_seen or error_seen:
                break

    if error_seen:
        return False, f"Space returned error event (move so far: {move_str!r})"
    if not move_str:
        return False, "SSE stream contained no data"
    if not _is_legal_uci(move_str):
        return False, f"illegal/non-UCI move: {move_str!r}"

    return True, f"move={move_str}"


class Command(BaseCommand):
    help = (
        "Probe each chess UserGameModel's Gradio Space and persist "
        "hf_inference_endpoint_status = 'ready' | 'failed'."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-test even models already marked 'ready'.",
        )
        parser.add_argument(
            "--user",
            type=int,
            help="Only verify models belonging to this user ID.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be tested without making any HTTP calls or DB writes.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        force: bool = options["force"]

        qs = (
            UserGameModel.objects
            .filter(game_type="chess")
            .exclude(hf_model_repo_id="")
            .select_related("user")
        )
        if options.get("user"):
            qs = qs.filter(user_id=options["user"])
        if not force:
            qs = qs.exclude(hf_inference_endpoint_status="ready")

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                "Nothing to verify — all models already ready (use --force to re-test)."
            ))
            return

        self.stdout.write(f"Found {total} chess model(s) to verify{' (dry-run)' if dry_run else ''}.\n")

        ok = failed = 0

        for ugm in qs.order_by("user_id"):
            base_url = _space_base_url(ugm)
            submit_url = f"{base_url}/gradio_api/call/{_GRADIO_FN}"
            self.stdout.write(
                f"  #{ugm.pk} user={ugm.user.username} repo={ugm.hf_model_repo_id}\n"
                f"    endpoint_name={ugm.hf_inference_endpoint_name!r} "
                f"status_before={ugm.hf_inference_endpoint_status!r}\n"
                f"    → POST {submit_url}"
            )

            if dry_run:
                self.stdout.write("    [dry-run — skipped]\n")
                continue

            success, msg = _probe_space(base_url)
            new_status = "ready" if success else "failed"

            if success:
                ok += 1
                self.stdout.write(self.style.SUCCESS(f"    ✓ {msg}"))
            else:
                failed += 1
                self.stdout.write(self.style.ERROR(f"    ✗ {msg}"))

            # Persist status and store the working Space URL so predict_chess
            # can use it without re-deriving it each time.
            update_fields: dict[str, str] = {"hf_inference_endpoint_status": new_status}
            if success and not ugm.hf_inference_endpoint_url:
                update_fields["hf_inference_endpoint_url"] = base_url

            with transaction.atomic():
                UserGameModel.objects.filter(pk=ugm.pk).update(**update_fields)

            self.stdout.write(
                f"    saved: status={new_status!r}"
                + (f"  endpoint_url={base_url!r}" if "hf_inference_endpoint_url" in update_fields else "")
                + "\n"
            )

        if not dry_run:
            self.stdout.write(self.style.SUCCESS(f"\nDone — {ok} ready, {failed} failed."))


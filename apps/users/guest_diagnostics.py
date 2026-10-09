"""Read-only diagnostics for guest accounts (public repos, no gated/AGL_VERIFY steps).

Reuses the output plumbing of apps.users.diagnostics without modifying it.
"""
from __future__ import annotations

import logging

from celery.exceptions import SoftTimeLimitExceeded
from django.utils import timezone

from . import diagnostics as d
from .guest_snapshot import GuestRepoError, check_limits, fetch_listing
from .models import DiagnosticRun, UserGameModel

log = logging.getLogger(__name__)


def _step_public_access(L, F, gm) -> None:
    d._step(L, 2, "Public repo access")
    try:
        listing = fetch_listing(gm.hf_model_repo_id, "model")
        check_limits(listing, gm.hf_model_repo_id)
    except GuestRepoError as exc:
        message = str(exc)
        if "Could not reach Hugging Face" in message:
            L.warn(message)
            F.platform_issue = True
            return
        L.fail(message)
        F.add(message, "Make the model repo public and keep it within the guest size limits.")
        return
    mb = listing.total_bytes / (1024 * 1024)
    L.ok(f"Model repo '{gm.hf_model_repo_id}' is public ({len(listing.files)} files, {mb:.1f} MB).")


def _step_namespace(L, F, user, gm) -> None:
    d._step(L, 3, "Ownership proof")
    owner = (user.guest_hf_username or "").lower()
    repos = [("model", gm.hf_model_repo_id)] + ([("data", gm.hf_data_repo_id)] if (gm.hf_data_repo_id or "").strip() else [])
    for kind, repo in repos:
        if repo.split("/", 1)[0].lower() == owner:
            L.ok(f"The {kind} repo is under your Hugging Face account '{user.guest_hf_username}'.")
        else:
            L.fail(f"The {kind} repo '{repo}' is not under your Hugging Face account.")
            F.add(f"The {kind} repo is not under your own account.", "Use repos under your own Hugging Face username.")


def _step_data_repo(L, F, gm) -> None:
    d._step(L, 4, "Data repo")
    repo = (gm.hf_data_repo_id or "").strip()
    if not repo:
        L.info("No data repo is connected.")
        return
    try:
        listing = fetch_listing(repo, "dataset")
        check_limits(listing, repo)
    except GuestRepoError as exc:
        message = str(exc)
        if "Could not reach Hugging Face" in message:
            L.warn(message)
            F.platform_issue = True
            return
        L.fail(message)
        F.add(message, "Make the data repo public and keep it within the guest size limits.")
        return
    L.ok(f"Data repo '{repo}' is public ({len(listing.files)} files).")


def _step_version(L, F, gm) -> None:
    d._step(L, 5, "Pinned version")
    L.info(f"Pinned model commit: {d._short(gm.approved_full_sha)}")
    if (gm.hf_data_repo_id or "").strip():
        L.info(f"Pinned data commit: {d._short(gm.approved_data_repo_sha)}")
    L.info("Guest games are unrated, so there is no rated-game cooldown.")


def _step_status(L, F, gm) -> None:
    d._step(L, 6, "Stored model check")
    if gm.status == UserGameModel.ContractStatus.ACTIVE:
        L.ok("Stored status: active")
    elif gm.status == UserGameModel.ContractStatus.PENDING:
        L.warn("Stored status: pending (Preparing your model...)")
        F.add(
            "Your model check has not finished yet, so the model cannot be used in games.",
            "Wait for the check to finish on the Gladiator tab, or save your repos again to restart it.",
        )
    else:
        L.fail("Stored status: failed")
        F.add("The stored model check failed.", "Fix the problems listed here, then save your repos again.")
    if gm.last_error:
        L.warn(f"Last stored error: {gm.last_error}")


def _step_live_test(L, F, gm) -> None:
    d._step(L, 7, "Live contract test")
    from apps.games.exceptions import SandboxUnavailableError
    from apps.games.model_check import check_model_verbose

    model_dir, data_dir = d._resolve_dirs_readonly(gm)
    if model_dir is None:
        L.warn("The live test could not run because your pinned files are not on the platform right now (Preparing your model...).")
        F.add(
            "The live test could not run because your pinned files are not downloaded yet.",
            "Open the Gladiator tab and wait until the model is prepared, then run the diagnostics again.",
        )
        return
    L.info("Running the contract test in the secure guest sandbox...")
    try:
        result = check_model_verbose(model_dir, data_dir, gm.game_type, profile="guest")
    except SandboxUnavailableError:
        L.warn("The platform sandbox is temporarily unavailable. This is not a problem with your model.")
        F.sandbox_unavailable = True
        return
    moves = list(result.get("moves") or [])
    problems = list(result.get("problems") or [])
    for move in moves:
        d._log_move(L, move)
    for problem in problems:
        if moves and problem.startswith("sample position"):
            continue
        L.fail(problem)
        F.add(problem, d._fix_for_contract_problem(problem))
    for note in result.get("warnings") or []:
        L.warn(note)
    if result.get("stage") == "sandbox" and not problems and moves and all(m.get("legal") for m in moves):
        L.ok(f"Live contract test passed ({len(moves)}/{len(moves)} sample moves legal).")


def _execute(run: DiagnosticRun, L) -> str:
    user, game_type = run.user, run.game_type
    label = UserGameModel.GameType(game_type).label
    F = d._Findings()
    L.info(f"Diagnostics for {label} started. Read-only: nothing about your model is changed.")
    gm = d._step_registration(L, F, user, game_type, label)
    if gm is None:
        return d._conclude(L, F)
    _step_public_access(L, F, gm)
    _step_namespace(L, F, user, gm)
    _step_data_repo(L, F, gm)
    gm.refresh_from_db()
    _step_version(L, F, gm)
    _step_status(L, F, gm)
    _step_live_test(L, F, gm)
    return d._conclude(L, F)


def run_guest_diagnostics(run_id: int) -> None:
    run = DiagnosticRun.objects.select_related("user").filter(pk=run_id).first()
    if run is None or run.state != DiagnosticRun.State.QUEUED:
        return
    run.state = DiagnosticRun.State.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["state", "started_at"])
    run_log = d._RunLog(run)
    try:
        run.verdict = _execute(run, run_log)
        run.state = DiagnosticRun.State.FINISHED
        run.finished_at = timezone.now()
        run.save(update_fields=["state", "verdict", "finished_at"])
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        log.exception("Guest diagnostics run %s crashed", run_id)
        d._finish_with_error(run, run_log, d.UNEXPECTED_FAILURE_MESSAGE)
    finally:
        d._prune_old_runs(run.user_id, run.game_type)

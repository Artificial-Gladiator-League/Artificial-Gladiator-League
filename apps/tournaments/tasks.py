# ──────────────────────────────────────────────
# apps/tournaments/tasks.py
#
# Celery task stubs for automated tournament
# lifecycle management.  Wire these into your
# celery beat schedule in settings.py:
#
#   CELERY_BEAT_SCHEDULE = {
#       "schedule-tournaments-weekly": {
#           "task": "apps.tournaments.tasks.schedule_weekly_tournaments",
#           "schedule": crontab(day_of_week="monday", hour=0, minute=0),
#       },
#       "check-stale-tournaments": {
#           "task": "apps.tournaments.tasks.check_stale_tournaments",
#           "schedule": crontab(minute="*/5"),
#       },
#   }
# ──────────────────────────────────────────────
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Attempt Celery import; if unavailable, tasks are plain functions
# callable from management commands or cron scripts.
try:
    from celery import shared_task
except ImportError:
    def shared_task(func=None, **kwargs):
        """No-op decorator when Celery is not installed."""
        if func is not None:
            return func
        return lambda f: f


@shared_task
def schedule_weekly_tournaments() -> str:
    """Create the weekly slate of tournaments.

    Equivalent to:  python manage.py schedule_tournaments
    """
    from django.core.management import call_command
    call_command("schedule_tournaments")
    return "Weekly tournaments scheduled."


@shared_task
def check_stale_tournaments() -> str:
    """Auto-start FULL tournaments that haven't kicked off yet.

    Also detects tournaments past their start_time that are still OPEN
    and either starts them (if ≥ 2 players) or aborts them.
    """
    from django.utils import timezone

    from apps.tournaments.engine import start_tournament
    from apps.tournaments.models import Tournament

    now = timezone.now()
    started = []

    # Start any tournament marked FULL
    for t in Tournament.objects.filter(status=Tournament.Status.FULL):
        _integrity_gate_and_start(t, started)
        log.info("Auto-started FULL tournament: %s", t.name)

    # Handle OPEN tournaments past their start_time
    for t in Tournament.objects.filter(
        status=Tournament.Status.OPEN,
        start_time__lte=now,
    ):
        if t.participant_count >= 2:
            _integrity_gate_and_start(t, started)
            log.info("Auto-started overdue tournament: %s (%d players)", t.name, t.participant_count)
        else:
            t.status = Tournament.Status.COMPLETED
            t.save(update_fields=["status"])
            log.info("Aborted tournament with <2 players: %s", t.name)

    return f"Checked tournaments. Started: {started or 'none'}"


def _integrity_gate_and_start(tournament, started: list) -> None:
    """Run integrity checks then start *tournament*, appending its name to *started*."""
    from apps.tournaments.engine import start_tournament
    # Run synchronous integrity gate (fire-and-forget already happened in the task)
    try:
        result = run_pre_tournament_integrity_checks(tournament.pk)
        failed = result.get("failed", [])
        if failed:
            log.warning(
                "Tournament %s: %d participant(s) removed after integrity failure before start: %s",
                tournament.name, len(failed), [f["user_id"] for f in failed],
            )
    except Exception:
        log.exception("Integrity gate failed for tournament %s — starting anyway", tournament.name)
    start_tournament(tournament)
    started.append(tournament.name)


@shared_task
def run_gladiator_gauntlet(
    participants: int = 16,
    rounds: int = 5,
    time_control: str = "3+1",
) -> str:
    """Run a full Gladiator Gauntlet tournament.

    This is the Celery-friendly wrapper around the management command.
    Schedule it with Celery Beat to fire every Sunday at 20:00 UTC.
    """
    from django.core.management import call_command

    call_command(
        "run_gauntlet",
        participants=participants,
        rounds=rounds,
        time_control=time_control,
    )
    return "Gladiator Gauntlet completed."


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pre-tournament integrity gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@shared_task(bind=True, max_retries=0)
def run_pre_tournament_integrity_checks(self, tournament_id: int) -> dict:
    """Check that all registered participants still own their repos.

    For each non-QA participant:
    - If the repo's latest commit is newer than the registration snapshot,
      reset is_verified=False and remove the participant.

    Returns a dict with lists of passed/failed usernames.
    """
    from apps.tournaments.models import Tournament, TournamentParticipant
    from apps.users.models import UserGameModel
    from apps.users.ownership_verification import has_repo_changed_since_registration

    try:
        tournament = Tournament.objects.get(pk=tournament_id)
    except Tournament.DoesNotExist:
        log.warning("run_pre_tournament_integrity_checks: tournament %s not found", tournament_id)
        return {"tournament_id": tournament_id, "passed": [], "failed": []}

    if tournament.type == Tournament.Type.QA:
        return {"tournament_id": tournament_id, "passed": [], "failed": []}

    participants = list(
        TournamentParticipant.objects.filter(
            tournament=tournament,
        ).select_related("user")
    )

    passed: list[str] = []
    failed: list[dict] = []

    for p in participants:
        try:
            gm = UserGameModel.objects.get(
                user=p.user, game_type=tournament.game_type,
            )
        except UserGameModel.DoesNotExist:
            failed.append({"user_id": p.user_id, "reason": "no game model registered"})
            p.delete()
            continue

        if not gm.is_verified:
            failed.append({"user_id": p.user_id, "reason": "repo not verified"})
            p.delete()
            continue

        # ── Live HF SHA re-check before round start (read-only) ────────────
        # Uses _resolve_ref_sha directly so model_integrity_ok is never
        # poisoned by a pre-start removal — that flag is reserved for
        # mid-game disqualifications only.
        try:
            from apps.users.integrity import _resolve_ref_sha, _get_stored_token
            _token = _get_stored_token(p.user) or ""
            _ref = (gm.submitted_ref or "main").strip() or "main"
            _repo_type = (gm.submission_repo_type or "model").strip() or "model"
            _db_sha = (
                gm.approved_full_sha
                or gm.original_model_commit_sha
                or gm.last_known_commit_id
                or ""
            ).strip() or None
            _live_sha = _resolve_ref_sha(
                gm.hf_model_repo_id, _token, ref=_ref, repo_type=_repo_type,
            )
            sha_ok = (_live_sha is None) or (not _db_sha) or (_live_sha == _db_sha)
            db_sha = _db_sha
            latest_sha = _live_sha
        except Exception:
            log.exception(
                "Pre-round SHA fetch failed for user=%s tournament=%s — skipping check",
                p.user.username, tournament.name,
            )
            sha_ok = True  # fail open: don't remove on network error
            db_sha = None
            latest_sha = None

        if not sha_ok:
            failed.append({
                "user_id": p.user_id,
                "reason": (
                    f"repo SHA changed before round start "
                    f"(approved={(db_sha or '')[:12]}, hf={(latest_sha or '')[:12]})"
                ),
            })
            p.delete()
            log.warning(
                "Pre-round SHA check: removed %s from tournament %s — repo SHA changed",
                p.user.username, tournament.name,
            )
            continue

        if has_repo_changed_since_registration(gm):
            gm.is_verified = False
            gm.save(update_fields=["is_verified"])
            failed.append({
                "user_id": p.user_id,
                "reason": "repo updated after tournament registration",
            })
            p.delete()
            log.warning(
                "Pre-round check: removed %s from tournament %s — repo updated after registration",
                p.user.username, tournament.name,
            )
        else:
            passed.append(p.user.username)

    if failed:
        log.warning(
            "Tournament %s: %d participant(s) removed by pre-round ownership check: %s",
            tournament.name, len(failed), [f["user_id"] for f in failed],
        )

    return {"tournament_id": tournament_id, "passed": passed, "failed": failed}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pre-tournament (registration-period) SHA poll
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@shared_task(bind=True, max_retries=0)
def run_registration_period_sha_audit(self) -> dict:
    """Continuous SHA check for participants in OPEN/FULL (not yet started) tournaments.

    Fires every 60s via Celery Beat. For every registered participant whose
    tournament has not yet gone ONGOING, hit the HF Hub and compare the live
    SHA against the approved baseline. A mismatch removes the participant from
    the tournament before it starts.

    Does not set disqualified_for_sha_mismatch — that flag is reserved for
    mid-game DQs. Removal here is a silent eligibility withdrawal.

    Add to CELERY_BEAT_SCHEDULE:
        "registration-period-sha-audit": {
            "task": "apps.tournaments.tasks.run_registration_period_sha_audit",
            "schedule": 60,  # seconds
        },
    """
    from django.utils import timezone

    from apps.tournaments.models import Tournament, TournamentParticipant
    from apps.users.integrity import live_sha_check
    from apps.users.models import UserGameModel

    summary = {"candidates": 0, "removed": 0, "errors": 0}

    # Only tournaments that are open for registration but not yet running.
    pre_start_statuses = [Tournament.Status.OPEN, Tournament.Status.FULL]
    candidates = list(
        TournamentParticipant.objects
        .filter(
            tournament__status__in=pre_start_statuses,
        )
        .select_related("user", "tournament")
    )
    summary["candidates"] = len(candidates)
    if not candidates:
        return summary

    for p in candidates:
        tournament = p.tournament
        try:
            gm = UserGameModel.objects.get(
                user=p.user, game_type=tournament.game_type,
            )
        except UserGameModel.DoesNotExist:
            # No model at all — remove immediately.
            log.warning(
                "run_registration_period_sha_audit: removing user=%s from "
                "tournament=%s — no UserGameModel found",
                p.user.username, tournament.pk,
            )
            p.delete()
            if tournament.status == Tournament.Status.FULL:
                tournament.status = Tournament.Status.OPEN
                tournament.save(update_fields=["status"])
            summary["removed"] += 1
            continue

        if not (gm.hf_model_repo_id or "").strip():
            continue  # no repo to check

        try:
            from apps.users.integrity import _resolve_ref_sha, _get_stored_token
            token = _get_stored_token(p.user) or ""
            ref = (gm.submitted_ref or "main").strip() or "main"
            repo_type = (gm.submission_repo_type or "model").strip() or "model"
            # Compare against the REGISTRATION baseline (pinned by
            # capture_round_baseline at join), so only repo changes made
            # *after* registration are flagged. Falling back to the
            # approved/known SHAs only when no registration baseline exists.
            db_sha = (
                (p.round_pinned_sha or "").strip()
                or (getattr(p, "registered_sha", "") or "").strip()
                or gm.approved_full_sha
                or gm.original_model_commit_sha
                or gm.last_known_commit_id
                or ""
            ).strip() or None
            live_sha = _resolve_ref_sha(
                gm.hf_model_repo_id, token, ref=ref, repo_type=repo_type,
            )
            sha_ok = (live_sha is None) or (not db_sha) or (live_sha == db_sha)
        except Exception:
            log.exception(
                "run_registration_period_sha_audit: SHA fetch raised "
                "for user=%s tournament=%s",
                p.user.username, tournament.pk,
            )
            summary["errors"] += 1
            continue

        if not sha_ok:
            log.warning(
                "run_registration_period_sha_audit: SHA CHANGED during "
                "registration period — disqualifying user=%s from tournament=%s "
                "(db_sha=%s live_sha=%s)",
                p.user.username, tournament.pk,
                (db_sha or "")[:12], (live_sha or "")[:12],
            )
            # Disqualify (keep the participant row so the waiting page can
            # redirect the offender to the disqualified screen). The actual
            # unregistration happens when they click "Return to Lobby" there.
            try:
                from apps.tournaments.disqualification import disqualify_for_repo_change
                disqualify_for_repo_change(
                    p,
                    reason=(
                        f"Repo SHA changed during registration period "
                        f"(approved={(db_sha or '')[:12]}, live={(live_sha or '')[:12]})"
                    ),
                    forfeit_live_match=False,  # tournament not started yet
                )
            except Exception:
                log.exception(
                    "run_registration_period_sha_audit: disqualify failed "
                    "for user=%s tournament=%s", p.user.username, tournament.pk,
                )
            summary["removed"] += 1

    log.info(
        "run_registration_period_sha_audit: candidates=%d removed=%d errors=%d",
        summary["candidates"], summary["removed"], summary["errors"],
    )
    return summary


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Probabilistic mid-round SHA audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ─────────────────────────────────────────────────────────────
# run_probabilistic_sha_audit — fires every 30s via Celery Beat
#
# Each tick evaluates ALL active tournament participants and assigns
# each a dynamic check probability based on:
#   - How recently they played a game (high risk window)
#   - How long since their last SHA check
#   - Tournament stage (finals = higher scrutiny)
#   - Prior anomaly history
#
# Multiple participants can be checked per tick. No fixed cycle.
# Unpredictable by design — cannot be gamed.
#
# Requires: celery -A agladiator beat -l info (real Redis broker)
# ─────────────────────────────────────────────────────────────

# Probability tuning constants (kept module-level so tests can patch).
_AUDIT_BASE_PROBABILITY = 0.25
_AUDIT_PROBABILITY_CAP = 0.95
_RECENT_GAME_WINDOW_SEC = 5 * 60
_VERY_RECENT_CHECK_SEC = 2 * 60
_RECENT_CHECK_SEC = 5 * 60
_STALE_CHECK_SEC = 10 * 60


def _score_check_probability(participant, tournament, now) -> float:
    """Compute the per-tick check probability for *participant*.

    See the docstring of :func:`run_probabilistic_sha_audit` for the
    full multiplier table. Pure function — no DB writes, only reads.
    """
    from datetime import timedelta

    from django.db.models import Q

    from apps.games.models import Game

    prob = _AUDIT_BASE_PROBABILITY

    # (a) Recently played a game inside this tournament.
    recent_cutoff = now - timedelta(seconds=_RECENT_GAME_WINDOW_SEC)
    user = participant.user
    played_recent = (
        Game.objects
        .filter(
            is_tournament_game=True,
            tournament_match__tournament=tournament,
        )
        .filter(Q(white=user) | Q(black=user))
        .filter(
            Q(last_move_at__gte=recent_cutoff)
            | Q(timestamp__gte=recent_cutoff),
        )
        .exists()
    )
    if played_recent:
        prob *= 3.0

    # (b) Time since last SHA check.
    last = participant.last_sha_check_at
    if last is None:
        prob *= 2.0
    else:
        age = (now - last).total_seconds()
        if age < _VERY_RECENT_CHECK_SEC:
            prob *= 0.1
        elif age > _STALE_CHECK_SEC:
            prob *= 1.5
        elif age > _RECENT_CHECK_SEC:
            prob *= 1.2

    # (c) Tournament stage: finals = highest scrutiny.
    rounds_total = getattr(tournament, "rounds_total", 0) or 0
    current_round = getattr(tournament, "current_round", 0) or 0
    if rounds_total and current_round == rounds_total:
        prob *= 1.5

    # (d) Prior anomaly history.
    if getattr(participant, "sha_anomaly_history", False):
        prob *= 2.0

    return min(prob, _AUDIT_PROBABILITY_CAP)


def _fetch_current_repo_sha(repo_id: str, token: str | None) -> str | None:
    """Hit HF Hub for the latest commit SHA on the ``main`` revision.

    Returns the full SHA string, or ``None`` on any failure (network,
    auth, missing repo, missing huggingface_hub package).
    """
    try:
        from huggingface_hub import HfApi
    except Exception:
        log.warning("huggingface_hub not installed — probabilistic audit cannot fetch SHAs")
        return None

    try:
        info = HfApi().repo_info(
            repo_id=repo_id,
            revision="main",
            token=token or None,
        )
    except Exception as exc:
        log.warning(
            "HF repo_info failed for repo=%s: %s",
            repo_id, exc,
        )
        return None

    sha = getattr(info, "sha", None)
    return sha.strip() if isinstance(sha, str) and sha.strip() else None


def _disqualify_for_sha_mismatch(
    participant,
    tournament,
    *,
    old_sha: str,
    new_sha: str,
) -> None:
    """Idempotently disqualify *participant* and forfeit their live game.

    Reuses ``apps.tournaments.engine._handle_mid_round_disqualification``
    so the post-save Game pipeline (ELO, bracket advancement, opponent
    walkover) runs exactly the same as everywhere else.
    """
    if participant.disqualified_for_sha_mismatch or participant.eliminated:
        return  # idempotent

    reason = (
        f"Probabilistic SHA audit mismatch: "
        f"baseline={old_sha[:12]}... live={new_sha[:12]}..."
    )

    log.warning(
        "SHA MISMATCH — user=%s tournament=%s old_sha=%s new_sha=%s — DISQUALIFIED",
        participant.user.username, tournament.pk, old_sha, new_sha,
    )

    try:
        from apps.tournaments.engine import _handle_mid_round_disqualification
        _handle_mid_round_disqualification(tournament, participant, reason)
    except Exception:
        log.exception(
            "Failed to forfeit live match for %s after SHA mismatch",
            participant.user.username,
        )

    participant.disqualified_for_sha_mismatch = True
    participant.disqualified_reason = reason
    participant.save(update_fields=[
        "disqualified_for_sha_mismatch", "disqualified_reason",
    ])


@shared_task(bind=True, max_retries=0)
def run_probabilistic_sha_audit(self) -> dict:
    """Probabilistic per-tick SHA audit.

    Fires every 30s via Celery Beat. Walks every active, non-disqualified
    tournament participant who has a pinned SHA, scores them with
    :func:`_score_check_probability`, and rolls a random Bernoulli to
    decide whether to perform the HF Hub round-trip on this tick.

    Multiple participants may be checked in a single tick; equally,
    every participant may be skipped. There is no fixed cycle.

    On a SHA mismatch the participant is disqualified and any live
    game in the current round is forfeited via the standard engine
    pipeline.
    """
    import random

    from django.conf import settings as dj_settings
    from django.utils import timezone

    from apps.tournaments.models import Tournament, TournamentParticipant
    from apps.users.integrity import _get_stored_token
    from apps.users.models import UserGameModel

    # ── Eager-mode guard ────────────────────────────────────
    if getattr(dj_settings, "CELERY_TASK_ALWAYS_EAGER", False):
        log.warning(
            "Probabilistic SHA audit skipped — CELERY_TASK_ALWAYS_EAGER=True. "
            "This task requires Celery Beat with a real Redis broker. "
            "Start with: celery -A agladiator beat -l info"
        )
        return {"skipped": "eager_mode"}

    now = timezone.now()
    summary = {
        "tournaments": 0,
        "candidates": 0,
        "rolled": 0,
        "checked": 0,
        "passed": 0,
        "disqualified": 0,
        "errors": 0,
    }

    ongoing_tournaments = Tournament.objects.filter(
        status=Tournament.Status.ONGOING,
    )
    summary["tournaments"] = ongoing_tournaments.count()
    if not summary["tournaments"]:
        return summary

    candidates = list(
        TournamentParticipant.objects
        .filter(
            tournament__in=ongoing_tournaments,
            eliminated=False,
            disqualified_for_sha_mismatch=False,
        )
        .select_related("user", "tournament")
    )
    summary["candidates"] = len(candidates)
    if not candidates:
        return summary

    # Delegate the actual SHA comparison + reaction to the canonical
    # ``perform_sha_check`` in ``sha_audit``. That function:
    #   • Falls back through round_pinned_sha → approved_full_sha →
    #     last_known_commit_id → original_model_commit_sha so the
    #     audit is never silently skipped just because one DB column
    #     happens to be empty.
    #   • Prints the loud "!! SHA MISMATCH DETECTED !!" terminal banner.
    #   • Calls _react_to_mismatch which DQs the participant, forfeits
    #     the live game, emails admins, AND broadcasts a WebSocket
    #     event with redirect_url=/games/lobby/ so the cheating user
    #     is bounced out of the tournament UI in real time.
    from apps.tournaments.sha_audit import perform_sha_check
    from apps.tournaments.models import TournamentShaCheck

    for participant in candidates:
        tournament = participant.tournament

        # Cheap pre-filter: must have a repo to check at all.
        try:
            gm = UserGameModel.objects.get(
                user=participant.user, game_type=tournament.game_type,
            )
        except UserGameModel.DoesNotExist:
            continue
        if not (gm.hf_model_repo_id or "").strip():
            continue

        prob = _score_check_probability(participant, tournament, now)
        if random.random() >= prob:
            continue
        summary["rolled"] += 1

        # Stamp last_sha_check_at up-front so the back-off multiplier
        # in _score_check_probability throttles repeated checks even
        # if the HF call later fails.
        participant.last_sha_check_at = now
        try:
            participant.save(update_fields=["last_sha_check_at"])
        except Exception:
            log.debug("Could not persist last_sha_check_at", exc_info=True)

        try:
            row = perform_sha_check(participant, context="random_audit")
        except Exception:
            log.exception(
                "Probabilistic audit: perform_sha_check raised for user=%s",
                participant.user.username,
            )
            summary["errors"] += 1
            continue

        if row is None:
            summary["errors"] += 1
            continue

        result = row.result
        if result == TournamentShaCheck.Result.PASS:
            summary["checked"] += 1
            summary["passed"] += 1
        elif result == TournamentShaCheck.Result.FAIL:
            summary["checked"] += 1
            summary["disqualified"] += 1
        else:
            # ERROR / SKIPPED — count as error for visibility.
            summary["errors"] += 1

    log.info(
        "run_probabilistic_sha_audit: tournaments=%d candidates=%d "
        "rolled=%d checked=%d passed=%d dq=%d errors=%d",
        summary["tournaments"], summary["candidates"], summary["rolled"],
        summary["checked"], summary["passed"], summary["disqualified"],
        summary["errors"],
    )
    return summary


# ── Global (always-on) SHA audit ────────────────────────────────────────────
#
# Covers every UserGameModel with a connected HF repo, not just active
# tournament participants. Uses modulo sharding to bound HF API call rate.
#
# Scale note: with _GLOBAL_AUDIT_SHARD_COUNT=60 and a 30s tick, each user
# shard is visited once every 1,800s (~30 min). At _AUDIT_BASE_PROBABILITY=0.25
# the expected check interval per user is ~2 hours — well within HF rate
# limits for any realistic user count. For 10,000+ users increase
# _GLOBAL_AUDIT_SHARD_COUNT proportionally (e.g. 200 → ~1 HF call/tick/200 users).

_GLOBAL_AUDIT_SHARD_COUNT = 60  # visit 1/N of the population per tick


def _score_global_probability(game_model, now) -> float:
    """Per-tick check probability for a UserGameModel outside a tournament.

    Same base probability and cap as the tournament audit; tournament-specific
    multipliers replaced with generic recency/staleness signals.
    """
    from datetime import timedelta
    from django.db.models import Q
    from apps.games.models import Game

    prob = _AUDIT_BASE_PROBABILITY

    # (a) Recently played any rated game — same window as the tournament audit.
    recent_cutoff = now - timedelta(seconds=_RECENT_GAME_WINDOW_SEC)
    played_recent = (
        Game.objects
        .filter(game_type=game_model.game_type)
        .filter(Q(white=game_model.user) | Q(black=game_model.user))
        .filter(
            Q(last_move_at__gte=recent_cutoff) | Q(timestamp__gte=recent_cutoff),
        )
        .exists()
    )
    if played_recent:
        prob *= 3.0

    # (b) Time since last validation (date-granularity field on UserGameModel).
    vdate = game_model.last_model_validation_date
    if vdate is None:
        prob *= 2.0          # never validated — prioritise
    elif vdate == now.date():
        prob *= 0.1          # already checked today — back off hard
    elif (now.date() - vdate).days >= 7:
        prob *= 1.5          # stale — boost

    return min(prob, _AUDIT_PROBABILITY_CAP)


@shared_task(bind=True, max_retries=0)
def run_global_sha_audit(self) -> dict:
    """Background SHA integrity check for every user with a connected HF repo.

    Fires every 30s (same cadence as run_probabilistic_sha_audit). Where the
    probabilistic audit covers only active tournament participants,  this task
    covers everyone — so a repo change is caught even when no tournament is
    running, and is_eligible_for_tournament() / can_join_tournament() will
    correctly block the user the next time they try to join.

    This task never disqualifies from a live match — it only flips
    model_integrity_ok to False (plus resets rated_games_since_revalidation
    to 0) via live_sha_check(). Disqualification during active matches is
    run_probabilistic_sha_audit's responsibility.

    Sharding: only 1/_GLOBAL_AUDIT_SHARD_COUNT of all UserGameModel rows are
    candidates per tick (determined by user_id % shard_count). This keeps the
    HF API call rate bounded regardless of total user count.
    """
    import random
    import time

    from django.conf import settings as dj_settings
    from django.utils import timezone

    if getattr(dj_settings, "CELERY_TASK_ALWAYS_EAGER", False):
        log.warning(
            "Global SHA audit skipped — CELERY_TASK_ALWAYS_EAGER=True. "
            "Requires Celery Beat with a real broker."
        )
        return {"skipped": "eager_mode"}

    from apps.tournaments.models import Tournament, TournamentParticipant
    from apps.users.integrity import live_sha_check
    from apps.users.models import UserGameModel

    now = timezone.now()
    shard_index = int(time.time() / 30) % _GLOBAL_AUDIT_SHARD_COUNT

    # One cheap query to find users already watched by run_probabilistic_sha_audit.
    tournament_user_ids = frozenset(
        TournamentParticipant.objects
        .filter(
            tournament__status=Tournament.Status.ONGOING,
            eliminated=False,
            disqualified_for_sha_mismatch=False,
        )
        .values_list("user_id", flat=True)
    )

    all_gms = list(
        UserGameModel.objects
        .filter(hf_model_repo_id__gt="")
        .select_related("user")
    )

    summary = {
        "shard": f"{shard_index}/{_GLOBAL_AUDIT_SHARD_COUNT}",
        "total_gms": len(all_gms),
        "candidates": 0,
        "rolled": 0,
        "checked": 0,
        "matched": 0,
        "mismatched": 0,
        "errors": 0,
    }

    for gm in all_gms:
        if gm.user_id % _GLOBAL_AUDIT_SHARD_COUNT != shard_index:
            continue
        # Already covered this tick by run_probabilistic_sha_audit.
        if gm.user_id in tournament_user_ids:
            continue

        summary["candidates"] += 1

        prob = _score_global_probability(gm, now)
        if random.random() >= prob:
            continue
        summary["rolled"] += 1

        try:
            matches, _db_sha, _latest_sha = live_sha_check(
                gm, context="global_daily", fail_open=True,
            )
        except Exception:
            log.exception(
                "run_global_sha_audit: live_sha_check raised for user=%s game=%s",
                getattr(gm.user, "username", "?"), gm.game_type,
            )
            summary["errors"] += 1
            continue

        summary["checked"] += 1
        if matches:
            summary["matched"] += 1
        else:
            summary["mismatched"] += 1
            log.warning(
                "run_global_sha_audit: SHA mismatch user=%s game=%s repo=%s "
                "— model_integrity_ok=False; blocked from next tournament join.",
                getattr(gm.user, "username", "?"), gm.game_type,
                gm.hf_model_repo_id,
            )

    log.info(
        "run_global_sha_audit: shard=%s total=%d candidates=%d "
        "rolled=%d checked=%d matched=%d mismatched=%d errors=%d",
        summary["shard"], summary["total_gms"], summary["candidates"],
        summary["rolled"], summary["checked"],
        summary["matched"], summary["mismatched"], summary["errors"],
    )
    return summary


@shared_task(bind=True, max_retries=0)
def run_sha_check_for_participant(self, participant_id: int) -> dict:
    """Manually trigger a single SHA check (used by admin actions)."""
    from apps.tournaments.models import TournamentParticipant
    from apps.tournaments.sha_audit import perform_sha_check

    try:
        p = TournamentParticipant.objects.select_related(
            "tournament", "user",
        ).get(pk=participant_id)
    except TournamentParticipant.DoesNotExist:
        return {"ok": False, "error": "participant not found"}

    row = perform_sha_check(p, context="manual")
    return {
        "ok": row is not None,
        "result": row.result if row else None,
        "check_id": row.pk if row else None,
    }


@shared_task(bind=True, max_retries=0)
def run_round_integrity_check(self, tournament_id: int, round_num: int) -> dict:
    """Per-round guaranteed integrity check.

    Scheduled by ``apps.tournaments.engine.generate_pairings`` at a
    randomised offset inside the round window so every round of every
    tournament gets at least one anti-cheat verification at an
    unpredictable time. The actual work is delegated to
    ``run_round_integrity_pass`` which dispatches one
    ``run_sha_check_for_participant`` task per active participant.
    """
    from apps.tournaments.sha_audit import run_round_integrity_pass

    summary = run_round_integrity_pass(tournament_id, round_num)
    log.info(
        "run_round_integrity_check: tournament=%s round=%d "
        "dispatched=%d checked=%d stale=%s",
        tournament_id, round_num,
        summary.get("dispatched", 0),
        summary.get("checked", 0),
        summary.get("skipped_stale", False),
    )
    return summary


@shared_task(bind=True, max_retries=0)
def ensure_tournament_integrity(self, tournament_id: int) -> dict:
    """Generic recovery task: fix lifecycle gaps + arm SHA enforcement.

    See :func:`apps.tournaments.lifecycle.ensure_tournament_integrity`
    for the full algorithm. This task is idempotent and safe to run
    repeatedly — useful for cron, admin-triggered recovery, or one-off
    operator debugging.
    """
    from apps.tournaments.lifecycle import ensure_tournament_integrity as _impl
    from apps.tournaments.models import Tournament

    try:
        t = Tournament.objects.get(pk=tournament_id)
    except Tournament.DoesNotExist:
        return {
            "tournament_id": tournament_id,
            "errors": ["tournament_not_found"],
            "actions": [],
        }
    report = _impl(t)
    log.info(
        "ensure_tournament_integrity: tournament=%s actions=%s",
        tournament_id, report.get("actions"),
    )
    return report


@shared_task
def expire_stale_prize_claims() -> str:
    """Set PrizeClaim.status=EXPIRED for unclaimed/uncollected claims past expires_at.

    Runs daily via Celery Beat. Also syncs Tournament.payout_status to CANCELLED
    and emails admins so no expired prize goes unnoticed.
    """
    from django.core.mail import mail_admins
    from django.utils import timezone

    from apps.tournaments.models import PrizeClaim, Tournament

    now = timezone.now()
    to_expire = list(
        PrizeClaim.objects
        .filter(
            status__in=[PrizeClaim.Status.PENDING, PrizeClaim.Status.CLAIMED],
            expires_at__lt=now,
        )
        .select_related("tournament", "winner")
    )

    if not to_expire:
        return "No claims to expire."

    pks = [c.pk for c in to_expire]
    PrizeClaim.objects.filter(pk__in=pks).update(status=PrizeClaim.Status.EXPIRED)

    for claim in to_expire:
        try:
            claim.tournament.payout_status = Tournament.PayoutStatus.CANCELLED
            claim.tournament.save(update_fields=["payout_status"])
        except Exception:
            log.exception(
                "Failed to update payout_status for tournament %s", claim.tournament_id
            )

    lines = "\n".join(
        f"  \u2022 {c.tournament.name} (pk={c.tournament_id}) \u2014 "
        f"winner: {c.winner.username}, was {c.status}, expired {c.expires_at:%Y-%m-%d}"
        for c in to_expire
    )
    try:
        mail_admins(
            subject=f"[AGL] {len(to_expire)} prize claim(s) expired",
            message=f"The following prize claims have been marked EXPIRED:\n\n{lines}",
            fail_silently=True,
        )
    except Exception:
        log.exception("Failed to send prize-expiry admin notification")

    log.warning("Expired %d prize claim(s):\n%s", len(to_expire), lines)
    return f"Expired {len(to_expire)} claim(s)."


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registration confirmation email with T&C PDF
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_registration_confirmation(self, tournament_id: int, user_id: int) -> dict:
    """Send a registration confirmation email with the T&C as a PDF attachment.

    Called immediately after a user successfully joins a money tournament.
    Retries up to 2 times on transient email failures (30s delay).

    Returns a dict with keys: ok (bool), error (str or None).
    """
    import io
    from django.contrib.auth import get_user_model
    from django.core.mail import EmailMessage
    from django.conf import settings as dj_settings
    from apps.tournaments.models import Tournament

    User = get_user_model()

    try:
        tournament = Tournament.objects.get(pk=tournament_id)
        user = User.objects.get(pk=user_id)
    except Exception as exc:
        log.error(
            "send_registration_confirmation: could not load tournament=%s user=%s: %s",
            tournament_id, user_id, exc,
        )
        return {"ok": False, "error": str(exc)}

    recipient = user.email
    if not recipient:
        log.warning(
            "send_registration_confirmation: user=%s has no email — skipping",
            user.username,
        )
        return {"ok": False, "error": "no email address"}

    # ── Build PDF ──────────────────────────────────────────────────
    try:
        pdf_bytes = _build_tc_pdf(tournament, user)
    except Exception:
        log.exception(
            "send_registration_confirmation: PDF generation failed for "
            "tournament=%s user=%s", tournament_id, user_id,
        )
        raise

    # ── Compose email ──────────────────────────────────────────────
    subject = f"Registration confirmed — {tournament.name}"
    body = (
        f"Hi {user.username},\n\n"
        f"You are now registered for {tournament.name}.\n\n"
        f"Tournament details:\n"
        f"  Name    : {tournament.name}\n"
    )
    if getattr(tournament, "start_time", None):
        body += f"  Starts  : {tournament.start_time.strftime('%Y-%m-%d %H:%M UTC')}\n"
    if getattr(tournament, "prize_amount", None):
        body += (
            f"  Prize   : {tournament.prize_amount} "
            f"{getattr(tournament, 'prize_currency', 'NIS')}\n"
        )
    body += (
        f"\n"
        f"The full Terms & Conditions are attached as a PDF for your records.\n\n"
        f"Good luck!\n"
        f"— Artificial Gladiator League"
    )

    try:
        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=dj_settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        )
        email.attach(
            filename=f"AGL_Terms_{tournament.pk}.pdf",
            content=pdf_bytes,
            mimetype="application/pdf",
        )
        email.send(fail_silently=False)
        log.info(
            "send_registration_confirmation: sent to user=%s tournament=%s",
            user.username, tournament_id,
        )
        return {"ok": True, "error": None}
    except Exception:
        log.exception(
            "send_registration_confirmation: email send failed for "
            "user=%s tournament=%s", user.username, tournament_id,
        )
        raise


def _build_tc_pdf(tournament, user) -> bytes:
    """Generate a PDF of the T&C for *tournament* and return raw bytes.

    Uses reportlab. The content mirrors the key points in money_terms.html.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
    from reportlab.lib import colors
    import io

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=f"AGL Terms & Conditions — {tournament.name}",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "AglTitle",
        parent=styles["Title"],
        fontSize=16,
        spaceAfter=12,
    )
    heading_style = ParagraphStyle(
        "AglHeading",
        parent=styles["Heading2"],
        fontSize=12,
        spaceBefore=10,
        spaceAfter=4,
    )
    body_style = styles["BodyText"]
    body_style.fontSize = 10
    body_style.leading = 14

    sub_style = ParagraphStyle(
        "AglSub", parent=body_style, leftIndent=16,
    )
    small_style = ParagraphStyle(
        "AglSmall", parent=body_style, fontSize=8, textColor=colors.grey,
    )

    story = []

    story.append(Paragraph("The Gladiator Gauntlet &mdash; Terms &amp; Conditions", title_style))
    story.append(Paragraph(f"Tournament: {tournament.name}", body_style))
    story.append(Spacer(1, 0.3 * cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 0.3 * cm))

    if getattr(tournament, "prize_amount", None):
        prize_line = (
            f"<b>Prize:</b> {tournament.prize_amount} "
            f"{getattr(tournament, 'prize_currency', 'NIS')} &mdash; "
            "awarded to the tournament champion, subject to the terms below."
        )
        story.append(Paragraph(prize_line, body_style))
        story.append(Spacer(1, 0.3 * cm))

    story.append(Paragraph(
        "AGL(TM), The Gladiator Gauntlet(TM), AG(TM), Gladiate(TM), Lets Gladiate(TM), "
        "Artificial Gladiator(TM), and Artificial Gladiator League(TM) are trademarks of PTK Group.",
        small_style,
    ))
    story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph(
        "<b>Organizer:</b> Artificial Gladiator League (AGL), Rishon LeZion, Israel",
        body_style,
    ))
    story.append(Spacer(1, 0.2 * cm))

    # Each block: ("h", heading) | ("p", body paragraph) | ("s", indented sub-clause)
    blocks = [
        ("h", "Key Points (Summary)"),
        ("p", "&bull; The Gladiator Gauntlet is a <b>weekly, Swiss-format AG tournament</b> "
              "(chess and/or breakthrough) &mdash; no elimination, everyone plays every round."),
        ("p", "&bull; <b>Eligibility:</b> you must be at least <b>18 years old</b> and a "
              "<b>resident of Israel</b> with a valid AGL account."),
        ("p", "&bull; <b>Prizes:</b> 1st place 100 NIS, 2nd place 50 NIS, 3rd place 25 NIS, "
              "paid in NIS, subject to tax and identity/payment information requirements."),
        ("p", "&bull; <b>Zero tolerance for cheating</b> &mdash; including changing your model's "
              "repository mid-tournament &mdash; results in immediate disqualification and "
              "forfeiture of prizes."),
        ("p", "&bull; AGL may cancel, postpone, or adjust the schedule with reasonable notice."),
        ("p", "&bull; Disputes are governed by <b>Israeli law</b>, under the exclusive "
              "jurisdiction of the <b>courts of Rishon LeZion</b>."),

        ("h", "1. Definitions"),
        ("p", "1.1 <b>\"AGL\" / \"we\"</b> means The Gladiator Gauntlet tournament operator, "
              "based in Rishon LeZion, Israel."),
        ("p", "1.2 <b>\"Participant\" / \"you\"</b> means any individual who registers for "
              "and/or competes in The Gladiator Gauntlet."),
        ("p", "1.3 <b>\"Tournament\"</b> means a single weekly instance of The Gladiator Gauntlet."),
        ("p", "1.4 <b>\"AG Champion\"</b> means The Artificial Gladiator champion, and its "
              "associated repository, that a participant submits to compete on their behalf."),
        ("p", "1.5 <b>\"Swiss System\"</b> means a multi-round tournament format in which no "
              "participant is eliminated; each round, participants are paired against others "
              "with a similar running score, and final standings are determined by cumulative "
              "score (and tiebreakers) after all rounds are complete."),
        ("p", "1.6 <b>\"Platform\"</b> means the AGL website and associated services through "
              "which the Tournament is operated."),

        ("h", "2. Eligibility &amp; Registration"),
        ("p", "2.1 To participate in The Gladiator Gauntlet, you must:"),
        ("s", "a. Be at least <b>18 years of age</b> at the time of registration;"),
        ("s", "b. Be a <b>resident of the State of Israel</b>;"),
        ("s", "c. Hold a valid, active AGL account in good standing; and"),
        ("s", "d. Agree to comply with these T&amp;C and all other applicable AGL platform "
              "terms and policies."),
        ("p", "2.2 AGL may request information reasonably necessary to verify your identity, "
              "age, or residency eligibility, and may suspend or deny entry pending such verification."),
        ("p", "2.3 AGL reserves the right, at its reasonable discretion, to refuse or revoke "
              "registration for any Participant who does not meet the eligibility criteria in "
              "this Section 2, or who has previously violated these T&amp;C."),
        ("p", "2.4 Registration for a given weekly Tournament is subject to the entry window, "
              "capacity limits, and any other requirements published on the Tournament page."),

        ("h", "3. Tournament Format &amp; Rules"),
        ("p", "3.1 <b>Format.</b> The Gladiator Gauntlet is run under the <b>Swiss system</b>. "
              "Participants are not eliminated after a loss; instead, each round they are paired "
              "against opponents with a comparable score, and final rankings are determined by "
              "total score across all rounds, with tiebreakers applied as needed to resolve ties."),
        ("p", "3.2 <b>Game type.</b> Matches are played by AG models submitted by Participants, "
              "in chess and/or breakthrough, as specified on the Tournament page for that week."),
        ("p", "3.3 <b>Time control.</b> Each match is played under the time control specified on "
              "the Tournament page for that week's Tournament."),
        ("p", "3.4 <b>Pairings and scoring.</b> Round pairings, scoring, and standings are "
              "generated and maintained by AGL's tournament system. AGL's determination of "
              "pairings, results, and final standings is final, save for manifest error or a "
              "successful dispute under Section 8."),
        ("p", "3.5 <b>Model conduct during matches.</b> Your AG Champion must compete as "
              "submitted at the time your Tournament registration is finalized. See Section 7 "
              "for restrictions on changes during an active Tournament."),
        ("p", "3.6 <b>Time per move.</b> Before joining a Tournament, the Participant selects a "
              "target \"time per move\" (AI thinking time) setting for their AG Champion, as "
              "offered on the registration page. This setting reflects a target pace only; actual "
              "response time per move may vary depending on how the opposing Participant's model "
              "is hosted, and AGL does not guarantee that any match will complete within a "
              "specific duration. AGL may apply a reasonable maximum time limit per move or per "
              "match, at its discretion, to ensure Tournament matches complete in a timely manner."),

        ("h", "4. Schedule &amp; Changes"),
        ("p", "4.1 The Gladiator Gauntlet is held <b>once per week</b>."),
        ("p", "4.2 AGL may adjust the day, time, or duration of a given week's Tournament, "
              "provided that reasonable notice is given to registered Participants."),
        ("p", "4.3 AGL reserves the right to cancel or postpone any Tournament due to technical "
              "issues, an insufficient number of participants, or any other reasonable operational "
              "cause. In such cases, AGL will make reasonable efforts to notify affected "
              "Participants and, where applicable, reschedule the Tournament or address any prize "
              "implications."),
        ("p", "4.4 AGL reserves the right, at its sole discretion, to increase the prize amounts "
              "specified in Section 5 for a given Tournament &mdash; for example, where the number "
              "of registered Participants is lower than expected &mdash; without any obligation to "
              "do so and without this establishing any expectation of increased prizes for future "
              "Tournaments."),

        ("h", "5. Prizes &amp; Payment"),
        ("p", "5.1 <b>Prize amounts</b> for each weekly Gladiator Gauntlet Tournament are as "
              "follows, unless otherwise stated on the Tournament page:"),
        ("s", "&bull; <b>1st place:</b> 100 NIS"),
        ("s", "&bull; <b>2nd place:</b> 50 NIS"),
        ("s", "&bull; <b>3rd place:</b> 25 NIS"),
        ("p", "5.2 Prizes are paid in New Israeli Shekels (NIS), via a payment method specified "
              "by AGL (which may include, for example, bank transfer or PayPal)."),
        ("p", "5.3 To receive a prize, a winning Participant may be required to provide additional "
              "information and to complete any identity or eligibility verification requested by "
              "AGL. This may include, without limitation, providing a PayPal email address, "
              "participating in a verification call via phone or WhatsApp, and/or providing a copy "
              "of the Participant's Israeli identity card (Teudat Zehut) together with its address "
              "appendix (Sefach), to confirm identity, age, and Israeli residency."),
        ("p", "5.4 Prizes are subject to any applicable taxes, withholdings, or other legal "
              "requirements under Israeli law. Each Participant is solely responsible for any tax "
              "obligations arising from a prize they receive."),
        ("p", "5.5 A Participant who is disqualified under Section 8, or who is later found "
              "ineligible under Section 2, forfeits any right to a prize for the relevant "
              "Tournament, and AGL may reallocate or withhold the prize accordingly."),
        ("p", "5.6 Unclaimed prizes may be forfeited if the winning Participant fails to provide "
              "required payment or verification information within a reasonable period specified "
              "by AGL."),

        ("h", "6. Code of Conduct &amp; Fair Play"),
        ("p", "6.1 All Participants must treat every other Participant, AGL staff, and any other "
              "person with respect at all times."),
        ("p", "6.2 Harassment, discrimination, hate speech, and abusive behavior of any kind are "
              "strictly prohibited, whether directed at another Participant, AGL, or any third party."),
        ("p", "6.3 There is no place on the Platform for violence, threats of violence, or "
              "incitement to violence of any kind."),
        ("p", "6.4 Cheating of any kind is strictly prohibited. Cheating includes, without "
              "limitation, the conduct described in Section 7."),
        ("p", "6.5 Violation of this Section 6 may result in disqualification, suspension, or "
              "termination of a Participant's account, in accordance with Section 8."),

        ("h", "7. Anti-Cheating &amp; Integrity"),
        ("p", "7.1 <b>Repository changes during a Tournament.</b> Changing your AG Champion's "
              "Model's repository, or the contents thereof, at any point during an active "
              "Tournament is considered cheating and will result in <b>immediate "
              "disqualification</b> from that Tournament."),
        ("p", "7.2 Without limiting Section 7.1, AGL may also disqualify a Participant for:"),
        ("s", "a. Manipulating a Model, its repository, or its inference endpoint during a Tournament;"),
        ("s", "b. Exploiting bugs, defects, or vulnerabilities in the Platform to gain an unfair "
              "advantage; or"),
        ("s", "c. Any other violation of this Section 7, Section 6 (Code of Conduct), or these "
              "T&amp;C generally."),
        ("p", "7.3 AGL may use automated or manual integrity checks to monitor compliance with "
              "this Section 7. Participants agree to cooperate with any reasonable request from "
              "AGL related to such checks."),
        ("p", "7.4 <b>Repository changes in proximity to a Tournament.</b> Changing, modifying, "
              "or replacing the Model's repository, or any contents thereof, during the period "
              "immediately preceding the scheduled start time of a Tournament for which the "
              "Participant is registered, shall likewise be deemed cheating for purposes of this "
              "Section 7, and shall result in the immediate disqualification of the Participant "
              "from that Tournament, irrespective of whether such change is detected prior to, "
              "during, or following the Tournament."),

        ("h", "8. Disqualification &amp; Sanctions"),
        ("p", "8.1 AGL may disqualify a Participant from a Tournament, at AGL's reasonable "
              "discretion, for any violation of Section 6 (Code of Conduct) or Section 7 "
              "(Anti-Cheating &amp; Integrity), or of these T&amp;C more generally."),
        ("p", "8.2 A disqualified Participant forfeits all prize eligibility for the Tournament "
              "in which the disqualification occurs."),
        ("p", "8.3 AGL may, in addition to disqualification from a specific Tournament, suspend "
              "or terminate a Participant's AGL account for repeated or serious violations."),
        ("p", "8.4 A Participant who believes they were disqualified or sanctioned in error may "
              "raise the matter with AGL through the contact channel in Section 13. AGL will "
              "review such disputes in good faith, but its determination following review shall "
              "be final."),

        ("h", "9. Data Protection &amp; Email Usage"),
        ("p", "9.1 AGL will collect and store your email address and will use it only for the "
              "following purposes:"),
        ("s", "a. Account registration and authentication on the AGL Platform;"),
        ("s", "b. Password reset and account recovery;"),
        ("s", "c. Communications related to your Tournament participation, including "
              "notifications and results; and"),
        ("s", "d. Processing and paying Tournament prizes."),
        ("p", "9.2 AGL will not sell Participants' email addresses to third parties."),
        ("p", "9.3 AGL may process other personal data reasonably necessary to operate the "
              "Platform and to comply with applicable law, including data protection law "
              "applicable in Israel."),
        ("p", "9.4 Participants may contact AGL using the details in Section 13 with questions "
              "or requests regarding their personal data."),

        ("h", "10. Limitation of Liability"),
        ("p", "10.1 The Platform and Tournament are provided on an \"as is\" and \"as available\" "
              "basis. AGL does not guarantee uninterrupted or error-free operation of the "
              "Platform or any Tournament."),
        ("p", "10.2 To the maximum extent permitted by applicable Israeli law, AGL shall not be "
              "liable for any indirect, incidental, or consequential damages arising from a "
              "Participant's use of the Platform or participation in a Tournament, including "
              "damages arising from technical failures, cancellations, or postponements under "
              "Section 4."),
        ("p", "10.3 Nothing in this Section 10 excludes or limits any liability that cannot "
              "lawfully be excluded or limited under applicable Israeli law."),

        ("h", "11. Modifications to These Terms"),
        ("p", "11.1 AGL may update or amend these T&amp;C from time to time. Material changes "
              "will be communicated to Participants by a reasonable method, such as posting an "
              "updated version on the Platform or notifying registered Participants by email."),
        ("p", "11.2 Continued participation in the Gladiator Gauntlet following the effective "
              "date of any updated T&amp;C constitutes acceptance of the updated terms."),

        ("h", "12. Governing Law &amp; Jurisdiction"),
        ("p", "12.1 These T&amp;C, and any dispute arising out of or in connection with the "
              "Gladiator Gauntlet or these T&amp;C, shall be governed by the laws of the State "
              "of Israel, without regard to its conflict-of-laws principles."),
        ("p", "12.2 The competent courts of Rishon LeZion, Israel, shall have exclusive "
              "jurisdiction over any such dispute."),
        ("p", "12.3 These T&amp;C are drafted in English. Should a Hebrew translation be "
              "provided for convenience, the English version shall prevail in the event of any "
              "conflict or inconsistency."),

        ("h", "13. Contact Information"),
        ("p", "For questions about these T&amp;C, the Gladiator Gauntlet, prizes, or a dispute "
              "regarding disqualification, please contact AGLadiator through the contact details "
              "published on the Platform."),
    ]

    for kind, text in blocks:
        if kind == "h":
            story.append(Paragraph(text, heading_style))
        elif kind == "s":
            story.append(Paragraph(text, sub_style))
        else:
            story.append(Paragraph(text, body_style))
        story.append(Spacer(1, 0.12 * cm))

    story.append(Spacer(1, 0.2 * cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph(
        f"Participant: {user.username} | Accepted at registration for {tournament.name}.",
        body_style,
    ))

    doc.build(story)
    return buf.getvalue()




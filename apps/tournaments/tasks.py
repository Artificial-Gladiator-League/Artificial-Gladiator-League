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
    from apps.users.integrity import live_sha_check

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

        # ── Live HF SHA re-check before round start ────────────
        # Hits the HF Hub API and prints a standard log line.
        sha_ok, db_sha, latest_sha = live_sha_check(gm, context="pre-round")
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


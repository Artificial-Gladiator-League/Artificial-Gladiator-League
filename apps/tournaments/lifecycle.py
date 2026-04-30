"""Generic tournament lifecycle / integrity-recovery manager.

Production data drifts: tournaments end up with status='open' but the
start_time has passed and they have enough participants; or they are
ONGOING with current_round=0 and no matches; or matches were generated
but no SHA baseline was pinned. This module provides a single
idempotent entry point that fixes all of those cases and re-arms the
anti-cheat audit chain.

Public API
----------
``ensure_tournament_integrity(tournament) -> dict``
    Inspect a single tournament, fix any inconsistencies, dispatch any
    missing SHA work, and return a structured report.

Used by:
    * The Celery task ``apps.tournaments.tasks.ensure_tournament_integrity``
    * The management command ``fix_stuck_tournaments``
    * Admin actions
"""
from __future__ import annotations

import logging

from django.db import transaction

log = logging.getLogger(__name__)


def _safe_call(label: str, fn, *args, **kwargs):
    """Run *fn* and swallow exceptions so one bad tournament can't
    block the recovery sweep. Errors are logged with full traceback."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        log.exception("ensure_tournament_integrity: step %r failed", label)
        return None


def ensure_tournament_integrity(tournament) -> dict:
    """Fix any lifecycle/integrity inconsistencies on *tournament*.

    Steps (each is best-effort and isolated — a failure in one step
    does NOT abort the others):

    1. Normalise status casing (``"open"`` → ``Status.OPEN`` etc).
       Django TextChoices already handle this in DB, but legacy rows
       may have raw strings that don't compare equal to the enum.
    2. If the tournament can be started (FULL, or OPEN past
       ``start_time`` with ≥ 2 players, non-QA), call
       ``engine.start_tournament`` — which itself runs the pre-round
       SHA check + dispatches the per-round audit.
    3. If the tournament is ONGOING but ``current_round == 0`` (matches
       never generated), call ``engine.generate_pairings(t, 1)``. That
       hook also pins the round-1 baseline and schedules the per-round
       integrity check.
    4. If the tournament is ONGOING and there ARE matches but NO
       baseline rows for the current round (audit infra was added
       after the round started, or the prior baseline call crashed),
       capture the baseline now AND fire a one-shot async audit pass
       so every active participant is verified immediately.
    5. Always (re-)dispatch ``pre_round_sha_check(tournament)`` on
       ONGOING non-QA tournaments — this is idempotent and guarantees
       the audit chain is armed even if Celery beat is currently down.

    Returns a structured report ``dict`` so callers can print/log it.
    """
    from apps.tournaments.models import Tournament, TournamentShaCheck

    report = {
        "tournament_id": tournament.pk,
        "tournament_name": tournament.name,
        "actions": [],
        "status_before": str(tournament.status),
        "status_after": str(tournament.status),
        "current_round_before": tournament.current_round,
        "current_round_after": tournament.current_round,
        "errors": [],
    }

    # ── Step 1: status normalisation ────────────────────────────
    # If a legacy row stored a status string that doesn't match the
    # canonical TextChoices value, save it through the ORM so the
    # serialised value gets re-coerced.
    valid_statuses = {s.value for s in Tournament.Status}
    if tournament.status not in valid_statuses:
        normalised = (tournament.status or "").strip().lower()
        if normalised in valid_statuses:
            tournament.status = normalised
            with transaction.atomic():
                tournament.save(update_fields=["status"])
            report["actions"].append(
                f"normalised_status:{normalised}",
            )

    # Skip QA — it has its own lobby/ready flow and is exempt from audit.
    if tournament.type == Tournament.Type.QA.value:
        report["actions"].append("skipped_qa")
        report["status_after"] = str(tournament.status)
        return report

    from apps.tournaments import engine as tengine
    from apps.tournaments.sha_audit import capture_round_baseline

    # ── Step 2: auto-start eligible OPEN/FULL tournaments ───────
    can_auto_start = (
        tournament.status in (
            Tournament.Status.FULL.value, Tournament.Status.OPEN.value,
        )
        and tournament.participant_count >= 2
    )
    if can_auto_start:
        # OPEN tournaments only auto-start if start_time has passed
        # (mirrors the existing check_stale_tournaments logic).
        from django.utils import timezone as _tz
        eligible = (
            tournament.status == Tournament.Status.FULL.value
            or tournament.start_time <= _tz.now()
        )
        if eligible:
            _safe_call(
                "start_tournament",
                tengine.start_tournament, tournament,
            )
            tournament.refresh_from_db()
            report["actions"].append("started_tournament")

    # ── Step 3: ONGOING with current_round=0 → generate round 1 ──
    if (
        tournament.status == Tournament.Status.ONGOING.value
        and tournament.current_round == 0
        and tournament.participant_count >= 2
    ):
        _safe_call(
            "generate_pairings_round_1",
            tengine.generate_pairings, tournament, 1,
        )
        tournament.refresh_from_db()
        report["actions"].append("generated_round_1")

    # ── Step 4: ONGOING but missing baseline for current round ──
    if (
        tournament.status == Tournament.Status.ONGOING.value
        and tournament.current_round >= 1
    ):
        rnum = tournament.current_round
        has_baseline = TournamentShaCheck.objects.filter(
            tournament=tournament,
            round_num=rnum,
            context=TournamentShaCheck.Context.ROUND_START,
        ).exists()
        if not has_baseline:
            pinned = _safe_call(
                "capture_round_baseline_recovery",
                capture_round_baseline, tournament, rnum,
            )
            if pinned is not None:
                report["actions"].append(
                    f"recovered_baseline_round_{rnum}"
                )

        # ── Step 5: always (re-)arm the audit chain ─────────────
        dispatched = _safe_call(
            "pre_round_sha_check",
            tengine.pre_round_sha_check, tournament,
        )
        if dispatched:
            report["actions"].append(
                f"dispatched_audits:{len(dispatched)}",
            )

    report["status_after"] = str(tournament.status)
    report["current_round_after"] = tournament.current_round
    return report


def ensure_all_open_tournaments() -> list[dict]:
    """Sweep every OPEN/FULL/ONGOING non-QA tournament. Returns a list
    of per-tournament reports."""
    from apps.tournaments.models import Tournament

    qs = Tournament.objects.exclude(
        type=Tournament.Type.QA,
    ).filter(
        status__in=[
            Tournament.Status.OPEN,
            Tournament.Status.FULL,
            Tournament.Status.ONGOING,
        ],
    )
    reports = []
    for t in qs:
        try:
            reports.append(ensure_tournament_integrity(t))
        except Exception:
            log.exception(
                "ensure_all_open_tournaments: catastrophic failure for "
                "tournament=%s", t.pk,
            )
            reports.append({
                "tournament_id": t.pk,
                "tournament_name": t.name,
                "errors": ["catastrophic_failure"],
                "actions": [],
            })
    return reports

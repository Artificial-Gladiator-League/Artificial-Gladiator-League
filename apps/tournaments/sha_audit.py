"""Anti-cheat SHA audit subsystem for live tournaments.

Random spot-checks compare each participant's pinned baseline commit
SHA (captured at the start of every round) against the live HF Hub
commit SHA. A mismatch is treated as cheating: the participant is
disqualified, their current match is forfeited, admins are notified
and a permanent ``TournamentShaCheck`` row is written.

Every check — pass, fail, or error — emits a single structured log
line in this exact format::

    [YYYY-MM-DD HH:MM:SS] SHA_CHECK | Tournament: "<name>" | Round: <n>
    | User: <username> | Model: <repo> | Expected: <12c>... | Current: <12c>...
    | Result: PASS|FAIL → <action>

Public API
----------
``capture_round_baseline(tournament, round_num)``
    Snapshot every active participant's current pinned SHA at round start.
``perform_sha_check(participant, *, context, round_num=None)``
    Run a single audit (returns the persisted ``TournamentShaCheck`` row).
``run_random_audit_pass()``
    Iterate ongoing tournaments and randomly pick participants to audit.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime
from typing import Optional

from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)

# Average expected interval between random checks per participant (seconds).
# Used by ``run_random_audit_pass`` to compute Bernoulli probability per tick.
RANDOM_AUDIT_INTERVAL_MIN_SEC = 45
RANDOM_AUDIT_INTERVAL_MAX_SEC = 150

# Hard cap on number of HF API calls per audit pass (rate-limit guard).
MAX_CHECKS_PER_PASS = 10

# Per-round guaranteed integrity check window. Every round, exactly one
# integrity check is scheduled per active participant at a uniformly
# random offset inside this window — guaranteeing each round of every
# tournament has at least one randomised check.
ROUND_CHECK_DELAY_MIN_SEC = 30
ROUND_CHECK_DELAY_MAX_SEC = 180


# ──────────────────────────────────────────────
#  Log formatter
# ──────────────────────────────────────────────
def _short(sha: str | None, n: int = 12) -> str:
    if not sha:
        return "<none>".ljust(n) + "..."
    return f"{sha[:n]}..."


def _emit_log_line(
    *,
    tournament_name: str,
    round_num: int,
    username: str,
    repo_id: str,
    expected_sha: str | None,
    current_sha: str | None,
    result: str,
    action: str,
) -> str:
    """Print + log the canonical SHA_CHECK line and return it."""
    line = (
        f"[{datetime.now():%Y-%m-%d %H:%M:%S}] SHA_CHECK | "
        f"Tournament: \"{tournament_name}\" | "
        f"Round: {round_num} | "
        f"User: {username} | "
        f"Model: {repo_id or '<no-repo>'} | "
        f"Expected: {_short(expected_sha)} | "
        f"Current: {_short(current_sha)} | "
        f"Result: {result} -> {action}"
    )
    # Mirror the line to both stdout (so it shows up in `manage.py` output
    # and Procfile worker logs) and the structured logger.
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Windows consoles often default to a non-UTF-8 codepage that can't
        # encode the arrow glyph — fall back to an ASCII transliteration so
        # the audit log line is never silently dropped.
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    if result == "FAIL":
        log.warning(line)
    elif result == "ERROR":
        log.error(line)
    else:
        log.info(line)
    return line


def _emit_summary_line(
    *,
    tournament,
    round_num: int,
    username: str,
    expected_sha: str | None,
    current_sha: str | None,
    result: str,
    action: str,
) -> str:
    """Print + log the canonical ``[MID-GAME SHA CHECK]`` verdict line.

    Distinct from the ``HF API check for repo …`` line emitted by
    ``live_sha_check`` itself — this one is the short verdict that
    operators grep for after a tournament.
    """
    line = (
        f"[MID-GAME SHA CHECK] Tournament #{tournament.pk} "
        f"Round {round_num} | "
        f"User: {username} | "
        f"Expected: {_short(expected_sha, 8)} | "
        f"Current: {_short(current_sha, 8)} | "
        f"Result: {result} -> {action}"
    )
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    if result == "FAIL":
        log.warning(line)
    elif result == "ERROR":
        log.error(line)
    else:
        log.info(line)
    return line


# ──────────────────────────────────────────────
#  Round baseline capture
# ──────────────────────────────────────────────
def capture_round_baseline(tournament, round_num: int) -> int:
    """Pin each active participant's current SHA as the round's baseline.

    Called from ``engine.generate_pairings`` at the start of every round.
    The baseline is what the random auditor compares against; any change
    after this call is treated as cheating.

    Returns the number of participants whose baseline was set.
    """
    from apps.tournaments.models import TournamentParticipant, TournamentShaCheck
    from apps.users.models import UserGameModel
    from apps.users.integrity import _resolve_ref_sha, _get_stored_token

    count = 0
    now = timezone.now()
    qs = TournamentParticipant.objects.filter(
        tournament=tournament,
        eliminated=False,
    ).select_related("user")

    for p in qs:
        try:
            gm = UserGameModel.objects.get(
                user=p.user, game_type=tournament.game_type,
            )
        except UserGameModel.DoesNotExist:
            continue

        # Pin the LIVE HF SHA at round start. This is the contract:
        # "your repo at this exact moment is the baseline; any change
        # after now counts as cheating". Falling back to db values
        # (approved_full_sha → last_known → original → registered)
        # only when HF is unreachable, so honest users with stale db
        # SHAs are never falsely flagged.
        live_sha = None
        repo_id = (gm.hf_model_repo_id or "").strip()
        if repo_id:
            token = _get_stored_token(p.user) or ""
            ref = (gm.submitted_ref or "main").strip() or "main"
            repo_type = (gm.submission_repo_type or "model").strip() or "model"
            try:
                live_sha = _resolve_ref_sha(
                    repo_id, token, ref=ref, repo_type=repo_type,
                )
            except Exception:
                live_sha = None

        baseline = (
            live_sha
            or gm.approved_full_sha
            or gm.last_known_commit_id
            or gm.original_model_commit_sha
            or p.registered_sha
            or ""
        )
        if not baseline:
            continue

        # Keep the model's last_known_commit_id in sync with what we
        # just pinned, so any other code paths that compare against it
        # see the same baseline.
        if live_sha and gm.last_known_commit_id != live_sha:
            try:
                gm.last_known_commit_id = live_sha
                gm.save(update_fields=["last_known_commit_id"])
            except Exception:
                log.debug("Could not persist last_known_commit_id", exc_info=True)

        p.round_pinned_sha = baseline
        p.round_pinned_at = now
        p.save(update_fields=["round_pinned_sha", "round_pinned_at"])
        count += 1

        # Persistent audit row so the baseline itself is auditable.
        TournamentShaCheck.objects.create(
            tournament=tournament,
            participant=p,
            user=p.user,
            round_num=round_num,
            game_type=tournament.game_type,
            repo_id=gm.hf_model_repo_id or "",
            expected_sha=baseline,
            current_sha=baseline,
            result=TournamentShaCheck.Result.PASS,
            context=TournamentShaCheck.Context.ROUND_START,
            action_taken="baseline_pinned",
        )

        _emit_log_line(
            tournament_name=tournament.name,
            round_num=round_num,
            username=p.user.username,
            repo_id=gm.hf_model_repo_id or "",
            expected_sha=baseline,
            current_sha=baseline,
            result="BASELINE",
            action=f"pinned for round {round_num}",
        )

    log.info(
        "capture_round_baseline: tournament=%s round=%d pinned=%d",
        tournament.name, round_num, count,
    )
    return count


# ──────────────────────────────────────────────
#  Single-participant SHA check
# ──────────────────────────────────────────────
def perform_sha_check(
    participant,
    *,
    context: str = "random_audit",
    round_num: Optional[int] = None,
    broadcast: bool = True,
):
    """Audit one participant against their pinned baseline SHA.

    Delegates the actual HF Hub call to
    :func:`apps.users.integrity.live_sha_check` so the standard
    ``HF API check for repo {repo}: current_sha=…, latest=…`` line
    always appears in the runserver / Celery worker console.

    On top of that, we emit a short verdict line in the format::

        [MID-GAME SHA CHECK] Tournament #5 Round 3 | User: alice
        | Expected: abc123.. | Current: xyz789..
        | Result: FAIL -> Disqualified

    Returns the persisted ``TournamentShaCheck`` row, or ``None`` if
    the participant is no longer eligible to be checked. On FAIL the
    participant is disqualified and the live match is forfeited via
    ``engine._handle_mid_round_disqualification``.
    """
    from apps.tournaments.models import TournamentShaCheck
    from apps.users.integrity import live_sha_check
    from apps.users.models import UserGameModel

    tournament = participant.tournament
    user = participant.user
    rnum = round_num if round_num is not None else tournament.current_round

    # Map our internal context to the live_sha_check label so the
    # existing print line says exactly what the spec asks for.
    sha_check_context = "mid-game" if context == "random_audit" else context

    if participant.eliminated or participant.disqualified_for_sha_mismatch:
        return None
    if tournament.status != tournament.Status.ONGOING:
        return None

    try:
        gm = UserGameModel.objects.get(
            user=user, game_type=tournament.game_type,
        )
    except UserGameModel.DoesNotExist:
        row = TournamentShaCheck.objects.create(
            tournament=tournament, participant=participant, user=user,
            round_num=rnum, game_type=tournament.game_type,
            result=TournamentShaCheck.Result.ERROR, context=context,
            action_taken="logged_only",
            error_message="No UserGameModel for tournament game_type.",
        )
        _emit_summary_line(
            tournament=tournament, round_num=rnum, username=user.username,
            expected_sha=None, current_sha=None,
            result="ERROR", action="No game model registered",
        )
        return row

    expected = participant.round_pinned_sha or gm.approved_full_sha or ""
    if not expected:
        row = TournamentShaCheck.objects.create(
            tournament=tournament, participant=participant, user=user,
            round_num=rnum, game_type=tournament.game_type,
            repo_id=gm.hf_model_repo_id or "",
            result=TournamentShaCheck.Result.SKIPPED, context=context,
            action_taken="logged_only",
            error_message="No baseline SHA pinned for this round.",
        )
        _emit_summary_line(
            tournament=tournament, round_num=rnum, username=user.username,
            expected_sha=None, current_sha=None,
            result="SKIPPED", action="No pinned baseline",
        )
        return row

    # ── Delegate to live_sha_check (prints the HF API check line) ──
    # fail_open=False so a network outage during a tournament round
    # is treated as a check failure (logged + audited, but NOT a DQ —
    # we explicitly differentiate ERROR vs FAIL below using db_sha).
    try:
        matches, db_sha, current = live_sha_check(
            gm, context=sha_check_context, fail_open=False,
        )
    except Exception as exc:
        row = TournamentShaCheck.objects.create(
            tournament=tournament, participant=participant, user=user,
            round_num=rnum, game_type=tournament.game_type,
            repo_id=gm.hf_model_repo_id or "", expected_sha=expected,
            result=TournamentShaCheck.Result.ERROR, context=context,
            action_taken="logged_only", error_message=str(exc)[:500],
        )
        _emit_summary_line(
            tournament=tournament, round_num=rnum, username=user.username,
            expected_sha=expected, current_sha=None,
            result="ERROR", action=f"HF unreachable: {exc}",
        )
        return row

    # Network failure (live_sha_check returned None for current).
    if current is None:
        row = TournamentShaCheck.objects.create(
            tournament=tournament, participant=participant, user=user,
            round_num=rnum, game_type=tournament.game_type,
            repo_id=gm.hf_model_repo_id or "", expected_sha=expected,
            result=TournamentShaCheck.Result.ERROR, context=context,
            action_taken="logged_only",
            error_message="HF returned no SHA (auth or network error).",
        )
        _emit_summary_line(
            tournament=tournament, round_num=rnum, username=user.username,
            expected_sha=expected, current_sha=None,
            result="ERROR", action="HF returned no SHA",
        )
        return row

    # Compare against the round-pinned baseline (NOT against db_sha,
    # because live_sha_check may already have flipped db state on a
    # prior mismatch). The pinned SHA is the single source of truth
    # for whether the model changed during this round.
    if current == expected:
        row = TournamentShaCheck.objects.create(
            tournament=tournament, participant=participant, user=user,
            round_num=rnum, game_type=tournament.game_type,
            repo_id=gm.hf_model_repo_id or "",
            expected_sha=expected, current_sha=current,
            result=TournamentShaCheck.Result.PASS, context=context,
            action_taken="ok",
        )
        _emit_summary_line(
            tournament=tournament, round_num=rnum, username=user.username,
            expected_sha=expected, current_sha=current,
            result="PASS", action="Model unchanged",
        )
        return row

    # ── MISMATCH = cheating ──
    row = TournamentShaCheck.objects.create(
        tournament=tournament, participant=participant, user=user,
        round_num=rnum, game_type=tournament.game_type,
        repo_id=gm.hf_model_repo_id or "",
        expected_sha=expected, current_sha=current,
        result=TournamentShaCheck.Result.FAIL, context=context,
        action_taken="disqualified_in_round",
    )
    # ── Required terminal alert (picked up by ops + the test harness) ──
    # Fires for BOTH QA and regular tournaments. The DB flag flip that
    # triggers DisqualificationInterceptMiddleware happens immediately
    # below in _react_to_mismatch → disqualify_for_repo_change.
    _alert = (
        f"\U0001F6A8 TERMINAL: REPO CHANGED - {user.username} "
        f"disqualified from {tournament.name} (Round {rnum})"
    )
    try:
        print(_alert, flush=True)
    except UnicodeEncodeError:
        print(_alert.encode("ascii", "replace").decode("ascii"), flush=True)
    log.warning(_alert)

    _emit_summary_line(
        tournament=tournament, round_num=rnum, username=user.username,
        expected_sha=expected, current_sha=current,
        result="FAIL", action="Disqualified",
    )

    # ── Highly visible terminal banner ──
    banner_lines = [
        "",
        "!" * 78,
        "!!  SHA MISMATCH DETECTED — PARTICIPANT DISQUALIFIED",
        "!!  Tournament : #{} {!r} (type={})".format(
            tournament.pk, tournament.name, tournament.type,
        ),
        "!!  Round      : {}".format(rnum),
        "!!  User       : {} (id={})".format(user.username, user.pk),
        "!!  Repo       : {}".format(gm.hf_model_repo_id or "<no-repo>"),
        "!!  Expected   : {}".format(expected),
        "!!  Current    : {}".format(current),
        "!!  Action     : DISQUALIFIED, current match forfeited,"
        " admins notified",
        "!" * 78,
        "",
    ]
    banner = "\n".join(banner_lines)
    try:
        print(banner, flush=True)
    except UnicodeEncodeError:
        print(banner.encode("ascii", "replace").decode("ascii"), flush=True)
    log.warning(banner)

    _react_to_mismatch(
        tournament=tournament,
        participant=participant,
        game_model=gm,
        expected=expected,
        current=current,
        round_num=rnum,
        broadcast=broadcast,
    )
    return row


# ──────────────────────────────────────────────
#  Mismatch reaction (DQ + admin email + ws broadcast)
# ──────────────────────────────────────────────
def _react_to_mismatch(
    *,
    tournament,
    participant,
    game_model,
    expected: str,
    current: str,
    round_num: int,
    broadcast: bool,
) -> None:
    from django.core.mail import mail_admins
    from apps.tournaments.disqualification import disqualify_for_repo_change

    # QA tournaments are for testing the audit pipeline itself. We
    # still DQ the participant from the current tournament, but we do
    # NOT trip the 30-rated-games re-validation gate so testers can
    # immediately re-register for a fresh QA tournament.
    is_qa = tournament.type == tournament.Type.QA

    reason = (
        f"Anti-cheat SHA mismatch in round {round_num}: "
        f"baseline={expected[:12]}... live={current[:12]}..."
    )

    # 1. + 2. Atomic DQ + live-match forfeit. The service handles
    #    both regular and QA flows (QA additionally clears `ready`
    #    so the user is removed from the lobby roster).
    try:
        disqualify_for_repo_change(
            participant, reason=reason, forfeit_live_match=True,
        )
    except Exception:
        log.exception(
            "disqualify_for_repo_change failed for %s — falling back to "
            "manual flag flip",
            participant.user.username,
        )
        try:
            participant.disqualified_for_sha_mismatch = True
            participant.eliminated = True
            participant.save(update_fields=[
                "disqualified_for_sha_mismatch", "eliminated",
            ])
        except Exception:
            log.exception("Fallback flag flip also failed")

    # 3. Flip integrity flag on their model so they can't requeue silently
    #    — EXCEPT for QA tournaments, where we want to keep re-entry
    #    frictionless for testers.
    if not is_qa:
        try:
            game_model.model_integrity_ok = False
            game_model.rated_games_since_revalidation = 0
            game_model.save(update_fields=[
                "model_integrity_ok", "rated_games_since_revalidation",
            ])
        except Exception:
            log.debug("Could not reset integrity flag", exc_info=True)
    else:
        log.info(
            "QA tournament DQ for %s — skipping 30-game revalidation gate",
            participant.user.username,
        )

    # 4. Email admins.
    try:
        mail_admins(
            subject=(
                f"[AGL][ANTI-CHEAT] {participant.user.username} disqualified "
                f"— SHA mismatch in {tournament.name}"
            ),
            message=(
                f"Tournament: {tournament.name} (pk={tournament.pk})\n"
                f"Round: {round_num}\n"
                f"User: {participant.user.username} (id={participant.user_id})\n"
                f"Repo: {game_model.hf_model_repo_id}\n"
                f"Expected SHA: {expected}\n"
                f"Current  SHA: {current}\n\n"
                "The participant has been disqualified, their live match "
                "forfeited, and their model integrity flag reset.\n"
            ),
        )
    except Exception:
        log.debug("mail_admins failed for SHA mismatch alert", exc_info=True)

    # 5. Optional channels broadcast.
    if broadcast:
        _broadcast_alert(
            tournament=tournament,
            user_id=participant.user_id,
            username=participant.user.username,
            repo_id=game_model.hf_model_repo_id,
            round_num=round_num,
        )


def _broadcast_alert(*, tournament, user_id, username, repo_id, round_num) -> None:
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
    except Exception:
        return
    layer = get_channel_layer()
    if layer is None:
        return

    redirect_url = "/tournaments/disqualified/"
    message = (
        f"⚠ Anti-cheat: {username} disqualified — "
        f"model {repo_id} changed during round {round_num}."
    )

    # 1. Fan-out to anyone watching the tournament page.
    try:
        async_to_sync(layer.group_send)(
            f"tournament_{tournament.pk}",
            {
                "type": "sha_check_alert",
                "tournament_id": tournament.pk,
                "round": round_num,
                "username": username,
                # The frontend compares this against the logged-in user
                # id and, on match, navigates to ``redirect_url``.
                "disqualified_user_id": user_id,
                "redirect_url": redirect_url,
                "repo": repo_id,
                "message": message,
            },
        )
    except Exception:
        log.debug("channels broadcast (tournament group) failed", exc_info=True)

    # 2. Direct push to the cheater's notifications channel — the
    #    base.html WebSocket handler reacts to ``new_notification``
    #    events and navigates the browser to ``redirect_url``. This
    #    works regardless of which page the cheater is currently on
    #    (lobby, profile, settings, etc.) so they always land on
    #    /tournaments/disqualified/ first.
    try:
        from apps.core.consumers import notif_group_name
        async_to_sync(layer.group_send)(
            notif_group_name(user_id),
            {
                "type": "send_notification",
                "verb": "tournament_disqualified",
                "actor": "system",
                "message": message,
                "url": redirect_url,
                "redirect_url": redirect_url,
                "unread_count": 0,
            },
        )
    except Exception:
        log.debug("channels broadcast (notifications group) failed", exc_info=True)


def _safe_user_token(user) -> str | None:
    try:
        from apps.users.integrity import _get_stored_token
        return _get_stored_token(user)
    except Exception:
        return None


# ──────────────────────────────────────────────
#  Per-(game, user) anti-cheat helper used by bot runners
# ──────────────────────────────────────────────
def check_player_for_tournament_game(*, game, user) -> bool:
    """Run one anti-cheat SHA check for *user* in tournament *game*.

    Used by ``apps.games.bot_runner._run_chess_game`` and
    ``_run_breakthrough_game`` to verify each player's HF model
    hasn't changed since the round started, BEFORE that player's
    bot is asked for a move.

    Behaviour
    ---------
    * The check is delegated to :func:`perform_sha_check`, which
      compares against the immutable ``round_pinned_sha`` baseline,
      prints the loud ``🚨 TERMINAL: REPO CHANGED`` banner, calls
      :func:`apps.tournaments.disqualification.disqualify_for_repo_change`
      (which sets ``disqualified_for_sha_mismatch=True`` so the
      ``DisqualificationInterceptMiddleware`` traps the cheater on
      ``/tournaments/disqualified/``), emails admins, and broadcasts
      the WebSocket alert that triggers the in-browser redirect.
    * Returns ``True`` iff the participant should be forfeited from
      the current move (FAIL outcome OR participant already DQ'd).
    * Returns ``False`` for PASS / ERROR / SKIPPED / no participant —
      i.e. fail-open for honest users, never penalising on a glitch.

    Throttling: a positive (FAIL / already-DQ'd) verdict is cached
    permanently per ``(game.pk, user.pk)`` — once a cheater, always
    a cheater for this game. A negative (PASS / ERROR / SKIPPED)
    verdict is cached only for ``MID_GAME_RECHECK_INTERVAL_SEC``
    seconds, after which the next bot move triggers a fresh HF
    round-trip. This is what catches mid-game repo pushes — without
    it, the very first PASS of a game would mask every later push.
    """
    if game is None or user is None:
        return False
    if not getattr(game, "is_tournament_game", False):
        return False

    import time as _time

    cache = _CHECK_CACHE.setdefault(int(game.pk), {})
    cached = cache.get(user.pk)
    if cached is not None:
        verdict, expires_at = cached
        # Positive verdicts (True) have expires_at == math.inf and
        # short-circuit forever. Negative verdicts expire so the
        # next move re-checks against the live HF SHA.
        if verdict or _time.monotonic() < expires_at:
            return verdict

    from apps.tournaments.models import (
        Tournament, TournamentParticipant, TournamentShaCheck,
    )

    def _store(verdict: bool) -> bool:
        if verdict:
            cache[user.pk] = (True, float("inf"))
        else:
            cache[user.pk] = (
                False, _time.monotonic() + MID_GAME_RECHECK_INTERVAL_SEC,
            )
        return verdict

    try:
        tm = getattr(game, "tournament_match", None)
        if not tm or not getattr(tm, "tournament_id", None):
            return _store(False)
        try:
            participant = TournamentParticipant.objects.select_related(
                "tournament", "user",
            ).get(tournament_id=tm.tournament_id, user=user)
        except TournamentParticipant.DoesNotExist:
            return _store(False)
        # Already DQ'd or eliminated — forfeit immediately.
        if participant.disqualified_for_sha_mismatch or participant.eliminated:
            return _store(True)
        if participant.tournament.status != Tournament.Status.ONGOING:
            return _store(False)

        row = perform_sha_check(
            participant,
            context="mid-game",
            round_num=getattr(participant.tournament, "current_round", None),
        )
        if row is None:
            return _store(False)
        return _store(row.result == TournamentShaCheck.Result.FAIL)
    except Exception:
        log.exception(
            "check_player_for_tournament_game failed for user=%s game=%s",
            getattr(user, "username", "?"), getattr(game, "pk", "?"),
        )
        return False


# Per-process throttle cache: {game_id: {user_id: (verdict, expires_at)}}.
# Positive verdicts never expire (cheater stays cheater); negative
# verdicts expire after MID_GAME_RECHECK_INTERVAL_SEC so a mid-game
# repo push is detected on the next bot move past the interval.
_CHECK_CACHE: dict[int, dict[int, tuple[bool, float]]] = {}

# Re-check interval (seconds) for negative SHA verdicts inside a single
# tournament bot game. Lower = more HF API calls but faster detection;
# higher = fewer calls but a cheater can sneak in changes between
# checks. 10s gives sub-move responsiveness without spamming HF.
MID_GAME_RECHECK_INTERVAL_SEC = 10.0


# ──────────────────────────────────────────────
#  Random audit pass (Celery entry point)
# ──────────────────────────────────────────────
def run_random_audit_pass(
    tick_seconds: float = 30.0,
    *,
    async_dispatch: bool = True,
) -> dict:
    """Iterate ONGOING tournaments and randomly audit eligible participants.

    The function is designed to be called once per Celery beat tick
    (default ``tick_seconds=30``). For each eligible participant the
    expected interval between checks is uniformly distributed in
    ``[RANDOM_AUDIT_INTERVAL_MIN_SEC, RANDOM_AUDIT_INTERVAL_MAX_SEC]``;
    we draw a random target interval per participant and convert that
    into a Bernoulli probability ``p = tick_seconds / target`` so the
    long-run rate matches the spec.

    When ``async_dispatch=True`` (default) each selected participant is
    pushed to the Celery task ``run_sha_check_for_participant`` via
    ``.delay()`` — the beat tick stays non-blocking and the HF round-
    trips happen in worker processes. ``async_dispatch=False`` runs
    each check inline (used by tests and the management command).

    Returns a summary dict with counts (useful for tests + logs).
    """
    from apps.tournaments.models import Tournament, TournamentParticipant

    ongoing = Tournament.objects.filter(
        status=Tournament.Status.ONGOING,
    )

    summary = {
        "tournaments": 0, "checked": 0, "dispatched": 0,
        "skipped": 0, "results": [],
    }
    if not ongoing.exists():
        return summary

    candidates = list(
        TournamentParticipant.objects
        .filter(
            tournament__in=ongoing,
            eliminated=False,
            disqualified_for_sha_mismatch=False,
        )
        .select_related("user", "tournament")
    )
    if not candidates:
        return summary

    summary["tournaments"] = ongoing.count()
    random.shuffle(candidates)
    checked = 0

    # Resolve the async task lazily so this module stays importable
    # in environments where Celery is not installed (tests).
    delay_fn = None
    if async_dispatch:
        try:
            from apps.tournaments.tasks import run_sha_check_for_participant
            delay_fn = getattr(run_sha_check_for_participant, "delay", None)
        except Exception:
            delay_fn = None
        if delay_fn is None:
            # Celery missing or task isn't wrapped — fall back to inline.
            async_dispatch = False

    for p in candidates:
        if checked >= MAX_CHECKS_PER_PASS:
            summary["skipped"] += 1
            continue
        target_interval = random.uniform(
            RANDOM_AUDIT_INTERVAL_MIN_SEC,
            RANDOM_AUDIT_INTERVAL_MAX_SEC,
        )
        prob = min(1.0, tick_seconds / target_interval)
        if random.random() > prob:
            summary["skipped"] += 1
            continue

        if async_dispatch:
            # Non-blocking: enqueue and move on. The worker will run
            # perform_sha_check, persist the audit row and react to
            # any mismatch (DQ + reset of rated_games_since_revalidation).
            try:
                delay_fn(p.pk)
                checked += 1
                summary["dispatched"] += 1
                summary["checked"] = checked
                summary["results"].append({
                    "user": p.user.username,
                    "tournament": p.tournament.name,
                    "result": "dispatched",
                })
            except Exception:
                log.exception(
                    "run_random_audit_pass: failed to enqueue check for participant=%s",
                    p.pk,
                )
                summary["skipped"] += 1
            continue

        row = perform_sha_check(p, context="random_audit")
        if row is None:
            summary["skipped"] += 1
            continue
        checked += 1
        summary["checked"] = checked
        summary["results"].append({
            "user": p.user.username,
            "tournament": p.tournament.name,
            "result": row.result,
        })

    log.info(
        "run_random_audit_pass: tournaments=%d checked=%d skipped=%d",
        summary["tournaments"], summary["checked"], summary["skipped"],
    )
    return summary


# ──────────────────────────────────────────────
#  Per-round guaranteed integrity check
# ──────────────────────────────────────────────
def schedule_round_integrity_check(tournament, round_num: int) -> float:
    """Schedule exactly one randomised SHA check for *round_num*.

    The check fires after a uniformly-random delay drawn from
    ``[ROUND_CHECK_DELAY_MIN_SEC, ROUND_CHECK_DELAY_MAX_SEC]``, so the
    timing is unpredictable to participants. Combined with the
    every-tick random audit pass, this guarantees every round of every
    tournament has at least one anti-cheat verification at a random
    time inside the round.

    Returns the chosen delay in seconds (so callers and tests can
    inspect what was scheduled).
    """
    from apps.tournaments.tasks import run_round_integrity_check

    delay = random.uniform(
        ROUND_CHECK_DELAY_MIN_SEC, ROUND_CHECK_DELAY_MAX_SEC,
    )
    try:
        run_round_integrity_check.apply_async(
            args=[tournament.pk, round_num],
            countdown=delay,
        )
        log.info(
            "schedule_round_integrity_check: tournament=%s round=%d "
            "delay=%.1fs",
            tournament.pk, round_num, delay,
        )
    except Exception:
        log.exception(
            "schedule_round_integrity_check: failed to enqueue "
            "tournament=%s round=%d",
            tournament.pk, round_num,
        )
    return delay


def run_round_integrity_pass(tournament_id: int, round_num: int) -> dict:
    """Run an integrity check for every active participant in a round.

    Called by the Celery task ``run_round_integrity_check`` after the
    randomised per-round delay elapses. Bails early if the tournament
    has finished, was reset, or has already advanced past *round_num*
    so we never check stale rounds.

    Each participant check is dispatched asynchronously via
    ``run_sha_check_for_participant.delay`` (when Celery is wired up)
    or executed inline as a fallback — never blocking the caller for
    multiple HF round-trips.
    """
    from apps.tournaments.models import Tournament, TournamentParticipant

    summary = {
        "tournament_id": tournament_id,
        "round_num": round_num,
        "dispatched": 0,
        "checked": 0,
        "skipped_stale": False,
    }

    try:
        tournament = Tournament.objects.get(pk=tournament_id)
    except Tournament.DoesNotExist:
        summary["skipped_stale"] = True
        return summary

    # Round must still be the active one and tournament must be live.
    if (
        tournament.status != Tournament.Status.ONGOING
        or tournament.current_round != round_num
    ):
        log.info(
            "run_round_integrity_pass: skipping stale round "
            "tournament=%s round=%d (current_round=%d status=%s)",
            tournament_id, round_num,
            tournament.current_round, tournament.status,
        )
        summary["skipped_stale"] = True
        return summary

    candidates = list(
        TournamentParticipant.objects
        .filter(
            tournament=tournament,
            eliminated=False,
            disqualified_for_sha_mismatch=False,
        )
        .select_related("user", "tournament")
    )

    # Try async dispatch first; fall back to inline if Celery missing.
    delay_fn = None
    try:
        from apps.tournaments.tasks import run_sha_check_for_participant
        delay_fn = getattr(run_sha_check_for_participant, "delay", None)
    except Exception:
        delay_fn = None

    for p in candidates:
        if delay_fn is not None:
            try:
                delay_fn(p.pk)
                summary["dispatched"] += 1
                continue
            except Exception:
                log.exception(
                    "run_round_integrity_pass: failed to enqueue "
                    "participant=%s — running inline",
                    p.pk,
                )
        # Inline fallback.
        row = perform_sha_check(
            p, context="random_audit", round_num=round_num,
        )
        if row is not None:
            summary["checked"] += 1

    log.info(
        "run_round_integrity_pass: tournament=%s round=%d "
        "dispatched=%d checked=%d",
        tournament_id, round_num,
        summary["dispatched"], summary["checked"],
    )
    return summary

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone

from apps.games.models import Game

from .models import Match, Tournament, TournamentParticipant, TournamentChatMessage


def tournament_list(request):
    tournaments = Tournament.objects.all()

    # Auto-recover stuck tournaments (ongoing/full with 0 participants)
    for t in tournaments:
        if t.status in (Tournament.Status.ONGOING, Tournament.Status.FULL):
            if t.participant_count == 0:
                t.matches.all().delete()
                t.status = Tournament.Status.OPEN
                t.current_round = 0
                t.champion = None
                t.save(update_fields=["status", "current_round", "champion"])

    joined_tournament_ids = set()
    if request.user.is_authenticated:
        joined_tournament_ids = set(
            TournamentParticipant.objects.filter(user=request.user)
            .values_list("tournament_id", flat=True)
        )

    # Map each tournament to one active (ongoing) game for spectating
    active_game_map = {}
    active_games = Game.objects.filter(
        status=Game.Status.ONGOING,
        is_tournament_game=True,
        tournament_match__tournament__in=tournaments,
    ).select_related("tournament_match__tournament")
    for game in active_games:
        tid = game.tournament_match.tournament_id
        if tid not in active_game_map:
            active_game_map[tid] = game

    # Attach active game to each tournament for template access
    tournament_list_items = list(tournaments)
    for t in tournament_list_items:
        t.active_game = active_game_map.get(t.pk)

    # ── Integrity check: compute join eligibility per tournament ──
    if request.user.is_authenticated:
        from apps.users.integrity import can_join_tournament, REVALIDATION_GAMES_REQUIRED
        from apps.users.models import UserGameModel

        for t in tournament_list_items:
            if t.status == Tournament.Status.OPEN and t.pk not in joined_tournament_ids:
                allowed, reason = can_join_tournament(request.user, t.game_type)
                t.join_blocked = not allowed
                t.join_blocked_reason = reason if not allowed else ""
            else:
                t.join_blocked = False
                t.join_blocked_reason = ""

            # Compute games remaining for tournament eligibility
            gm = UserGameModel.objects.filter(
                user=request.user, game_type=t.game_type,
            ).first()
            if gm:
                t.games_remaining = max(
                    0,
                    REVALIDATION_GAMES_REQUIRED - gm.rated_games_since_revalidation,
                )
            else:
                t.games_remaining = REVALIDATION_GAMES_REQUIRED

    return render(
        request, "tournaments/list.html", {
            "tournaments": tournament_list_items,
            "joined_tournament_ids": joined_tournament_ids,
        }
    )


def tournament_detail(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk)

    # ── Auto-recover stuck tournaments ────────────────────────────
    # If a tournament is ongoing/full but has zero participants
    # (e.g. all participants left, DB was cleaned up), reset it.
    if tournament.status in (Tournament.Status.ONGOING, Tournament.Status.FULL):
        if tournament.participant_count == 0:
            tournament.matches.all().delete()
            tournament.status = Tournament.Status.OPEN
            tournament.current_round = 0
            tournament.champion = None
            tournament.save(update_fields=["status", "current_round", "champion"])

    # ── Auto-sync QA status ─────────────────────────────────────
    # When participants are added / removed via admin the status
    # field can get out of sync with the actual participant count.
    if tournament.type == Tournament.Type.QA and tournament.status in (
        Tournament.Status.OPEN, Tournament.Status.FULL,
    ):
        if tournament.is_full and tournament.status == Tournament.Status.OPEN:
            tournament.status = Tournament.Status.FULL
            tournament.save(update_fields=["status"])
        elif not tournament.is_full and tournament.status == Tournament.Status.FULL:
            tournament.status = Tournament.Status.OPEN
            tournament.save(update_fields=["status"])
            TournamentParticipant.objects.filter(
                tournament=tournament,
            ).update(ready=False)

    # ── Auto-start QA when every participant is ready ──────────
    if (
        tournament.type == Tournament.Type.QA
        and tournament.status == Tournament.Status.FULL
        and tournament.participant_count >= 2
        and not tournament.participants.filter(ready=False).exists()
    ):
        from .engine import start_tournament

        first_matches = start_tournament(tournament)
        if first_matches:
            channel_layer = get_channel_layer()
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    f"tournament_{tournament.pk}",
                    {
                        "type": "tournament_event",
                        "data": {
                            "type": "qa_game_start",
                            "tournament_id": tournament.pk,
                            "match_id": first_matches[0].pk,
                        },
                    },
                )
            if request.user.is_authenticated and tournament.participants.filter(
                user=request.user,
            ).exists():
                return redirect(
                    "tournaments:live_match",
                    pk=tournament.pk,
                    match_id=first_matches[0].pk,
                )

    matches = (
        tournament.matches
        .select_related("player1", "player2", "winner")
        .order_by("round_num", "bracket_position")
    )

    # Check if the current user is a participant
    is_participant = False
    user_ready = False
    if request.user.is_authenticated:
        participant = tournament.participants.filter(user=request.user).first()
        is_participant = participant is not None
        user_ready = participant.ready if participant else False

    # QA lobby data: list of participants with ready status
    qa_participants = []
    if tournament.type == Tournament.Type.QA:
        for p in tournament.participants.select_related("user").all():
            fide = p.user.get_fide_title()
            qa_participants.append({
                "username": p.user.username,
                "elo": p.user.elo,
                "flag": p.user.country_flag,
                "ready": p.ready,
                "fide_abbr": fide["abbr"],
                "fide_css": fide["css"],
                "fide_title": fide["title"],
            })

    # If the tournament is ongoing with a live match, get the match id
    # so we can auto-redirect QA participants to the game view.
    qa_live_match_id = None
    qa_match = None
    if tournament.type == Tournament.Type.QA:
        if tournament.status == Tournament.Status.ONGOING:
            live = tournament.matches.filter(
                match_status=Match.MatchStatus.LIVE, is_armageddon=False,
            ).first()
            if live:
                qa_live_match_id = live.pk
                qa_match = live
        elif tournament.status == Tournament.Status.COMPLETED:
            qa_match = tournament.matches.select_related(
                "player1", "player2", "winner",
            ).first()

    # Build bracket data grouped by round
    rounds = {}
    for m in matches:
        rounds.setdefault(m.round_num, []).append(m)

    bracket_rounds = []
    for r in range(1, tournament.rounds_total + 1):
        r_matches = rounds.get(r, [])
        bracket_rounds.append({
            "round_num": r,
            "label": _round_label(r, tournament.rounds_total),
            "matches": r_matches,
            "is_current": r == tournament.current_round,
            "is_completed": all(m.is_completed for m in r_matches) if r_matches else False,
            "is_future": r > tournament.current_round,
        })

    # Live matches (for "Watch" buttons)
    live_matches = matches.filter(match_status=Match.MatchStatus.LIVE)

    # ── Integrity check for join eligibility ──────────────────
    can_join = False
    join_blocked_reason = ""
    if request.user.is_authenticated and not is_participant:
        from apps.users.integrity import can_join_tournament

        can_join, join_blocked_reason = can_join_tournament(
            request.user, tournament.game_type,
        )
        if can_join:
            join_blocked_reason = ""

    return render(
        request,
        "tournaments/detail.html",
        {
            "tournament": tournament,
            "bracket_rounds": bracket_rounds,
            "live_matches": live_matches,
            "cat_meta": tournament.category_info,
            "is_participant": is_participant,
            "user_ready": user_ready,
            "qa_participants": qa_participants,
            "qa_live_match_id": qa_live_match_id,
            "qa_match": qa_match,
            "can_join": can_join,
            "join_blocked_reason": join_blocked_reason,
        },
    )


def live_match(request, pk, match_id):
    """Spectator view for a single ongoing (or completed) tournament match."""
    tournament = get_object_or_404(Tournament, pk=pk)
    match = get_object_or_404(
        Match.objects.select_related("player1", "player2", "winner", "tournament"),
        pk=match_id,
        tournament=tournament,
    )

    # Determine time per side based on time_control string
    parts = match.time_control.split("+")
    base_min = int(parts[0]) if len(parts) >= 1 else 3
    increment = int(parts[1]) if len(parts) >= 2 else 0

    # Player identity — used for board orientation and resign button
    is_white = request.user.is_authenticated and request.user == match.player1
    is_black = request.user.is_authenticated and request.user == match.player2
    is_match_participant = is_white or is_black

    return render(
        request,
        "tournaments/game_view.html",
        {
            "tournament": tournament,
            "match": match,
            "base_seconds": base_min * 60,
            "increment": increment,
            "is_white": is_white,
            "is_black": is_black,
            "is_match_participant": is_match_participant,
            "game_type": tournament.game_type,
        },
    )


@login_required
def resign_match(request, pk, match_id):
    """Handle a player resigning from a live tournament match.

    Saves the linked Game with a resignation result which triggers
    the post_save signal chain (ELO update → handle_game_result →
    bracket advancement → tournament completion if final).
    """
    if request.method != "POST":
        return redirect("tournaments:live_match", pk=pk, match_id=match_id)

    tournament = get_object_or_404(Tournament, pk=pk)
    match = get_object_or_404(
        Match.objects.select_related("player1", "player2"),
        pk=match_id,
        tournament=tournament,
    )

    if match.match_status != Match.MatchStatus.LIVE:
        messages.error(request, "This match is not currently live.")
        return redirect("tournaments:live_match", pk=pk, match_id=match_id)

    user = request.user
    if user != match.player1 and user != match.player2:
        messages.error(request, "You are not a participant in this match.")
        return redirect("tournaments:live_match", pk=pk, match_id=match_id)

    # Find the active (non-terminal) Game linked to this match
    from apps.games.models import Game

    game = (
        match.games
        .exclude(status__in=Game.TERMINAL_STATUSES)
        .select_related("white", "black")
        .first()
    )
    if game is None:
        messages.error(request, "No active game found for this match.")
        return redirect("tournaments:live_match", pk=pk, match_id=match_id)

    # Set result based on who is resigning and their colour in the Game
    if user.pk == getattr(game.white, "pk", None):
        game.status = Game.Status.BLACK_WINS
        game.result = Game.Result.BLACK_WIN
        game.winner = game.black
        resign_result = "0-1"
        winner = game.black
    elif user.pk == getattr(game.black, "pk", None):
        game.status = Game.Status.WHITE_WINS
        game.result = Game.Result.WHITE_WIN
        game.winner = game.white
        resign_result = "1-0"
        winner = game.white
    else:
        messages.error(request, "You are not a player in the active game.")
        return redirect("tournaments:live_match", pk=pk, match_id=match_id)

    game.result_reason = "resignation"
    game.save()
    # post_save signal handles: ELO, match update, bracket advancement,
    # and tournament completion if this was the final match.

    # Broadcast game_over to spectators via WebSocket
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            f"match_{match_id}",
            {
                "type": "game_over",
                "data": {
                    "type": "game_over",
                    "result": resign_result,
                    "winner": winner.username if winner else "",
                    "reason": "resignation",
                },
            },
        )

    messages.success(request, "You resigned the game.")
    return redirect("tournaments:live_match", pk=pk, match_id=match_id)


@login_required
def join_tournament(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk, status=Tournament.Status.OPEN)

    # ── Model integrity & 30-game gate (QA exempt) ──────────
    if tournament.type != Tournament.Type.QA:
        from apps.users.integrity import can_join_tournament
        from apps.users.models import UserGameModel

        allowed, reason = can_join_tournament(request.user, tournament.game_type)
        if not allowed:
            messages.error(request, reason)
            return redirect("tournaments:detail", pk=pk)

        try:
            game_model = UserGameModel.objects.get(
                user=request.user, game_type=tournament.game_type,
            )
        except UserGameModel.DoesNotExist:
            game_model = None
    else:
        game_model = None

    # Check if already joined
    if tournament.participants.filter(user=request.user).exists():
        messages.info(request, "You are already registered in this tournament.")
        return redirect("tournaments:detail", pk=pk)

    # Validate ELO category eligibility (skip for QA tournaments)
    if tournament.category and tournament.type != Tournament.Type.QA:
        elo_min, elo_max = Tournament.CATEGORY_ELO_RANGE.get(
            tournament.category, (0, 99999)
        )
        if not (elo_min <= request.user.elo <= elo_max):
            messages.error(
                request,
                f"Your ELO ({request.user.elo}) is outside the "
                f"{tournament.get_category_display()} range ({elo_min}–{elo_max}).",
            )
            return redirect("tournaments:detail", pk=pk)

    # ── Model-lock check (30 rated games rule) ──────────────
    # The user must supply their HF token so we can verify the
    # model hasn't changed since it was locked.  The token is
    # used only for this API call and never stored.
    if tournament.type != Tournament.Type.QA:
        from apps.users.rating_lock import can_play_rated_game, get_latest_commit_id

        hf_token = request.POST.get("hf_token", "")
        allowed, reason = can_play_rated_game(request.user, hf_token, game_type=tournament.game_type)
        if not allowed:
            messages.error(request, reason)
            return redirect("tournaments:detail", pk=pk)

        # Snapshot latest commit for future locking at game 30
        if hf_token and game_model:
            commit = get_latest_commit_id(game_model.hf_model_repo_id, hf_token)
            if commit and commit != game_model.last_known_commit_id:
                game_model.last_known_commit_id = commit
                game_model.save(update_fields=["last_known_commit_id"])

    if not tournament.is_full:
        tournament.players.add(request.user)

        # Auto‑close registration and start when full
        if tournament.is_full:
            tournament.status = Tournament.Status.FULL
            tournament.save(update_fields=["status"])

            # QA tournaments wait for both players to press Ready
            if tournament.type != Tournament.Type.QA:
                # Auto-start the tournament
                from .engine import start_tournament
                start_tournament(tournament)

        # Also auto-start if the scheduled start_time has passed and
        # we have at least 2 players (mirrors check_stale_tournaments).
        elif (tournament.type != Tournament.Type.QA
              and tournament.start_time <= timezone.now()
              and tournament.participant_count >= 2):
            from .engine import start_tournament
            start_tournament(tournament)

        # Broadcast updated count to lobby WS group
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                "lobby",
                {
                    "type": "tournament.count",
                    "data": {
                        "type": "tournament_count",
                        "tournament_id": tournament.pk,
                        "count": tournament.participant_count,
                        "capacity": tournament.capacity,
                        "status": tournament.status,
                    },
                },
            )

            # If tournament just started, broadcast to tournament group
            if tournament.status == Tournament.Status.ONGOING:
                async_to_sync(channel_layer.group_send)(
                    f"tournament_{tournament.pk}",
                    {
                        "type": "round_started",
                        "data": {
                            "type": "round_started",
                            "tournament_id": tournament.pk,
                            "round": tournament.current_round,
                        },
                    },
                )

            # Notify QA tournament detail page of new participant
            if tournament.type == Tournament.Type.QA:
                _broadcast_qa_ready_state(tournament)

    return redirect("tournaments:detail", pk=pk)


@login_required
def leave_tournament(request, pk):
    """Allow a player to leave a tournament before it starts."""
    if request.method != "POST":
        return redirect("tournaments:detail", pk=pk)

    tournament = get_object_or_404(Tournament, pk=pk)

    if tournament.status not in (Tournament.Status.OPEN, Tournament.Status.FULL):
        messages.error(request, "You cannot leave a tournament that has already started.")
        return redirect("tournaments:detail", pk=pk)

    deleted, _ = TournamentParticipant.objects.filter(
        tournament=tournament, user=request.user,
    ).delete()

    if deleted:
        # If the tournament was full, reopen registration
        if tournament.status == Tournament.Status.FULL:
            tournament.status = Tournament.Status.OPEN
            tournament.save(update_fields=["status"])

        # Reset all remaining participants' ready state when someone leaves
        if tournament.type == Tournament.Type.QA:
            TournamentParticipant.objects.filter(
                tournament=tournament,
            ).update(ready=False)

        messages.success(request, "You have left the tournament.")

        # Broadcast updated count
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                "lobby",
                {
                    "type": "tournament.count",
                    "data": {
                        "type": "tournament_count",
                        "tournament_id": tournament.pk,
                        "count": tournament.participant_count,
                        "capacity": tournament.capacity,
                        "status": tournament.status,
                    },
                },
            )
            # Notify the tournament detail page
            if tournament.type == Tournament.Type.QA:
                _broadcast_qa_ready_state(tournament)
    else:
        messages.info(request, "You are not registered in this tournament.")

    return redirect("tournaments:detail", pk=pk)


@login_required
def ready_tournament(request, pk):
    """Mark the current player as ready in a QA tournament.

    When both players are ready the tournament starts automatically
    and the user is redirected to the live match view.
    """
    if request.method != "POST":
        return redirect("tournaments:detail", pk=pk)

    tournament = get_object_or_404(
        Tournament, pk=pk, type=Tournament.Type.QA, status=Tournament.Status.FULL,
    )

    participant = get_object_or_404(
        TournamentParticipant, tournament=tournament, user=request.user,
    )

    if not participant.ready:
        participant.ready = True
        participant.save(update_fields=["ready"])

    # Check if all participants are ready
    all_ready = not TournamentParticipant.objects.filter(
        tournament=tournament, ready=False,
    ).exists()

    channel_layer = get_channel_layer()

    if all_ready:
        # Start the tournament
        from .engine import start_tournament
        first_matches = start_tournament(tournament)

        # Broadcast game-start to tournament WS group
        if channel_layer and first_matches:
            match = first_matches[0]
            async_to_sync(channel_layer.group_send)(
                f"tournament_{tournament.pk}",
                {
                    "type": "tournament_event",
                    "data": {
                        "type": "qa_game_start",
                        "tournament_id": tournament.pk,
                        "match_id": match.pk,
                    },
                },
            )
            async_to_sync(channel_layer.group_send)(
                "lobby",
                {
                    "type": "tournament.count",
                    "data": {
                        "type": "tournament_count",
                        "tournament_id": tournament.pk,
                        "count": tournament.participant_count,
                        "capacity": tournament.capacity,
                        "status": tournament.status,
                    },
                },
            )

        # Redirect the player who pressed Ready last to the game
        if first_matches:
            return redirect(
                "tournaments:live_match",
                pk=tournament.pk,
                match_id=first_matches[0].pk,
            )
    else:
        # Broadcast updated ready state to everyone watching
        if channel_layer:
            _broadcast_qa_ready_state(tournament)

    return redirect("tournaments:detail", pk=pk)


def _broadcast_qa_ready_state(tournament):
    """Push current QA participant ready states to the tournament WS group."""
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    participants = []
    for p in TournamentParticipant.objects.filter(
        tournament=tournament,
    ).select_related("user"):
        participants.append({
            "username": p.user.username,
            "elo": p.user.elo,
            "flag": p.user.country_flag,
            "ready": p.ready,
        })

    async_to_sync(channel_layer.group_send)(
        f"tournament_{tournament.pk}",
        {
            "type": "tournament_event",
            "data": {
                "type": "qa_ready_update",
                "tournament_id": tournament.pk,
                "participants": participants,
                "count": tournament.participant_count,
                "status": tournament.status,
            },
        },
    )


def _round_label(round_num, total_rounds):
    """Human label for bracket round (e.g. 'Final', 'Semi‑final')."""
    remaining = total_rounds - round_num
    if remaining == 0:
        return "Final"
    elif remaining == 1:
        return "Semi‑final"
    elif remaining == 2:
        return "Quarter‑final"
    else:
        return f"Round {round_num}"


# ──────────────────────────────────────────────
# Gladiator Gauntlet views
# ──────────────────────────────────────────────

def gauntlet_detail(request, pk=None):
    """Show the current (or specific) Gladiator Gauntlet tournament page.

    URL: /tournaments/gauntlet/       → latest gauntlet
         /tournaments/gauntlet/<pk>/  → specific gauntlet
    """
    from apps.games.models import Comment

    if pk:
        tournament = get_object_or_404(Tournament, pk=pk, type=Tournament.Type.GAUNTLET)
    else:
        tournament = (
            Tournament.objects
            .filter(type=Tournament.Type.GAUNTLET)
            .order_by("-start_time")
            .first()
        )
        if tournament is None:
            return render(request, "tournaments/gauntlet.html", {
                "tournament": None,
            })

    # Standings table
    standings = list(
        tournament.standings
        .select_related("user")
        .order_by("rank")
    )

    # Matches grouped by round
    matches = (
        tournament.matches
        .filter(is_armageddon=False)
        .select_related("player1", "player2", "winner")
        .order_by("round_num", "bracket_position")
    )
    rounds = {}
    for m in matches:
        rounds.setdefault(m.round_num, []).append(m)

    round_list = []
    for r in range(1, tournament.rounds_total + 1):
        round_list.append({
            "num": r,
            "matches": rounds.get(r, []),
            "is_current": r == tournament.current_round,
            "is_future": r > tournament.current_round,
        })

    # Live matches (for watch buttons)
    live_matches = matches.filter(match_status=Match.MatchStatus.LIVE)

    # Map matches to their linked Game IDs for replay links
    from apps.games.models import Game
    game_map = {}
    match_ids = [m.pk for m in matches]
    if match_ids:
        games = Game.objects.filter(tournament_match_id__in=match_ids).values(
            "tournament_match_id", "pk",
        )
        for g in games:
            game_map[g["tournament_match_id"]] = g["pk"]

    # Comments (reuse Comment model from games app attached to a Game,
    # or we show tournament-level discussion via the first game)
    # For simplicity: show standalone comments section
    comment_count = 0
    page_obj = None

    # Past gauntlets (for archive navigation)
    past_gauntlets = (
        Tournament.objects
        .filter(type=Tournament.Type.GAUNTLET, status=Tournament.Status.COMPLETED)
        .exclude(pk=tournament.pk)
        .order_by("-week_number")[:10]
    )

    # Countdown: seconds until start_time (for upcoming gauntlets)
    countdown_seconds = 0
    if tournament.status in (Tournament.Status.UPCOMING, Tournament.Status.OPEN):
        delta = tournament.start_time - timezone.now()
        countdown_seconds = max(0, int(delta.total_seconds()))

    return render(request, "tournaments/gauntlet.html", {
        "tournament": tournament,
        "standings": standings,
        "round_list": round_list,
        "live_matches": live_matches,
        "game_map": game_map,
        "past_gauntlets": past_gauntlets,
        "countdown_seconds": countdown_seconds,
    })


def gauntlet_standings_partial(request, pk):
    """HTMX partial: return just the <tbody> rows for the standings table."""
    from .models import GauntletStanding

    tournament = get_object_or_404(Tournament, pk=pk, type=Tournament.Type.GAUNTLET)
    standings = list(
        tournament.standings
        .select_related("user")
        .order_by("rank")
    )
    html = render_to_string(
        "tournaments/partials/standings_rows.html",
        {"standings": standings, "tournament": tournament},
        request=request,
    )
    return HttpResponse(html)


# ── Tournament Live Chat (REMOVED) ───────────────────────────
# Chat/comment functionality on tournaments has been removed.

# def chat_messages(request, pk):
#     """Return the latest 50 chat messages as an HTML partial."""
#     tournament = get_object_or_404(Tournament, pk=pk)
#     msgs = (
#         tournament.chat_messages
#         .select_related("user")
#         .order_by("-created_at")[:50]
#     )
#     msgs = list(reversed(msgs))
#     return render(request, "tournaments/partials/chat_messages.html", {
#         "messages": msgs,
#         "tournament": tournament,
#     })


# @login_required
# def chat_send(request, pk):
#     """Post a new chat message, return the refreshed partial."""
#     tournament = get_object_or_404(Tournament, pk=pk)
#     if request.method == "POST" and tournament.status == Tournament.Status.ONGOING:
#         content = request.POST.get("content", "").strip()
#         if content:
#             TournamentChatMessage.objects.create(
#                 tournament=tournament,
#                 user=request.user,
#                 content=content[:500],
#             )
#     msgs = (
#         tournament.chat_messages
#         .select_related("user")
#         .order_by("-created_at")[:50]
#     )
#     msgs = list(reversed(msgs))
#     return render(request, "tournaments/partials/chat_messages.html", {
#         "messages": msgs,
#         "tournament": tournament,
#     })

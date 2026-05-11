import json
import logging
import random

import chess
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse

from .models import Comment, Game
from apps.tournaments.models import Tournament

log = logging.getLogger(__name__)

TIME_CONTROLS = [
    {"value": "3+1", "label": "3+1",  "desc": "Blitz",       "primary": True},
    {"value": "1+0", "label": "1+0",  "desc": "Bullet"},
    {"value": "1+1", "label": "1+1",  "desc": "Bullet"},
    {"value": "2+0", "label": "2+0",  "desc": "Bullet"},
    {"value": "2+1", "label": "2+1",  "desc": "Blitz"},
    {"value": "3+0", "label": "3+0",  "desc": "Blitz"},
    {"value": "5+0", "label": "5+0",  "desc": "Blitz"},
    {"value": "5+1", "label": "5+1",  "desc": "Blitz"},
    {"value": "10+0", "label": "10+0", "desc": "Rapid"},
    {"value": "10+5", "label": "10+5", "desc": "Rapid"},
    {"value": "15+0", "label": "15+0", "desc": "Rapid"},
    {"value": "15+5", "label": "15+5", "desc": "Rapid"},
    {"value": "30+0", "label": "30+0", "desc": "Classical"},
    {"value": "30+5", "label": "30+5", "desc": "Classical"},
    {"value": "60+0", "label": "60+0", "desc": "Classical"},
    {"value": "60+5", "label": "60+5", "desc": "Classical"},
]


def _classify_time_control(tc: str) -> str:
    """Classify a time control string into bullet, blitz, rapid, or classical."""
    parts = tc.split("+")
    base_min = int(parts[0]) if parts else 3
    if base_min < 3:
        return "bullet"
    elif base_min < 10:
        return "blitz"
    elif base_min < 30:
        return "rapid"
    return "classical"


def _broadcast_lobby_update(action: str, game) -> None:
    """Broadcast a lobby update via the channel layer (best-effort)."""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return

        if action == "new_waiting_game":
            creator = game.white or game.black
            if creator:
                fide = creator.get_fide_title()
                async_to_sync(channel_layer.group_send)("lobby", {
                    "type": "lobby_update",
                    "data": {
                        "type": "new_waiting_game",
                        "game": {
                            "pk": game.pk,
                            "white_name": (creator.ai_name or creator.username),
                            "white_elo": creator.elo,
                            "fide_abbr": fide["abbr"],
                            "fide_title": fide["title"],
                            "fide_css": fide["css"],
                            "time_control": game.time_control,
                            "variant": _classify_time_control(game.time_control).capitalize(),
                            "join_url": reverse("games:join_game", args=[game.pk]),
                        },
                    },
                })
        elif action == "remove_waiting_game":
            async_to_sync(channel_layer.group_send)("lobby", {
                "type": "lobby_update",
                "data": {
                    "type": "remove_waiting_game",
                    "game_pk": game.pk,
                },
            })
    except Exception:
        log.debug("Could not broadcast lobby update for game %s", game.pk)


def _wants_json(request) -> bool:
    """Return True when the client is an AJAX/fetch call that expects JSON."""
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )


def has_valid_repo(user) -> bool:
    """Return True if *user* has a valid repo for at least chess.

    Delegates to the canonical ``has_repo_for_game_type`` in consumers so
    both the HTTP and WebSocket paths share the same logic.
    """
    from .consumers import has_repo_for_game_type
    return has_repo_for_game_type(user, "chess")


def _live_ai_games_queryset(*, exclude_game_id: int | None = None):
    qs = (
        Game.objects.filter(
            status=Game.Status.ONGOING,
            white__isnull=False,
            black__isnull=False,
        )
        .exclude(white__hf_model_repo_id="")
        .exclude(black__hf_model_repo_id="")
        .select_related("white", "black", "winner")
    )
    if exclude_game_id is not None:
        qs = qs.exclude(pk=exclude_game_id)
    return qs


def _serialize_live_ai_games(games):
    items = []
    for game in games:
        items.append({
            "game": game,
            "white_ai_name": game.white.ai_name or game.white.username,
            "black_ai_name": game.black.ai_name or game.black.username,
            "category": _classify_time_control(game.time_control).capitalize(),
        })
    return items


def _random_finished_game():
    qs = (
        Game.objects.exclude(result=Game.Result.NONE)
        .filter(white__isnull=False, black__isnull=False)
        .select_related("white", "black", "winner")
    )
    total = qs.count()
    if total == 0:
        return None
    return qs[random.randrange(total)]


def _preview_moves(game, *, max_moves=None):
    """Return game moves as [[from, to], …] square-pair lists.

    The JavaScript side starts Chessground at the standard starting position
    and calls cg.move(from, to) for each pair.  That is the only Chessground
    API that triggers real animated piece movement; cg.set({fen}) only snaps.
    """
    source_moves = game.move_list if max_moves is None else game.move_list[:max_moves]
    result = []
    for uci in source_moves:
        if len(uci) >= 4:
            result.append([uci[:2], uci[2:4]])
    return result


# Three different classic openings used as fallback animation when a game
# has no recorded moves yet.  Each is a verified, castling-free 15-move
# sequence so cg.move() replays them cleanly from the starting position.
_DEMO_SEQUENCES = [
    # Italian Game / Giuoco Piano
    [["e2","e4"],["e7","e5"],["g1","f3"],["b8","c6"],
     ["f1","c4"],["f8","c5"],["c2","c3"],["g8","f6"],
     ["d2","d4"],["e5","d4"],["c3","d4"],["c5","b4"],
     ["b1","c3"],["b4","c3"],["b2","c3"]],
    # Sicilian Najdorf
    [["e2","e4"],["c7","c5"],["g1","f3"],["d7","d6"],
     ["d2","d4"],["c5","d4"],["f3","d4"],["g8","f6"],
     ["b1","c3"],["a7","a6"],["f1","e2"],["e7","e5"],
     ["d4","b3"],["f8","e7"],["h2","h3"]],
    # Queen's Gambit Declined
    [["d2","d4"],["d7","d5"],["c2","c4"],["e7","e6"],
     ["b1","c3"],["g8","f6"],["g1","f3"],["f8","e7"],
     ["e2","e3"],["b7","b6"],["f1","d3"],["c8","b7"],
     ["a2","a3"],["b8","d7"],["c4","d5"]],
]


def _serialize_display_game(game, *, is_live, slot=0):
    white_name = game.white.ai_name or game.white.username
    black_name = game.black.ai_name or game.black.username
    white_fide = game.white.get_fide_title()
    black_fide = game.black.get_fide_title()
    preview_moves = _preview_moves(game)
    base_seconds, increment_seconds = game.parse_time_control()

    # Fall back to a demo opening if the game has fewer than 2 recorded moves
    # so the board always has something to animate.
    if len(preview_moves) < 2:
        preview_moves = _DEMO_SEQUENCES[slot % len(_DEMO_SEQUENCES)]

    return {
        "pk": game.pk,
        "white_name": white_name,
        "black_name": black_name,
        "white_elo": game.white.elo,
        "black_elo": game.black.elo,
        "white_fide_abbr": white_fide["abbr"],
        "white_fide_css": white_fide["css"],
        "white_fide_title": white_fide["title"],
        "black_fide_abbr": black_fide["abbr"],
        "black_fide_css": black_fide["css"],
        "black_fide_title": black_fide["title"],
        "white_time": int(game.white_time),
        "black_time": int(game.black_time),
        "base_seconds": int(base_seconds),
        "increment_seconds": int(increment_seconds),
        "current_fen": game.current_fen or chess.STARTING_FEN,
        "is_live": is_live,
        "spectate_url": reverse("games:spectate", args=[game.pk]),
        "preview_moves_json": json.dumps(preview_moves),
        "game_type": game.game_type,
        "time_control": game.time_control,
        "variant": _classify_time_control(game.time_control).capitalize(),
        "move_count": len(game.move_list) if game.move_list else 0,
        "date_played": game.timestamp,
    }


def _placeholder_display_game(idx=0):
    """Board shown when fewer than 3 real games are available.
    Cycles through _DEMO_SEQUENCES so all three boards show different openings."""
    moves = _DEMO_SEQUENCES[idx % len(_DEMO_SEQUENCES)]
    return {
        "pk": None,
        "white_name": "White",
        "black_name": "Black",
        "white_elo": None,
        "black_elo": None,
        "white_fide_abbr": "",
        "white_fide_css": "",
        "white_fide_title": "",
        "black_fide_abbr": "",
        "black_fide_css": "",
        "black_fide_title": "",
        "white_time": None,
        "black_time": None,
        "base_seconds": 180,
        "increment_seconds": 1,
        "current_fen": chess.STARTING_FEN,
        "is_live": False,
        "spectate_url": None,
        "preview_moves_json": json.dumps(moves),
        "game_type": "chess",
        "time_control": "",
        "date_played": None,
    }


def _build_display_games():
    live_candidates = list(
        Game.objects.filter(
            status__in=[Game.Status.ONGOING, "active"],
            white__isnull=False,
            black__isnull=False,
        )
        .select_related("white", "black")
        .order_by("-last_move_at", "-timestamp")[:10]
    )
    finished_candidates = list(
        Game.objects.filter(
            status__in=list(Game.TERMINAL_STATUSES),
            white__isnull=False,
            black__isnull=False,
        )
        .select_related("white", "black")
        .order_by("-last_move_at", "-timestamp")[:10]
    )

    selected_live = (
        random.sample(live_candidates, 3)
        if len(live_candidates) > 3
        else list(live_candidates)
    )
    remaining_slots = 3 - len(selected_live)

    selected_finished = []
    if remaining_slots > 0 and finished_candidates:
        sample_size = min(remaining_slots, len(finished_candidates))
        selected_finished = random.sample(finished_candidates, sample_size)

    # Build display list — only real games, no placeholders.
    display_games = []
    for slot, game in enumerate(selected_live):
        display_games.append(_serialize_display_game(game, is_live=True, slot=slot))
    for game in selected_finished:
        slot = len(display_games)
        display_games.append(_serialize_display_game(game, is_live=False, slot=slot))

    return display_games[:3]


def _build_waiting_games():
    waiting_games = (
        Game.objects.filter(
            status=Game.Status.WAITING,
        )
        .filter(
            Q(white__isnull=True, black__isnull=False)
            | Q(white__isnull=False, black__isnull=True)
        )
        .select_related("white", "black")
        .order_by("-timestamp")[:20]
    )
    items = []
    for game in waiting_games:
        creator = game.white or game.black
        fide = creator.get_fide_title()
        items.append({
            "pk": game.pk,
            "white_name": creator.ai_name or creator.username,
            "white_elo": creator.elo,
            "fide_abbr": fide["abbr"],
            "fide_title": fide["title"],
            "fide_css": fide["css"],
            "time_control": game.time_control,
            "variant": _classify_time_control(game.time_control).capitalize(),
            "game_type": game.game_type,
            "game_type_label": Game.GameType(game.game_type).label,
            "join_url": reverse("games:join_game", args=[game.pk]),
        })
    return items


def _build_ongoing_games():
    ongoing_games = (
        Game.objects.filter(
            status=Game.Status.ONGOING,
            white__isnull=False,
            black__isnull=False,
        )
        .select_related("white", "black")
        .order_by("-last_move_at", "-timestamp")[:20]
    )
    items = []
    for game in ongoing_games:
        white_fide = game.white.get_fide_title()
        black_fide = game.black.get_fide_title()
        items.append({
            "pk": game.pk,
            "white_name": game.white.ai_name or game.white.username,
            "white_elo": game.white.elo,
            "white_fide_abbr": white_fide["abbr"],
            "white_fide_title": white_fide["title"],
            "white_fide_css": white_fide["css"],
            "black_name": game.black.ai_name or game.black.username,
            "black_elo": game.black.elo,
            "black_fide_abbr": black_fide["abbr"],
            "black_fide_title": black_fide["title"],
            "black_fide_css": black_fide["css"],
            "time_control": game.time_control,
            "variant": _classify_time_control(game.time_control).capitalize(),
            "spectate_url": reverse("games:spectate", args=[game.pk]),
            "move_count": len(game.move_list) if game.move_list else 0,
        })
    return items


@login_required
def lobby(request):
    """Lobby with quick pairing, open AI games, and live/replay previews."""
    user = request.user
    log.info("[LOBBY] User %s joined lobby", user.username)
    display_games = _build_display_games()
    waiting_games = _build_waiting_games()
    ongoing_games_list = _build_ongoing_games()
    fide = user.get_fide_title()

    ongoing_count = Game.objects.filter(status=Game.Status.ONGOING).count()
    live_gladiators_count = ongoing_count * 2
    active_tournaments_count = Tournament.objects.filter(
        status__in=[Tournament.Status.OPEN, Tournament.Status.ONGOING]
    ).count()

    return render(request, "games/lobby.html", {
        "time_controls": TIME_CONTROLS,
        "user_profile": user,
        "category": user.get_category(),
        "fide_title": fide,
        "waiting_games": waiting_games,
        "ongoing_games": ongoing_games_list,
        "display_games": display_games,
        "has_live_display_games": any(g["is_live"] for g in display_games),
        "live_gladiators_count": live_gladiators_count,
        "active_tournaments_count": active_tournaments_count,
    })


@login_required
def create_lobby_game(request):
    """Create a new open lobby game from the modal form."""
    if request.method != "POST":
        return redirect("games:lobby")
    ajax = _wants_json(request)
    from .consumers import has_repo_for_game_type
    game_type = request.POST.get("game_type", Game.GameType.CHESS)
    if game_type not in {Game.GameType.CHESS, Game.GameType.BREAKTHROUGH}:
        game_type = Game.GameType.CHESS
    # Primary gate: user must have a repo for this specific game type
    if not has_repo_for_game_type(request.user, game_type):
        _game_label = Game.GameType(game_type).label
        _no_repo_msg = f"You can not create or join a game of {_game_label}. Please submit your Artificial Gladiator first."
        if ajax:
            return JsonResponse({"error": _no_repo_msg, "no_repo": True}, status=403)
        messages.error(request, _no_repo_msg)
        return redirect("games:lobby")
    # Proof-of-Ownership gate: repo must be verified
    _gm = request.user.get_game_model(game_type)
    if _gm and not _gm.is_verified:
        _msg = (
            "Your model repository must be verified before creating games. "
            "Go to your profile, add AGL_VERIFY.txt to your repo, and click 'Verify Ownership'."
        )
        if ajax:
            return JsonResponse({"error": _msg}, status=403)
        messages.error(request, _msg)
        return redirect("games:lobby")
    base_minutes = request.POST.get("base_minutes", "3")
    increment_sec = request.POST.get("increment_seconds", "1")
    try:
        base_min = int(base_minutes)
        inc = int(increment_sec)
    except (ValueError, TypeError):
        base_min, inc = 3, 1
    # Clamp to sensible ranges
    base_min = max(1, min(base_min, 60))
    inc = max(0, min(inc, 60))
    tc = f"{base_min}+{inc}"
    base_sec = base_min * 60
    from . import breakthrough_engine as bt
    starting_fen = bt.STARTING_FEN if game_type == Game.GameType.BREAKTHROUGH else chess.STARTING_FEN
    # Determine color assignment
    color = request.POST.get("color", "random")
    if color == "black":
        white_player = None
        black_player = request.user
    elif color == "white":
        white_player = request.user
        black_player = None
    else:
        # random: coin flip
        import random as _rng
        if _rng.choice([True, False]):
            white_player = request.user
            black_player = None
        else:
            white_player = None
            black_player = request.user
    game = Game.objects.create(
        white=white_player,
        black=black_player,
        time_control=tc,
        white_time=float(base_sec),
        black_time=float(base_sec),
        increment=inc,
        game_type=game_type,
        current_fen=starting_fen,
        ai_thinking_seconds=1.0,
    )
    _broadcast_lobby_update("new_waiting_game", game)
    dest = reverse("games:game_detail", args=[game.pk])
    if ajax:
        return JsonResponse({"redirect": dest})
    return redirect(dest)


@login_required
def create_game(request):
    """Create a new open game (quick pair) — fallback for non‑WS clients."""
    ajax = _wants_json(request)
    from .consumers import has_repo_for_game_type
    game_type = request.POST.get("game_type", Game.GameType.CHESS)
    if game_type not in {Game.GameType.CHESS, Game.GameType.BREAKTHROUGH}:
        game_type = Game.GameType.CHESS
    # Primary gate: user must have a repo for this specific game type
    if not has_repo_for_game_type(request.user, game_type):
        _game_label = Game.GameType(game_type).label
        _no_repo_msg = f"You can not create or join a game of {_game_label}. Please submit your Artificial Gladiator first."
        if ajax:
            return JsonResponse({"error": _no_repo_msg, "no_repo": True}, status=403)
        messages.error(request, _no_repo_msg)
        return redirect("games:lobby")
    # Proof-of-Ownership gate: repo must be verified
    _gm = request.user.get_game_model(game_type)
    if _gm and not _gm.is_verified:
        _msg = (
            "Your model repository must be verified before creating games. "
            "Go to your profile, add AGL_VERIFY.txt to your repo, and click 'Verify Ownership'."
        )
        if ajax:
            return JsonResponse({"error": _msg}, status=403)
        messages.error(request, _msg)
        return redirect("games:lobby")
    tc = request.POST.get("time_control", "3+1")
    parts = tc.split("+")
    base_sec = int(parts[0]) * 60 if parts else 180
    inc = int(parts[1]) if len(parts) > 1 else 0
    from . import breakthrough_engine as bt
    starting_fen = bt.STARTING_FEN if game_type == Game.GameType.BREAKTHROUGH else chess.STARTING_FEN
    game = Game.objects.create(
        white=request.user,
        time_control=tc,
        white_time=float(base_sec),
        black_time=float(base_sec),
        increment=inc,
        game_type=game_type,
        current_fen=starting_fen,
        ai_thinking_seconds=1.0,
    )
    _broadcast_lobby_update("new_waiting_game", game)
    dest = reverse("games:game_detail", args=[game.pk])
    if ajax:
        return JsonResponse({"redirect": dest})
    return redirect(dest)


@login_required
def join_game(request, game_id):
    """Join an open game, filling whichever side is empty."""
    ajax = _wants_json(request)
    game = get_object_or_404(
        Game.objects.select_related("white", "black"),
        pk=game_id,
        result=Game.Result.NONE,
    )
    # Already in this game
    if request.user in (game.white, game.black):
        dest = reverse("games:game_detail", args=[game.pk])
        if ajax:
            return JsonResponse({"redirect": dest})
        return redirect(dest)
    # Primary gate: user must have a repo for this specific game type
    from .consumers import has_repo_for_game_type
    if not has_repo_for_game_type(request.user, game.game_type):
        _game_label = Game.GameType(game.game_type).label
        _no_repo_msg = f"You can not create or join a game of {_game_label}. Please submit your Artificial Gladiator first."
        if ajax:
            return JsonResponse({"error": _no_repo_msg, "no_repo": True}, status=403)
        messages.error(request, _no_repo_msg)
        return redirect("games:lobby")
    # Proof-of-Ownership gate: repo must be verified
    _gm = request.user.get_game_model(game.game_type)
    if _gm and not _gm.is_verified:
        _msg = (
            "Your model repository must be verified before joining games. "
            "Go to your profile, add AGL_VERIFY.txt to your repo, and click 'Verify Ownership'."
        )
        if ajax:
            return JsonResponse({"error": _msg}, status=403)
        messages.error(request, _msg)
        return redirect("games:lobby")
    if game.black is None:
        game.black = request.user
        game.save(update_fields=["black"])
        log.info(
            "[GAME START] %s vs %s | game_id=%s | type=%s | tc=%s",
            game.white.username, game.black.username,
            game.pk, game.game_type, game.time_control,
        )
    elif game.white is None:
        game.white = request.user
        game.save(update_fields=["white"])
        log.info(
            "[GAME START] %s vs %s | game_id=%s | type=%s | tc=%s",
            game.white.username, game.black.username,
            game.pk, game.game_type, game.time_control,
        )
    else:
        if ajax:
            return JsonResponse({"error": "This game is already full."}, status=409)
        messages.error(request, "This game is already full.")
        return redirect("games:lobby")
    _broadcast_lobby_update("remove_waiting_game", game)
    dest = reverse("games:game_detail", args=[game.pk])
    if ajax:
        return JsonResponse({"redirect": dest})
    return redirect(dest)


@login_required
def cancel_game(request, game_id):
    """Cancel/abort a game that hasn't started yet (creator only)."""
    game = get_object_or_404(
        Game.objects.select_related("white", "black"),
        pk=game_id,
    )
    if game.status != Game.Status.WAITING:
        return redirect("games:game_detail", game_id=game.pk)
    # Only the game creator can cancel
    creator = game.white or game.black
    if request.user != creator:
        return redirect("games:game_detail", game_id=game.pk)
    game.status = Game.Status.ABORTED
    game.result = Game.Result.NONE
    game.result_reason = "cancelled"
    game.save(update_fields=["status", "result", "result_reason"])
    _broadcast_lobby_update("remove_waiting_game", game)
    return redirect("games:lobby")


@login_required
def leave_game(request, game_id):
    """Leave a game as the joining player (black) before it starts."""
    game = get_object_or_404(
        Game.objects.select_related("white", "black"),
        pk=game_id,
    )
    if game.status != Game.Status.WAITING:
        return redirect("games:game_detail", game_id=game.pk)
    # The joining player can leave — remove them from whichever side
    if request.user == game.black:
        game.black = None
        game.save(update_fields=["black"])
    elif request.user == game.white:
        game.white = None
        game.save(update_fields=["white"])
    else:
        return redirect("games:game_detail", game_id=game.pk)
    _broadcast_lobby_update("new_waiting_game", game)
    return redirect("games:lobby")


def game_detail(request, game_id):
    """Live spectator / review view for a casual game — includes threaded comments."""
    game = get_object_or_404(
        Game.objects.select_related("white", "black", "winner"),
        pk=game_id,
    )

    parts = game.time_control.split("+")
    base_sec = int(parts[0]) * 60 if parts else 180
    increment = int(parts[1]) if len(parts) > 1 else 0

    # ── Threaded comments ─────────────────────────
    comment_count = game.comments.count()
    replies_qs = Comment.objects.select_related("user").order_by("created_at")
    top_comments = (
        game.comments
        .filter(parent__isnull=True)
        .select_related("user")
        .prefetch_related(
            Prefetch("replies", queryset=replies_qs),
            Prefetch("replies__replies", queryset=replies_qs),
            Prefetch("replies__replies__replies", queryset=replies_qs),
        )
        .order_by("-created_at")          # newest top-level first
    )
    paginator = Paginator(top_comments, 20)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    return render(request, "games/game_view.html", {
        "game": game,
        "base_seconds": base_sec,
        "increment": increment,
        "page_obj": page_obj,
        "comment_count": comment_count,
    })


def game_history(request):
    """List of completed games with stats."""
    completed = Game.objects.exclude(result=Game.Result.NONE)
    return render(request, "games/history.html", {"games": completed})


def spectate_game(request, game_id):
    """Spectator room — shows the live board and a real-time spectator count."""
    game = get_object_or_404(
        Game.objects.select_related("white", "black", "winner"),
        pk=game_id,
    )
    parts = game.time_control.split("+")
    base_sec = int(parts[0]) * 60 if parts else 180
    increment = int(parts[1]) if len(parts) > 1 else 0

    from apps.users.models import CustomUser
    white_rank = CustomUser.objects.filter(
        elo__gt=game.white.elo, is_active=True
    ).count() + 1 if game.white else None
    black_rank = CustomUser.objects.filter(
        elo__gt=game.black.elo, is_active=True
    ).count() + 1 if game.black else None

    return render(request, "games/spectate.html", {
        "game": game,
        "base_seconds": base_sec,
        "increment": increment,
        "white_rank": white_rank,
        "black_rank": black_rank,
    })


def next_live_ai_game(request, game_id):
    next_game = (
        _live_ai_games_queryset(exclude_game_id=game_id)
        .order_by("-last_move_at", "-timestamp")
        .first()
    )
    return JsonResponse({
        "next_spectate_url": (
            reverse("games:spectate", args=[next_game.pk]) if next_game else None
        ),
        "replay_url": reverse("games:game_detail", args=[game_id]),
    })


# ──────────────────────────────────────────────
# Comment views (HTMX-powered)
# ──────────────────────────────────────────────

def game_comments(request, game_id):
    """Return a page of top-level comments (for HTMX pagination)."""
    game = get_object_or_404(Game, pk=game_id)
    replies_qs = Comment.objects.select_related("user").order_by("created_at")
    top_comments = (
        game.comments
        .filter(parent__isnull=True)
        .select_related("user")
        .prefetch_related(
            Prefetch("replies", queryset=replies_qs),
            Prefetch("replies__replies", queryset=replies_qs),
            Prefetch("replies__replies__replies", queryset=replies_qs),
        )
        .order_by("-created_at")
    )
    paginator = Paginator(top_comments, 20)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    comment_count = game.comments.count()
    return render(request, "games/partials/comment_list.html", {
        "page_obj": page_obj,
        "game": game,
        "comment_count": comment_count,
    })


@login_required
def add_comment(request, game_id):
    """Create a comment or reply (POST, returns HTMX partial)."""
    if request.method != "POST":
        return HttpResponse(status=405)

    game = get_object_or_404(Game, pk=game_id)
    content = request.POST.get("content", "").strip()
    parent_id = request.POST.get("parent_id")

    if not content:
        return HttpResponse(
            '<p class="text-red-400 text-sm py-1">Comment cannot be empty.</p>',
            status=422,
        )
    if len(content) > 2000:
        return HttpResponse(
            '<p class="text-red-400 text-sm py-1">Comment too long (max 2 000 characters).</p>',
            status=422,
        )

    parent = None
    depth = 0
    if parent_id:
        parent = Comment.objects.filter(pk=parent_id, game=game).first()
        if parent:
            # Walk up to compute visual depth (capped at 4)
            depth = 1
            p = parent
            while p.parent_id and depth < 4:
                p = Comment.objects.get(pk=p.parent_id)
                depth += 1

    comment = Comment.objects.create(
        user=request.user,
        game=game,
        parent=parent,
        content=content,
    )

    comment_count = game.comments.count()

    html = render_to_string(
        "games/partials/comment.html",
        {"comment": comment, "game": game, "depth": depth, "user": request.user},
        request=request,
    )
    # Out-of-band swap to update the visible comment count
    html += f'\n<span id="comment-count" hx-swap-oob="true">{comment_count}</span>'
    return HttpResponse(html)

import requests
from django.views.decorators.csrf import csrf_exempt

@csrf_exempt
def chess_mcvs_move(request):
    """YOUR ChessMCVS Space API"""
    fen = request.GET.get('fen')
    if not fen:
        return JsonResponse({'error': 'Missing FEN'}, status=400)
    
    # YOUR Space URL
    space_url = "https://typical-cyber-typical-cyber.hf.space"
    
    try:
        resp = requests.get(space_url, params={'FEN': fen}, timeout=10)
        uci_move = resp.text.strip()
        
        # Validate with python-chess
        board = chess.Board(fen)
        board.push_uci(uci_move)
        
        return JsonResponse({
            'move': uci_move,
            'new_fen': board.fen(),
            'success': True
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

# Replace AI move logic in game_detail/create_game etc.
def ai_make_move(game):
    """Call YOUR ChessMCVS for game AI"""
    fen = game.current_fen or chess.STARTING_FEN
    response = chess_mcvs_move({'GET': {'fen': fen}})
    if response.status_code == 200:
        data = response.json()
        game.move_list.append(data['move'])
        game.current_fen = data['new_fen']
        game.save()
        return data['move']
    return None
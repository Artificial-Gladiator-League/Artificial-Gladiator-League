import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import redirect, render

from apps.games.models import Game
from apps.tournaments.models import Badge, GauntletStanding, Tournament
from apps.users.models import CustomUser

log = logging.getLogger(__name__)

# ── Category ELO boundaries (match CustomUser.get_category) ──
CATEGORY_FILTERS = {
    "global":    {},
    "super_gm":  {"elo__gte": 2700},
    "gm":        {"elo__gte": 2500, "elo__lt": 2700},
    "im":        {"elo__gte": 2400, "elo__lt": 2500},
    "fm":        {"elo__gte": 2300, "elo__lt": 2400},
    "cm":        {"elo__gte": 2200, "elo__lt": 2300},
    "expert":    {"elo__gte": 2000, "elo__lt": 2200},
    "class_a":   {"elo__gte": 1800, "elo__lt": 2000},
    "class_b":   {"elo__gte": 1600, "elo__lt": 1800},
    "class_c":   {"elo__gte": 1400, "elo__lt": 1600},
    "class_d":   {"elo__gte": 1200, "elo__lt": 1400},
    "beginner":  {"elo__lt": 1200},
}

# Per-game ELO filters — used when a game_type is selected on the leaderboard.
GAME_ELO_FIELD = {
    "chess":        "elo_chess",
    "breakthrough": "elo_breakthrough",
}


def _category_for_elo(elo: int) -> dict:
    """Return a category dict for a given ELO value (matches CustomUser.get_category)."""
    if elo >= 2700:
        return {"tier": "Super Grandmaster", "icon": "\U0001f451", "css": "text-yellow-300"}
    elif elo >= 2500:
        return {"tier": "Grandmaster",       "icon": "\U0001f3c6", "css": "text-purple-400"}
    elif elo >= 2400:
        return {"tier": "International Master", "icon": "\U0001f947", "css": "text-yellow-400"}
    elif elo >= 2300:
        return {"tier": "FIDE Master",       "icon": "\U0001f948", "css": "text-blue-300"}
    elif elo >= 2200:
        return {"tier": "Candidate Master",  "icon": "\U0001f948", "css": "text-gray-300"}
    elif elo >= 2000:
        return {"tier": "Expert",            "icon": "\U0001f947", "css": "text-orange-400"}
    elif elo >= 1800:
        return {"tier": "Class A",           "icon": "\U0001f534", "css": "text-red-400"}
    elif elo >= 1600:
        return {"tier": "Class B",           "icon": "\U0001f7e0", "css": "text-orange-300"}
    elif elo >= 1400:
        return {"tier": "Class C",           "icon": "\U0001f7e1", "css": "text-yellow-500"}
    elif elo >= 1200:
        return {"tier": "Class D",           "icon": "\U0001f7e2", "css": "text-green-400"}
    else:
        return {"tier": "Beginner",          "icon": "\U0001f949", "css": "text-amber-600"}

CATEGORY_META = {
    "global":   {"label": "Global",              "icon": "🌍", "css": "text-brand",       "tier": "",                     "elo": "All"},
    "super_gm": {"label": "Super Grandmaster",  "icon": "👑", "css": "text-yellow-300",  "tier": "Super Grandmaster",   "elo": "2700+"},
    "gm":       {"label": "Grandmaster",        "icon": "🏆", "css": "text-purple-400",  "tier": "Grandmaster",         "elo": "2500–2699"},
    "im":       {"label": "International Master","icon": "🥇", "css": "text-yellow-400", "tier": "International Master", "elo": "2400–2499"},
    "fm":       {"label": "FIDE Master",        "icon": "🥈", "css": "text-blue-300",    "tier": "FIDE Master",         "elo": "2300–2399"},
    "cm":       {"label": "Candidate Master",   "icon": "🥈", "css": "text-gray-300",    "tier": "Candidate Master",    "elo": "2200–2299"},
    "expert":   {"label": "Expert",             "icon": "🥇", "css": "text-orange-400",  "tier": "Expert",              "elo": "2000–2199"},
    "class_a":  {"label": "Class A",            "icon": "🔴", "css": "text-red-400",     "tier": "Class A",             "elo": "1800–1999"},
    "class_b":  {"label": "Class B",            "icon": "🟠", "css": "text-orange-300",  "tier": "Class B",             "elo": "1600–1799"},
    "class_c":  {"label": "Class C",            "icon": "🟡", "css": "text-yellow-500",  "tier": "Class C",             "elo": "1400–1599"},
    "class_d":  {"label": "Class D",            "icon": "🟢", "css": "text-green-400",   "tier": "Class D",             "elo": "1200–1399"},
    "beginner": {"label": "Beginner",           "icon": "🥉", "css": "text-amber-600",   "tier": "Beginner",            "elo": "< 1200"},
}

LEADERBOARD_LIMIT = 100


def _ranked_qs(tab: str, game_type: str = "chess"):
    """Return a queryset filtered and ordered for the given tab and game type."""
    elo_field = GAME_ELO_FIELD.get(game_type, "elo_chess")
    # Remap legacy category filters to use the correct per-game ELO field.
    raw_filt = CATEGORY_FILTERS.get(tab, {})
    filt = {}
    for k, v in raw_filt.items():
        new_key = k.replace("elo", elo_field, 1)
        filt[new_key] = v
    return (
        CustomUser.objects
        .filter(**filt, is_active=True, total_games__gt=0)
        .order_by(f"-{elo_field}", "-wins", "username")[:LEADERBOARD_LIMIT]
    )


def _category_counts():
    """Return player counts per category for the tab badges."""
    base = CustomUser.objects.filter(is_active=True, total_games__gt=0)
    return {
        key: base.filter(**filt).count() if filt else base.count()
        for key, filt in CATEGORY_FILTERS.items()
    }


def home(request):
    # ── Live counters ───────────────────────────
    ongoing_games = Game.objects.filter(status=Game.Status.ONGOING).count()
    live_gladiators_count = ongoing_games * 2
    active_tournaments_count = Tournament.objects.filter(
        status__in=[Tournament.Status.OPEN, Tournament.Status.ONGOING]
    ).count()
    total_players = CustomUser.objects.filter(is_active=True, total_games__gt=0).count()
    total_games = Game.objects.count()

    # ── Latest live game for hero preview ───────
    latest_game = (
        Game.objects.filter(status=Game.Status.ONGOING)
        .select_related("white", "black")
        .first()
    )
    latest_game_id = latest_game.pk if latest_game else None
    latest_game_tc = latest_game.time_control if latest_game else "3+1"
    latest_game_white = latest_game.white.ai_name if latest_game and latest_game.white else "—"
    latest_game_black = latest_game.black.ai_name if latest_game and latest_game.black else "—"
    latest_game_white_elo = latest_game.white.elo if latest_game and latest_game.white else 1200
    latest_game_black_elo = latest_game.black.elo if latest_game and latest_game.black else 1200

    # ── Upcoming / active tournaments ───────────
    upcoming_tournaments = Tournament.objects.filter(
        status__in=[Tournament.Status.OPEN, Tournament.Status.ONGOING, Tournament.Status.FULL]
    ).order_by("start_time")[:6]

    joined_ids = []
    if request.user.is_authenticated:
        joined_ids = list(
            request.user.tournament_entries
            .values_list("tournament_id", flat=True)
        )

    # ── Top players for leaderboard preview ─────
    top_players = (
        CustomUser.objects
        .filter(is_active=True, total_games__gt=0)
        .order_by("-elo", "-wins")[:10]
    )
    highest_elo = top_players[0].elo if top_players else 1200

    # ── Recent completed games ──────────────────
    recent_games = (
        Game.objects
        .filter(
            status__in=[
                Game.Status.WHITE_WINS,
                Game.Status.BLACK_WINS,
                Game.Status.DRAW,
            ]
        )
        .select_related("white", "black", "winner")
        .order_by("-timestamp")[:10]
    )

    # ── Gauntlet Hall of Fame ────────────────────
    gauntlet_champions = (
        Tournament.objects
        .filter(
            type=Tournament.Type.GAUNTLET,
            status=Tournament.Status.COMPLETED,
            champion__isnull=False,
        )
        .select_related("champion")
        .order_by("-week_number")[:3]
    )

    # ── Community feed (latest events) ──────────
    community_feed = []
    for g in recent_games[:5]:
        result_text = "drew" if g.result == Game.Result.DRAW else "won against"
        if g.result == Game.Result.DRAW:
            w_name = g.white.ai_name if g.white else "?"
            b_name = g.black.ai_name if g.black else "?"
            text = f"{w_name} drew {b_name}"
        else:
            winner_name = g.winner.ai_name if g.winner else "?"
            loser = g.black if g.winner == g.white else g.white
            loser_name = loser.ai_name if loser else "?"
            text = f"{winner_name} defeated {loser_name}"
        community_feed.append({
            "icon": "⚔️",
            "text": text,
            "time": g.timestamp,
        })

    return render(request, "core/home.html", {
        "live_gladiators_count": live_gladiators_count,
        "active_tournaments_count": active_tournaments_count,
        "total_players": total_players,
        "total_games": total_games,
        "latest_game_id": latest_game_id,
        "latest_game_tc": latest_game_tc,
        "latest_game_white": latest_game_white,
        "latest_game_black": latest_game_black,
        "latest_game_white_elo": latest_game_white_elo,
        "latest_game_black_elo": latest_game_black_elo,
        "upcoming_tournaments": upcoming_tournaments,
        "joined_ids": joined_ids,
        "top_players": top_players,
        "highest_elo": highest_elo,
        "recent_games": recent_games,
        "community_feed": community_feed,
        "gauntlet_champions": gauntlet_champions,
    })


def privacy(request):
    return render(request, "core/privacy.html")


def about(request):
    return render(request, "core/about.html")


def terms(request):
    return render(request, "core/terms.html")


def accessibility(request):
    return render(request, "core/accessibility.html")


def cookies(request):
    return render(request, "core/cookies.html")


def how_to_upload(request):
    return render(request, "core/how_to_upload.html")

def how_it_works(request):
    return render(request, "core/how_it_works.html")

def _per_game_stats(player_ids, game_type):
    """Return per-game W/L/D/total/streak keyed by player PK for the given game_type."""
    terminal = [
        Game.Status.WHITE_WINS, Game.Status.BLACK_WINS, Game.Status.DRAW,
        Game.Status.TIMEOUT_WHITE, Game.Status.TIMEOUT_BLACK,
    ]
    wins_map = dict(
        Game.objects
        .filter(winner_id__in=player_ids, game_type=game_type)
        .values('winner_id')
        .annotate(c=Count('pk'))
        .values_list('winner_id', 'c')
    )
    draws_as_white = dict(
        Game.objects
        .filter(white_id__in=player_ids, game_type=game_type, status=Game.Status.DRAW)
        .values('white_id')
        .annotate(c=Count('pk'))
        .values_list('white_id', 'c')
    )
    draws_as_black = dict(
        Game.objects
        .filter(black_id__in=player_ids, game_type=game_type, status=Game.Status.DRAW)
        .values('black_id')
        .annotate(c=Count('pk'))
        .values_list('black_id', 'c')
    )
    played_as_white = dict(
        Game.objects
        .filter(white_id__in=player_ids, game_type=game_type, status__in=terminal)
        .values('white_id')
        .annotate(c=Count('pk'))
        .values_list('white_id', 'c')
    )
    played_as_black = dict(
        Game.objects
        .filter(black_id__in=player_ids, game_type=game_type, status__in=terminal)
        .values('black_id')
        .annotate(c=Count('pk'))
        .values_list('black_id', 'c')
    )

    # Compute per-game streak: fetch each player's recent terminal games for
    # this game type, ordered newest-first, then walk until the outcome changes.
    win_statuses = {Game.Status.WHITE_WINS, Game.Status.BLACK_WINS, Game.Status.TIMEOUT_BLACK, Game.Status.TIMEOUT_WHITE}
    draw_statuses = {Game.Status.DRAW}
    recent_games = (
        Game.objects
        .filter(
            game_type=game_type,
            status__in=terminal,
        )
        .filter(Q(white_id__in=player_ids) | Q(black_id__in=player_ids))
        .order_by('-last_move_at', '-timestamp')
        .values('pk', 'white_id', 'black_id', 'winner_id', 'status')
    )
    # Group by player
    from collections import defaultdict
    player_games = defaultdict(list)
    for g in recent_games:
        for side in ('white_id', 'black_id'):
            uid = g[side]
            if uid in player_ids:
                player_games[uid].append(g)

    def _streak(uid, games):
        streak = 0
        last = None  # 'win', 'draw', 'loss'
        for g in games:
            if g['status'] in win_statuses:
                outcome = 'win' if g['winner_id'] == uid else 'loss'
            else:
                outcome = 'draw'
            if last is None:
                last = outcome
            if outcome != last:
                break
            if outcome == 'win':
                streak = (streak + 1) if streak >= 0 else 1
            elif outcome == 'loss':
                streak = (streak - 1) if streak <= 0 else -1
            # draws reset / hold at 0
        return streak

    result = {}
    for uid in player_ids:
        wins = wins_map.get(uid, 0)
        draws = draws_as_white.get(uid, 0) + draws_as_black.get(uid, 0)
        played = played_as_white.get(uid, 0) + played_as_black.get(uid, 0)
        losses = max(0, played - wins - draws)
        result[uid] = {
            'wins': wins,
            'losses': losses,
            'draws': draws,
            'total_games': played,
            'streak': _streak(uid, player_games.get(uid, [])),
        }
    return result


def leaderboard(request):
    tab = request.GET.get("tab", "global")
    if tab not in CATEGORY_FILTERS:
        tab = "global"
    game_type = request.GET.get("game_type", "chess")
    if game_type not in GAME_ELO_FIELD:
        game_type = "chess"

    players = list(_ranked_qs(tab, game_type))
    stats_map = _per_game_stats([p.pk for p in players], game_type)
    elo_field = GAME_ELO_FIELD[game_type]
    for p in players:
        st = stats_map.get(p.pk, {})
        p.wins = st.get('wins', p.wins)
        p.losses = st.get('losses', p.losses)
        p.draws = st.get('draws', p.draws)
        p.total_games = st.get('total_games', p.total_games)
        p.current_streak = st.get('streak', p.current_streak)
        p.display_category = _category_for_elo(getattr(p, elo_field))
    counts = _category_counts()

    return render(request, "core/leaderboard.html", {
        "players": players,
        "active_tab": tab,
        "active_game_type": game_type,
        "tabs": CATEGORY_META,
        "counts": counts,
    })


def leaderboard_json(request):
    """Lightweight JSON endpoint for real‑time WS/AJAX refresh."""
    tab = request.GET.get("tab", "global")
    if tab not in CATEGORY_FILTERS:
        tab = "global"
    game_type = request.GET.get("game_type", "chess")
    if game_type not in GAME_ELO_FIELD:
        game_type = "chess"
    elo_field = GAME_ELO_FIELD[game_type]

    players = list(_ranked_qs(tab, game_type))
    stats_map = _per_game_stats([p.pk for p in players], game_type)
    rows = []
    for rank, p in enumerate(players, 1):
        cat = _category_for_elo(getattr(p, elo_field))
        st = stats_map.get(p.pk, {})
        wins = st.get('wins', p.wins)
        losses = st.get('losses', p.losses)
        draws = st.get('draws', p.draws)
        played = st.get('total_games', p.total_games)
        rows.append({
            "rank": rank,
            "username": p.username,
            "flag": p.country_flag,
            "ai_name": p.ai_name or "—",
            "elo": getattr(p, elo_field),
            "elo_chess": p.elo_chess,
            "elo_breakthrough": p.elo_breakthrough,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "win_pct": round(wins / played * 100) if played else 0,
            "streak": st.get('streak', p.current_streak),
            "tier_icon": cat["icon"],
            "tier_label": cat["tier"],
            "tier_css": cat["css"],
        })

    return JsonResponse({"tab": tab, "game_type": game_type, "rows": rows, "counts": _category_counts()})


def presence_view(request):
    """Simple HTTP endpoint for the lobby online-count badge.

    Returns the number of distinct authenticated users who have had an active
    session in the last 15 minutes.  Falls back to 1 when the cache/DB is
    unavailable so the badge never shows 0 erroneously.
    """
    from django.contrib.sessions.models import Session
    from django.utils import timezone
    import datetime

    try:
        cutoff = timezone.now() - datetime.timedelta(minutes=15)
        count = Session.objects.filter(expire_date__gte=cutoff).count()
        count = max(count, 1)
    except Exception:
        count = 1

    return JsonResponse({"type": "online_count", "count": count})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  "Test My Space" self-service verification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _probe_space(repo_id: str, game_type: str) -> tuple[str, int | None, str]:
    """Probe a user's HF Space ``get_move`` endpoint with a starting position.

    Reuses the exact Space-URL resolution, 20-second stream timeout, shared
    HTTP session, Gradio endpoint name and move-validation used by the live
    match engine (apps.games.predict_chess / predict_breakthrough). The only
    difference here is that outcomes are classified granularly —
    ok / cold_start / unreachable / bad_response — instead of silently
    falling back to a random move.

    Returns ``(status, latency_ms, trace)``; ``latency_ms`` is ``None`` when
    the Space did not return a response and ``trace`` is a short diagnostic
    string (empty on success) to help the user debug their Space.
    """
    import json as _json
    import time as _time

    import requests

    if game_type == "breakthrough":
        from apps.games import predict_breakthrough as _pb
        from apps.games.breakthrough_engine import STARTING_FEN, is_legal_move

        base = _pb._space_base_url(repo_id).rstrip("/")
        session = _pb._session
        submit_timeout = _pb._SUBMIT_TIMEOUT
        stream_timeout = _pb._STREAM_TIMEOUT
        gradio_fn = _pb._GRADIO_FN
        fen, player = STARTING_FEN, "w"
        payload = {"data": [fen, player]}

        def _legal(mv: str) -> bool:
            return is_legal_move(fen, mv)
    else:
        import chess

        from apps.games import predict_chess as _pc
        from apps.games.chess_engine import is_legal_move

        base = _pc._space_url_for(repo_id).rstrip("/")
        session = _pc._session
        submit_timeout = _pc._SUBMIT_TIMEOUT
        stream_timeout = _pc._STREAM_TIMEOUT
        gradio_fn = _pc._GRADIO_FN
        board = chess.Board()
        fen = board.fen()
        payload = {"data": [fen]}

        def _legal(mv: str) -> bool:
            return is_legal_move(board, mv)

    submit_url = f"{base}/gradio_api/call/{gradio_fn}"
    headers = {"Content-Type": "application/json"}
    t0 = _time.monotonic()

    try:
        # Step 1 — submit
        resp = session.post(submit_url, json=payload, headers=headers, timeout=submit_timeout)
        resp.raise_for_status()
        if "application/json" not in resp.headers.get("content-type", ""):
            # Non-JSON response = Space is asleep/booting.
            return "cold_start", None, f"POST {submit_url} → HTTP {resp.status_code}: non-JSON response (Space is asleep/booting)"
        event_id = resp.json().get("event_id")
        if not event_id:
            return "cold_start", None, f"POST {submit_url} → HTTP {resp.status_code}: no event_id returned (Space not ready)"

        # Step 2 — stream result (SSE)
        result_url = f"{base}/gradio_api/call/{gradio_fn}/{event_id}"
        stream = session.get(result_url, stream=True, timeout=stream_timeout)
        stream.raise_for_status()

        move_str: str | None = None
        complete_seen = False
        error_seen = False
        for raw_line in stream.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            if line.startswith("event: error"):
                error_seen = True
            elif line.startswith("event: complete"):
                complete_seen = True
            elif line.startswith("data:"):
                try:
                    data = _json.loads(line[len("data:"):].strip())
                    if isinstance(data, list) and data:
                        move_str = str(data[0]).strip()
                except Exception:
                    pass
                if complete_seen or error_seen:
                    break

        latency_ms = int((_time.monotonic() - t0) * 1000)
        if move_str and _legal(move_str):
            return "ok", latency_ms, ""
        if move_str:
            return "bad_response", latency_ms, f"Space returned move '{move_str}', which is not legal for the starting position"
        return "bad_response", latency_ms, "Space stream returned no move (empty or invalid SSE data)"
    except requests.exceptions.RequestException as exc:
        # Timeouts and connection errors alike mean the Space is unreachable.
        log.warning("verify_space probe failed (repo=%s): %s", repo_id, exc)
        return "unreachable", None, f"{type(exc).__name__}: {exc} (url={base})"
    except Exception as exc:
        log.exception("Unexpected error probing Space (repo=%s)", repo_id)
        return "unreachable", None, f"{type(exc).__name__}: {exc}"


@login_required
def verify_space(request):
    """Self-service 'Test My Space' check.

    Sends the starting position for the user's default game type to their
    Space's ``get_move`` endpoint (the same way the match engine calls it),
    measures latency, validates the returned move, and stores the result on
    the user. Never crashes the request on network errors — failures are
    caught and reported as an ``unreachable`` status (mirrors presence_view).
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    from django.utils import timezone

    user = request.user
    game_type = request.POST.get("game_type", "chess").strip().lower()
    if game_type not in ("chess", "breakthrough"):
        game_type = "chess"

    repo_id = user.get_repo_for_game(game_type)
    if not repo_id:
        return JsonResponse({
            "status": "unverified",
            "latency_ms": None,
            "message": f"No {game_type} model registered yet.",
            "verified_at": None,
            "trace": None,
        })

    status, latency_ms, trace = _probe_space(repo_id, game_type)

    user.space_status = status
    user.space_last_latency_ms = latency_ms
    if status == "ok":
        user.space_last_verified_at = timezone.now()
    try:
        user.save(update_fields=[
            "space_status", "space_last_latency_ms", "space_last_verified_at",
        ])
    except Exception:
        log.exception("Failed to save Space status for user=%s", user.pk)

    messages_map = {
        "ok": "Your Space responded with a legal move.",
        "cold_start": "Your Space is waking up — try again in a few seconds.",
        "unreachable": "Your Space did not respond in time.",
        "bad_response": "Your Space responded, but the move was invalid.",
    }

    return JsonResponse({
        "status": status,
        "latency_ms": latency_ms,
        "message": messages_map.get(status, "Unknown status."),
        "verified_at": (
            user.space_last_verified_at.isoformat()
            if user.space_last_verified_at else None
        ),
        "trace": trace or None,
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Upload AI Wizard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@login_required
def upload_ai(request):
    """Render the Upload-AI wizard page."""
    return render(request, "core/upload.html")


@login_required
def upload_ai_submit(request):
    """Handle the Upload-AI form submission (HF repo ID)."""
    if request.method != "POST":
        return redirect("core:upload_ai")

    ai_name = request.POST.get("ai_name", "").strip()
    hf_model_repo_id = request.POST.get("hf_model_repo_id", "").strip()

    if not ai_name:
        messages.error(request, "AI Gladiator name is required.")
        return redirect("core:upload_ai")

    if not hf_model_repo_id:
        messages.error(request, "Please enter a Hugging Face repo ID.")
        return redirect("core:upload_ai")

    # Save to the user profile
    user = request.user
    user.ai_name = ai_name
    user.hf_model_repo_id = hf_model_repo_id
    user.save()

    messages.success(request, f"🎉 '{ai_name}' deployed! Your gladiator is ready for battle.")
    return redirect("users:profile")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Contact
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def contact(request):
    return render(request, "core/contact.html")

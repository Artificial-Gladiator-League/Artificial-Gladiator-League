from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render

from apps.games.models import Game
from apps.tournaments.models import Badge, GauntletStanding, Tournament
from apps.users.models import CustomUser

# ── Category ELO boundaries (match CustomUser.get_category) ──
CATEGORY_FILTERS = {
    "global":       {},
    "beginner":     {"elo__lte": 1200},
    "intermediate": {"elo__gt": 1200, "elo__lte": 1600},
    "advanced":     {"elo__gt": 1600, "elo__lte": 2000},
    "expert":       {"elo__gt": 2000},
}

CATEGORY_META = {
    "global":       {"label": "Global",       "icon": "🌍", "css": "text-brand"},
    "beginner":     {"label": "Beginner",     "icon": "🥉", "css": "text-amber-600",  "tier": "Novice",      "elo": "≤ 1200"},
    "intermediate": {"label": "Intermediate", "icon": "🥈", "css": "text-gray-300",   "tier": "Club Player", "elo": "1201–1600"},
    "advanced":     {"label": "Advanced",     "icon": "🥇", "css": "text-yellow-400", "tier": "Expert",      "elo": "1601–2000"},
    "expert":       {"label": "Expert",       "icon": "🏆", "css": "text-purple-400", "tier": "Master",      "elo": "2001+"},
}

LEADERBOARD_LIMIT = 100


def _ranked_qs(tab: str):
    """Return a queryset filtered and ordered for the given tab."""
    filt = CATEGORY_FILTERS.get(tab, {})
    return (
        CustomUser.objects
        .filter(**filt, is_active=True, total_games__gt=0)
        .order_by("-elo", "-wins", "username")[:LEADERBOARD_LIMIT]
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


def leaderboard(request):
    tab = request.GET.get("tab", "global")
    if tab not in CATEGORY_FILTERS:
        tab = "global"

    players = _ranked_qs(tab)
    counts = _category_counts()

    return render(request, "core/leaderboard.html", {
        "players": players,
        "active_tab": tab,
        "tabs": CATEGORY_META,
        "counts": counts,
    })


def leaderboard_json(request):
    """Lightweight JSON endpoint for real‑time WS/AJAX refresh."""
    tab = request.GET.get("tab", "global")
    if tab not in CATEGORY_FILTERS:
        tab = "global"

    players = _ranked_qs(tab)
    rows = []
    for rank, p in enumerate(players, 1):
        cat = p.get_category()
        rows.append({
            "rank": rank,
            "username": p.username,
            "flag": p.country_flag,
            "ai_name": p.ai_name or "—",
            "elo": p.elo,
            "wins": p.wins,
            "losses": p.losses,
            "draws": p.draws,
            "win_pct": round(p.win_rate * 100),
            "streak": p.current_streak,
            "tier_icon": cat["icon"],
            "tier_label": cat["tier"],
            "tier_css": cat["css"],
        })

    return JsonResponse({"tab": tab, "rows": rows, "counts": _category_counts()})


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

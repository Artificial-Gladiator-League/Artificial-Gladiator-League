# ──────────────────────────────────────────────
# apps/games/engine.py
#
# Pure python-chess logic: move validation, game
# outcome detection, Armageddon creation, clock
# management, and ELO calculation.
#
# All functions operate on Game model instances
# or raw chess.Board objects — no Django ORM
# queries, so they stay testable in isolation.
# ──────────────────────────────────────────────
from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

import chess
import chess.pgn

if TYPE_CHECKING:
    from apps.games.models import Game
    from apps.users.models import CustomUser


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Move validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def is_legal_move(board: chess.Board, uci: str) -> bool:
    """Check whether a UCI string is a legal move on the given board."""
    try:
        move = chess.Move.from_uci(uci)
    except (ValueError, chess.InvalidMoveError):
        return False
    return move in board.legal_moves


def make_move(game: Game, uci: str) -> tuple[bool, str]:
    """Validate and apply a move to a Game instance.

    Returns:
        (True, "")  on success
        (False, error_message)  on failure
    The caller is responsible for calling game.save() afterwards.
    """
    if game.is_finished:
        return False, "Game is already finished."

    board = game.board()

    if not is_legal_move(board, uci):
        return False, f"Illegal move: {uci}"

    move = chess.Move.from_uci(uci)
    san = board.san(move)
    board.push(move)

    # Update model fields
    game.current_fen = board.fen()
    game.move_list.append(uci)
    game.pgn = _build_pgn(game, board)

    # Check for game-ending conditions
    outcome = get_game_outcome(board)
    if outcome:
        _apply_outcome(game, outcome)

    return True, ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Outcome detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_game_outcome(board: chess.Board) -> str | None:
    """Return a reason string if the game is over, else None.

    Possible return values:
        'checkmate', 'stalemate', 'insufficient_material',
        'seventyfive_moves', 'fifty_moves', 'repetition'
    """
    if board.is_checkmate():
        return "checkmate"
    if board.is_stalemate():
        return "stalemate"
    if board.is_insufficient_material():
        return "insufficient_material"
    if board.is_seventyfive_moves():
        return "seventyfive_moves"
    if board.is_fivefold_repetition():
        return "repetition"
    # Claimable draws (auto-claim on server for simplicity)
    if board.can_claim_fifty_moves():
        return "fifty_moves"
    if board.can_claim_threefold_repetition():
        return "repetition"
    return None


def _apply_outcome(game: Game, reason: str) -> None:
    """Set game status, result, and winner from a detected outcome reason."""
    from apps.games.models import Game as GameModel

    game.result_reason = reason
    board = game.board()

    if reason == "checkmate":
        # The side that just moved delivered checkmate
        if board.turn == chess.BLACK:
            # White just moved and checkmated Black
            game.status = GameModel.Status.WHITE_WINS
            game.result = GameModel.Result.WHITE_WIN
            game.winner = game.white
        else:
            game.status = GameModel.Status.BLACK_WINS
            game.result = GameModel.Result.BLACK_WIN
            game.winner = game.black
    else:
        # All other reasons are draws
        game.status = GameModel.Status.DRAW
        game.result = GameModel.Result.DRAW
        game.winner = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Clock / increment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def apply_increment(game: Game, color: chess.Color) -> None:
    """Add the per-move increment to the specified side's clock.

    Call this AFTER the move is made but BEFORE saving.
    """
    if color == chess.WHITE:
        game.white_time += game.increment
    else:
        game.black_time += game.increment


def apply_time_spent(game: Game, color: chess.Color, elapsed: float) -> None:
    """Subtract elapsed seconds from the specified side's clock.

    If the clock reaches ≤ 0, set the game as a timeout loss.
    """
    from apps.games.models import Game as GameModel

    if color == chess.WHITE:
        game.white_time = max(0.0, game.white_time - elapsed)
        if game.white_time <= 0:
            game.status = GameModel.Status.TIMEOUT_WHITE
            game.result = GameModel.Result.BLACK_WIN
            game.result_reason = "timeout"
            game.winner = game.black
    else:
        game.black_time = max(0.0, game.black_time - elapsed)
        if game.black_time <= 0:
            game.status = GameModel.Status.TIMEOUT_BLACK
            game.result = GameModel.Result.WHITE_WIN
            game.result_reason = "timeout"
            game.winner = game.white


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Armageddon
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def create_armageddon(drawn_game: Game) -> Game:
    """Create an Armageddon tiebreak game from a drawn parent.

    Rules:
        • Colours assigned randomly.
        • White gets 2 minutes, Black gets 1 minute.
        • No increment.
        • If Armageddon draws → Black wins (draw odds).

    Returns the newly created (unsaved) Armageddon Game instance.
    """
    from apps.games.models import Game as GameModel

    players = [drawn_game.white, drawn_game.black]
    random.shuffle(players)
    arm_white, arm_black = players

    arm = GameModel(
        white=arm_white,
        black=arm_black,
        time_control="2+0",
        white_time=120.0,   # 2 minutes
        black_time=60.0,    # 1 minute
        increment=0,
        status=GameModel.Status.ONGOING,
        is_tournament_game=drawn_game.is_tournament_game,
        tournament_match=drawn_game.tournament_match,
        armageddon_of=drawn_game,
    )
    return arm


def resolve_armageddon_draw(game: Game) -> None:
    """If an Armageddon game ends in a draw, award the win to Black (draw odds)."""
    from apps.games.models import Game as GameModel

    if game.armageddon_of is None:
        return  # not an Armageddon game
    if game.result != GameModel.Result.DRAW:
        return  # decisive result, nothing to override

    game.status = GameModel.Status.BLACK_WINS
    game.result = GameModel.Result.BLACK_WIN
    game.result_reason = "armageddon_draw_odds"
    game.winner = game.black


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ELO calculation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROVISIONAL_THRESHOLD = 30   # first N games use higher K
K_STANDARD = 32
K_PROVISIONAL = 40


def _k_factor(player: CustomUser) -> int:
    """Higher K-factor for provisional (new) players."""
    if player.total_games < PROVISIONAL_THRESHOLD:
        return K_PROVISIONAL
    return K_STANDARD


def _expected_score(ra: int, rb: int) -> float:
    return 1.0 / (1.0 + math.pow(10, (rb - ra) / 400.0))


def compute_elo_deltas(
    winner: CustomUser | None,
    loser: CustomUser | None,
    white: CustomUser,
    black: CustomUser,
    result: str,
    white_elo: int | None = None,
    black_elo: int | None = None,
) -> tuple[int, int]:
    """Compute ELO deltas for both players.

    *white_elo* and *black_elo* can be passed explicitly to use a
    per-game ELO (e.g. elo_chess / elo_breakthrough) instead of the
    combined legacy ``user.elo`` field.

    Returns (delta_white, delta_black).
    """
    if result == "1-0":
        s_w, s_b = 1.0, 0.0
    elif result == "0-1":
        s_w, s_b = 0.0, 1.0
    elif result == "1/2-1/2":
        s_w, s_b = 0.5, 0.5
    else:
        return 0, 0

    elo_w = white_elo if white_elo is not None else white.elo
    elo_b = black_elo if black_elo is not None else black.elo

    e_w = _expected_score(elo_w, elo_b)
    e_b = _expected_score(elo_b, elo_w)

    k_w = _k_factor(white)
    k_b = _k_factor(black)

    delta_w = round(k_w * (s_w - e_w))
    delta_b = round(k_b * (s_b - e_b))
    return delta_w, delta_b


def update_elo(
    white: CustomUser,
    black: CustomUser,
    result: str,
) -> tuple[int, int]:
    """Compute and apply ELO changes to both users (does NOT save).

    Returns (delta_white, delta_black).
    """
    delta_w, delta_b = compute_elo_deltas(
        winner=None, loser=None,
        white=white, black=black,
        result=result,
    )
    white.elo += delta_w
    black.elo += delta_b
    return delta_w, delta_b


def update_player_stats(game: Game) -> None:
    """Update wins/losses/draws/total_games/current_streak on both players.

    Also computes and applies ELO changes, persists both users.
    Call this exactly once when a game reaches a terminal state.
    """
    from apps.games.models import Game as GameModel

    white = game.white
    black = game.black
    if white is None or black is None:
        return

    # ELO deltas
    delta_w, delta_b = update_elo(white, black, game.result)
    game.elo_change_white = delta_w
    game.elo_change_black = delta_b

    # Stat counters
    if game.result == GameModel.Result.WHITE_WIN:
        white.wins += 1
        black.losses += 1
        white.current_streak = max(1, white.current_streak + 1) if white.current_streak >= 0 else 1
        black.current_streak = min(-1, black.current_streak - 1) if black.current_streak <= 0 else -1
    elif game.result == GameModel.Result.BLACK_WIN:
        black.wins += 1
        white.losses += 1
        black.current_streak = max(1, black.current_streak + 1) if black.current_streak >= 0 else 1
        white.current_streak = min(-1, white.current_streak - 1) if white.current_streak <= 0 else -1
    elif game.result == GameModel.Result.DRAW:
        white.draws += 1
        black.draws += 1
        white.current_streak = 0
        black.current_streak = 0

    white.total_games += 1
    black.total_games += 1

    white.save(update_fields=[
        "elo", "wins", "losses", "draws", "total_games", "current_streak",
    ])
    black.save(update_fields=[
        "elo", "wins", "losses", "draws", "total_games", "current_streak",
    ])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PGN builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _build_pgn(game: Game, board: chess.Board) -> str:
    """Rebuild PGN from the move list on the Game."""
    pgn_game = chess.pgn.Game()
    pgn_game.headers["White"] = game.white.username if game.white else "?"
    pgn_game.headers["Black"] = game.black.username if game.black else "?"
    pgn_game.headers["TimeControl"] = game.time_control

    if game.result != "*":
        pgn_game.headers["Result"] = game.result

    node = pgn_game
    replay = chess.Board()
    for uci_str in game.move_list:
        move = chess.Move.from_uci(uci_str)
        node = node.add_variation(move)
        replay.push(move)

    return str(pgn_game)

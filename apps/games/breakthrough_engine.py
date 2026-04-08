# ──────────────────────────────────────────────
# apps/games/breakthrough_engine.py
#
# Pure game logic for Breakthrough — the abstract
# strategy board game played on an 8×8 grid.
#
# Rules:
#   • Each side starts with 16 pieces on their two
#     home ranks (White on ranks 1-2, Black on 7-8).
#   • Pieces move one square forward (straight or
#     diagonally).  Captures are diagonal-only.
#   • A player wins by advancing any piece to the
#     opponent's home rank (rank 8 for White, rank 1
#     for Black) or by capturing all opponent pieces.
#   • There are no draws.
#
# Board encoding:
#   FEN-like string — 8 ranks separated by '/',
#   rank 8 first (same visual order as chess FEN).
#   'W' = White piece, 'B' = Black piece, digits =
#   consecutive empty squares.  A trailing " w" or
#   " b" indicates the side to move.
#
# Move encoding:
#   UCI-style: "<from><to>", e.g. "a2a3", "b2c3".
#
# All functions operate on Game model instances or
# raw board lists — no Django ORM queries, so they
# stay testable in isolation.
# ──────────────────────────────────────────────
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.games.models import Game


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOARD_SIZE = 8

STARTING_FEN = (
    "BBBBBBBB/BBBBBBBB/8/8/8/8/WWWWWWWW/WWWWWWWW w"
)

_FILES = "abcdefgh"
_RANKS = "12345678"

WHITE = "w"
BLACK = "b"
WHITE_PIECE = "W"
BLACK_PIECE = "B"
EMPTY = "."


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Board setup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def initial_board() -> list[list[str]]:
    """Return the starting 8×8 grid for Breakthrough.

    White pieces ('W') fill ranks 1-2 (rows 6-7),
    Black pieces ('B') fill ranks 7-8 (rows 0-1),
    all other squares are empty ('.').
    """
    grid, _ = _fen_to_grid(STARTING_FEN)
    return grid


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Board representation helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _fen_to_grid(fen: str) -> tuple[list[list[str]], str]:
    """Parse a Breakthrough FEN into an 8×8 grid and the side to move.

    Returns:
        (grid, turn)  where grid[row][col] is 'W', 'B', or '.'
        row 0 = rank 8 (top), row 7 = rank 1 (bottom).
    """
    parts = fen.strip().split()
    turn = parts[1] if len(parts) > 1 else WHITE
    ranks = parts[0].split("/")

    grid: list[list[str]] = []
    for rank_str in ranks:
        row: list[str] = []
        for ch in rank_str:
            if ch.isdigit():
                row.extend([EMPTY] * int(ch))
            else:
                row.append(ch)
        grid.append(row)

    return grid, turn


def _grid_to_fen(grid: list[list[str]], turn: str) -> str:
    """Convert an 8×8 grid and side-to-move back to a FEN string."""
    rank_strs: list[str] = []
    for row in grid:
        fen_row = ""
        empty = 0
        for cell in row:
            if cell == EMPTY:
                empty += 1
            else:
                if empty:
                    fen_row += str(empty)
                    empty = 0
                fen_row += cell
        if empty:
            fen_row += str(empty)
        rank_strs.append(fen_row)
    return "/".join(rank_strs) + f" {turn}"


def _sq_to_coords(sq: str) -> tuple[int, int]:
    """Convert a square name like 'a2' to (row, col) indices.

    row 0 = rank 8, row 7 = rank 1.
    """
    file_idx = _FILES.index(sq[0])
    rank_idx = int(sq[1]) - 1          # 0-based rank (0 = rank 1)
    row = BOARD_SIZE - 1 - rank_idx    # flip so row 0 = rank 8
    return row, file_idx


def _coords_to_sq(row: int, col: int) -> str:
    """Convert (row, col) back to a square name."""
    rank_idx = BOARD_SIZE - 1 - row
    return f"{_FILES[col]}{_RANKS[rank_idx]}"


def _opponent_turn(turn: str) -> str:
    return BLACK if turn == WHITE else WHITE


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Legal-move generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _legal_moves_for(grid: list[list[str]], turn: str) -> list[str]:
    """Return all legal UCI moves for the side to move."""
    piece = WHITE_PIECE if turn == WHITE else BLACK_PIECE
    direction = -1 if turn == WHITE else 1   # White moves "up" (row decreases)
    moves: list[str] = []

    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            if grid[r][c] != piece:
                continue
            nr = r + direction
            if nr < 0 or nr >= BOARD_SIZE:
                continue

            # Straight forward — only onto empty square
            if grid[nr][c] == EMPTY:
                moves.append(_coords_to_sq(r, c) + _coords_to_sq(nr, c))

            # Diagonal captures (or moving to empty diagonal)
            for dc in (-1, 1):
                nc = c + dc
                if nc < 0 or nc >= BOARD_SIZE:
                    continue
                target = grid[nr][nc]
                if target == piece:
                    # Can't move onto own piece
                    continue
                moves.append(_coords_to_sq(r, c) + _coords_to_sq(nr, nc))

    return moves


def legal_moves(fen: str) -> list[str]:
    """Public helper — return all legal UCI moves for the position."""
    grid, turn = _fen_to_grid(fen)
    return _legal_moves_for(grid, turn)


def get_legal_moves(board: list[list[str]], player: str) -> list[str]:
    """Return all legal UCI moves for *player* on the given grid.

    *board*  — 8×8 grid (as returned by ``initial_board()``).
    *player* — ``'w'`` (White) or ``'b'`` (Black).
    """
    return _legal_moves_for(board, player)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Win detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_win(board: list[list[str]], player: str) -> str | None:
    """Check whether *player*'s last move produced a winner.

    *board*  — 8×8 grid reflecting the state AFTER *player* moved.
    *player* — ``'w'`` or ``'b'`` — the side that just moved.

    Returns:
        ``'w'``  if White has won,
        ``'b'``  if Black has won,
        ``None`` if the game continues.

    Win conditions (checked in order):
        1. **Breakthrough** — *player* has a piece on the opponent's
           home rank (rank 8 / row 0 for White, rank 1 / row 7 for
           Black).
        2. **Elimination** — the opponent has no pieces left on the
           board.
        3. **No legal moves** — the opponent has no legal moves
           (equivalent to a loss for the stuck side).
    """
    opponent = _opponent_turn(player)
    own_piece = WHITE_PIECE if player == WHITE else BLACK_PIECE
    opp_piece = BLACK_PIECE if player == WHITE else WHITE_PIECE

    # 1. Breakthrough — did the moving side reach the far rank?
    target_row = 0 if player == WHITE else BOARD_SIZE - 1
    if own_piece in board[target_row]:
        return player

    # 2. Elimination — does the opponent have any pieces?
    if not any(cell == opp_piece for row in board for cell in row):
        return player

    # 3. No legal moves — is the opponent stuck?
    if not _legal_moves_for(board, opponent):
        return player

    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Move validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def is_legal_move(fen: str, uci: str) -> bool:
    """Check whether a UCI string is a legal Breakthrough move."""
    if len(uci) != 4:
        return False
    return uci in legal_moves(fen)


def make_move(game: Game, uci: str) -> tuple[bool, str]:
    """Validate and apply a move to a Game instance.

    Returns:
        (True, "")  on success
        (False, error_message)  on failure
    The caller is responsible for calling game.save() afterwards.
    """
    if game.is_finished:
        return False, "Game is already finished."

    fen = game.current_fen
    if not is_legal_move(fen, uci):
        return False, f"Illegal move: {uci}"

    grid, turn = _fen_to_grid(fen)

    fr, fc = _sq_to_coords(uci[:2])
    tr, tc = _sq_to_coords(uci[2:])

    # Execute the move
    grid[tr][tc] = grid[fr][fc]
    grid[fr][fc] = EMPTY

    next_turn = _opponent_turn(turn)
    game.current_fen = _grid_to_fen(grid, next_turn)
    game.move_list.append(uci)

    # Check for game-ending conditions
    outcome = get_game_outcome(grid, turn)
    if outcome:
        _apply_outcome(game, outcome)

    return True, ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Outcome detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_game_outcome(
    grid: list[list[str]], last_mover: str
) -> str | None:
    """Return a reason string if the game is over, else None.

    Called AFTER the move has been applied to the grid.

    Possible return values:
        'breakthrough'        — a piece reached the far rank
        'elimination'         — opponent has no pieces left
        'no_moves'            — opponent has no legal moves
    """
    # 1. Breakthrough — did the moving side reach the opponent's home rank?
    if last_mover == WHITE:
        # White wins by reaching rank 8 (row 0)
        if WHITE_PIECE in grid[0]:
            return "breakthrough"
    else:
        # Black wins by reaching rank 1 (row 7)
        if BLACK_PIECE in grid[7]:
            return "breakthrough"

    # 2. Elimination — did the opponent lose all pieces?
    opponent_piece = BLACK_PIECE if last_mover == WHITE else WHITE_PIECE
    has_opponent_piece = any(
        cell == opponent_piece for row in grid for cell in row
    )
    if not has_opponent_piece:
        return "elimination"

    # 3. No legal moves — opponent is stuck (extremely rare but possible)
    next_turn = _opponent_turn(last_mover)
    if not _legal_moves_for(grid, next_turn):
        return "no_moves"

    return None


def _apply_outcome(game: Game, reason: str) -> None:
    """Set game status, result, and winner from a detected outcome."""
    from apps.games.models import Game as GameModel

    game.result_reason = reason

    # Determine who just moved (the winner in Breakthrough — no draws)
    turn_in_fen = game.current_fen.strip().split()[-1]
    # The FEN already has the NEXT side to move, so the last mover
    # is the opposite of what's in the FEN.
    if turn_in_fen == BLACK:
        # White just moved → White wins
        game.status = GameModel.Status.WHITE_WINS
        game.result = GameModel.Result.WHITE_WIN
        game.winner = game.white
    else:
        # Black just moved → Black wins
        game.status = GameModel.Status.BLACK_WINS
        game.result = GameModel.Result.BLACK_WIN
        game.winner = game.black


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Clock / increment  (shared with chess — same
#  signature so callers can be game-type agnostic)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def apply_increment(game: Game, color: str) -> None:
    """Add the per-move increment to the specified side's clock.

    *color* is 'w' or 'b' (Breakthrough notation).
    """
    if color == WHITE:
        game.white_time += game.increment
    else:
        game.black_time += game.increment


def apply_time_spent(game: Game, color: str, elapsed: float) -> None:
    """Subtract elapsed seconds from the specified side's clock.

    If the clock reaches ≤ 0, set the game as a timeout loss.
    """
    from apps.games.models import Game as GameModel

    if color == WHITE:
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

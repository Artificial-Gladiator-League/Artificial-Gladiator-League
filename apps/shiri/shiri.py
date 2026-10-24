"""ShiRi: the fixed MCVS opponent for AGladiator (chess and Breakthrough)."""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Optional

from apps.shiri.abc.board_embedding import BoardEmbedding
from apps.shiri.constants import (
    BLACK, DEFAULT_THINKING, MAX_THINKING, MIN_THINKING, WHITE,
)
from apps.shiri.database.atomic_runner import AtomicRunner
from apps.shiri.database.vector_database import VectorDatabase
from apps.shiri.rules import make_rules
from apps.shiri.search.common import SearchConfig
from apps.shiri.search.mcvs_player import MCVSPlayer

log = logging.getLogger(__name__)

_COLORS = {"white": WHITE, "w": WHITE, "black": BLACK, "b": BLACK}


def clamp_thinking(seconds) -> float:
    """Same clamp the lobby applies to ``ai_thinking_seconds`` (3..15 s)."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = DEFAULT_THINKING
    return max(MIN_THINKING, min(value, MAX_THINKING))


class ShiRi:
    NAME = "ShiRi"

    def __init__(self, game: str, *, seed: Optional[int] = None, background: bool = True,
                 config: Optional[SearchConfig] = None):
        self.rules = make_rules(game)
        self.game = self.rules.name
        self.config = config or SearchConfig()
        self.background = background
        self._seed = seed
        # Static board geometry only; it carries no information between games.
        self.embedding = BoardEmbedding(self.rules, connected=True)
        self._lock = threading.Lock()
        self.db: Optional[VectorDatabase] = None
        self.player: Optional[MCVSPlayer] = None
        self.runner: Optional[AtomicRunner] = None
        self.human_color: Optional[int] = None
        self.thinking_time = DEFAULT_THINKING
        self.games_started = 0
        self.last_result = None
        self._abort = threading.Event()

    def abort(self) -> None:
        """Make a running ``get_move`` return as soon as possible (its result is then arbitrary)."""
        self._abort.set()

    # ------------------------------------------------------------------ lifecycle
    @property
    def shiri_color(self) -> Optional[int]:
        return None if self.human_color is None else 1 - self.human_color

    @property
    def database(self) -> Optional[VectorDatabase]:
        return self.db

    def start_new_game(self, color: str, thinking_time: float) -> str:
        """Reset everything game-specific. ``color`` is the human's colour.

        Returns the resolved human colour ("white" / "black"); "random" is resolved here.
        """
        key = str(color).strip().lower()
        if key == "random":
            human = random.choice((WHITE, BLACK))
        elif key in _COLORS:
            human = _COLORS[key]
        else:
            raise ValueError(f"color must be 'white', 'black' or 'random', got {color!r}")
        with self._lock:
            self._abort.clear()
            self._stop_runner()
            self.human_color = human
            self.thinking_time = clamp_thinking(thinking_time)
            # A brand-new, empty database: nothing survives from the previous game.
            self.db = VectorDatabase(self.embedding.dim)
            seed = None if self._seed is None else self._seed + self.games_started
            rng = random.Random(seed)
            self.player = MCVSPlayer(self.rules, self.embedding, self.db, rng, self.config)
            self.runner = AtomicRunner(self.rules, self.embedding, self.db, seed=seed)
            self.runner.set_root(self.rules.initial_state())
            if self.background:
                self.runner.start()
        return "white" if human == WHITE else "black"

    def close(self) -> None:
        """Stop background work (call when the game / connection ends)."""
        with self._lock:
            self._stop_runner()

    def _stop_runner(self) -> None:
        if self.runner is not None:
            self.runner.stop()
            self.runner = None

    # ----------------------------------------------------------------------- play
    def get_move(self, board_state, thinking_time: Optional[float] = None, ply: Optional[int] = None) -> str:
        """Return a legal move for the side to move, in the site's UCI-style format.

        ``board_state``: ``chess.Board`` / FEN for chess, the site's Breakthrough FEN
        (or ``(grid, turn)``) for Breakthrough.  The search returns before
        ``thinking_time`` seconds (default: the value given to ``start_new_game``).
        """
        started = time.perf_counter()
        if self.db is None or self.player is None:
            raise RuntimeError("start_new_game() must be called before get_move()")
        budget = self.thinking_time if thinking_time is None else float(thinking_time)
        if budget <= 0:
            raise ValueError("thinking_time must be positive")
        with self._lock:
            self._abort.clear()
            state = self.rules.state_from(board_state, ply=ply)
            if not state.legal_moves():
                raise ValueError("No legal moves in this position")
            deadline = started + budget - min(0.05, 0.02 * budget)
            runner = self.runner
            if runner is not None:
                runner.pause()
                runner.set_root(state)
            result = None
            try:
                result = self.player.choose(state, deadline, self._abort)
                move = result.move
            except Exception:
                log.exception("ShiRi search failed; falling back to a random legal move")
                move = random.choice(state.legal_moves())
            self.last_result = result
            if runner is not None:
                after = state.copy()
                after.push(move)
                runner.set_root(after)  # self-play continues from the position the human will face
                runner.resume()
            return state.move_uci(move)

"""WebSocket consumer for the "Playing against ShiRi" page.

The logged-in user's OWN model (run by the existing Docker sandbox through
``apps.games.local_inference.get_move_local``) plays ShiRi; the user only watches.
Games are ephemeral: nothing is written to the Game table, so ratings, the lobby and
tournaments are untouched.  One connection == one game at a time == one ShiRi instance;
a new game resets ShiRi's per-game VectorDatabase and closing the socket stops it.

Client -> server
    {"type": "new_game", "game_type": "chess"|"breakthrough",
     "color": "white"|"black"|"random",      # the colour ShiRi plays
     "base_minutes": 1|2|3|5|10|15|30|60,    # minutes per side (as in Create Lobby Game)
     "increment": 0|1|2|3|5|10,              # seconds added after each move
     "thinking": 3|5|8|12|15,                # ShiRi's thinking time per move
     "model_thinking": 3|5|8|12|15}          # the user's AI thinking time (target pace per move)
    {"type": "stop"}
Server -> client
    {"type": "start_refused", "code", "message"}
    {"type": "game_state", "game_type", "fen", "last_move", "turn", "moves", "status",
     "result", "reason", "message", "model_name", "model_color", "shiri_color",
     "thinking", "model_thinking", "base_minutes", "increment",
     "white_time", "black_time", "clock_active", "thinking_side", "phase"}
    status: ongoing | finished | cancelled | stopped

Clocks: a standard chess clock. Exactly one clock runs at any moment: it starts at the instant the
opponent's move is made and stops at the instant its own move is made (same timestamp, no gap, no
overlap). That includes the model's AI-thinking pace wait. The increment is added after every move and
a side whose clock reaches zero loses on time. No clock runs while the model is being loaded.
"""
import asyncio
import functools
import json
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import chess
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.db import connections

from apps.games import breakthrough_engine as bt_engine
from apps.games.chess_engine import is_legal_move as chess_is_legal_move
from apps.games.exceptions import (
    ModelNotPrecachedError,
    SandboxDataMissingError,
    SandboxUnavailableError,
)
from apps.games.local_inference import get_move_local, prewarm_local
from apps.shiri import ShiRi
from apps.shiri.constants import (
    BASE_MINUTES_CHOICES, BLACK, DEFAULT_BASE_MINUTES, DEFAULT_INCREMENT, DRAW,
    INCREMENT_CHOICES, SUPPORTED_GAMES, THINKING_CHOICES, WHITE,
)
from apps.users.guests import guest_snapshot_block, is_guest

log = logging.getLogger(__name__)

# ShiRi's searches are CPU-bound pure Python; model calls are blocking sandbox round-trips.
# They get separate pools so a slow model can never starve ShiRi (or the other way round).
_SEARCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=int(getattr(settings, "SHIRI_MAX_CONCURRENT_SEARCHES", 4)),
    thread_name_prefix="shiri-search",
)
_MODEL_EXECUTOR = ThreadPoolExecutor(
    max_workers=int(getattr(settings, "SHIRI_MAX_CONCURRENT_MODEL_CALLS", 4)),
    thread_name_prefix="shiri-model",
)

_RESULT_TEXT = {WHITE: "1-0", BLACK: "0-1", DRAW: "1/2-1/2"}
_COLOR_NAME = {WHITE: "white", BLACK: "black"}
_GAME_LABEL = {"chess": "Chess", "breakthrough": "Breakthrough"}
_PLATFORM_ERRORS = (SandboxUnavailableError, ModelNotPrecachedError, SandboxDataMissingError)
_PLATFORM_DETAIL = {
    SandboxUnavailableError: "the model sandbox is unavailable",
    ModelNotPrecachedError: "your model has not been downloaded to the platform yet",
    SandboxDataMissingError: "a data file your model needs is missing on the platform",
}
_PLATFORM_TAIL = " This is a platform problem, not your model's fault. The game was cancelled and no result was recorded."


@dataclass(frozen=True)
class _ModelCtx:
    user_id: int
    repo: str
    name: str
    guest: bool


def _game_reason(rules_name: str, state) -> str:
    if rules_name == "chess":
        board = state.board
        if board.is_checkmate():
            return "checkmate"
        if board.is_stalemate():
            return "stalemate"
        if board.is_insufficient_material():
            return "insufficient_material"
        if board.halfmove_clock >= 100:
            return "fifty_moves"
        return "repetition"
    cells = state.cells
    if 1 in cells[0:8] or 2 in cells[56:64]:
        return "breakthrough"
    if 1 not in cells or 2 not in cells:
        return "elimination"
    return "no_moves"


# ------------------------------------------------------------------ blocking helpers
def _preflight(user, game_type: str) -> dict:
    """Can this user's own model play now? Mirrors the lobby's create-game gates. Runs in a thread (ORM)."""
    label = _GAME_LABEL[game_type]
    repo = (user.get_repo_for_game(game_type) or "").strip()  # never the default-repo fallback of bot_runner
    if not repo:
        return {"ok": False, "code": "no_model", "message": (
            f"You don't have a {label} model of your own yet. "
            "Connect your repository in the Gladiator tab of your profile first.")}
    gm = user.get_game_model(game_type)
    if gm is not None and not gm.is_verified:
        return {"ok": False, "code": "not_verified", "message": (
            f"Your {label} model repository must be verified before it can play. "
            "Go to your profile and complete the ownership verification.")}
    if gm is not None and gm.status == gm.ContractStatus.FAILED:
        return {"ok": False, "code": "model_failed", "message": (
            gm.last_error or f"Your {label} model failed its check. See the Gladiator tab of your profile.")}
    reason = guest_snapshot_block(user, game_type)
    if reason:
        return {"ok": False, "code": "preparing", "message": reason}
    name = (getattr(user, "ai_name", "") or "").strip() or repo.rsplit("/", 1)[-1]
    return {"ok": True, "ctx": _ModelCtx(user.pk, repo, name, is_guest(user))}


def _model_failed(user_id: int, game_type: str) -> bool:
    """True if the model's last contract check failed (then a missing move is not a mystery)."""
    from apps.users.models import UserGameModel
    gm = UserGameModel.objects.filter(user_id=user_id, game_type=game_type).first()
    return bool(gm is not None and gm.status == UserGameModel.ContractStatus.FAILED)


def _call_model(ctx: _ModelCtx, game_type: str, fen: str, player: str):
    """Blocking: one sandboxed model move. Returns (uci_or_None, model_check_failed)."""
    try:
        move = get_move_local(ctx.user_id, game_type, fen, player, repo_id=ctx.repo)
        return move, (False if move else _model_failed(ctx.user_id, game_type))
    finally:
        connections.close_all()  # this pool thread is not request-bound: never leave its DB connection open


def _search(bot, board, budget, ply):
    """Blocking: one ShiRi search."""
    return bot.get_move(board, budget, ply)


def _prewarm(ctx: _ModelCtx, game_type: str) -> None:
    try:
        prewarm_local(ctx.user_id, game_type, repo_id=ctx.repo)
    finally:
        connections.close_all()


def _site_says_legal(game_type: str, fen: str, uci: str) -> bool:
    """The same legality check predict_chess / predict_breakthrough apply to a model's move."""
    if game_type == "breakthrough":
        return bt_engine.is_legal_move(fen, uci)
    return chess_is_legal_move(chess.Board(fen), uci)


class ShiRiConsumer(AsyncWebsocketConsumer):
    budget_override = None   # tests only: replaces ShiRi's thinking time
    clock_scale = 1.0        # tests only: shrinks the clocks (real minutes -> real minutes * scale)
    pace_scale = 1.0         # tests only: shrinks the model's AI-thinking pace wait
    move_pause = float(getattr(settings, "SHIRI_MOVE_PAUSE", 0.4))   # minimum seconds each move takes on its clock
    max_plies = int(getattr(settings, "SHIRI_MAX_PLIES", 300))       # longer games are drawn
    model_grace = float(getattr(settings, "SHIRI_MODEL_GRACE", 30))  # extra wait on top of the sandbox timeout

    async def connect(self):
        user = self.scope.get("user")
        if user is None or not getattr(user, "is_authenticated", False):
            await self.close(code=4401)
            return
        self.bot = None
        self.task = None
        self.token = 0  # bumped whenever a game ends/changes so late results are never applied
        self.status = "idle"
        self._reset_game_fields()
        await self.accept()

    def _reset_game_fields(self):
        self.game_type = "chess"
        self.state = None
        self.moves = []
        self.last_move = None
        self.result = None
        self.reason = None
        self.message = ""
        self.shiri_color = BLACK
        self.thinking = 5            # ShiRi's search time per move
        self.model_thinking = 5      # the user's AI thinking time (target pace per model move)
        self.base_minutes = DEFAULT_BASE_MINUTES
        self.increment = DEFAULT_INCREMENT
        self.clocks = {WHITE: 0.0, BLACK: 0.0}
        self.clock_color = None     # whose clock is running right now (never both)
        self.turn_started = None    # time.monotonic() at which that clock started
        self.turn_ended = None      # time.monotonic() at which the previous move was made
        self.ctx = None

    async def disconnect(self, code):
        self.token = getattr(self, "token", 0) + 1
        task = getattr(self, "task", None)
        if task is not None:
            task.cancel()
        bot = getattr(self, "bot", None)
        self.bot = None
        if bot is not None:
            bot.abort()
            await asyncio.get_running_loop().run_in_executor(None, bot.close)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data or len(text_data) > 2048:
            return
        try:
            data = json.loads(text_data)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind == "new_game":
            await self._new_game(data)
        elif kind == "stop":
            await self._stop()

    # ------------------------------------------------------------------ actions
    def _halt(self):
        """Cancel the running game loop at once and invalidate everything it still has in flight."""
        self.token += 1
        if self.task is not None:
            self.task.cancel()
            self.task = None
        if self.bot is not None:
            self.bot.abort()

    async def _new_game(self, data):
        game_type = data.get("game_type")
        if game_type not in SUPPORTED_GAMES:
            game_type = "chess"
        color = data.get("color") if data.get("color") in ("white", "black", "random") else "random"
        thinking = self._choice(data.get("thinking"), THINKING_CHOICES, 5)
        model_thinking = self._choice(data.get("model_thinking"), THINKING_CHOICES, 5)
        base_minutes = self._choice(data.get("base_minutes"), BASE_MINUTES_CHOICES, DEFAULT_BASE_MINUTES)
        increment = self._choice(data.get("increment"), INCREMENT_CHOICES, DEFAULT_INCREMENT)
        self._halt()  # one active game per connection
        self.status = "idle"
        pre = await database_sync_to_async(_preflight)(self.scope["user"], game_type)
        if not pre["ok"]:
            await self._send_json({"type": "start_refused", "code": pre["code"], "message": pre["message"]})
            return

        loop = asyncio.get_running_loop()
        old = self.bot
        if old is not None and old.game != game_type:
            self.bot = None
            await loop.run_in_executor(None, old.close)
        if self.bot is None:
            self.bot = ShiRi(game_type)
        shiri = random.choice((WHITE, BLACK)) if color == "random" else (WHITE if color == "white" else BLACK)
        # ShiRi.start_new_game() takes the *opponent's* colour; a fresh, empty database every game.
        await loop.run_in_executor(None, self.bot.start_new_game, _COLOR_NAME[1 - shiri], thinking)

        self._reset_game_fields()
        self.game_type, self.shiri_color, self.thinking, self.ctx = game_type, shiri, thinking, pre["ctx"]
        self.model_thinking, self.base_minutes, self.increment = model_thinking, base_minutes, increment
        base_seconds = base_minutes * 60 * self.clock_scale
        self.clocks = {WHITE: base_seconds, BLACK: base_seconds}
        self.state = self.bot.rules.initial_state()
        self.status = "ongoing"
        self.task = asyncio.ensure_future(self._game_loop(self.token))

    @staticmethod
    def _choice(value, allowed, default):
        """The value if it is one of the lobby's allowed options, else the default."""
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return default
        return number if number in allowed else default

    async def _stop(self):
        if self.status != "ongoing":
            return
        self._halt()
        self._settle()   # the running clock stops at the moment of Stop
        self._finish("stopped", None, "stopped", "The game was stopped.")
        await self._send_state()

    # -------------------------------------------------------------------- game loop
    async def _game_loop(self, token):
        loop = asyncio.get_running_loop()
        try:
            await self._send_state(phase="preparing")   # no clock runs while the model is being loaded
            await loop.run_in_executor(_MODEL_EXECUTOR, _prewarm, self.ctx, self.game_type)
            if token != self.token:
                return
            self._start_clock(self.state.turn())
            await self._send_state()
            while token == self.token and self.status == "ongoing":
                if self.state.turn() == self.shiri_color:
                    await self._shiri_turn(token)
                else:
                    await self._model_turn(token)
                if token != self.token:
                    return
                await self._send_state()   # also announces whose clock is now running
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ShiRi game loop crashed")
            if token == self.token:
                self._settle()
                self._finish("cancelled", None, "platform_problem",
                             "Cancelled because of an unexpected platform problem. "
                             "This is not your model's fault. No result was recorded.")
                await self._send_state()

    async def _shiri_turn(self, token):
        color = self.shiri_color
        # never let ShiRi's own search flag her: use at most half of what is left on her clock
        budget = min(self.budget_override if self.budget_override is not None else self.thinking,
                     max(0.2, self.clocks[color] / 2))
        board = self.bot.rules.state_from(self.state).board_state()  # private copy for the worker thread
        call = functools.partial(_search, self.bot, board, budget, self.state.ply())
        uci = await asyncio.get_running_loop().run_in_executor(_SEARCH_EXECUTOR, call)
        if token != self.token:
            return
        move = self.state.parse_uci(uci)
        if move is None:
            raise RuntimeError(f"ShiRi returned an illegal move {uci!r}")
        if not await self._hold_and_charge(color, token, self.move_pause):
            return
        self.clocks[color] += self.increment
        self._apply(move)

    async def _model_turn(self, token):
        ctx, game_type = self.ctx, self.game_type
        color = 1 - self.shiri_color
        left = self.clocks[color] - self._spent()
        fen = self.state.fen()
        player = "w" if self.state.turn() == WHITE else "b"
        sandbox_timeout = float(getattr(
            settings, "SANDBOX_GUEST_MOVE_TIMEOUT" if ctx.guest else "SANDBOX_MOVE_TIMEOUT", 15 if ctx.guest else 30,
        )) + self.model_grace
        clock_limited = left <= sandbox_timeout  # then the clock, not the sandbox, ends a slow move
        fut = asyncio.get_running_loop().run_in_executor(_MODEL_EXECUTOR, _call_model, ctx, game_type, fen, player)
        try:
            move, check_failed = await asyncio.wait_for(fut, max(0.0, min(sandbox_timeout, left)))
        except asyncio.TimeoutError:
            if token == self.token:
                if clock_limited:
                    self._flag(color)
                else:
                    self._forfeit("forfeit_timeout", f"{ctx.name} forfeited: it did not answer in time.")
            return
        except _PLATFORM_ERRORS as exc:
            if token == self.token:
                detail = _PLATFORM_DETAIL[type(exc)] if type(exc) in _PLATFORM_DETAIL else "a platform fault occurred"
                log.warning("ShiRi game cancelled by platform fault: %r", exc)
                self._settle()
                self._finish("cancelled", None, "platform_problem",
                             f"Cancelled because of a platform problem ({detail})." + _PLATFORM_TAIL)
            return
        if token != self.token:
            return  # stopped / restarted / closed while the model was thinking
        if not move:
            if check_failed:
                self._forfeit("forfeit_model_failed", f"{ctx.name} forfeited: its model failed the contract check.")
            else:
                self._forfeit("forfeit_no_move", (
                    f"{ctx.name} forfeited: it returned no move (it may have timed out or crashed)."))
            return
        if not isinstance(move, str) or len(move) > 5 or not _site_says_legal(game_type, fen, move):
            self._forfeit("forfeit_illegal_move", f"{ctx.name} forfeited: it played an illegal move ({str(move)[:12]}).")
            return
        parsed = self.state.parse_uci(move)
        if parsed is None:
            self._forfeit("forfeit_illegal_move", f"{ctx.name} forfeited: it played an illegal move ({move}).")
            return
        # AI thinking time = target pace, as in Lobby games: a faster answer is held back until the target
        # is reached. The model's clock keeps running meanwhile: it is still that side's turn.
        target = max(self.model_thinking * self.pace_scale, self.move_pause)
        if not await self._hold_and_charge(color, token, target):
            return
        self.clocks[color] += self.increment
        self._apply(parsed)

    # ------------------------------------------------------------------- clocks
    # Exactly one clock runs at any moment. It starts at the instant the opponent's move is made and stops
    # at the instant its own move is made; both use the same timestamp, so there is no gap and no overlap.
    def _start_clock(self, color, at=None):
        self.clock_color = color
        self.turn_started = time.monotonic() if at is None else at

    def _spent(self) -> float:
        """Seconds the running clock has been running in the current turn."""
        if self.clock_color is None or self.turn_started is None:
            return 0.0
        return max(0.0, time.monotonic() - self.turn_started)

    def _settle(self):
        """Stop the running clock (game ended some other way) and take its elapsed time off."""
        if self.clock_color is not None:
            self.clocks[self.clock_color] = max(0.0, self.clocks[self.clock_color] - self._spent())
            self.clock_color = None

    def _flag(self, color):
        """``color`` ran out of time: that side loses."""
        self.clocks[color] = 0.0
        self.clock_color = None
        who = "ShiRi" if color == self.shiri_color else self.ctx.name
        self._finish("finished", 1 - color, "timeout", f"{who} ran out of time.")

    async def _hold_and_charge(self, color, token, min_turn) -> bool:
        """Keep ``color``'s clock running until its turn has lasted ``min_turn`` seconds, then stop it.

        False if the side flagged meanwhile, or the game was stopped / replaced.
        """
        spent = self._spent()
        if spent < min_turn:
            await asyncio.sleep(min(min_turn - spent, max(0.0, self.clocks[color] - spent)))
            if token != self.token:
                return False
        now = time.monotonic()
        spent = now - self.turn_started
        if spent >= self.clocks[color]:
            self._flag(color)
            return False
        self.clocks[color] -= spent
        self.clock_color = None
        self.turn_ended = now   # the opponent's clock starts at exactly this instant
        return True

    # ------------------------------------------------------------------- helpers
    def _apply(self, move):
        uci = self.state.move_uci(move)
        self.state.push(move)
        self.moves.append(uci)
        self.last_move = uci
        res = self.state.full_result()
        if res is not None:
            self._finish("finished", res, _game_reason(self.bot.rules.name, self.state))
        elif len(self.moves) >= self.max_plies:
            self._finish("finished", DRAW, "max_length",
                         f"Draw: the game reached the maximum length of {self.max_plies} moves.")
        else:
            self._start_clock(self.state.turn(), at=self.turn_ended)

    def _forfeit(self, reason: str, message: str):
        self._settle()
        model_color = 1 - self.shiri_color
        self._finish("finished", 1 - model_color, reason, message)

    def _finish(self, status, result, reason, message=""):
        self.status, self.result, self.reason, self.message = status, result, reason, message

    async def _send_state(self, phase=None):
        ongoing = self.status == "ongoing"
        running = self.clock_color if ongoing else None   # the one clock that is ticking right now
        await self._send_json({
            "type": "game_state",
            "game_type": self.game_type,
            "fen": self.state.fen(),
            "last_move": self.last_move,
            "turn": _COLOR_NAME[self.state.turn()],
            "moves": self.moves,
            "status": self.status,
            "result": None if self.result is None else _RESULT_TEXT[self.result],
            "reason": self.reason,
            "message": self.message,
            "model_name": self.ctx.name,
            "model_color": _COLOR_NAME[1 - self.shiri_color],
            "shiri_color": _COLOR_NAME[self.shiri_color],
            "thinking": self.thinking,
            "model_thinking": self.model_thinking,
            "base_minutes": self.base_minutes,
            "increment": self.increment,
            # times as they were when the running clock started; the page subtracts ``turn_elapsed`` and
            # keeps counting down, so the displayed clock matches the server's accounting exactly
            "white_time": round(self.clocks[WHITE], 3),
            "black_time": round(self.clocks[BLACK], 3),
            "clock_active": None if running is None else _COLOR_NAME[running],
            "turn_elapsed": round(self._spent(), 3) if running is not None else 0,
            "thinking_side": (None if running is None else ("shiri" if running == self.shiri_color else "model")),
            "phase": phase if ongoing else None,
        })

    async def _send_json(self, payload):
        await self.send(text_data=json.dumps(payload))

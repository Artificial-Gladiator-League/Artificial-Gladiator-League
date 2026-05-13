import asyncio
import json
import logging
import random
import time

import chess
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from apps.tournaments.models import Tournament

log = logging.getLogger(__name__)


# ── Repo validation ────────────────────────────────────────────────────────
def has_repo_for_game_type(user, game_type: str) -> bool:
    """Return True if *user* has a non-empty HF model repo for *game_type*.

    Uses ``UserGameModel.hf_model_repo_id`` (the canonical per-game field)
    plus the legacy ``CustomUser.hf_model_repo_id`` fallback for chess.
    Both chess and breakthrough require a submitted repo to create or join games.
    """
    try:
        from apps.users.models import UserGameModel
        gm = UserGameModel.objects.get(user=user, game_type=game_type)
        return bool(gm.hf_model_repo_id and gm.hf_model_repo_id.strip())
    except Exception:
        pass
    # Legacy fallback: chess repo on the user object itself
    if game_type == "chess":
        legacy = getattr(user, "hf_model_repo_id", "") or ""
        return bool(legacy.strip())
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  In‑memory matchmaking queues (per time‑control)
#  Production: move to Redis sorted sets.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_queues: dict[str, list[dict]] = {}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  In‑memory spectator count per game
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_spectator_counts: dict[int, int] = {}
# Each entry: {"channel": channel_name, "user_id": int, "username": str}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Ready tracking for casual game start
#  game_id -> set of user PKs who clicked Start
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_game_ready_players: dict[int, set] = {}

VALID_TIME_CONTROLS = {"1+0", "1+1", "2+0", "2+1", "3+0", "3+1"}


class LobbyConsumer(AsyncWebsocketConsumer):
    """
    Lobby WebSocket — handles:
      • live player / spectator count
      • tournament player‑count broadcasts
      • quick‑pair matchmaking queue
    """

    GROUP = "lobby"

    async def connect(self):
        self.user = self.scope.get("user")
        username = getattr(self.user, "username", "anonymous")
        log.info("[LOBBY CONNECT]  user=%s channel=%s", username, self.channel_name)
        await self.channel_layer.group_add(self.GROUP, self.channel_name)
        await self.accept()
        counts = await self._tournament_counts()
        await self.send(text_data=json.dumps({
            "type": "tournament_counts",
            "counts": counts,
        }))

    async def disconnect(self, close_code):
        username = getattr(self.user, "username", "anonymous") if hasattr(self, "user") else "unknown"
        log.info("[LOBBY DISCONNECT] user=%s channel=%s close_code=%s", username, self.channel_name, close_code)
        self._remove_from_all_queues()
        await self.channel_layer.group_discard(self.GROUP, self.channel_name)

    # ── Incoming messages ──────────────────────
    async def receive(self, text_data=None, bytes_data=None):
        data = json.loads(text_data)
        action = data.get("action")

        if action == "queue":
            await self._handle_queue(data)
        elif action == "cancel_queue":
            await self._handle_cancel(data)
        elif action == "request_counts":
            counts = await self._tournament_counts()
            await self.send(text_data=json.dumps({
                "type": "tournament_counts",
                "counts": counts,
            }))

    # ── Queue logic ────────────────────────────
    async def _handle_queue(self, data):
        tc = data.get("time_control", "3+1")
        if tc not in VALID_TIME_CONTROLS:
            tc = "3+1"

        if not self.user or self.user.is_anonymous:
            await self.send(text_data=json.dumps({
                "type": "error", "message": "Login required.",
            }))
            return

        # ── Rated-game model-lock check ────────────
        # The client sends their HF token so we can verify the model
        # commit hasn't changed since they were locked at 30 games.
        hf_token = data.get("hf_token", "")
        game_type = data.get("game_type", "chess")
        allowed, reason = await self._check_rated_permission(hf_token, game_type=game_type)
        if not allowed:
            await self.send(text_data=json.dumps({
                "type": "error", "message": reason,
            }))
            return

        # ── Repo gate: block users without a model for this game type ──────
        repo_ok = await database_sync_to_async(has_repo_for_game_type)(self.user, game_type)
        if not repo_ok:
            log.warning(
                "BLOCKED no repo: user=%s game_type=%s — refusing queue entry",
                self.user.username, game_type,
            )
            await self.send(text_data=json.dumps({
                "type": "error",
                "message": (
                    f"Missing {game_type} repo. "
                    "Upload your Artificial Gladiator to Hugging Face first."
                ),
            }))
            return

        # Snapshot the latest commit so we can lock it at game 30
        if hf_token:
            await self._snapshot_commit(hf_token, game_type=game_type)

        queue = _queues.setdefault(tc, [])

        # Don't double‑queue
        if any(e["user_id"] == self.user.id for e in queue):
            await self.send(text_data=json.dumps({
                "type": "queued", "time_control": tc,
                "position": next(
                    i + 1 for i, e in enumerate(queue)
                    if e["user_id"] == self.user.id
                ),
            }))
            return

        entry = {
            "channel": self.channel_name,
            "user_id": self.user.id,
            "username": self.user.username,
        }

        # Try to match with someone waiting
        if queue:
            opponent = queue.pop(0)
            if opponent["user_id"] == self.user.id:
                # Edge case — same user in two tabs
                queue.append(entry)
                return

            # Create game with random colour assignment
            game = await self._create_game(
                entry, opponent, tc
            )

            match_msg = {
                "type": "matched",
                "game_id": game["id"],
                "time_control": tc,
            }
            # Notify both players
            await self.send(text_data=json.dumps({
                **match_msg,
                "opponent": opponent["username"],
                "color": game["your_color"],
            }))
            await self.channel_layer.send(opponent["channel"], {
                "type": "match.found",
                "data": {
                    **match_msg,
                    "opponent": entry["username"],
                    "color": game["opp_color"],
                },
            })

            # Broadcast updated queue size
            await self._broadcast_queue_size(tc)
        else:
            queue.append(entry)
            await self.send(text_data=json.dumps({
                "type": "queued", "time_control": tc, "position": len(queue),
            }))
            await self._broadcast_queue_size(tc)

    async def _handle_cancel(self, data):
        tc = data.get("time_control")
        if tc and tc in _queues:
            _queues[tc] = [
                e for e in _queues[tc]
                if e["channel"] != self.channel_name
            ]
            await self.send(text_data=json.dumps({"type": "queue_cancelled"}))
            await self._broadcast_queue_size(tc)
        else:
            self._remove_from_all_queues()
            await self.send(text_data=json.dumps({"type": "queue_cancelled"}))

    def _remove_from_all_queues(self):
        for tc in list(_queues.keys()):
            _queues[tc] = [
                e for e in _queues[tc]
                if e["channel"] != self.channel_name
            ]

    # ── DB helpers ─────────────────────────────
    @database_sync_to_async
    def _check_rated_permission(self, hf_token: str, game_type: str = "chess") -> tuple[bool, str]:
        """Check whether this user may enter a rated queue."""
        from apps.users.rating_lock import can_play_rated_game

        self.user.refresh_from_db()
        return can_play_rated_game(self.user, hf_token, game_type=game_type)

    @database_sync_to_async
    def _snapshot_commit(self, hf_token: str, game_type: str = "chess") -> None:
        """Save the latest commit SHA as last_known_commit_id for future locking."""
        from apps.users.rating_lock import get_latest_commit_id
        from apps.users.models import UserGameModel

        self.user.refresh_from_db()
        try:
            gm = UserGameModel.objects.get(user=self.user, game_type=game_type)
            repo_id = gm.hf_model_repo_id
        except UserGameModel.DoesNotExist:
            gm = None
            repo_id = self.user.hf_model_repo_id

        commit = get_latest_commit_id(repo_id, hf_token)
        if gm:
            if commit and commit != gm.last_known_commit_id:
                gm.last_known_commit_id = commit
                gm.save(update_fields=["last_known_commit_id"])
        else:
            if commit and commit != self.user.last_known_commit_id:
                self.user.last_known_commit_id = commit
                self.user.save(update_fields=["last_known_commit_id"])

    @database_sync_to_async
    def _create_game(self, entry, opponent, tc):
        from .models import Game

        if random.random() < 0.5:
            white_id, black_id = entry["user_id"], opponent["user_id"]
            your_color, opp_color = "white", "black"
        else:
            white_id, black_id = opponent["user_id"], entry["user_id"]
            your_color, opp_color = "black", "white"

        # Parse time control to set correct initial clocks
        parts = tc.split("+")
        base_sec = int(parts[0]) * 60 if parts else 180
        inc = int(parts[1]) if len(parts) > 1 else 0

        game = Game.objects.create(
            white_id=white_id,
            black_id=black_id,
            time_control=tc,
            white_time=float(base_sec),
            black_time=float(base_sec),
            increment=inc,
            status=Game.Status.WAITING,
            ai_thinking_seconds=1.0,
        )
        return {
            "id": game.pk,
            "your_color": your_color,
            "opp_color": opp_color,
        }

    @database_sync_to_async
    def _tournament_counts(self):
        result = {}
        for t in Tournament.objects.filter(
            status__in=[Tournament.Status.OPEN, Tournament.Status.FULL,
                        Tournament.Status.ONGOING],
        ):
            result[t.id] = {
                "count": t.participant_count,
                "capacity": t.capacity,
                "status": t.status,
            }
        return result

    # ── Broadcast helpers ──────────────────────
    async def _broadcast_queue_size(self, tc):
        size = len(_queues.get(tc, []))
        await self.channel_layer.group_send(self.GROUP, {
            "type": "queue.size",
            "data": {"time_control": tc, "size": size},
        })

    # ── Group message handlers ─────────────────
    async def lobby_update(self, event):
        await self.send(text_data=json.dumps(event["data"]))

    async def queue_size(self, event):
        await self.send(text_data=json.dumps({
            "type": "queue_size",
            **event["data"],
        }))

    async def tournament_count(self, event):
        await self.send(text_data=json.dumps({
            "type": "tournament_count",
            **event["data"],
        }))

    async def match_found(self, event):
        """Direct channel send from matcher to the waiting player."""
        await self.send(text_data=json.dumps(event["data"]))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GameConsumer — server‑validated chess moves
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class GameConsumer(AsyncWebsocketConsumer):
    """
    Real‑time game WebSocket.

    Every move is validated server‑side using python‑chess.
    The server is the single source of truth for:
        • board position (FEN)
        • clocks (white_time, black_time)
        • game outcome

    Client messages:
        {"type": "move", "uci": "e2e4", "clock": 178.5}
        {"type": "resign"}
        {"type": "request_state"}

    Server broadcasts:
        {"type": "game_state", fen, last_move, white_time, black_time, turn, status, …}
        {"type": "game_over", result, winner, reason}
        {"type": "armageddon", game_id, white, black, white_time, black_time}
        {"type": "error", message}
    """

    async def connect(self):
        self.game_id = int(self.scope["url_route"]["kwargs"]["game_id"])
        self.group_name = f"game_{self.game_id}"
        self.user = self.scope.get("user")
        username = getattr(self.user, "username", "anonymous")
        log.info(
            "[CONNECT] game=%s user=%s channel=%s",
            self.game_id, username, self.channel_name,
        )
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # ── Repo gate: block players who lack a model for this game type ───
        # We read game_type from the DB here (cheap single-field query).
        # Spectators are always allowed through — only block if the
        # connecting user is an actual player in this game.
        if self.user and not self.user.is_anonymous:
            game_type_for_check = await self._get_game_type()
            is_player = await self._user_is_player()
            if is_player:
                repo_ok = await database_sync_to_async(has_repo_for_game_type)(
                    self.user, game_type_for_check
                )
                if not repo_ok:
                    log.warning(
                        "BLOCKED no repo: user=%s game=%s game_type=%s — closing WS",
                        self.user.username, self.game_id, game_type_for_check,
                    )
                    await self.send(text_data=json.dumps({
                        "type": "error",
                        "message": (
                            f"Missing {game_type_for_check} repo. "
                            "Upload your Artificial Gladiator to Hugging Face first."
                        ),
                    }))
                    await self.close(code=4003)
                    return

        # Log model cache status for this user
        await self._log_model_cache_status()

        # Push full game state on connect
        state = await self._get_game_state()
        await self.send(text_data=json.dumps(state))
        await self.channel_layer.group_send(self.group_name, {
            "type": "broadcast_state",
            "data": state if state.get("type") != "error" else await self._get_game_state(),
        })

    async def disconnect(self, close_code):
        username = getattr(self.user, "username", "anonymous") if hasattr(self, "user") else "unknown"
        log.info(
            "[DISCONNECT] game=%s user=%s channel=%s close_code=%s",
            getattr(self, "game_id", "?"), username, self.channel_name, close_code,
        )
        # Remove from ready set so a reconnect requires re-confirming
        game_id = getattr(self, 'game_id', None)
        if game_id and hasattr(self, 'user') and self.user and not self.user.is_anonymous:
            ready_set = _game_ready_players.get(game_id)
            if ready_set:
                ready_set.discard(self.user.pk)
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    @database_sync_to_async
    def _log_model_cache_status(self):
        """Log whether this user's model is cached and ready to play."""
        if not self.user or self.user.is_anonymous:
            return
        try:
            from apps.users.models import UserGameModel
            import os
            game_models = UserGameModel.objects.filter(user=self.user)
            if not game_models.exists():
                log.warning(
                    "[WARN] CONNECT game=%s user=%s — no UserGameModel found (no AI model configured)",
                    self.game_id, self.user.username,
                )
                return
            for gm in game_models:
                if gm.cached_path and os.path.exists(gm.cached_path):
                    log.info(
                        "[OK] CONNECT game=%s user=%s game_type=%s — model ready at %s",
                        self.game_id, self.user.username, gm.game_type, gm.cached_path,
                    )
                elif gm.cached_path:
                    log.warning(
                        "[WARN] CONNECT game=%s user=%s game_type=%s — cached_path set but file MISSING: %s",
                        self.game_id, self.user.username, gm.game_type, gm.cached_path,
                    )
                else:
                    log.warning(
                        "[WARN] CONNECT game=%s user=%s game_type=%s — no cached model (status=%s)",
                        self.game_id, self.user.username, gm.game_type, gm.verification_status,
                    )
        except Exception:
            log.exception("[ERROR] _log_model_cache_status failed for user=%s", self.user.username)

    async def receive(self, text_data=None, bytes_data=None):
        data = json.loads(text_data)
        msg_type = data.get("type", "")

        if msg_type == "move":
            await self._handle_move(data)
        elif msg_type == "resign":
            await self._handle_resign()
        elif msg_type == "start":
            await self._handle_start()
        elif msg_type == "request_state":
            state = await self._get_game_state()
            await self.send(text_data=json.dumps(state))
        else:
            await self.send(text_data=json.dumps({
                "type": "error", "message": f"Unknown message type: {msg_type}",
            }))

    # ── Move handling (the core loop) ──────────
    async def _handle_move(self, data):
        uci = data.get("uci", "")
        client_clock = data.get("clock")  # optional client-reported remaining time

        result = await self._process_move(uci, client_clock)
        if result.get("error"):
            await self.send(text_data=json.dumps({
                "type": "error", "message": result["error"],
            }))
            return

        # Broadcast new state to all spectators / players
        await self.channel_layer.group_send(self.group_name, {
            "type": "broadcast_state",
            "data": result["state"],
        })

        # If game ended, broadcast game_over + possibly trigger Armageddon
        if result.get("game_over"):
            await self.channel_layer.group_send(self.group_name, {
                "type": "broadcast_game_over",
                "data": result["game_over"],
            })
        if result.get("armageddon"):
            await self.channel_layer.group_send(self.group_name, {
                "type": "broadcast_armageddon",
                "data": result["armageddon"],
            })

    async def _handle_resign(self):
        result = await self._process_resign()
        if result.get("error"):
            await self.send(text_data=json.dumps({
                "type": "error", "message": result["error"],
            }))
            return
        await self.channel_layer.group_send(self.group_name, {
            "type": "broadcast_game_over",
            "data": result["game_over"],
        })

    async def _handle_start(self):
        """Player clicks 'Start Match'. Game begins only when BOTH players have clicked."""
        if not self.user or self.user.is_anonymous:
            await self.send(text_data=json.dumps({"type": "error", "message": "Authentication required."}))
            return

        # ── Repo gate ────────────────────────────────────────────────────────
        game_type_for_check = await self._get_game_type()
        repo_ok = await database_sync_to_async(has_repo_for_game_type)(
            self.user, game_type_for_check
        )
        if not repo_ok:
            log.warning(
                "BLOCKED no repo at start: user=%s game=%s game_type=%s",
                self.user.username, self.game_id, game_type_for_check,
            )
            await self.send(text_data=json.dumps({
                "type": "error",
                "message": (
                    f"Missing {game_type_for_check} repo. "
                    "Upload your Artificial Gladiator to Hugging Face first."
                ),
            }))
            return

        # ── Verify user is actually a player in this game ────────────────────
        white_id, black_id = await self._get_player_ids()
        if white_id is None or black_id is None:
            await self.send(text_data=json.dumps({"type": "error", "message": "Waiting for an opponent to join."}))
            return
        if self.user.pk not in (white_id, black_id):
            await self.send(text_data=json.dumps({"type": "error", "message": "Only players in this game can start it."}))
            return

        # ── Record this player as ready ──────────────────────────────────────
        ready_set = _game_ready_players.setdefault(self.game_id, set())
        ready_set.add(self.user.pk)
        my_color = "white" if self.user.pk == white_id else "black"

        # Broadcast to group so the other player's UI updates
        await self.channel_layer.group_send(self.group_name, {
            "type": "broadcast_player_ready",
            "color": my_color,
        })

        # ── If both players are ready, start the game ────────────────────────
        if white_id not in ready_set or black_id not in ready_set:
            # Still waiting for the other player
            return

        # Atomically claim the start (guard against duplicate triggers)
        if _game_ready_players.pop(self.game_id, None) is None:
            return

        result = await self._process_start()
        if result.get("error"):
            await self.send(text_data=json.dumps({
                "type": "error", "message": result["error"],
            }))
            return
        game_type = await self._get_game_type()
        log.info("[GAME START] Game %s started -- type=%s -- launching bot loop", self.game_id, game_type)
        # Broadcast updated game state to all participants
        await self.channel_layer.group_send(self.group_name, {
            "type": "broadcast_state",
            "data": result["state"],
        })
        # Notify the lobby that a new live game has started.
        try:
            game_data = await self._get_lobby_game_data()
            if game_data:
                await self.channel_layer.group_send("lobby", {
                    "type": "lobby_update",
                    "data": {
                        "type": "ongoing_game_added",
                        "game": game_data,
                    },
                })
            else:
                log.warning("[WARN] [game %s] _get_lobby_game_data returned None -- skipping lobby broadcast", self.game_id)
        except Exception as exc:
            log.exception("[ERROR] [game %s] Failed to broadcast ongoing_game_added: %s", self.game_id, exc)
        # Start the AI bot game loop
        asyncio.ensure_future(self._run_bot_game_loop())

    # ── Group broadcast handlers ───────────────
    async def broadcast_state(self, event):
        await self.send(text_data=json.dumps(event["data"]))

    async def broadcast_game_over(self, event):
        data = event["data"]
        result  = data.get("result", "*")
        winner  = data.get("winner") or "—"
        reason  = data.get("reason") or "unknown"
        print(
            f"\n{'='*50}\n"
            f"  GAME OVER  game_id={self.game_id}\n"
            f"  Result : {result}\n"
            f"  Winner : {winner}\n"
            f"  Reason : {reason}\n"
            f"{'='*50}\n"
        )
        await self.send(text_data=json.dumps(data))
        # Notify the lobby that this live game is now finished.
        try:
            await self.channel_layer.group_send("lobby", {
                "type": "lobby_update",
                "data": {
                    "type": "ongoing_game_removed",
                    "game_pk": self.game_id,
                },
            })
        except Exception:
            pass

    async def broadcast_armageddon(self, event):
        await self.send(text_data=json.dumps(event["data"]))

    async def broadcast_player_ready(self, event):
        """Notify clients which color has clicked 'Start Match'."""
        await self.send(text_data=json.dumps({
            "type": "player_ready",
            "color": event["color"],
        }))

    async def broadcast_game_cancelled(self, event):
        """Notify all connected clients that the game was cancelled by the creator."""
        await self.send(text_data=json.dumps({"type": "game_cancelled"}))

    # ── AI Bot game loop ───────────────────────
    @database_sync_to_async
    def _load_bots(self):
        """Create bot handles for both players using Docker sandbox inference."""
        from .models import Game
        from .bot_runner import load_bot

        try:
            game = Game.objects.select_related("white", "black").get(pk=self.game_id)
        except Game.DoesNotExist:
            return None, None

        game_type = game.game_type or "chess"

        white_repo = game.white.get_repo_for_game(game_type) if game.white else ""
        black_repo = game.black.get_repo_for_game(game_type) if game.black else ""

        white_bot = load_bot(white_repo, game_type=game_type) if game.white else None
        black_bot = load_bot(black_repo, game_type=game_type) if game.black else None
        return white_bot, black_bot

    async def _run_bot_game_loop(self):
        """Run the AI-vs-AI game loop after the match has been started."""
        game_type = await self._get_game_type()
        log.info("[BOT LOOP] [game %s] Bot loop starting -- type=%s", self.game_id, game_type)
        if game_type == 'breakthrough':
            await self._run_bt_bot_loop()
            return

        white_bot, black_bot = await self._load_bots()

        if white_bot is None or black_bot is None:
            # Determine which side failed and forfeit properly
            log.error("[ERROR] [game %s] Failed to load bots -- white=%s black=%s",
                      self.game_id,
                      "loaded" if white_bot else "FAILED",
                      "loaded" if black_bot else "FAILED")
            if white_bot is None and black_bot is None:
                forfeit_color = "white"  # arbitrary when both fail
            elif white_bot is None:
                forfeit_color = "white"
            else:
                forfeit_color = "black"
            forfeit = await self._bot_forfeit(forfeit_color)
            if forfeit.get("game_over"):
                await self.channel_layer.group_send(self.group_name, {
                    "type": "broadcast_game_over",
                    "data": forfeit["game_over"],
                })
            return

        # Play moves in a loop until the game ends
        while True:
            # Small delay so the UI can render each move
            await asyncio.sleep(0.8)

            result = await self._bot_make_move(white_bot, black_bot)

            if result.get("error"):
                # Bot failed to produce a move — forfeit
                forfeit = await self._bot_forfeit(result.get("forfeit_color"))
                if forfeit.get("game_over"):
                    await self.channel_layer.group_send(self.group_name, {
                        "type": "broadcast_game_over",
                        "data": forfeit["game_over"],
                    })
                break

            # Broadcast the new state
            if result.get("state"):
                await self.channel_layer.group_send(self.group_name, {
                    "type": "broadcast_state",
                    "data": result["state"],
                })

            # Check if game ended
            if result.get("game_over"):
                await self.channel_layer.group_send(self.group_name, {
                    "type": "broadcast_game_over",
                    "data": result["game_over"],
                })
                if result.get("armageddon"):
                    await self.channel_layer.group_send(self.group_name, {
                        "type": "broadcast_armageddon",
                        "data": result["armageddon"],
                    })
                break

    @database_sync_to_async
    def _bot_make_move(self, white_bot, black_bot):
        """Get the current side's bot to produce a move and process it."""
        from django.utils import timezone
        from .models import Game
        from .bot_runner import get_bot_move
        from .chess_engine import (
            make_move, apply_increment, apply_time_spent,
            create_armageddon, resolve_armageddon_draw,
        )

        try:
            game = Game.objects.select_related("white", "black").get(pk=self.game_id)
        except Game.DoesNotExist:
            return {"error": "Game not found."}

        if game.is_finished:
            return {"error": "Game is already finished."}

        board = game.board()
        moving_color = board.turn  # chess.WHITE or chess.BLACK

        # Pick the right bot
        if moving_color == chess.WHITE:
            bot = white_bot
            bot_time = game.white_time
            opp_time = game.black_time
            forfeit_color = "white"
        else:
            bot = black_bot
            bot_time = game.black_time
            opp_time = game.white_time
            forfeit_color = "black"

        # ── Mid-game HF SHA check (deterministic, once per player per game) ──
        # For tournament games we run exactly ONE anti-cheat SHA check per
        # participant per game, before that participant's first move. The
        # check is delegated to the unified pipeline
        # (apps.tournaments.sha_audit.perform_sha_check) which compares
        # against the immutable round-pinned baseline, prints the loud
        # terminal banner, flips disqualified_for_sha_mismatch, calls the
        # disqualification service (admin email + WS broadcast +
        # disqualified.html redirect), and returns FAIL on a mismatch.
        moving_user = game.white if moving_color == chess.WHITE else game.black
        if moving_user is not None and game.is_tournament_game:
            sha_failed = self._tournament_sha_check_for_move(
                user=moving_user, game=game,
            )
            if sha_failed:
                log.warning(
                    "Mid-game repo change detected for user %s (username=%s) "
                    "in tournament game %s — forfeiting %s",
                    moving_user.pk, moving_user.username,
                    self.game_id, forfeit_color,
                )
                return {
                    "error": (
                        f"Bot ({forfeit_color}) repo changed mid-game — "
                        "forfeiting per Fair Play Policy."
                    ),
                    "forfeit_color": forfeit_color,
                    "reason": "mid_game_repo_change",
                }

        # Apply elapsed time since last move
        now = timezone.now()
        if game.last_move_at:
            elapsed = (now - game.last_move_at).total_seconds()
            apply_time_spent(game, moving_color, elapsed)
            if game.is_finished:
                game.save()
                return {
                    "state": self._build_state(game),
                    "game_over": self._build_game_over(game),
                }

        # Ask the bot for a move
        uci = get_bot_move(bot, game.current_fen, time_left=bot_time, opponent_time=opp_time)
        if not uci:
            return {"error": f"Bot ({forfeit_color}) failed to produce a move.", "forfeit_color": forfeit_color}

        # Validate and apply
        ok, err = make_move(game, uci)
        if not ok:
            return {"error": f"Bot ({forfeit_color}) made an illegal move: {uci}", "forfeit_color": forfeit_color}

        apply_increment(game, moving_color)
        game.last_move_at = now

        if not game.is_finished:
            game.status = Game.Status.ONGOING

        result = {"state": self._build_state(game)}

        if game.is_finished:
            if game.armageddon_of is not None and game.result == Game.Result.DRAW:
                resolve_armageddon_draw(game)
            game.save()
            result["game_over"] = self._build_game_over(game)
            if (game.result == Game.Result.DRAW
                    and game.armageddon_of is None
                    and game.is_tournament_game):
                arm = create_armageddon(game)
                arm.save()
                result["armageddon"] = {
                    "type": "armageddon",
                    "game_id": arm.pk,
                    "white": arm.white.username if arm.white else "?",
                    "black": arm.black.username if arm.black else "?",
                    "white_time": arm.white_time,
                    "black_time": arm.black_time,
                    "message": "Draw! Armageddon tiebreak starting.",
                }
        else:
            game.save()

        return result

    @database_sync_to_async
    def _bot_forfeit(self, forfeit_color):
        """Forfeit the game for the bot that failed to move."""
        from .models import Game

        try:
            game = Game.objects.select_related("white", "black").get(pk=self.game_id)
        except Game.DoesNotExist:
            return {"error": "Game not found."}

        if game.is_finished:
            return {"error": "Game is already finished."}

        if forfeit_color == "white":
            game.status = Game.Status.BLACK_WINS
            game.result = Game.Result.BLACK_WIN
            game.result_reason = "bot_error"
            game.winner = game.black
        else:
            game.status = Game.Status.WHITE_WINS
            game.result = Game.Result.WHITE_WIN
            game.result_reason = "bot_error"
            game.winner = game.white

        game.save()

        return {"game_over": self._build_game_over(game)}

    # ── Mid-game SHA verification helpers ──────
    def _tournament_sha_check_for_move(self, *, user, game) -> bool:
        """Run an anti-cheat SHA check for *user* before they move.

        Runs exactly ONCE per (game, user) per consumer lifetime — the
        result is cached on ``self._sha_checked_user_ids`` so we do not
        hit the HF API on every move. Delegates to the unified
        ``perform_sha_check`` pipeline, which:

            * compares against ``participant.round_pinned_sha`` (the
              immutable round baseline — NOT the mutable
              ``last_known_commit_id`` that ``live_sha_check`` keeps
              re-syncing);
            * on FAIL: prints the "🚨 TERMINAL: REPO CHANGED" banner,
              calls ``disqualify_for_repo_change`` (which sets
              ``disqualified_for_sha_mismatch=True`` so the
              ``DisqualificationInterceptMiddleware`` redirects the
              cheater to ``/tournaments/disqualified/``), emails admins,
              and broadcasts a WebSocket alert.

        Returns True iff the participant was disqualified by this
        check (i.e. the caller should forfeit the current move).
        Network errors / missing baseline / no participant → False
        (fail-open: do not penalise honest users for a glitch).
        """
        # Per-consumer dedupe — first move triggers the check, the rest
        # of the game just trusts the verdict.
        cached = getattr(self, "_sha_checked_user_ids", None)
        if cached is None:
            cached = set()
            self._sha_checked_user_ids = cached
        if user.pk in cached:
            return False
        cached.add(user.pk)

        try:
            from apps.tournaments.models import (
                Tournament, TournamentParticipant, TournamentShaCheck,
            )
            from apps.tournaments.sha_audit import perform_sha_check
        except Exception:
            log.exception("mid-game sha check: import failed")
            return False

        try:
            tm = game.tournament_match
            if not tm or not tm.tournament_id:
                return False
            try:
                participant = TournamentParticipant.objects.select_related(
                    "tournament", "user",
                ).get(
                    tournament_id=tm.tournament_id, user=user,
                )
            except TournamentParticipant.DoesNotExist:
                return False
            if participant.disqualified_for_sha_mismatch or participant.eliminated:
                # Already disqualified — forfeit the move.
                return True
            if participant.tournament.status != Tournament.Status.ONGOING:
                return False

            row = perform_sha_check(
                participant,
                context="mid-game",
                round_num=getattr(participant.tournament, "current_round", None),
            )
            if row is None:
                return False
            return row.result == TournamentShaCheck.Result.FAIL
        except Exception:
            log.exception(
                "mid-game sha check failed for user=%s game=%s",
                getattr(user, "username", "?"), self.game_id,
            )
            return False

    # ── Breakthrough helpers ───────────────────

    @database_sync_to_async
    def _get_game_type(self):
        from .models import Game
        try:
            return Game.objects.values_list('game_type', flat=True).get(pk=self.game_id)
        except Game.DoesNotExist:
            return 'chess'

    @database_sync_to_async
    def _get_player_ids(self):
        """Return (white_id, black_id) for this game, or (None, None) if not found / incomplete."""
        from .models import Game
        try:
            row = Game.objects.values('white_id', 'black_id').get(pk=self.game_id)
            return row['white_id'], row['black_id']
        except Game.DoesNotExist:
            return None, None

    @database_sync_to_async
    def _user_is_player(self):
        """Return True if self.user is white or black in this game."""
        from django.db.models import Q
        from .models import Game
        if not self.user or self.user.is_anonymous:
            return False
        try:
            return Game.objects.filter(
                pk=self.game_id,
            ).filter(
                Q(white=self.user) | Q(black=self.user)
            ).exists()
        except Exception:
            return False

    @database_sync_to_async
    def _get_lobby_game_data(self):
        """Return a JSON-serialisable dict for the lobby ongoing_game_added event."""
        import json
        from django.urls import reverse
        from .models import Game
        from .views import _serialize_display_game

        try:
            game = Game.objects.select_related("white", "black").get(pk=self.game_id)
        except Game.DoesNotExist:
            log.warning("[WARN] [game %s] _get_lobby_game_data: game not found", self.game_id)
            return None

        try:
            d = _serialize_display_game(game, is_live=True)
        except Exception:
            log.exception("[ERROR] [game %s] _serialize_display_game failed", self.game_id)
            return None

        # Convert preview_moves_json string → Python list so it serialises cleanly
        try:
            d["preview_moves"] = json.loads(d.pop("preview_moves_json", "[]"))
        except Exception:
            d["preview_moves"] = []
        # date_played is a datetime — convert to ISO string
        if d.get("date_played"):
            d["date_played"] = d["date_played"].strftime("%Y-%m-%d %H:%M:%S")
        # move_count and variant are now included by _serialize_display_game
        log.info("[OK] [game %s] _get_lobby_game_data success: white=%s black=%s", self.game_id, d.get("white_name"), d.get("black_name"))
        return d

    async def _run_bt_bot_loop(self):
        """Run the AI-vs-AI loop for Breakthrough using random legal moves."""
        while True:
            await asyncio.sleep(0.8)
            result = await self._bt_bot_make_move()

            if result.get("error"):
                forfeit = await self._bot_forfeit(result.get("forfeit_color", "white"))
                if forfeit.get("game_over"):
                    await self.channel_layer.group_send(self.group_name, {
                        "type": "broadcast_game_over",
                        "data": forfeit["game_over"],
                    })
                break

            if result.get("state"):
                await self.channel_layer.group_send(self.group_name, {
                    "type": "broadcast_state",
                    "data": result["state"],
                })

            if result.get("game_over"):
                await self.channel_layer.group_send(self.group_name, {
                    "type": "broadcast_game_over",
                    "data": result["game_over"],
                })
                break

    @database_sync_to_async
    def _bt_bot_make_move(self):
        """Get a Breakthrough move via Docker sandbox and apply it."""
        from django.utils import timezone
        from .models import Game
        from . import breakthrough_engine as bt
        from .bot_runner import _get_bot_move, _get_repo_for_user

        try:
            game = Game.objects.select_related("white", "black").get(pk=self.game_id)
        except Game.DoesNotExist:
            return {"error": "Game not found."}

        if game.is_finished:
            return {"error": "Game is already finished."}

        parts = game.current_fen.strip().split()
        turn = parts[1] if len(parts) >= 2 else bt.WHITE
        forfeit_color = "white" if turn == bt.WHITE else "black"

        now = timezone.now()
        if game.last_move_at:
            elapsed = (now - game.last_move_at).total_seconds()
            bt.apply_time_spent(game, turn, elapsed)
            if game.is_finished:
                game.save()
                return {
                    "state": self._build_state(game),
                    "game_over": self._build_game_over(game),
                }

        # Get the bot's move via Docker sandbox
        current_user = game.white if turn == bt.WHITE else game.black

        # ── Mid-game HF SHA check (deterministic, once per player per game) ──
        # Same anti-cheat path as the chess loop in `_bot_make_move`. For
        # tournament Breakthrough games we run exactly ONE SHA check per
        # participant per game, before that participant's first move,
        # delegating to ``apps.tournaments.sha_audit.perform_sha_check``
        # so the loud terminal banner + ``disqualified.html`` redirect
        # fire the same way they do for chess.
        if current_user is not None and game.is_tournament_game:
            sha_failed = self._tournament_sha_check_for_move(
                user=current_user, game=game,
            )
            if sha_failed:
                log.warning(
                    "Mid-game repo change detected for user %s (username=%s) "
                    "in tournament Breakthrough game %s — forfeiting %s",
                    current_user.pk, current_user.username,
                    self.game_id, forfeit_color,
                )
                return {
                    "error": (
                        f"Bot ({forfeit_color}) repo changed mid-game — "
                        "forfeiting per Fair Play Policy."
                    ),
                    "forfeit_color": forfeit_color,
                    "reason": "mid_game_repo_change",
                }

        repo = _get_repo_for_user(current_user, "breakthrough") or ""
        uci = _get_bot_move("breakthrough", game.current_fen, turn, repo)

        if not uci:
            return {"error": f"Bot ({forfeit_color}) failed to produce a move.", "forfeit_color": forfeit_color}

        ok, err = bt.make_move(game, uci)
        if not ok:
            return {"error": f"Bot ({forfeit_color}) illegal move: {uci}", "forfeit_color": forfeit_color}

        bt.apply_increment(game, turn)
        game.last_move_at = now

        if not game.is_finished:
            game.status = Game.Status.ONGOING

        result = {"state": self._build_state(game)}

        if game.is_finished:
            game.save()
            result["game_over"] = self._build_game_over(game)
        else:
            game.save()

        return result

    def _process_move_bt(self, game, uci, client_clock):
        """Process a Breakthrough move (sync helper called from _process_move)."""
        from django.utils import timezone
        from .models import Game as GameModel
        from . import breakthrough_engine as bt

        parts = game.current_fen.strip().split()
        turn = parts[1] if len(parts) >= 2 else bt.WHITE

        if self.user and not self.user.is_anonymous:
            if turn == bt.WHITE and self.user.pk != getattr(game.white, "pk", None):
                return {"error": "It is not your turn (White to move)."}
            if turn == bt.BLACK and self.user.pk != getattr(game.black, "pk", None):
                return {"error": "It is not your turn (Black to move)."}

        now = timezone.now()
        if game.last_move_at:
            elapsed = (now - game.last_move_at).total_seconds()
            bt.apply_time_spent(game, turn, elapsed)
            if game.is_finished:
                game.save()
                return {
                    "state": self._build_state(game),
                    "game_over": self._build_game_over(game),
                }

        ok, err = bt.make_move(game, uci)
        if not ok:
            return {"error": err}

        bt.apply_increment(game, turn)
        game.last_move_at = now

        if not game.is_finished:
            game.status = GameModel.Status.ONGOING

        result = {"state": self._build_state(game)}

        if game.is_finished:
            game.save()
            result["game_over"] = self._build_game_over(game)
        else:
            game.save()

        return result

    # ── DB‑touching logic (sync, wrapped) ──────
    @database_sync_to_async
    def _process_move(self, uci: str, client_clock: float | None) -> dict:
        """Validate move, update clocks, check outcome, save. Return broadcast payload."""
        from django.utils import timezone
        from .models import Game
        from .chess_engine import (
            make_move, apply_increment, apply_time_spent,
            create_armageddon, resolve_armageddon_draw,
        )

        try:
            game = Game.objects.select_related("white", "black").get(pk=self.game_id)
        except Game.DoesNotExist:
            return {"error": "Game not found."}

        if game.is_finished:
            return {"error": "Game is already finished."}

        # ── Breakthrough path ──────────────────
        if game.game_type == Game.GameType.BREAKTHROUGH:
            return self._process_move_bt(game, uci, client_clock)

        # Auth check: only the player whose turn it is may move
        if self.user and not self.user.is_anonymous:
            board = game.board()
            if board.turn == chess.WHITE and self.user.pk != getattr(game.white, "pk", None):
                return {"error": "It is not your turn (White to move)."}
            if board.turn == chess.BLACK and self.user.pk != getattr(game.black, "pk", None):
                return {"error": "It is not your turn (Black to move)."}
            moving_color = board.turn
        else:
            moving_color = game.board().turn

        # Apply elapsed time
        now = timezone.now()
        if game.last_move_at:
            elapsed = (now - game.last_move_at).total_seconds()
            apply_time_spent(game, moving_color, elapsed)
            if game.is_finished:
                game.save()
                return {
                    "state": self._build_state(game),
                    "game_over": self._build_game_over(game),
                }

        # Validate and apply the move
        ok, err = make_move(game, uci)
        if not ok:
            return {"error": err}

        # Apply increment to the side that just moved
        apply_increment(game, moving_color)
        game.last_move_at = now

        # If ongoing and not finished by board logic, set status ongoing
        if not game.is_finished:
            game.status = Game.Status.ONGOING

        result = {"state": self._build_state(game)}

        # Handle game-ending scenarios
        if game.is_finished:
            # Armageddon draw odds
            if game.armageddon_of is not None and game.result == Game.Result.DRAW:
                resolve_armageddon_draw(game)

            game.save()
            result["game_over"] = self._build_game_over(game)

            # If this was a regular game that drew (and it's a tournament game),
            # create an Armageddon tiebreak
            if (game.result == Game.Result.DRAW
                    and game.armageddon_of is None
                    and game.is_tournament_game):
                arm = create_armageddon(game)
                arm.save()
                result["armageddon"] = {
                    "type": "armageddon",
                    "game_id": arm.pk,
                    "white": arm.white.username if arm.white else "?",
                    "black": arm.black.username if arm.black else "?",
                    "white_time": arm.white_time,
                    "black_time": arm.black_time,
                    "message": "Draw! Armageddon tiebreak starting.",
                }
        else:
            game.save()

        return result

    @database_sync_to_async
    def _process_resign(self) -> dict:
        from .models import Game

        try:
            game = Game.objects.select_related("white", "black").get(pk=self.game_id)
        except Game.DoesNotExist:
            return {"error": "Game not found."}

        if game.is_finished:
            return {"error": "Game is already finished."}

        if not self.user or self.user.is_anonymous:
            return {"error": "Authentication required."}

        if self.user.pk == getattr(game.white, "pk", None):
            game.status = Game.Status.BLACK_WINS
            game.result = Game.Result.BLACK_WIN
            game.result_reason = "resignation"
            game.winner = game.black
        elif self.user.pk == getattr(game.black, "pk", None):
            game.status = Game.Status.WHITE_WINS
            game.result = Game.Result.WHITE_WIN
            game.result_reason = "resignation"
            game.winner = game.white
        else:
            return {"error": "You are not a player in this game."}

        game.save()

        return {"game_over": self._build_game_over(game)}

    @database_sync_to_async
    def _try_activate_game(self) -> bool:
        """If the game is 'waiting' and both players are assigned, set it to 'ongoing'.

        Returns True if the game was just activated.
        """
        from .models import Game

        try:
            game = Game.objects.select_related("white", "black").get(pk=self.game_id)
        except Game.DoesNotExist:
            return False

        if game.status != Game.Status.WAITING:
            return False

        # Both players must be assigned — don't auto-activate;
        # white must click "Start Match" via the start message.
        return False

    @database_sync_to_async
    def _process_start(self) -> dict:
        """Transition game from WAITING to ONGOING when white clicks Start."""
        from .models import Game

        try:
            game = Game.objects.select_related("white", "black").get(pk=self.game_id)
        except Game.DoesNotExist:
            return {"error": "Game not found."}

        if game.status != Game.Status.WAITING:
            return {"error": "Game has already started."}

        if not self.user or self.user.is_anonymous:
            return {"error": "Authentication required."}

        is_white = self.user.pk == getattr(game.white, "pk", None)
        is_black = self.user.pk == getattr(game.black, "pk", None)
        if not is_white and not is_black:
            return {"error": "Only a player in this game can start the match."}

        if game.black is None:
            return {"error": "Waiting for an opponent to join."}

        # Validate both players have an AI model configured (chess only)
        if game.game_type != Game.GameType.BREAKTHROUGH:
            if not game.white.get_repo_for_game(game.game_type):
                return {"error": f"{game.white.username} does not have an AI model configured."}
            if not game.black.get_repo_for_game(game.game_type):
                return {"error": f"{game.black.username} does not have an AI model configured."}

        game.status = Game.Status.ONGOING
        game.save(update_fields=["status"])
        return {"state": self._build_state(game)}

    @database_sync_to_async
    def _get_game_state(self) -> dict:
        from .models import Game

        try:
            game = Game.objects.select_related("white", "black", "winner").get(pk=self.game_id)
        except Game.DoesNotExist:
            return {"type": "error", "message": "Game not found."}
        return self._build_state(game)

    # ── Payload builders ───────────────────────
    @staticmethod
    def _build_san_move_list(game) -> list[str]:
        if game.game_type == 'breakthrough':
            return list(game.move_list)
        board = chess.Board()
        san_moves: list[str] = []
        for uci in game.move_list:
            try:
                move = chess.Move.from_uci(uci)
                san_moves.append(board.san(move))
                board.push(move)
            except Exception:
                # Keep spectators connected even if historical move data is imperfect.
                return game.move_list
        return san_moves

    @staticmethod
    def _build_state(game) -> dict:
        last_move = game.move_list[-1] if game.move_list else None
        san_move_list = GameConsumer._build_san_move_list(game)
        white_fide = game.white.get_fide_title() if game.white else {}
        black_fide = game.black.get_fide_title() if game.black else {}
        def _ai_name(user):
            if not user:
                return ""
            return user.ai_name or ""

        return {
            "type": "game_state",
            "game_id": game.pk,
            "fen": game.current_fen,
            "last_move": last_move,
            "move_list": game.move_list,
            "san_move_list": san_move_list,
            "white": game.white.username if game.white else "?",
            "white_elo": game.white.elo if game.white else 0,
            "white_model": _ai_name(game.white),
            "black": game.black.username if game.black else "?",
            "black_elo": game.black.elo if game.black else 0,
            "black_model": _ai_name(game.black),
            "white_fide_abbr": white_fide.get("abbr", ""),
            "white_fide_css": white_fide.get("css", ""),
            "white_fide_title": white_fide.get("title", ""),
            "black_fide_abbr": black_fide.get("abbr", ""),
            "black_fide_css": black_fide.get("css", ""),
            "black_fide_title": black_fide.get("title", ""),
            "white_time": round(game.white_time, 1),
            "black_time": round(game.black_time, 1),
            "increment": game.increment,
            "turn": game.turn_color,
            "status": game.status,
            "result": game.result,
            "time_control": game.time_control,
            "is_armageddon": game.armageddon_of_id is not None,
            "game_type": game.game_type,
        }

    @staticmethod
    def _build_game_over(game) -> dict:
        return {
            "type": "game_over",
            "game_id": game.pk,
            "result": game.result,
            "winner": game.winner.username if game.winner else None,
            "reason": game.result_reason,
            "status": game.status,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SpectateConsumer — live spectator count
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class SpectateConsumer(AsyncWebsocketConsumer):
    """
    Tracks how many spectators are watching a game and broadcasts
    the count to everyone in the spectate group.
    """

    async def connect(self):
        self.game_id = int(self.scope["url_route"]["kwargs"]["game_id"])
        self.group_name = f"spectate_{self.game_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        _spectator_counts[self.game_id] = _spectator_counts.get(self.game_id, 0) + 1
        await self._broadcast_count()

    async def disconnect(self, close_code):
        count = _spectator_counts.get(self.game_id, 1)
        _spectator_counts[self.game_id] = max(0, count - 1)
        if _spectator_counts[self.game_id] == 0:
            _spectator_counts.pop(self.game_id, None)

        await self._broadcast_count()
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        pass

    async def _broadcast_count(self):
        count = _spectator_counts.get(self.game_id, 0)
        await self.channel_layer.group_send(self.group_name, {
            "type": "spectator_count",
            "data": {
                "type": "spectator_count",
                "count": count,
                "game_id": self.game_id,
            },
        })

    async def spectator_count(self, event):
        await self.send(text_data=json.dumps(event["data"]))

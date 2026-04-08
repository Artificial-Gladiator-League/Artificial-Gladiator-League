import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from .models import Match, Tournament


class TournamentConsumer(AsyncWebsocketConsumer):
    """
    Real‑time tournament bracket page.
    Broadcasts:
        • player‑count / registration updates
        • round progression (new round started, match results)
        • bracket‑wide status changes
    """

    async def connect(self):
        self.tournament_id = self.scope["url_route"]["kwargs"]["tournament_id"]
        self.group_name = f"tournament_{self.tournament_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        # Push full bracket state on connect
        state = await self._get_bracket_state()
        await self.send(text_data=json.dumps(state))

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        data = json.loads(text_data)
        action = data.get("action")

        if action == "request_bracket":
            state = await self._get_bracket_state()
            await self.send(text_data=json.dumps(state))
        elif action == "join":
            info = await self._get_info()
            msg = {"type": "tournament_count", "tournament_id": self.tournament_id, **info}
            await self.channel_layer.group_send(
                self.group_name, {"type": "tournament_event", "data": msg}
            )
            await self.channel_layer.group_send(
                "lobby", {"type": "tournament.count", "data": {**msg, "tournament_id": int(self.tournament_id)}}
            )
        else:
            await self.channel_layer.group_send(
                self.group_name, {"type": "tournament_event", "data": data}
            )

    # ── Group message handlers ─────────────────
    async def tournament_event(self, event):
        await self.send(text_data=json.dumps(event["data"]))

    async def bracket_update(self, event):
        await self.send(text_data=json.dumps(event["data"]))

    async def match_result(self, event):
        await self.send(text_data=json.dumps(event["data"]))

    async def round_started(self, event):
        await self.send(text_data=json.dumps(event["data"]))

    # ── DB helpers ─────────────────────────────
    @database_sync_to_async
    def _get_info(self):
        try:
            t = Tournament.objects.get(pk=self.tournament_id)
        except Tournament.DoesNotExist:
            return {"count": 0, "capacity": 0, "status": ""}
        return {
            "count": t.participant_count,
            "capacity": t.capacity,
            "status": t.status,
        }

    @database_sync_to_async
    def _get_bracket_state(self):
        try:
            t = Tournament.objects.get(pk=self.tournament_id)
        except Tournament.DoesNotExist:
            return {"type": "bracket_state", "error": "not_found"}

        rounds_data = []
        matches = t.matches.select_related("player1", "player2", "winner").all()
        for m in matches:
            rounds_data.append({
                "match_id": m.pk,
                "round": m.round_num,
                "bracket_pos": m.bracket_position,
                "p1": m.player1.username,
                "p1_elo": m.player1.elo,
                "p1_flag": m.player1.country_flag,
                "p2": m.player2.username,
                "p2_elo": m.player2.elo,
                "p2_flag": m.player2.country_flag,
                "result": m.result or "",
                "winner": m.winner.username if m.winner else "",
                "status": m.match_status,
                "is_armageddon": m.is_armageddon,
                "time_control": m.time_control,
            })

        return {
            "type": "bracket_state",
            "tournament_id": t.pk,
            "name": t.name,
            "status": t.status,
            "current_round": t.current_round,
            "rounds_total": t.rounds_total,
            "capacity": t.capacity,
            "count": t.participant_count,
            "category": t.category or "",
            "matches": rounds_data,
            "champion": t.champion.username if t.champion else "",
        }


class LiveMatchConsumer(AsyncWebsocketConsumer):
    """
    Real‑time move stream for a single tournament match.
    Moves are streamed live but never persisted — only the
    final result is stored in the Match row.

    Messages:
        Server → client:
            {type: "move", fen, lastMove: [from, to], clock: {white, black}}
            {type: "game_over", result, winner}
            {type: "armageddon", message}
        Client → server:
            (spectators send nothing; engine/arbiter sends moves)
    """

    async def connect(self):
        self.match_id = self.scope["url_route"]["kwargs"]["match_id"]
        self.group_name = f"match_{self.match_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        # Push current match info on connect
        info = await self._get_match_info()
        await self.send(text_data=json.dumps(info))

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        data = json.loads(text_data)
        msg_type = data.get("type")

        if msg_type == "move":
            # Broadcast move to all spectators (not persisted)
            await self.channel_layer.group_send(
                self.group_name,
                {"type": "match_move", "data": data},
            )
        elif msg_type == "clock":
            await self.channel_layer.group_send(
                self.group_name,
                {"type": "clock_sync", "data": data},
            )
        elif msg_type == "game_over":
            await self.channel_layer.group_send(
                self.group_name,
                {"type": "game_over", "data": data},
            )
            # Also broadcast to tournament group
            match_info = await self._get_match_info()
            tid = match_info.get("tournament_id")
            if tid:
                await self.channel_layer.group_send(
                    f"tournament_{tid}",
                    {"type": "match_result", "data": {
                        "type": "match_result",
                        "match_id": int(self.match_id),
                        **data,
                    }},
                )
        elif msg_type == "armageddon":
            await self.channel_layer.group_send(
                self.group_name,
                {"type": "armageddon_start", "data": data},
            )

    # ── Group message handlers ─────────────────
    async def match_move(self, event):
        await self.send(text_data=json.dumps(event["data"]))

    async def clock_sync(self, event):
        await self.send(text_data=json.dumps(event["data"]))

    async def game_over(self, event):
        await self.send(text_data=json.dumps(event["data"]))

    async def armageddon_start(self, event):
        await self.send(text_data=json.dumps(event["data"]))

    # ── DB helpers ─────────────────────────────
    @database_sync_to_async
    def _get_match_info(self):
        try:
            m = Match.objects.select_related(
                "tournament", "player1", "player2", "winner"
            ).get(pk=self.match_id)
        except Match.DoesNotExist:
            return {"type": "match_info", "error": "not_found"}

        return {
            "type": "match_info",
            "match_id": m.pk,
            "tournament_id": m.tournament_id,
            "tournament_name": m.tournament.name,
            "round": m.round_num,
            "bracket_pos": m.bracket_position,
            "p1": m.player1.username,
            "p1_elo": m.player1.elo,
            "p1_flag": m.player1.country_flag,
            "p1_ai": m.player1.ai_name or m.player1.username,
            "p2": m.player2.username,
            "p2_elo": m.player2.elo,
            "p2_flag": m.player2.country_flag,
            "p2_ai": m.player2.ai_name or m.player2.username,
            "result": m.result or "",
            "winner": m.winner.username if m.winner else "",
            "status": m.match_status,
            "is_armageddon": m.is_armageddon,
            "time_control": m.time_control,
        }

"""Consumer tests for "Playing against ShiRi": the user's model (mocked here) plays ShiRi.

No Docker is involved: ``get_move_local`` / ``prewarm_local`` are replaced with fakes.
"""
import asyncio
import random
import threading
import time
from contextlib import ExitStack
from unittest import mock

import chess
from channels.testing import WebsocketCommunicator
from django.test import SimpleTestCase

from apps.games import breakthrough_engine as bt
from apps.games.exceptions import (
    ModelNotPrecachedError,
    SandboxDataMissingError,
    SandboxUnavailableError,
)
from apps.games.shiri_consumer import ShiRiConsumer
from apps.shiri import ShiRi

MOD = "apps.games.shiri_consumer"


class _GM:
    is_verified = True
    status = "active"
    last_error = ""

    class ContractStatus:
        FAILED = "failed"


class _User:
    is_authenticated = True
    pk = 77
    ai_name = "Zeta"

    def __init__(self, repos=None, gm=None):
        self.repos = {"chess": "zeta/chess-model", "breakthrough": "zeta/bt-model"} if repos is None else repos
        self.gm = _GM() if gm is None else gm

    def get_repo_for_game(self, game_type):
        return self.repos.get(game_type, "")

    def get_game_model(self, game_type):
        return self.gm if self.repos.get(game_type) else None


class _FastConsumer(ShiRiConsumer):
    budget_override = 0.05
    move_pause = 0
    pace_scale = 0   # no artificial AI-thinking pace wait unless a test asks for it
    max_plies = 300


class _SlowShiRiConsumer(_FastConsumer):
    budget_override = 6  # long enough that any delay in handling Stop / disconnect is obvious


def _comm(consumer_cls=_FastConsumer, user=None):
    comm = WebsocketCommunicator(consumer_cls.as_asgi(), "/ws/shiri/")
    comm.scope["user"] = user or _User()
    return comm


def _random_legal(game_type, fen):
    if game_type == "breakthrough":
        return random.choice(bt.legal_moves(fen))
    return random.choice(list(chess.Board(fen).legal_moves)).uci()


class _Patches:
    """Patch everything the consumer reaches out to; ``model`` replaces get_move_local."""

    def __init__(self, model=None, snapshot_block=None, model_failed=False):
        self.calls = []
        self._model = model
        self.stack = ExitStack()
        self.prewarm = mock.Mock(return_value=True)
        self.snapshot_block = mock.Mock(return_value=snapshot_block)
        self.model_failed = model_failed

    def _fake_model(self, user_id, game_type, fen, player, *, repo_id=None):
        self.calls.append({"user_id": user_id, "game_type": game_type, "fen": fen, "player": player, "repo": repo_id})
        if self._model is not None:
            return self._model(game_type, fen)
        return _random_legal(game_type, fen)

    def __enter__(self):
        self.get_move_local = self.stack.enter_context(mock.patch(f"{MOD}.get_move_local", side_effect=self._fake_model))
        self.stack.enter_context(mock.patch(f"{MOD}.prewarm_local", self.prewarm))
        self.stack.enter_context(mock.patch(f"{MOD}.guest_snapshot_block", self.snapshot_block))
        self.stack.enter_context(mock.patch(f"{MOD}._model_failed", return_value=self.model_failed))
        return self

    def __exit__(self, *exc):
        self.stack.close()


async def _until(comm, pred, timeout=30):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        msg = await comm.receive_json_from(timeout=max(0.1, end - loop.time()))
        if pred(msg):
            return msg


def _is_state(**want):
    def pred(msg):
        return msg["type"] == "game_state" and all(msg.get(k) == v for k, v in want.items())
    return pred


async def _finished(comm, timeout=60):
    return await _until(comm, lambda m: m["type"] == "game_state" and m["status"] != "ongoing", timeout)


async def _start(comm, game_type="chess", color="white", thinking=3, **extra):
    await comm.send_json_to({"type": "new_game", "game_type": game_type, "color": color, "thinking": thinking, **extra})


class ShiRiTimeControlTests(SimpleTestCase):
    def test_lobby_options_are_accepted_and_clocks_get_the_increment(self):
        async def run():
            with _Patches():
                comm = _comm()
                await comm.connect()
                await _start(comm, "chess", "white", base_minutes=1, increment=5, model_thinking=8)
                first = await _until(comm, _is_state(phase="preparing"))
                self.assertEqual((first["base_minutes"], first["increment"], first["model_thinking"], first["thinking"]),
                                 (1, 5, 8, 3))
                self.assertEqual((first["white_time"], first["black_time"]), (60, 60))
                # three moves each: every move adds the 5 s increment, only real thinking time is charged
                state = await _until(comm, lambda m: m["type"] == "game_state" and m["status"] == "ongoing"
                                     and len(m["moves"]) == 6)
                await comm.disconnect()
            for side in ("white_time", "black_time"):
                self.assertGreater(state[side], 74, side)
                self.assertLessEqual(state[side], 75, side)

        asyncio.run(run())

    def test_values_outside_the_lobby_options_fall_back_to_the_lobby_defaults(self):
        async def run():
            with _Patches():
                comm = _comm()
                await comm.connect()
                await _start(comm, "chess", "white", thinking=6, base_minutes=7, increment=4, model_thinking=4)
                first = await _until(comm, _is_state(phase="preparing"))
                await comm.disconnect()
            self.assertEqual((first["base_minutes"], first["increment"], first["model_thinking"], first["thinking"]),
                             (3, 1, 5, 5))
            self.assertEqual(first["white_time"], 180)

        asyncio.run(run())

    def test_models_ai_thinking_time_is_a_pace_and_its_clock_keeps_running(self):
        class Paced(_FastConsumer):
            pace_scale = 1 / 3   # model_thinking=3 -> a 1 s wait

        async def run():
            with _Patches():
                comm = _comm(Paced)
                await comm.connect()
                await _start(comm, "chess", "black", base_minutes=3, increment=0, model_thinking=3)
                await _until(comm, _is_state(thinking_side="model", clock_active="white"))
                t = time.perf_counter()
                state = await _until(comm, lambda m: m["type"] == "game_state" and len(m["moves"]) == 1)
                waited = time.perf_counter() - t
                await comm.disconnect()
            self.assertGreaterEqual(waited, 0.9)             # the instant answer was held back to the pace
            self.assertTrue(178.8 < state["white_time"] < 179.2, state["white_time"])  # ...and it cost the model that time
            self.assertEqual((state["clock_active"], state["thinking_side"]), ("black", "shiri"))  # ShiRi's clock starts at once

        asyncio.run(run())

    def test_exactly_one_clock_runs_and_the_hand_over_has_no_gap(self):
        class Paced(_FastConsumer):
            pace_scale = 1 / 15   # model_thinking=3 -> 0.2 s
            move_pause = 0.1

        async def run():
            with _Patches():
                comm = _comm(Paced)
                await comm.connect()
                await _start(comm, "chess", "white", base_minutes=3, increment=0, model_thinking=3)
                await _until(comm, _is_state(phase="preparing"))
                first = await _until(comm, _is_state(status="ongoing", thinking_side="shiri"))
                t0 = time.perf_counter()
                seen, last = [first], first
                while len(last["moves"]) < 6:
                    last = await _until(comm, lambda m: m["type"] == "game_state")
                    seen.append(last)
                wall = time.perf_counter() - t0
                await comm.disconnect()
            # every state of a running game names exactly one running clock: whoever is to move
            for s in seen:
                self.assertEqual(s["status"], "ongoing")
                self.assertEqual(s["clock_active"], s["turn"], s)
                self.assertEqual(s["thinking_side"], "shiri" if s["turn"] == s["shiri_color"] else "model")
            # no gap and no overlap: the two clocks together lost exactly the real time that passed
            lost = 2 * 180 - last["white_time"] - last["black_time"]
            self.assertLess(abs(lost - wall), 0.3, (lost, wall))
            self.assertGreater(lost, 6 * 0.1)   # each of the 6 moves took at least the minimum turn time

        asyncio.run(run())

    def test_model_that_runs_out_of_time_loses_on_time(self):
        class TinyClock(_FastConsumer):
            clock_scale = 0.05   # 1 minute -> 3 seconds

        def slow(game_type, fen):
            time.sleep(1.6)
            return _random_legal(game_type, fen)

        async def run():
            with _Patches(model=slow):
                comm = _comm(TinyClock)
                await comm.connect()
                await _start(comm, "chess", "black", base_minutes=1, increment=0)
                final = await _finished(comm)
                await comm.disconnect()
            self.assertEqual((final["status"], final["result"], final["reason"]), ("finished", "0-1", "timeout"))
            self.assertIn("ran out of time", final["message"])
            self.assertEqual(final["white_time"], 0)

        asyncio.run(run())


class ShiRiVsModelTests(SimpleTestCase):
    def test_anonymous_is_refused(self):
        async def run():
            comm = _comm()
            comm.scope["user"] = mock.Mock(is_authenticated=False)
            connected, _ = await comm.connect()
            self.assertFalse(connected)

        asyncio.run(run())

    def test_breakthrough_game_runs_to_the_end_with_the_users_own_model(self):
        async def run():
            with _Patches() as p:
                comm = _comm()
                await comm.connect()
                await _start(comm, "breakthrough", "white")
                final = await _finished(comm)
                await comm.disconnect()
            self.assertEqual(final["status"], "finished")
            self.assertIn(final["result"], ("1-0", "0-1"))  # no draws in Breakthrough
            self.assertIn(final["reason"], ("breakthrough", "elimination", "no_moves"))
            self.assertEqual((final["model_color"], final["shiri_color"], final["model_name"]), ("black", "white", "Zeta"))
            # the model only ever ran as the user's own model, with the user's own repo
            self.assertTrue(p.calls)
            self.assertEqual({(c["user_id"], c["game_type"], c["repo"]) for c in p.calls},
                             {(77, "breakthrough", "zeta/bt-model")})
            p.prewarm.assert_called_once_with(77, "breakthrough", repo_id="zeta/bt-model")
            # FEN / move formats in both directions: replay the final move list with the site's engine
            fen = bt.STARTING_FEN
            seen = {}
            for i, uci in enumerate(final["moves"]):
                self.assertTrue(bt.is_legal_move(fen, uci), (i, uci))
                grid, turn = bt._fen_to_grid(fen)
                fr, fc = bt._sq_to_coords(uci[:2])
                tr, tc = bt._sq_to_coords(uci[2:])
                grid[tr][tc], grid[fr][fc] = grid[fr][fc], bt.EMPTY
                fen = bt._grid_to_fen(grid, bt._opponent_turn(turn))
                seen[fen] = i
            replay_fens = {bt.STARTING_FEN} | set(seen)
            for c in p.calls:
                self.assertIn(c["fen"], replay_fens)
                self.assertEqual(c["player"], c["fen"].split()[-1])
                self.assertEqual(c["player"], "b")  # the model plays black

        asyncio.run(run())

    def test_chess_fens_are_standard_and_game_is_drawn_at_max_length(self):
        class Short(_FastConsumer):
            max_plies = 10

        async def run():
            with _Patches() as p:
                comm = _comm(Short)
                await comm.connect()
                await _start(comm, "chess", "black")  # ShiRi black -> the model opens
                final = await _finished(comm)
                await comm.disconnect()
            if final["reason"] == "max_length":
                self.assertEqual((final["status"], final["result"]), ("finished", "1/2-1/2"))
                self.assertEqual(len(final["moves"]), 10)
            self.assertEqual(final["model_color"], "white")
            board = chess.Board()
            fens = {board.fen()}
            for uci in final["moves"]:
                board.push_uci(uci)
                fens.add(board.fen())
            self.assertTrue(p.calls)
            for c in p.calls:
                self.assertEqual(chess.Board(c["fen"]).fen(), c["fen"])  # canonical python-chess FEN
                self.assertIn(c["fen"], fens)
                self.assertEqual(c["player"], "w")

        asyncio.run(run())

    def test_illegal_move_forfeits_and_page_says_why(self):
        async def run():
            with _Patches(model=lambda g, fen: "e2e5") as p:
                comm = _comm()
                await comm.connect()
                await _start(comm, "chess", "black")
                final = await _finished(comm)
                await comm.disconnect()
            self.assertEqual((final["status"], final["result"], final["reason"]),
                             ("finished", "0-1", "forfeit_illegal_move"))
            self.assertIn("Zeta forfeited", final["message"])
            self.assertIn("e2e5", final["message"])
            self.assertEqual(final["moves"], [])  # no random replacement move was played
            self.assertEqual(len(p.calls), 1)

        asyncio.run(run())

    def test_bare_promotion_without_piece_letter_is_illegal_like_on_the_site(self):
        async def run():
            # a legal-looking pawn move that is not legal without a promotion piece must forfeit
            with _Patches(model=lambda g, fen: "e7e8") as p:
                comm = _comm()
                await comm.connect()
                await _start(comm, "chess", "black")
                final = await _finished(comm)
                await comm.disconnect()
            self.assertEqual(final["reason"], "forfeit_illegal_move")

        asyncio.run(run())

    def test_model_returning_none_forfeits(self):
        async def run():
            with _Patches(model=lambda g, fen: None):
                comm = _comm()
                await comm.connect()
                await _start(comm, "breakthrough", "black")
                final = await _finished(comm)
                await comm.disconnect()
            self.assertEqual((final["status"], final["result"], final["reason"]),
                             ("finished", "0-1", "forfeit_no_move"))
            self.assertIn("no move", final["message"])

        asyncio.run(run())

    def test_failed_model_forfeits_with_that_reason(self):
        async def run():
            with _Patches(model=lambda g, fen: None, model_failed=True):
                comm = _comm()
                await comm.connect()
                await _start(comm, "chess", "black")
                final = await _finished(comm)
                await comm.disconnect()
            self.assertEqual(final["reason"], "forfeit_model_failed")

        asyncio.run(run())

    def test_model_timeout_forfeits(self):
        class Impatient(_FastConsumer):
            model_grace = 0

        def slow(game_type, fen):
            time.sleep(1.5)
            return _random_legal(game_type, fen)

        async def run():
            with _Patches(model=slow), mock.patch(f"{MOD}.settings") as s:
                s.SANDBOX_MOVE_TIMEOUT = 0.3
                s.SANDBOX_GUEST_MOVE_TIMEOUT = 0.3
                comm = _comm(Impatient)
                await comm.connect()
                await _start(comm, "chess", "black")
                final = await _finished(comm)
                await comm.disconnect()
            self.assertEqual((final["result"], final["reason"]), ("0-1", "forfeit_timeout"))

        asyncio.run(run())

    def test_platform_faults_cancel_the_game_without_a_result(self):
        for exc in (SandboxUnavailableError("docker down"), ModelNotPrecachedError("x"), SandboxDataMissingError("y")):
            def boom(game_type, fen, exc=exc):
                raise exc

            async def run():
                with _Patches(model=boom):
                    comm = _comm()
                    await comm.connect()
                    await _start(comm, "chess", "black")
                    final = await _finished(comm)
                    await comm.disconnect()
                self.assertEqual((final["status"], final["result"], final["reason"]),
                                 ("cancelled", None, "platform_problem"), type(exc).__name__)
                self.assertIn("not your model's fault", final["message"])
                self.assertIn("no result was recorded", final["message"])

            asyncio.run(run())

    def test_no_model_configured_does_not_start(self):
        async def run():
            with _Patches() as p:
                comm = _comm(user=_User(repos={"chess": "zeta/chess-model"}))  # no breakthrough model
                await comm.connect()
                await _start(comm, "breakthrough", "white")
                msg = await _until(comm, lambda m: m["type"] == "start_refused")
                self.assertEqual(msg["code"], "no_model")
                self.assertIn("Breakthrough model", msg["message"])
                self.assertTrue(await comm.receive_nothing(timeout=0.5))
                await comm.disconnect()
            p.get_move_local.assert_not_called()
            p.prewarm.assert_not_called()

        asyncio.run(run())

    def test_unverified_or_failed_model_does_not_start(self):
        async def run():
            for attr, value, code in (("is_verified", False, "not_verified"), ("status", "failed", "model_failed")):
                gm = _GM()
                setattr(gm, attr, value)
                with _Patches() as p:
                    comm = _comm(user=_User(gm=gm))
                    await comm.connect()
                    await _start(comm, "chess", "white")
                    msg = await _until(comm, lambda m: m["type"] == "start_refused")
                    self.assertEqual(msg["code"], code)
                    await comm.disconnect()
                p.get_move_local.assert_not_called()

        asyncio.run(run())

    def test_guest_snapshot_block_reason_is_shown_and_game_does_not_start(self):
        async def run():
            with _Patches(snapshot_block="Preparing your model...") as p:
                comm = _comm()
                await comm.connect()
                await _start(comm, "chess", "white")
                msg = await _until(comm, lambda m: m["type"] == "start_refused")
                self.assertEqual((msg["code"], msg["message"]), ("preparing", "Preparing your model..."))
                self.assertTrue(await comm.receive_nothing(timeout=0.5))
                await comm.disconnect()
            p.snapshot_block.assert_called_once()
            p.get_move_local.assert_not_called()
            p.prewarm.assert_not_called()

        asyncio.run(run())

    def test_stop_while_the_model_is_thinking(self):
        release = threading.Event()

        def blocked(game_type, fen):
            release.wait(10)
            return _random_legal(game_type, fen)

        async def run():
            with _Patches(model=blocked):
                comm = _comm()
                await comm.connect()
                await _start(comm, "chess", "black")  # the model moves first
                await _until(comm, _is_state(thinking_side="model"))
                t = time.perf_counter()
                await comm.send_json_to({"type": "stop"})
                final = await _until(comm, _is_state(status="stopped"), timeout=3)
                self.assertLess(time.perf_counter() - t, 2)
                self.assertEqual((final["result"], final["reason"]), (None, "stopped"))
                release.set()  # the late model answer must be discarded
                self.assertTrue(await comm.receive_nothing(timeout=1.0))
                await comm.disconnect()

        asyncio.run(run())

    def test_stop_while_shiri_is_thinking(self):
        async def run():
            with _Patches() as p:
                comm = _comm(_SlowShiRiConsumer)
                await comm.connect()
                await _start(comm, "chess", "white")  # ShiRi moves first and thinks for 6 s
                await _until(comm, _is_state(thinking_side="shiri"))
                t = time.perf_counter()
                await comm.send_json_to({"type": "stop"})
                final = await _until(comm, _is_state(status="stopped"), timeout=3)
                self.assertLess(time.perf_counter() - t, 2)
                self.assertEqual(final["moves"], [])
                self.assertTrue(await comm.receive_nothing(timeout=1.0))  # ShiRi's late move is discarded
                await comm.disconnect()
            p.get_move_local.assert_not_called()

        asyncio.run(run())

    def test_new_game_replaces_the_running_game_and_resets_shiri(self):
        async def run():
            with _Patches() as p:
                comm = _comm(_SlowShiRiConsumer)
                await comm.connect()
                await _start(comm, "chess", "white")
                await _until(comm, _is_state(thinking_side="shiri"))
                t = time.perf_counter()
                await _start(comm, "breakthrough", "white", 5)
                state = await _until(comm, _is_state(game_type="breakthrough", phase="preparing"), timeout=5)
                self.assertLess(time.perf_counter() - t, 3)
                self.assertEqual((state["moves"], state["thinking"]), ([], 5))
                await comm.disconnect()

        asyncio.run(run())

    def test_disconnect_mid_game_aborts_shiri_and_closes_it(self):
        async def run():
            with _Patches(), \
                    mock.patch.object(ShiRi, "abort", autospec=True, side_effect=ShiRi.abort) as abort, \
                    mock.patch.object(ShiRi, "close", autospec=True, side_effect=ShiRi.close) as close:
                comm = _comm(_SlowShiRiConsumer)
                await comm.connect()
                await _start(comm, "chess", "white")
                await _until(comm, _is_state(thinking_side="shiri"))
                t = time.perf_counter()
                await comm.disconnect()
                self.assertLess(time.perf_counter() - t, 3)
                abort.assert_called()
                close.assert_called_once()

        asyncio.run(run())

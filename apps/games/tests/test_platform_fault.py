"""A game is cancelled only when the sandbox itself is unavailable; a player's own failures are unchanged."""
from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

import chess
import requests
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.games import bot_runner, platform_fault, sandbox_runner
from apps.games.consumers import GameConsumer
from apps.games.exceptions import (
    ModelNotPrecachedError, SandboxDataMissingError, SandboxUnavailableError,
)
from apps.games.models import Game
from apps.tournaments import engine
from apps.tournaments.models import Match, Tournament
from apps.users.models import UserGameModel

User = get_user_model()
BT_START = "BBBBBBBB/BBBBBBBB/8/8/8/8/WWWWWWWW/WWWWWWWW w"


def _users():
    w = User.objects.create_user("white_p", password="pw")
    b = User.objects.create_user("black_p", password="pw")
    return w, b


def _casual(white, black, **kw):
    fields = dict(white=white, black=black, status=Game.Status.ONGOING, game_type="chess",
                  time_control="3+1", white_time=180.0, black_time=180.0, increment=1)
    fields.update(kw)
    return Game.objects.create(**fields)


def _tournament_game(white, black, game_type="chess", **kw):
    t = Tournament.objects.create(
        name="T", type=Tournament.Type.GAUNTLET, status=Tournament.Status.ONGOING, game_type=game_type,
        start_time=timezone.now() - timedelta(hours=1), capacity=2, rounds_total=1,
        is_money_tournament=False, terms_version="",
    )
    match = Match.objects.create(
        tournament=t, round_num=1, player1=white, player2=black,
        match_status=Match.MatchStatus.LIVE, time_control="3+1",
    )
    fen = BT_START if game_type == "breakthrough" else chess.STARTING_FEN
    game = Game.objects.create(
        white=white, black=black, status=Game.Status.ONGOING, game_type=game_type, time_control="3+1",
        white_time=180.0, black_time=180.0, increment=1, is_tournament_game=True,
        tournament_match=match, current_fen=fen,
    )
    return match, game


class CancelCasualGameTests(TestCase):
    def setUp(self):
        self.w, self.b = _users()
        self.w.refresh_from_db()
        self.elo = (self.w.elo, self.b.elo)

    def test_cancelled_game_has_no_result_and_no_rating_change(self):
        game = _casual(self.w, self.b)
        gm = UserGameModel.objects.create(user=self.w, game_type="chess", hf_model_repo_id="w/m",
                                          rated_games_since_revalidation=7)
        payload = platform_fault.handle_sandbox_unavailable(game.pk)
        game.refresh_from_db()
        self.assertEqual(game.status, Game.Status.ABORTED)
        self.assertEqual(game.result, Game.Result.NONE)
        self.assertEqual(game.result_reason, "platform_error")
        self.assertIsNone(game.winner)
        self.assertEqual(payload["message"], platform_fault.CASUAL_MESSAGE)
        self.w.refresh_from_db(); self.b.refresh_from_db()
        self.assertEqual((self.w.elo, self.b.elo), self.elo)
        self.assertEqual((self.w.wins, self.w.losses, self.w.draws), (0, 0, 0))
        gm.refresh_from_db()
        self.assertEqual(gm.rated_games_since_revalidation, 7)

    def test_second_call_and_finished_games_are_left_alone(self):
        game = _casual(self.w, self.b)
        self.assertIsNotNone(platform_fault.handle_sandbox_unavailable(game.pk))
        self.assertIsNone(platform_fault.handle_sandbox_unavailable(game.pk))
        done = _casual(self.w, self.b, status=Game.Status.WHITE_WINS, result=Game.Result.WHITE_WIN)
        self.assertIsNone(platform_fault.handle_sandbox_unavailable(done.pk))
        done.refresh_from_db()
        self.assertEqual(done.status, Game.Status.WHITE_WINS)


class _RaisingBot:
    def __init__(self, exc=None, move=None):
        self.exc, self.move = exc, move

    def get_move(self, fen, **kw):
        if self.exc:
            raise self.exc
        return self.move, 0.1


class LobbyLoopsCancelTests(TestCase):
    """The Chess and Breakthrough lobby move functions (consumers.py)."""

    def setUp(self):
        self.w, self.b = _users()
        self.consumer = GameConsumer.__new__(GameConsumer)

    def _chess_move(self, game, white_bot, black_bot):
        self.consumer.game_id = game.pk
        return GameConsumer.__dict__["_bot_make_move"].func(self.consumer, white_bot, black_bot)

    def test_chess_lobby_sandbox_unavailable_cancels_and_nobody_forfeits(self):
        game = _casual(self.w, self.b)
        result = self._chess_move(game, _RaisingBot(SandboxUnavailableError("docker down")), _RaisingBot())
        self.assertEqual(result["cancelled"]["reason"], "sandbox_unavailable")
        self.assertNotIn("error", result)
        game.refresh_from_db()
        self.assertEqual((game.status, game.result), (Game.Status.ABORTED, Game.Result.NONE))

    def test_chess_lobby_players_own_failures_still_forfeit(self):
        for exc, move in ((RuntimeError("crash"), None), (None, None),
                          (ModelNotPrecachedError("x"), None), (SandboxDataMissingError("x"), None)):
            game = _casual(self.w, self.b)
            result = self._chess_move(game, _RaisingBot(exc, move), _RaisingBot())
            self.assertEqual(result.get("forfeit_color"), "white", repr(exc))
            self.assertNotIn("cancelled", result)
            game.refresh_from_db()
            self.assertEqual(game.status, Game.Status.ONGOING)  # the loop forfeits it, nothing cancels it

    def test_chess_lobby_illegal_move_still_forfeits(self):
        game = _casual(self.w, self.b)
        result = self._chess_move(game, _RaisingBot(move="e2e5"), _RaisingBot())
        self.assertEqual(result.get("forfeit_color"), "white")
        self.assertNotIn("cancelled", result)

    def _bt_move(self, game, side_effect):
        self.consumer.game_id = game.pk
        with mock.patch("apps.games.bot_runner._get_bot_move", side_effect=side_effect), \
             mock.patch("apps.games.bot_runner._get_repo_for_user", return_value="u/bt"):
            return GameConsumer.__dict__["_bt_bot_make_move"].func(self.consumer)

    def test_breakthrough_lobby_sandbox_unavailable_cancels(self):
        game = _casual(self.w, self.b, game_type="breakthrough", current_fen=BT_START)
        result = self._bt_move(game, SandboxUnavailableError("docker down"))
        self.assertEqual(result["cancelled"]["reason"], "sandbox_unavailable")
        game.refresh_from_db()
        self.assertEqual((game.status, game.result), (Game.Status.ABORTED, Game.Result.NONE))

    def test_breakthrough_lobby_no_move_still_forfeits(self):
        game = _casual(self.w, self.b, game_type="breakthrough", current_fen=BT_START)
        result = self._bt_move(game, lambda *a, **k: (None, 0.0))
        self.assertEqual(result.get("forfeit_color"), "white")
        self.assertNotIn("cancelled", result)
        game.refresh_from_db()
        self.assertEqual(game.status, Game.Status.ONGOING)


class GetBotMoveTests(TestCase):
    """Re-raising SandboxUnavailableError must not change how any other exception is handled."""

    def test_only_sandbox_unavailable_propagates(self):
        with self.assertRaises(SandboxUnavailableError):
            bot_runner.get_bot_move(_RaisingBot(SandboxUnavailableError("down")), chess.STARTING_FEN)
        for exc in (RuntimeError("crash"), ValueError("bad"), TimeoutError(), KeyError("k"),
                    ModelNotPrecachedError("x"), SandboxDataMissingError("x")):
            self.assertEqual(bot_runner.get_bot_move(_RaisingBot(exc), chess.STARTING_FEN), (None, 0.0), repr(exc))

    def test_non_string_move_and_missing_bot_are_unchanged(self):
        self.assertEqual(bot_runner.get_bot_move(None, chess.STARTING_FEN), (None, 0.0))
        self.assertEqual(bot_runner.get_bot_move(_RaisingBot(move=42), chess.STARTING_FEN), (None, 0.1))

    def test_get_bot_move_internal_catch_all_is_unchanged(self):
        user = User.objects.create_user("catcher", password="pw")
        UserGameModel.objects.create(user=user, game_type="chess", hf_model_repo_id="u/m",
                                     status=UserGameModel.ContractStatus.ACTIVE)
        with mock.patch("apps.games.local_inference.get_move_local", side_effect=RuntimeError("player crash")):
            self.assertEqual(bot_runner._get_bot_move("chess", chess.STARTING_FEN, "w", "u/m"), (None, 0.0))


class PlayersOwnFailuresKeepTodaysBehaviourTests(TestCase):
    """A player cannot get a game cancelled by making their own program fail."""

    def setUp(self):
        self.user = User.objects.create_user("coder", password="pw")
        self.gm = UserGameModel.objects.create(
            user=self.user, game_type="chess", hf_model_repo_id="coder/chess-model",
            status=UserGameModel.ContractStatus.ACTIVE,
        )

    def _move(self, **local):
        from apps.games.predict_chess import get_move
        with mock.patch("apps.games.local_inference.get_move_local", **local):
            return get_move(chess.STARTING_FEN, "w", "coder/chess-model")[0]

    def test_no_move_timeout_or_crash_in_the_sandbox_gives_a_random_legal_move(self):
        legal = {m.uci() for m in chess.Board().legal_moves}
        self.assertIn(self._move(return_value=None), legal)

    def test_illegal_move_gives_a_random_legal_move(self):
        legal = {m.uci() for m in chess.Board().legal_moves}
        self.assertIn(self._move(return_value="e2e5"), legal)

    def test_failed_model_check_still_means_forfeit(self):
        UserGameModel.objects.filter(pk=self.gm.pk).update(status=UserGameModel.ContractStatus.FAILED)
        self.assertIsNone(self._move(return_value=None))

    def test_sandbox_unavailable_is_not_turned_into_a_random_move_or_forfeit(self):
        from apps.games.predict_chess import get_move
        with mock.patch("apps.games.local_inference.get_move_local", side_effect=SandboxUnavailableError("down")):
            with self.assertRaises(SandboxUnavailableError):
                get_move(chess.STARTING_FEN, "w", "coder/chess-model")


class SandboxRunnerPlayerFaultsTests(TestCase):
    """In the real runner a timeout or a crash returns None; only Docker faults raise."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="agl_pf_")
        self.addCleanup(tmp.cleanup)
        self.model_dir = Path(tmp.name)
        self.container = mock.MagicMock()
        self.client = mock.MagicMock()
        self.client.containers.create.return_value = self.container

    def _run(self):
        with mock.patch.object(sandbox_runner, "_docker_client", return_value=self.client):
            return sandbox_runner._run_predict_one_shot(
                self.model_dir, None, chess.STARTING_FEN, "w", "chess", timeout=1,
            )

    def test_move_timeout_returns_none(self):
        self.container.wait.side_effect = requests.exceptions.ReadTimeout()
        self.assertIsNone(self._run())

    def test_program_crash_returns_none(self):
        self.container.wait.return_value = {"StatusCode": 1}
        self.container.logs.return_value = b"Traceback: crash"
        self.assertIsNone(self._run())

    def test_no_output_returns_none(self):
        self.container.wait.return_value = {"StatusCode": 0}
        self.container.logs.return_value = b""
        self.assertIsNone(self._run())

    def test_docker_api_error_is_the_only_thing_that_raises(self):
        from docker.errors import APIError
        self.client.containers.create.side_effect = APIError("daemon down")
        with self.assertRaises(SandboxUnavailableError):
            self._run()


class TournamentRunnerTests(TestCase):
    def setUp(self):
        self.w, self.b = _users()
        patches = [
            mock.patch("apps.games.bot_runner._get_repo_for_user", return_value="u/repo"),
            mock.patch("apps.games.bot_runner._start_sha_check", return_value=None),
            mock.patch("apps.tournaments.engine._start_bot_game_thread"),
            mock.patch("apps.games.bot_runner._broadcast_state"),
            mock.patch("apps.games.bot_runner._broadcast_game_over"),
            mock.patch("apps.games.platform_fault.broadcast_game_aborted"),
        ]
        started = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.start_thread = started[2]
        self.broadcast_aborted = started[5]

    def _run(self, game, move):
        with mock.patch("apps.games.bot_runner._get_bot_move", side_effect=move):
            if game.game_type == "breakthrough":
                bot_runner._run_breakthrough_game(game)
            else:
                bot_runner._run_chess_game(game)

    def test_chess_game_is_replayed_with_the_same_colours_and_full_clocks(self):
        match, game = _tournament_game(self.w, self.b)
        self._run(game, SandboxUnavailableError("down"))
        game.refresh_from_db(); match.refresh_from_db()
        self.assertEqual((game.status, game.result), (Game.Status.ABORTED, Game.Result.NONE))
        self.assertEqual(match.match_status, Match.MatchStatus.LIVE)
        replacement = Game.objects.exclude(pk=game.pk).get(tournament_match=match)
        self.assertEqual((replacement.white, replacement.black), (self.w, self.b))
        self.assertEqual((replacement.status, replacement.white_time, replacement.increment),
                         (Game.Status.ONGOING, 180.0, 1))
        self.start_thread.assert_called_once_with(replacement.pk, 60)
        payload = self.broadcast_aborted.call_args.args[0]
        self.assertIn("attempt 2 of 3", payload["message"])
        self.assertEqual(payload["replacement_in"], 60)
        self.w.refresh_from_db()
        self.assertEqual((self.w.wins, self.w.losses, self.w.draws), (0, 0, 0))

    def test_breakthrough_game_is_replayed(self):
        match, game = _tournament_game(self.w, self.b, game_type="breakthrough")
        self._run(game, SandboxUnavailableError("down"))
        replacement = Game.objects.exclude(pk=game.pk).get(tournament_match=match)
        self.assertEqual((replacement.game_type, replacement.current_fen), ("breakthrough", BT_START))
        game.refresh_from_db()
        self.assertEqual(game.status, Game.Status.ABORTED)

    def test_third_cancelled_game_postpones_the_match_and_notifies_the_admins(self):
        match, game = _tournament_game(self.w, self.b)
        for _ in range(2):
            Game.objects.create(white=self.w, black=self.b, status=Game.Status.ABORTED, result_reason="platform_error",
                                is_tournament_game=True, tournament_match=match)
        with mock.patch("django.core.mail.mail_admins") as mail:
            self._run(game, SandboxUnavailableError("down"))
        match.refresh_from_db()
        self.assertEqual(match.match_status, Match.MatchStatus.POSTPONED)
        mail.assert_called_once()
        self.start_thread.assert_not_called()
        self.assertFalse(Game.objects.filter(tournament_match=match, status=Game.Status.ONGOING).exists())
        payload = self.broadcast_aborted.call_args.args[0]
        self.assertEqual(
            payload["message"],
            "Match postponed because of a platform problem. The organizers have been notified.",
        )
        self.assertTrue(payload["postponed"])

    def test_cancelled_armageddon_is_replayed_as_an_armageddon(self):
        match, parent = _tournament_game(self.w, self.b)
        Game.objects.filter(pk=parent.pk).update(
            status=Game.Status.DRAW, result=Game.Result.DRAW, result_reason="stalemate")
        parent.refresh_from_db()
        arm = Game.objects.create(
            white=self.b, black=self.w, status=Game.Status.ONGOING, time_control="2+0", white_time=120.0,
            black_time=60.0, is_tournament_game=True, tournament_match=match, armageddon_of=parent,
        )
        payload = platform_fault.handle_sandbox_unavailable(arm.pk)
        arm.refresh_from_db()
        self.assertEqual(arm.status, Game.Status.ABORTED)
        self.assertIsNone(arm.armageddon_of)
        new_arm = Game.objects.get(pk=payload["replacement_game_id"])
        self.assertEqual(new_arm.armageddon_of_id, parent.pk)
        self.assertEqual((new_arm.white_time, new_arm.black_time), (120.0, 60.0))

    def test_players_own_failure_in_a_tournament_game_still_forfeits_and_is_never_replayed(self):
        match, game = _tournament_game(self.w, self.b)
        with mock.patch("apps.games.bot_runner._forfeit_game") as forfeit:
            self._run(game, [(None, 0.0)])  # no move / crash / timeout all arrive here as (None, 0.0)
        forfeit.assert_called_once()
        self.assertEqual(forfeit.call_args.args[1], "white")
        game.refresh_from_db()
        self.assertEqual(game.status, Game.Status.ONGOING)  # not aborted
        self.assertEqual(Game.objects.filter(tournament_match=match).count(), 1)
        self.start_thread.assert_not_called()
        self.broadcast_aborted.assert_not_called()

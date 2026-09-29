"""Verify predict_chess/predict_breakthrough refuse to fall back to a
random move when the model's contract check has failed
(UserGameModel.status == FAILED) — the caller (apps.games.bot_runner) must
see ``None`` and forfeit instead of the game continuing on random moves.
"""
from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.users.models import UserGameModel

User = get_user_model()

CHESS_START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
BT_START_FEN = "BBBBBBBB/BBBBBBBB/8/8/8/8/WWWWWWWW/WWWWWWWW w"


def _make_user_and_model(game_type: str, repo: str, status: str) -> UserGameModel:
    user = User.objects.create_user(
        username=f"u_{game_type}_{status}", email=f"u_{game_type}_{status}@example.com", password="x",
    )
    return UserGameModel.objects.create(
        user=user,
        game_type=game_type,
        hf_model_repo_id=repo,
        status=status,
    )


class PredictChessNoRandomOnFailedTests(TestCase):
    def test_failed_status_returns_none_instead_of_random(self):
        _make_user_and_model("chess", "user/failed-chess", UserGameModel.ContractStatus.FAILED)
        from apps.games import predict_chess

        with mock.patch("apps.games.local_inference.get_move_local", return_value=None):
            move, _latency = predict_chess.get_move(CHESS_START_FEN, "w", "user/failed-chess")

        self.assertIsNone(move)

    def test_pending_status_still_falls_back_to_random(self):
        _make_user_and_model("chess", "user/pending-chess", UserGameModel.ContractStatus.PENDING)
        from apps.games import predict_chess

        with mock.patch("apps.games.local_inference.get_move_local", return_value=None):
            move, _latency = predict_chess.get_move(CHESS_START_FEN, "w", "user/pending-chess")

        self.assertIsNotNone(move)


class PredictBreakthroughNoRandomOnFailedTests(TestCase):
    def test_failed_status_returns_none_instead_of_random(self):
        _make_user_and_model("breakthrough", "user/failed-bt", UserGameModel.ContractStatus.FAILED)
        from apps.games import predict_breakthrough

        with mock.patch("apps.games.local_inference.get_move_local", return_value=None):
            move, _latency = predict_breakthrough.get_move(BT_START_FEN, "w", "user/failed-bt")

        self.assertIsNone(move)

    def test_pending_status_still_falls_back_to_random(self):
        _make_user_and_model("breakthrough", "user/pending-bt", UserGameModel.ContractStatus.PENDING)
        from apps.games import predict_breakthrough

        with mock.patch("apps.games.local_inference.get_move_local", return_value=None):
            move, _latency = predict_breakthrough.get_move(BT_START_FEN, "w", "user/pending-bt")

        self.assertIsNotNone(move)

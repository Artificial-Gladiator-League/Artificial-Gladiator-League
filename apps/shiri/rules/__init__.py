from apps.shiri.constants import GAME_BREAKTHROUGH, GAME_CHESS, SUPPORTED_GAMES
from apps.shiri.rules.base import GameRules, GameState
from apps.shiri.rules.breakthrough_rules import BreakthroughRules
from apps.shiri.rules.chess_rules import ChessRules


def make_rules(game: str) -> GameRules:
    g = (game or "").strip().lower()
    if g == GAME_CHESS:
        return ChessRules()
    if g == GAME_BREAKTHROUGH:
        return BreakthroughRules()
    raise ValueError(f"Unsupported game {game!r}; expected one of {SUPPORTED_GAMES}")


__all__ = ["GameRules", "GameState", "ChessRules", "BreakthroughRules", "make_rules"]

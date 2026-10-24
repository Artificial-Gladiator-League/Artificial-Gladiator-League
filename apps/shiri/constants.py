"""Shared constants for the ShiRi / MCVS package."""

WHITE = 0
BLACK = 1
DRAW = 2

ZONE_WIN = "win"
ZONE_LOSS = "loss"
ZONE_DRAW = "draw"
ZONES = (ZONE_WIN, ZONE_LOSS, ZONE_DRAW)

# Same values offered in the lobby "Time per move (AI thinking)" selector.
THINKING_CHOICES = (3, 5, 8, 12, 15)
# Same values offered in the lobby "Create Lobby Game" modal.
BASE_MINUTES_CHOICES = (1, 2, 3, 5, 10, 15, 30, 60)
INCREMENT_CHOICES = (0, 1, 2, 3, 5, 10)
DEFAULT_BASE_MINUTES = 3
DEFAULT_INCREMENT = 1
MIN_THINKING = 3.0
MAX_THINKING = 15.0
DEFAULT_THINKING = 5.0

GAME_CHESS = "chess"
GAME_BREAKTHROUGH = "breakthrough"
SUPPORTED_GAMES = (GAME_CHESS, GAME_BREAKTHROUGH)

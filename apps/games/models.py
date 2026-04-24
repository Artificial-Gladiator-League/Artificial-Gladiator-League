import chess
from django.conf import settings
from django.db import models


STARTING_FEN = chess.STARTING_FEN


class Game(models.Model):
    """A single chess game — casual or tournament — with full server‑side board state."""

    # ── Result / status choices ─────────────────
    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting for opponent"
        ONGOING = "ongoing", "Ongoing"
        WHITE_WINS = "white_wins", "White wins"
        BLACK_WINS = "black_wins", "Black wins"
        DRAW = "draw", "Draw"
        TIMEOUT_WHITE = "timeout_white", "White timed out"
        TIMEOUT_BLACK = "timeout_black", "Black timed out"
        ABORTED = "aborted", "Aborted"

    class Result(models.TextChoices):
        WHITE_WIN = "1-0", "White wins"
        BLACK_WIN = "0-1", "Black wins"
        DRAW = "1/2-1/2", "Draw"
        NONE = "*", "Undecided"

    class GameType(models.TextChoices):
        CHESS = "chess", "Chess"
        BREAKTHROUGH = "breakthrough", "Breakthrough"

    TERMINAL_STATUSES = {
        Status.WHITE_WINS, Status.BLACK_WINS, Status.DRAW,
        Status.TIMEOUT_WHITE, Status.TIMEOUT_BLACK, Status.ABORTED,
    }

    # ── Players ─────────────────────────────────
    white = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="games_as_white",
        null=True, blank=True,
    )
    black = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="games_as_black",
        null=True, blank=True,
    )
    winner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="won_games",
    )

    # ── Game type ───────────────────────────────
    game_type = models.CharField(
        max_length=20,
        choices=GameType.choices,
        default=GameType.CHESS,
    )

    # ── Board state (authoritative server copy) ─
    current_fen = models.TextField(default=STARTING_FEN)
    move_list = models.JSONField(
        default=list,
        help_text="Ordered list of UCI move strings, e.g. ['e2e4','e7e5',…].",
    )
    pgn = models.TextField(
        blank=True,
        help_text="Full PGN text, built incrementally.",
    )

    # ── Time control ────────────────────────────
    time_control = models.CharField(
        max_length=20,
        default="3+1",
        help_text="Format: '<base_minutes>+<increment_seconds>'.",
    )
    white_time = models.FloatField(
        default=180.0,
        help_text="White's remaining clock in seconds.",
    )
    black_time = models.FloatField(
        default=180.0,
        help_text="Black's remaining clock in seconds.",
    )
    increment = models.IntegerField(
        default=1,
        help_text="Increment per move in seconds.",
    )

    # Maximum time (seconds) allotted for an AI to think per move.
    ai_thinking_seconds = models.FloatField(
        default=1.0,
        help_text="Maximum seconds allowed for AI to think per move.",
    )

    # ── Status / result ─────────────────────────
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.WAITING,
    )
    result = models.CharField(
        max_length=10, choices=Result.choices, default=Result.NONE,
    )
    result_reason = models.CharField(
        max_length=60, blank=True,
        help_text="e.g. 'checkmate', 'timeout', 'stalemate', 'resignation'.",
    )

    # ── ELO deltas (set by signal after game ends)
    elo_change_white = models.IntegerField(default=0)
    elo_change_black = models.IntegerField(default=0)

    # ── Tournament linkage ──────────────────────
    is_tournament_game = models.BooleanField(default=False)
    tournament_match = models.ForeignKey(
        "tournaments.Match",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="games",
        help_text="The tournament Match this game belongs to (if any).",
    )

    # ── Armageddon linkage ──────────────────────
    armageddon_of = models.OneToOneField(
        "self",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="armageddon_game",
        help_text="If set, this game is the Armageddon tiebreak for the linked game.",
    )

    timestamp = models.DateTimeField(auto_now_add=True)
    last_move_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        w = self.white.username if self.white else "???"
        b = self.black.username if self.black else "???"
        return f"Game #{self.pk}: {w} vs {b} [{self.result}]"

    # ── Helpers ─────────────────────────────────
    def board(self) -> chess.Board:
        """Return a python-chess Board with reconstructed move history.

        Replaying the stored UCI list preserves repetition history so
        python-chess can correctly detect draw conditions such as
        threefold repetition.
        """
        board = chess.Board()
        for uci in self.move_list:
            try:
                board.push_uci(uci)
            except ValueError:
                return chess.Board(self.current_fen)
        return board

    @property
    def is_finished(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

    @property
    def turn_color(self) -> str:
        """'white' or 'black' based on the FEN side‑to‑move.

        Works for both chess FEN (``... w KQkq ...``) and Breakthrough
        FEN (``... w``).  The side-to-move token is always ``parts[1]``.
        """
        parts = self.current_fen.strip().split()
        if len(parts) >= 2:
            return "white" if parts[1] == "w" else "black"
        return "white"

    def parse_time_control(self) -> tuple[float, int]:
        """Return (base_seconds, increment_seconds) from the time_control string."""
        parts = self.time_control.split("+")
        base = int(parts[0]) * 60 if parts else 180
        inc = int(parts[1]) if len(parts) > 1 else 0
        return float(base), inc


class Comment(models.Model):
    """Threaded comment on a game replay (YouTube / Reddit style)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="game_comments",
    )
    game = models.ForeignKey(
        Game,
        on_delete=models.CASCADE,
        related_name="comments",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="replies",
    )
    content = models.TextField(
        max_length=2000,
        help_text="Supports basic markdown: **bold**, *italic*, `code`, [links](url).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]  # Chronological (replies read top→bottom)

    def __str__(self):
        return f"Comment by {self.user.username} on Game #{self.game_id}"

    def save(self, *args, **kwargs):
        # Auto‑set initial clocks from time_control on first save
        if self.pk is None:
            base, inc = self.parse_time_control()
            self.white_time = base
            self.black_time = base
            self.increment = inc
        super().save(*args, **kwargs)

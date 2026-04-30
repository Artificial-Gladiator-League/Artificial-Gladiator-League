from django.conf import settings
from django.db import models


class Tournament(models.Model):
    """A scheduled AI chess tournament with bracket and prize pool."""

    class Type(models.TextChoices):
        SMALL = "small", "Small (128 players)"
        LARGE = "large", "Large (1024 players)"
        QA = "qa", "QA (2 players)"
        GAUNTLET = "gauntlet", "Gladiator Gauntlet (Swiss)"

    class Category(models.TextChoices):
        BEGINNER = "beginner", "Beginner (≤1200)"
        INTERMEDIATE = "intermediate", "Intermediate (1201‑1600)"
        ADVANCED = "advanced", "Advanced (1601‑2000)"
        EXPERT = "expert", "Expert (2001+)"

    class Status(models.TextChoices):
        OPEN = "open", "Open for registration"
        FULL = "full", "Full"
        UPCOMING = "upcoming", "Upcoming (auto-scheduled)"
        ONGOING = "ongoing", "Ongoing"
        COMPLETED = "completed", "Completed"

    CATEGORY_META = {
        "beginner":     {"icon": "🥉", "css": "text-amber-600",  "label": "Beginner",     "elo": "≤ 1200"},
        "intermediate": {"icon": "🥈", "css": "text-gray-300",   "label": "Intermediate", "elo": "1201‑1600"},
        "advanced":     {"icon": "🥇", "css": "text-yellow-400", "label": "Advanced",     "elo": "1601‑2000"},
        "expert":       {"icon": "🏆", "css": "text-purple-400", "label": "Expert",       "elo": "2001+"},
    }

    CATEGORY_ELO_RANGE = {
        "beginner":     (0, 1200),
        "intermediate": (1201, 1600),
        "advanced":     (1601, 2000),
        "expert":       (2001, 99999),
    }

    TIME_CONTROL_CHOICES = [
        ("1+0", "1+0 Bullet"),
        ("2+1", "2+1 Bullet"),
        ("3+0", "3+0 Blitz"),
        ("3+1", "3+1 Blitz"),
        ("3+2", "3+2 Blitz"),
        ("5+0", "5+0 Blitz"),
        ("5+3", "5+3 Blitz"),
        ("10+0", "10+0 Rapid"),
        ("10+5", "10+5 Rapid"),
        ("15+10", "15+10 Rapid"),
    ]

    name = models.CharField(max_length=200)
    class GameType(models.TextChoices):
        CHESS = "chess", "Chess"
        BREAKTHROUGH = "breakthrough", "Breakthrough"

    description = models.TextField(blank=True)
    type = models.CharField(
        max_length=10, choices=Type.choices, default=Type.SMALL
    )
    game_type = models.CharField(
        max_length=20,
        choices=GameType.choices,
        default=GameType.CHESS,
        help_text="Game type for this tournament.",
    )
    time_control = models.CharField(
        max_length=10,
        choices=TIME_CONTROL_CHOICES,
        default="3+1",
        help_text="Time control for all games in this tournament.",
    )
    category = models.CharField(
        max_length=20,
        choices=Category.choices,
        null=True,
        blank=True,
        help_text="Fixed for small tournaments; large tournaments rotate categories each round.",
    )
    capacity = models.PositiveIntegerField(
        default=128,
        help_text="128 for small, 1024 for large.",
    )
    rounds_total = models.PositiveIntegerField(
        default=7,
        help_text="7 for small, 10 for large.",
    )
    current_round = models.PositiveIntegerField(
        default=0,
        help_text="Currently active round (0 = not started).",
    )
    players = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        through="TournamentParticipant",
        related_name="tournaments",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    champion = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tournament_wins",
        help_text="Winner of the tournament.",
    )
    start_time = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    # ── Gauntlet-specific fields ────────────────
    week_number = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Gauntlet week number (auto-incremented).",
    )
    format = models.CharField(
        max_length=20,
        default="elimination",
        choices=[
            ("elimination", "Single Elimination"),
            ("swiss", "Swiss System"),
        ],
        help_text="Bracket format for this tournament.",
    )
    announcement = models.TextField(
        blank=True,
        help_text="Auto-generated announcement text after completion.",
    )

    class Meta:
        ordering = ["-start_time"]

    def __str__(self):
        return self.name

    TYPE_DEFAULTS = {
        Type.SMALL:    {"capacity": 128,  "rounds_total": 7},
        Type.LARGE:    {"capacity": 1024, "rounds_total": 10},
        Type.QA:       {"capacity": 2,    "rounds_total": 1},
        Type.GAUNTLET: {"capacity": 16,   "rounds_total": 5},
    }

    def save(self, *args, **kwargs):
        # QA tournaments are always locked to 2 players / 1 round.
        if self.type == self.Type.QA:
            self.capacity = 2
            self.rounds_total = 1
        elif self.type == self.Type.GAUNTLET:
            # Gauntlet always uses Swiss format.
            self.format = "swiss"
            if not self.pk:
                defaults = self.TYPE_DEFAULTS[self.Type.GAUNTLET]
                self.capacity = self.capacity or defaults["capacity"]
                self.rounds_total = self.rounds_total or defaults["rounds_total"]
        elif not self.pk:
            # Apply sensible defaults on first save; admin can override afterward.
            defaults = self.TYPE_DEFAULTS.get(self.type, {})
            if self.capacity == 128 or self.capacity is None:
                self.capacity = defaults.get("capacity", self.capacity)
            if self.rounds_total == 7 or self.rounds_total is None:
                self.rounds_total = defaults.get("rounds_total", self.rounds_total)
        super().save(*args, **kwargs)

    @property
    def participant_count(self):
        return self.participants.count()

    @property
    def is_full(self):
        return self.participant_count >= self.capacity

    @property
    def fill_pct(self):
        if self.capacity == 0:
            return 0
        return round(self.participant_count / self.capacity * 100)

    @property
    def category_info(self):
        return self.CATEGORY_META.get(self.category, {})

    @property
    def time_control_display(self):
        """Return the human-readable time control label."""
        for val, label in self.TIME_CONTROL_CHOICES:
            if val == self.time_control:
                return label
        return self.time_control

    def bracket_for_round(self, round_num):
        """Return matches for a given round, ordered by bracket position."""
        return self.matches.filter(round_num=round_num).order_by("bracket_position")

    def rounds_with_matches(self):
        """Return a list of (round_num, queryset) tuples for all played rounds."""
        if self.current_round == 0:
            return []
        rounds = []
        for r in range(1, self.current_round + 1):
            qs = self.bracket_for_round(r)
            rounds.append((r, qs))
        return rounds


class TournamentParticipant(models.Model):
    """A player's entry in a tournament — tracks elimination status."""

    tournament = models.ForeignKey(
        Tournament, on_delete=models.CASCADE, related_name="participants"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tournament_entries",
    )
    seed = models.PositiveIntegerField(
        default=0, help_text="Random seed position in the bracket."
    )
    current_round = models.PositiveIntegerField(
        default=0, help_text="Last round the player competed in."
    )
    ready = models.BooleanField(
        default=False,
        help_text="Whether the player has clicked Ready (used by QA tournaments).",
    )
    eliminated = models.BooleanField(default=False)
    eliminated_in_round = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="The round number in which this player was eliminated.",
    )
    joined_at = models.DateTimeField(auto_now_add=True)
    registered_sha = models.CharField(
        max_length=40,
        blank=True,
        default="",
        help_text=(
            "HF commit SHA at tournament registration time. "
            "Used to detect model changes during the tournament. "
            "Deleted at tournament end."
        ),
    )
    tournament_hf_token = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Read-only HF token stored for pre/mid-round integrity checks. "
            "Never used for writes. Deleted at tournament end."
        ),
    )
    round_pinned_sha = models.CharField(
        max_length=40,
        blank=True,
        default="",
        help_text=(
            "Official pinned HF commit SHA captured at the start of the "
            "current round. Compared against live HF SHA by the random "
            "anti-cheat audit task."
        ),
    )
    round_pinned_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when round_pinned_sha was last refreshed.",
    )
    disqualified_for_sha_mismatch = models.BooleanField(
        default=False,
        help_text=(
            "True when the participant was disqualified by the random "
            "mid-round SHA audit (repo changed during a live round)."
        ),
    )

    # ── Probabilistic SHA audit bookkeeping ─────────────
    last_sha_check_at = models.DateTimeField(
        null=True, blank=True,
        help_text=(
            "Timestamp of the last probabilistic SHA audit performed on "
            "this participant. Used by run_probabilistic_sha_audit to "
            "score 'time-since-last-check' as a check probability factor."
        ),
    )
    sha_anomaly_history = models.BooleanField(
        default=False,
        help_text=(
            "True if this participant has ever triggered a SHA mismatch "
            "that was manually cleared/forgiven by an admin. Increases "
            "their probability of being audited again."
        ),
    )
    disqualified_reason = models.TextField(
        blank=True, default="",
        help_text="Free-form reason recorded when this participant was disqualified.",
    )

    class Meta:
        unique_together = ("tournament", "user")
        ordering = ["seed"]

    def __str__(self):
        status = "eliminated" if self.eliminated else "active"
        return f"{self.user.username} in {self.tournament.name} ({status})"


class Match(models.Model):
    """A single match within a tournament — links to one or two Game instances."""

    class MatchStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        LIVE = "live", "Live"
        COMPLETED = "completed", "Completed"

    tournament = models.ForeignKey(
        Tournament, on_delete=models.CASCADE, related_name="matches"
    )
    round_num = models.PositiveIntegerField()
    bracket_position = models.PositiveIntegerField(
        default=0,
        help_text="Position within the round (0‑indexed) for bracket rendering.",
    )
    player1 = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tournament_matches_as_p1",
    )
    player2 = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tournament_matches_as_p2",
    )
    winner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tournament_match_wins",
    )
    result = models.CharField(
        max_length=10,
        blank=True,
        help_text="'1-0', '0-1', or '1/2-1/2'.",
    )
    match_status = models.CharField(
        max_length=10,
        choices=MatchStatus.choices,
        default=MatchStatus.PENDING,
    )
    is_armageddon = models.BooleanField(
        default=False,
        help_text="True if this is an Armageddon tiebreak game.",
    )
    time_control = models.CharField(
        max_length=20,
        default="3+1",
        help_text="'3+1' for normal, '2+1' for Armageddon white, '1+0' for Armageddon black.",
    )
    elo_change_p1 = models.IntegerField(default=0)
    elo_change_p2 = models.IntegerField(default=0)
    duration = models.DurationField(
        null=True, blank=True, help_text="How long the match lasted."
    )
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["tournament", "round_num", "bracket_position", "timestamp"]

    def __str__(self):
        tag = " [ARM]" if self.is_armageddon else ""
        return (
            f"{self.tournament.name} R{self.round_num}: "
            f"{self.player1.username} vs {self.player2.username} "
            f"[{self.result or self.match_status}]{tag}"
        )

    @property
    def is_live(self):
        return self.match_status == self.MatchStatus.LIVE

    @property
    def is_completed(self):
        return self.match_status == self.MatchStatus.COMPLETED


class GauntletStanding(models.Model):
    """Per-player score tracking for a Swiss / Gauntlet tournament."""

    tournament = models.ForeignKey(
        Tournament, on_delete=models.CASCADE, related_name="standings",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="gauntlet_standings",
    )
    score = models.FloatField(default=0.0, help_text="1 per win, 0.5 per draw, 0 per loss.")
    wins = models.PositiveIntegerField(default=0)
    draws = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)
    rank = models.PositiveIntegerField(default=0, help_text="Final rank (set after each round).")
    buchholz = models.FloatField(
        default=0.0,
        help_text="Sum of opponents' scores — first tiebreaker in Swiss.",
    )

    class Meta:
        unique_together = ("tournament", "user")
        ordering = ["-score", "-buchholz", "-wins"]

    def __str__(self):
        return f"{self.user.username} — {self.score} pts in {self.tournament.name}"


class Badge(models.Model):
    """Achievements and trophies earned by users (e.g. Gauntlet Champion)."""

    class BadgeType(models.TextChoices):
        GAUNTLET_CHAMPION = "gauntlet_champion", "Gauntlet Champion"
        GAUNTLET_TOP3 = "gauntlet_top3", "Gauntlet Top 3"
        WIN_STREAK_5 = "win_streak_5", "5-Win Streak"
        WIN_STREAK_10 = "win_streak_10", "10-Win Streak"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="badges",
    )
    badge_type = models.CharField(
        max_length=30, choices=BadgeType.choices,
    )
    label = models.CharField(
        max_length=120,
        help_text="Human-readable label, e.g. 'Gauntlet Champion Week 12'.",
    )
    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="awarded_badges",
    )
    awarded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-awarded_at"]

    def __str__(self):
        return f"{self.label} — {self.user.username}"


class TournamentChatMessage(models.Model):
    """
    Simple chat message for live tournament pages (HTMX-polled).

    DEPRECATED: Tournament commenting/chat has been removed.
    Model kept to avoid migration breakage — can be removed with a future migration.
    """

    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="chat_messages",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tournament_chat_messages",
    )
    content = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["tournament", "created_at"]),
        ]

    def __str__(self):
        return f"{self.user.username}: {self.content[:40]}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Keep only the latest 100 messages per tournament
        qs = TournamentChatMessage.objects.filter(
            tournament=self.tournament
        ).order_by("-created_at")
        overflow_ids = qs.values_list("pk", flat=True)[100:]
        if overflow_ids:
            TournamentChatMessage.objects.filter(pk__in=list(overflow_ids)).delete()


class TournamentShaCheck(models.Model):
    """Persistent audit log for every random anti-cheat SHA verification.

    One row per check (PASS or FAIL) so the full forensic history of a
    tournament round can be reconstructed and filtered in admin.

    Failed checks always carry ``action_taken`` so an auditor can see at
    a glance what the system did in response (typically
    "disqualified_in_round").
    """

    class Result(models.TextChoices):
        PASS = "pass", "Pass"
        FAIL = "fail", "Fail (mismatch)"
        ERROR = "error", "Error (HF unreachable / no token)"
        SKIPPED = "skipped", "Skipped (no baseline)"

    class Context(models.TextChoices):
        ROUND_START = "round_start", "Round start baseline"
        RANDOM_AUDIT = "random_audit", "Random mid-round audit"
        MANUAL = "manual", "Manual / management command"

    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="sha_checks",
    )
    participant = models.ForeignKey(
        TournamentParticipant,
        on_delete=models.CASCADE,
        related_name="sha_checks",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sha_audit_entries",
        help_text="Denormalised — kept after participant deletion for audit.",
    )
    round_num = models.PositiveIntegerField(
        default=0,
        help_text="Tournament round when the check ran (0 = pre-start).",
    )
    game_type = models.CharField(max_length=20, blank=True, default="")
    repo_id = models.CharField(max_length=255, blank=True, default="")
    expected_sha = models.CharField(
        max_length=40, blank=True, default="",
        help_text="Pinned/baseline SHA at the start of the round.",
    )
    current_sha = models.CharField(
        max_length=40, blank=True, default="",
        help_text="SHA returned by the live HF Hub call.",
    )
    result = models.CharField(
        max_length=10, choices=Result.choices, default=Result.PASS,
    )
    context = models.CharField(
        max_length=20, choices=Context.choices, default=Context.RANDOM_AUDIT,
    )
    action_taken = models.CharField(
        max_length=80, blank=True, default="",
        help_text="e.g. 'disqualified_in_round', 'logged_only', 'forfeited_match'.",
    )
    error_message = models.TextField(
        blank=True, default="",
        help_text="Populated on Result=ERROR with the underlying exception text.",
    )
    checked_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-checked_at"]
        indexes = [
            models.Index(fields=["tournament", "round_num"]),
            models.Index(fields=["result", "-checked_at"]),
        ]
        verbose_name = "Tournament SHA Check"
        verbose_name_plural = "Tournament SHA Checks"

    def __str__(self):
        return (
            f"[{self.checked_at:%Y-%m-%d %H:%M:%S}] "
            f"R{self.round_num} {self.user_id} {self.repo_id} → {self.result}"
        )

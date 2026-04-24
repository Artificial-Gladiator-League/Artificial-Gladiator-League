from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models


def validate_hf_repo_id(value):
    """Basic validation for a Hugging Face repo ID (e.g. 'austindavis/ChessGPT_d12')."""
    import re
    if value and not re.match(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$', value):
        raise ValidationError(
            "Enter a valid Hugging Face repo ID (e.g. 'austindavis/ChessGPT_d12')."
        )


# ── Legacy stubs kept so existing migrations can still reference them ──
def ai_upload_path(instance, filename):
    return f"ai_bots/{instance.id}/{filename}"


def validate_ai_file_extension(value):
    pass


# ── Legacy stub kept so the 0001_initial migration can still reference it ──
def validate_min_age(value):
    pass


class CustomUser(AbstractUser):
    """Extended user model with AI bot info, ELO, and aggregated stats.

    Email is inherited from AbstractUser (optional, blank=True) but never
    collected during registration.  HF tokens are validated at signup and
    discarded — they are never persisted.
    """

    # ── AI Bot ──────────────────────────────────
    ai_name = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        unique=True,
        help_text="Display name for this user's AI bot.",
    )
    # DEPRECATED: Use UserGameModel.hf_model_repo_id per game type instead.
    # Kept for backwards compatibility and existing migrations.
    hf_model_repo_id = models.CharField(
        max_length=255,
        blank=True,
        validators=[validate_hf_repo_id],
        help_text="DEPRECATED — use UserGameModel. Legacy chess repo ID.",
    )

    # ── Rating & Stats (aggregated) ─────────────
    elo = models.IntegerField(default=1200)
    wins = models.IntegerField(default=0)
    losses = models.IntegerField(default=0)
    draws = models.IntegerField(default=0)
    total_games = models.IntegerField(default=0)
    current_streak = models.IntegerField(
        default=0,
        help_text="Positive = win streak, negative = loss streak.",
    )

    # ── Rated-game tracking & model locking ─────
    # Users must complete 30 rated games before their model is locked.
    # After locking, the commit SHA is frozen; changing the model blocks
    # further rated/tournament play (casual games remain allowed).
    rated_games_played = models.PositiveIntegerField(
        default=0,
        help_text="Number of rated games completed with the current AI agent.",
    )
    locked_commit_id = models.CharField(
        max_length=40,
        null=True,
        blank=True,
        help_text="Commit SHA of the HF model at the time the user reached 30 rated games.",
    )
    locked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when the model was locked after 30 rated games.",
    )
    last_known_commit_id = models.CharField(
        max_length=40,
        null=True,
        blank=True,
        help_text="Most recent commit SHA seen for this user's HF model repo.",
    )

    # ── Submission identity (HF model snapshot) ──
    submission_repo_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="HF repo ID at submission time (e.g. 'user/my-model').",
    )
    submission_repo_type = models.CharField(
        max_length=20,
        blank=True,
        default="model",
        help_text="HF repo type at submission time (e.g. 'model').",
    )
    submitted_ref = models.CharField(
        max_length=255,
        blank=True,
        help_text="Branch or tag the user chose at submission (e.g. 'main', 'v1.0').",
    )
    approved_full_sha = models.CharField(
        max_length=40,
        blank=True,
        help_text="Exact immutable commit SHA approved at submission.",
    )
    submitted_by_user = models.CharField(
        max_length=255,
        blank=True,
        help_text="HF username of the person who submitted this model.",
    )
    pinned_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when this model version was approved/pinned.",
    )

    # ── HF OAuth identity ───────────────────────
    hf_username = models.CharField(
        max_length=255,
        blank=True,
        help_text="Hugging Face username obtained via OAuth.",
    )
    hf_oauth_token_encrypted = models.TextField(
        blank=True,
        help_text="Fernet-encrypted HF OAuth access token for API calls.",
    )
    hf_oauth_linked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when the user linked their HF account via OAuth.",
    )

    # ── Daily model-integrity verification ──────
    original_model_commit_sha = models.CharField(
        max_length=40,
        null=True,
        blank=True,
        help_text="Commit SHA recorded at registration — the baseline for integrity checks.",
    )
    last_model_validation_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date (UTC) of the most recent successful daily model integrity check.",
    )
    model_integrity_ok = models.BooleanField(
        default=True,
        help_text="False = model changed or needs re-validation. Blocks rated/tournament play.",
    )

    class Meta:
        ordering = ["-elo"]
        db_table = "users_user"

    def __str__(self):
        return f"{self.username} (ELO {self.elo})"

    # ── Computed properties ─────────────────────
    def get_game_model(self, game_type: str):
        """Return the UserGameModel for the given game type, or None."""
        try:
            return self.game_models.get(game_type=game_type)
        except UserGameModel.DoesNotExist:
            return None

    def get_repo_for_game(self, game_type: str) -> str:
        """Return the HF repo ID for a game type, with legacy fallback."""
        gm = self.get_game_model(game_type)
        if gm:
            return gm.hf_model_repo_id
        # Legacy fallback: use the old field for chess
        if game_type == "chess" and self.hf_model_repo_id:
            return self.hf_model_repo_id
        return ""

    @property
    def country_flag(self) -> str:
        """Stub — country field was removed; returns empty string."""
        return ""

    # ── Friend helpers ─────────────────────────
    @property
    def friends(self):
        """Social features removed — return empty queryset."""
        return CustomUser.objects.none()

    @property
    def pending_sent_requests(self):
        return []

    @property
    def pending_received_requests(self):
        return []

    def is_friend(self, other_user) -> bool:
        return False

    @property
    def win_rate(self) -> float:
        """Win rate as a float between 0.0 and 1.0."""
        if self.total_games == 0:
            return 0.0
        return round(self.wins / self.total_games, 4)

    def get_category(self) -> dict:
        """Return a dict with category label, tier name, and icon class.

        Ranges:
            ≤ 1200  → Beginner  / Novice   / 🥉 Bronze
            1201‑1600 → Intermediate / Club Player / 🥈 Silver
            1601‑2000 → Advanced / Expert  / 🥇 Gold
            2001+   → Expert  / Master  / 🏆 Platinum
        """
        if self.elo <= 1200:
            return {
                "category": "Beginner",
                "tier": "Novice",
                "icon": "🥉",
                "css": "text-amber-600",
            }
        elif self.elo <= 1600:
            return {
                "category": "Intermediate",
                "tier": "Club Player",
                "icon": "🥈",
                "css": "text-gray-300",
            }
        elif self.elo <= 2000:
            return {
                "category": "Advanced",
                "tier": "Expert",
                "icon": "🥇",
                "css": "text-yellow-400",
            }
        else:
            return {
                "category": "Expert",
                "tier": "Master",
                "icon": "🏆",
                "css": "text-purple-400",
            }

    def get_fide_title(self) -> dict:
        """Return FIDE-style title based on ELO rating.

        Ranges mirror standard FIDE title thresholds (adapted):
            < 1200  → (none)
            1200–1399 → CM  (Candidate Master)
            1400–1599 → FM  (FIDE Master)
            1600–1799 → IM  (International Master)
            1800–1999 → GM  (Grandmaster)
            2000–2199 → SGM (Super Grandmaster)
            ≥ 2200   → WC  (World Champion)
        """
        if self.elo < 1200:
            return {"abbr": "", "title": "", "css": ""}
        elif self.elo < 1400:
            return {"abbr": "CM", "title": "Candidate Master", "css": "text-gray-400"}
        elif self.elo < 1600:
            return {"abbr": "FM", "title": "FIDE Master", "css": "text-blue-400"}
        elif self.elo < 1800:
            return {"abbr": "IM", "title": "International Master", "css": "text-yellow-400"}
        elif self.elo < 2000:
            return {"abbr": "GM", "title": "Grandmaster", "css": "text-orange-400"}
        elif self.elo < 2200:
            return {"abbr": "SGM", "title": "Super Grandmaster", "css": "text-red-400"}
        else:
            return {"abbr": "WC", "title": "World Champion", "css": "text-purple-400"}


class UserGameModel(models.Model):
    """Per-game AI model configuration for a user.

    Each user can register a different HF model for each supported game type.
    Integrity-check fields live here instead of on CustomUser so they are
    tracked independently per game.
    """

    class GameType(models.TextChoices):
        CHESS = "chess", "Chess"
        BREAKTHROUGH = "breakthrough", "Breakthrough"

    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="game_models",
    )
    game_type = models.CharField(
        max_length=20,
        choices=GameType.choices,
    )
    hf_model_repo_id = models.CharField(
        max_length=255,
        validators=[validate_hf_repo_id],
        help_text="Hugging Face repo ID for this game (e.g. austindavis/ChessGPT_d12).",
    )
    hf_data_repo_id = models.CharField(
        max_length=255,
        blank=True,
        validators=[validate_hf_repo_id],
        help_text="Hugging Face data repo ID (e.g. username/breakthrough-data or username/chess-data). Used for Breakthrough and Chess.",
    )

    # ── Integrity & locking (per-game) ──────────
    original_model_commit_sha = models.CharField(
        max_length=40,
        null=True,
        blank=True,
        help_text="Commit SHA recorded at submission — baseline for integrity checks.",
    )
    last_known_commit_id = models.CharField(
        max_length=40,
        null=True,
        blank=True,
        help_text="Most recent commit SHA seen for this model.",
    )
    model_integrity_ok = models.BooleanField(
        default=True,
        help_text="False = model changed or needs re-validation.",
    )
    last_model_validation_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date (UTC) of the most recent successful integrity check.",
    )
    locked_commit_id = models.CharField(
        max_length=40,
        null=True,
        blank=True,
        help_text="Commit SHA frozen after the rated-game threshold.",
    )
    rated_games_played = models.PositiveIntegerField(
        default=0,
        help_text="Number of rated games completed with this model.",
    )
    rated_games_since_revalidation = models.PositiveIntegerField(
        default=0,
        help_text="Rated games played since the last integrity re-validation. "
                  "Must reach 30 before tournament entry after a revision change.",
    )
    locked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when the model was locked after reaching the game threshold.",
    )

    # ── Inference Endpoint (deprecated — kept for migration compat) ──
    hf_inference_endpoint_url = models.URLField(
        max_length=500,
        blank=True,
        help_text="DEPRECATED. Kept for backwards compatibility.",
    )
    hf_inference_endpoint_name = models.CharField(
        max_length=120,
        blank=True,
        help_text="DEPRECATED. Kept for backwards compatibility.",
    )

    # Add hf_inference_endpoint_id for compatibility with DB
    hf_inference_endpoint_id = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        default="",
        help_text="HF Inference Endpoint ID (for compatibility with DB, can be blank)",
    )

    # Add hf_inference_endpoint_status to avoid DB IntegrityError when column exists
    hf_inference_endpoint_status = models.CharField(
        max_length=40,
        blank=True,
        null=True,
        default="",
        help_text="HF Inference Endpoint status (compatibility field, can be blank)",
    )

    # ── Docker sandbox verification ────────────
    class VerificationStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SUSPICIOUS = "suspicious", "Suspicious"

    verification_status = models.CharField(
        max_length=20,
        choices=VerificationStatus.choices,
        default=VerificationStatus.PENDING,
        help_text="Result of the Docker sandbox security scan.",
    )
    last_verified_commit = models.CharField(
        max_length=40,
        blank=True,
        help_text="Commit SHA that was last verified in the sandbox.",
    )
    last_verified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the most recent sandbox verification.",
    )
    scan_report = models.JSONField(
        default=dict,
        blank=True,
        help_text="JSON output from the security scanner (bandit, modelscan, etc.).",
    )
    # ── Cached model metadata (persisted after successful verification) ──
    cached_path = models.CharField(
        max_length=1024, blank=True,
        help_text="Filesystem path to the user's cached model directory.",
    )
    cached_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Timestamp when the cached model files were written.",
    )
    cached_commit = models.CharField(
        max_length=40, blank=True,
        help_text="Commit SHA corresponding to the cached snapshot.",
    )

    # ── Submission identity ─────────────────────
    submission_repo_type = models.CharField(
        max_length=20, blank=True, default="model",
    )
    submitted_ref = models.CharField(
        max_length=255, blank=True,
        help_text="Branch or tag chosen at submission (e.g. 'main').",
    )
    approved_full_sha = models.CharField(
        max_length=40, blank=True,
        help_text="Exact immutable commit SHA approved at submission.",
    )
    pinned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("user", "game_type")
        verbose_name = "User Game Model"
        verbose_name_plural = "User Game Models"

    def __str__(self):
        return f"{self.user.username} — {self.get_game_type_display()} ({self.hf_model_repo_id})"


class GDPRRequest(models.Model):
    """User request for data access (export) or account deletion (GDPR Art. 15 / 17)."""

    class RequestType(models.TextChoices):
        ACCESS = "access", "Data Access (Export)"
        DELETION = "deletion", "Account Deletion"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        DENIED = "denied", "Denied"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="gdpr_requests",
    )
    request_type = models.CharField(
        max_length=10,
        choices=RequestType.choices,
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.PENDING,
    )
    reason = models.TextField(
        blank=True,
        help_text="Optional reason or notes.",
    )
    admin_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "GDPR Request"
        verbose_name_plural = "GDPR Requests"

    def __str__(self):
        return f"{self.get_request_type_display()} — {self.user.username} ({self.status})"

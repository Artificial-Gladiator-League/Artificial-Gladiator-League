from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models


def validate_hf_repo_id(value):
    """Basic validation for a Hugging Face repo ID (e.g. 'austindavis/ChessGPT_d12').

    Rejects Space runtime URLs (e.g. 'https://user-space.hf.space') with a
    specific, actionable message instead of the generic format error, since
    that is the most common mistake users make when pasting this field.
    """
    import re
    if not value:
        return

    lowered = value.strip().lower()
    if "hf.space" in lowered or lowered.startswith(("http://", "https://")):
        suggestion = ""
        m = re.match(r'^https?://([\w-]+)-([\w-]+)\.hf\.space/?$', value.strip(), re.IGNORECASE)
        if m:
            suggestion = f" Did you mean '{m.group(1)}/{m.group(2)}'?"
        raise ValidationError(
            "Enter your repo ID as 'username/repo-name', not the Space URL." + suggestion
        )

    if not re.match(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$', value):
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


COUNTRY_LOCKED_MESSAGE = (
    "The country of residence cannot be changed once it is saved. "
    "To correct it, contact support."
)


class CustomUser(AbstractUser):
    """Extended user model with AI bot info, ELO, and aggregated stats.

    Email is inherited from AbstractUser (optional, blank=True) but never
    collected during registration.  HF tokens are validated at signup and
    discarded — they are never persisted.
    """

    # ── Pending email change ─────────────────────
    pending_email = models.EmailField(
        blank=True,
        null=True,
        help_text="Unconfirmed new email awaiting confirmation from the user.",
    )

    # ── Gladiator character sheet ───────────────
    class FightingStyle(models.TextChoices):
        UNHINGED_RAGE = "Unhinged Rage", "Unhinged Rage"
        SILENT_ASSASSIN = "Silent Assassin", "Silent Assassin"
        RAGE_PHILOSOPHER = "The Rage Philosopher", "The Rage Philosopher"
        CHAOTIC_GENIUS = "Chaotic Genius", "Chaotic Genius"

    MOODS = ("😈", "🔥", "🧠", "😎", "💀", "🤡")

    origin_story = models.TextField(max_length=1000, blank=True, default="")
    fighting_style = models.CharField(
        max_length=40,
        choices=FightingStyle.choices,
        default=FightingStyle.UNHINGED_RAGE,
    )
    current_mood = models.CharField(max_length=8, default="😈")

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
    # Per-game ELO ratings — tracked independently so Chess and Breakthrough
    # ratings do not interfere with each other.
    elo_chess = models.IntegerField(
        default=1200,
        db_default=1200,
        help_text="ELO rating for Chess games only.",
    )
    elo_breakthrough = models.IntegerField(
        default=1200,
        db_default=1200,
        help_text="ELO rating for Breakthrough games only.",
    )
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

    # ── PayPal payout email ──────────────────────
    paypal_email = models.EmailField(
        max_length=254,
        blank=True,
        default="",
        help_text=(
            "PayPal email address for cash-tournament prize payouts. "
            "Set by the user in their profile. Never used as a login credential."
        ),
    )

    is_official_bot = models.BooleanField(default=False)

    # ── Country of residence (convenience layer) ─────────────
    # Set once (registration, or once from profile settings for older accounts) and
    # then locked. It never replaces the IP geo gate, the per-tournament residency
    # declaration or winner verification; it only powers an early filter and the
    # profile flag. Changed only by staff, with a reason (see change_country_by_staff).
    country = models.CharField(
        max_length=2,
        blank=True,
        default="",
        help_text="ISO 3166-1 alpha-2 country of residence. Blank = not set yet.",
    )
    country_set_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the country was saved (and locked).",
    )
    country_locked = models.BooleanField(
        default=False,
        help_text="True once the country has been saved; users cannot change it afterwards.",
    )
    show_flag = models.BooleanField(
        default=True,
        help_text="Show the country flag next to the username on the user's profile page.",
    )

    COUNTRY_FIELDS = ("country", "country_set_at", "country_locked")

    class Meta:
        ordering = ["-elo"]
        db_table = "users_user"

    def __str__(self):
        return f"{self.username} (ELO {self.elo})"

    # ── Country lock ───────────────────────────────────────
    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        if "country" in field_names:
            instance._country_snapshot = instance.country
        return instance

    def save(self, *args, **kwargs):
        """Plain saves can never write the country fields on an existing user.

        * An explicit ``update_fields`` naming a country field raises.
        * A full save whose in-memory country differs from what was loaded raises.
        * Otherwise the country fields are left out of the UPDATE, so a stale copy of
          the user can never overwrite a country saved in the meantime.
        Legitimate writers use ``claim_country`` or ``change_country_by_staff``
        (queryset updates), which do not go through this method.
        """
        if not self._state.adding and self.pk:
            fields = kwargs.get("update_fields")
            if fields is not None:
                if any(f in self.COUNTRY_FIELDS for f in fields):
                    raise ValidationError(COUNTRY_LOCKED_MESSAGE)
            else:
                loaded = getattr(self, "_country_snapshot", None)
                if loaded is not None and loaded != self.country:
                    raise ValidationError(COUNTRY_LOCKED_MESSAGE)
                deferred = self.get_deferred_fields()
                kwargs["update_fields"] = [
                    f.name for f in self._meta.concrete_fields
                    if not f.primary_key and f.name not in self.COUNTRY_FIELDS
                    and f.attname not in deferred
                ]
        super().save(*args, **kwargs)

    def claim_country(self, code: str) -> bool:
        """Set the country exactly once and lock it. True if this call set it.

        The conditional UPDATE is atomic, so two concurrent requests cannot both win.
        """
        from django.utils import timezone

        code = (code or "").upper()
        now = timezone.now()
        updated = type(self).objects.filter(pk=self.pk, country="").update(
            country=code, country_set_at=now, country_locked=True,
        )
        if updated:
            self.country, self.country_set_at, self.country_locked = code, now, True
            self._country_snapshot = code
        return bool(updated)

    def change_country_by_staff(self, new_code: str, by, reason: str):
        """The only way to change a saved country: needs a reason and is logged."""
        from django.db import transaction
        from django.utils import timezone

        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("A reason is required to change a locked country.")
        new_code = (new_code or "").upper()
        with transaction.atomic():
            old = type(self).objects.select_for_update().get(pk=self.pk).country
            now = timezone.now()
            type(self).objects.filter(pk=self.pk).update(
                country=new_code, country_set_at=now, country_locked=bool(new_code),
            )
            log = CountryChangeLog.objects.create(
                user=self, changed_by=by, old_country=old, new_country=new_code, reason=reason,
            )
        self.country, self.country_set_at, self.country_locked = new_code, now, bool(new_code)
        self._country_snapshot = new_code
        return log

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

    def get_elo_for_game(self, game_type: str) -> int:
        """Return the per-game ELO for the given game type."""
        if game_type == "breakthrough":
            return self.elo_breakthrough
        return self.elo_chess  # chess and any unknown type

    def set_elo_for_game(self, game_type: str, value: int) -> None:
        """Set the per-game ELO field for the given game type (does NOT save)."""
        if game_type == "breakthrough":
            self.elo_breakthrough = value
        else:
            self.elo_chess = value
        # Keep the legacy elo field as the best-of-both so category/title
        # calculations and any code still using user.elo stay meaningful.
        self.elo = max(self.elo_chess, self.elo_breakthrough)

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
        """Return a dict with category label, tier name, icon and CSS class.

        Ranges (FIDE-aligned):
            2700+      → Super Grandmaster (elite)
            2500–2699  → Grandmaster (GM)
            2400–2499  → International Master (IM)
            2300–2399  → FIDE Master (FM)
            2200–2299  → Candidate Master (CM)
            2000–2199  → Expert
            1800–1999  → Class A
            1600–1799  → Class B
            1400–1599  → Class C
            1200–1399  → Class D
            < 1200     → Beginner
        """
        elo = self.elo
        if elo >= 2700:
            return {"category": "super_gm",    "tier": "Super Grandmaster", "icon": "👑", "css": "text-yellow-300"}
        elif elo >= 2500:
            return {"category": "gm",          "tier": "Grandmaster",       "icon": "🏆", "css": "text-purple-400"}
        elif elo >= 2400:
            return {"category": "im",          "tier": "International Master", "icon": "🥇", "css": "text-yellow-400"}
        elif elo >= 2300:
            return {"category": "fm",          "tier": "FIDE Master",       "icon": "🥈", "css": "text-blue-300"}
        elif elo >= 2200:
            return {"category": "cm",          "tier": "Candidate Master",  "icon": "🥈", "css": "text-gray-300"}
        elif elo >= 2000:
            return {"category": "expert",      "tier": "Expert",            "icon": "🥇", "css": "text-orange-400"}
        elif elo >= 1800:
            return {"category": "class_a",     "tier": "Class A",           "icon": "🔴", "css": "text-red-400"}
        elif elo >= 1600:
            return {"category": "class_b",     "tier": "Class B",           "icon": "🟠", "css": "text-orange-300"}
        elif elo >= 1400:
            return {"category": "class_c",     "tier": "Class C",           "icon": "🟡", "css": "text-yellow-500"}
        elif elo >= 1200:
            return {"category": "class_d",     "tier": "Class D",           "icon": "🟢", "css": "text-green-400"}
        else:
            return {"category": "beginner",    "tier": "Beginner",          "icon": "🥉", "css": "text-amber-600"}

    def get_fide_title(self) -> dict:
        """Return FIDE-style title abbreviation based on ELO rating.

        Ranges (standard FIDE thresholds):
            2700+      → SGM (Super Grandmaster)
            2500–2699  → GM  (Grandmaster)
            2400–2499  → IM  (International Master)
            2300–2399  → FM  (FIDE Master)
            2200–2299  → CM  (Candidate Master)
            < 2200     → (none)
        """
        if self.elo >= 2700:
            return {"abbr": "SGM", "title": "Super Grandmaster", "css": "text-yellow-300"}
        elif self.elo >= 2500:
            return {"abbr": "GM",  "title": "Grandmaster",       "css": "text-purple-400"}
        elif self.elo >= 2400:
            return {"abbr": "IM",  "title": "International Master", "css": "text-yellow-400"}
        elif self.elo >= 2300:
            return {"abbr": "FM",  "title": "FIDE Master",       "css": "text-blue-300"}
        elif self.elo >= 2200:
            return {"abbr": "CM",  "title": "Candidate Master",  "css": "text-gray-300"}
        else:
            return {"abbr": "", "title": "", "css": ""}


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
    # True only for the user who changed their repo; cleared by the cooldown logic.
    # Avoids global side-effects: only the offender is flagged, not all participants.
    repo_changed = models.BooleanField(
        default=False,
        help_text="Set when this user changes their HF repo. Requires 30 rated games to clear.",
    )
    locked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when the model was locked after reaching the game threshold.",
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

    # ── User-code contract check (apps.games.model_check.check_model) ──
    # Only a SHA whose check passes (status=ACTIVE) may be used in games.
    class ContractStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        FAILED = "failed", "Failed"

    status = models.CharField(
        max_length=20,
        choices=ContractStatus.choices,
        default=ContractStatus.PENDING,
        help_text="Result of the last model_check.check_model() contract check.",
    )
    last_error = models.TextField(
        blank=True,
        default="",
        help_text="Human-readable problems from the last check_model() run.",
    )

    # ── Sandbox runtime errors (surfaced to the model owner) ────
    last_sandbox_error = models.TextField(
        blank=True, default="",
        help_text="Most recent Docker sandbox error for this model (e.g. missing "
                  "data file). Cleared automatically after a successful move.",
    )
    last_sandbox_error_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Timestamp of the most recent sandbox error.",
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
    current_repo_sha = models.CharField(
        max_length=64, null=True, blank=True,
        help_text="SHA of the repo at the time it was last verified/approved.",
    )
    new_repo_sha = models.CharField(
        max_length=64, null=True, blank=True,
        help_text="SHA of the incoming repo when a change is detected.",
    )
    approved_data_repo_sha = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Exact immutable data-repo commit SHA approved at submission.",
    )
    current_data_repo_sha = models.CharField(
        max_length=64, blank=True, default="",
        help_text="SHA of the data repo at the time it was last verified/approved.",
    )
    new_data_repo_sha = models.CharField(
        max_length=64, blank=True, default="",
        help_text="SHA of the incoming data repo when a change is detected.",
    )
    pinned_at = models.DateTimeField(null=True, blank=True)

    # ── Proof-of-Ownership verification ─────────
    is_verified = models.BooleanField(
        default=False,
        help_text="True once the user has proven repo ownership via AGL_VERIFY.txt challenge.",
    )
    # Per-step ownership verification flags (set individually by check_full_ownership)
    model_repo_ownership_verified = models.BooleanField(
        default=False,
        help_text="True when AGL_VERIFY.txt in the model repo matches the verification code.",
    )
    data_repo_ownership_verified = models.BooleanField(
        default=False,
        help_text="True when AGL_VERIFY.txt in the data repo matches the verification code.",
    )
    verification_code = models.CharField(
        max_length=64,
        blank=True,
        help_text="Challenge code the user must place in AGL_VERIFY.txt to prove ownership.",
    )
    verification_code_issued_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the verification challenge code was last generated.",
    )
    verified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the most recent successful ownership verification.",
    )
    repo_last_modified_at_registration = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Latest commit timestamp of the repo at tournament registration. Used to detect post-registration changes.",
    )

    class Meta:
        unique_together = ("user", "game_type")
        verbose_name = "User Game Model"
        verbose_name_plural = "User Game Models"

    # ── Repo-change guard ──────────────────────────────────────────────────
    # Whenever the model repo ID is changed (by anyone — admin, view, command)
    # the user must re-accumulate 30 rated games before entering a tournament.
    # Resetting the counter here (rather than relying on the async integrity
    # check) ensures the gate is enforced immediately, regardless of code path.
    _REPO_CHANGE_FIELDS = ("hf_model_repo_id",)

    def save(self, *args, **kwargs):
        if self.pk:
            try:
                old = UserGameModel.objects.only(*self._REPO_CHANGE_FIELDS).get(pk=self.pk)
            except UserGameModel.DoesNotExist:
                old = None

            if old is not None:
                repo_changed = any(
                    getattr(old, f) != getattr(self, f)
                    for f in self._REPO_CHANGE_FIELDS
                )
                if repo_changed:
                    self.current_repo_sha = self.new_repo_sha
                    self.new_repo_sha = self.last_known_commit_id
                    self.rated_games_since_revalidation = 0
                    self.model_integrity_ok = False
                    self.repo_changed = True
                    # When the caller uses update_fields, ensure our reset
                    # fields are included so they are actually written to DB.
                    update_fields = kwargs.get("update_fields")
                    if update_fields is not None:
                        extra = {
                            "rated_games_since_revalidation", "model_integrity_ok",
                            "repo_changed", "current_repo_sha", "new_repo_sha",
                        }
                        kwargs["update_fields"] = list(set(update_fields) | extra)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} — {self.get_game_type_display()} ({self.hf_model_repo_id})"


class DiagnosticRun(models.Model):
    """One read-only "Diagnostics" run for a user's model (see apps.users.diagnostics)."""

    class State(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        FINISHED = "finished", "Finished"
        ERROR = "error", "Error"

    class Verdict(models.TextChoices):
        NONE = "", "None"
        PASS = "pass", "Pass"
        FAIL = "fail", "Fail"
        PLATFORM_ERROR = "platform_error", "Platform error"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="diagnostic_runs",
    )
    game_type = models.CharField(max_length=20, choices=UserGameModel.GameType.choices)
    state = models.CharField(max_length=10, choices=State.choices, default=State.QUEUED)
    verdict = models.CharField(max_length=20, choices=Verdict.choices, blank=True, default="")
    lines = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "game_type", "-created_at"], name="diagrun_user_game_created"),
        ]

    def __str__(self):
        return f"DiagnosticRun {self.pk} {self.user_id}/{self.game_type} ({self.state})"


class CountryChangeLog(models.Model):
    """Audit trail of staff changes to a user's locked country."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="country_changes",
    )
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    changed_at = models.DateTimeField(auto_now_add=True)
    old_country = models.CharField(max_length=2, blank=True)
    new_country = models.CharField(max_length=2, blank=True)
    reason = models.TextField()

    class Meta:
        ordering = ["-changed_at"]

    def __str__(self):
        return f"{self.user_id}: {self.old_country or '-'} -> {self.new_country or '-'} ({self.changed_at:%Y-%m-%d})"


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

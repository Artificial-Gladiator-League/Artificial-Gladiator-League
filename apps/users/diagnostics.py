# ──────────────────────────────────────────────
# apps/users/diagnostics.py
#
# run_diagnostics(run_id): the READ-ONLY "Diagnostics" check behind the
# profile page's Diagnostics tab. Executed by the Celery task
# apps.users.tasks.run_diagnostics_task; the browser polls DiagnosticRun.lines.
#
# Hard rule: this module never writes to UserGameModel. It only reads fields,
# so it must not call gm.save(), check_full_ownership(), record_original_sha()
# or anything else with side effects on the model state.
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.utils import timezone

from .models import DiagnosticRun, UserGameModel

log = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 600
KEEP_RUNS_PER_GAME = 5
TOTAL_STEPS = 7

PLATFORM_ACCOUNT = "ArtificialGladiatorLeague"
UNEXPECTED_FAILURE_MESSAGE = "Diagnostics failed unexpectedly, please try again later."


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Output sanitizing (everything below goes to the user)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_WIN_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s'\"<>|]*")
_POSIX_PATH_RE = re.compile(
    r"(?<![\w./-])/(?:home|tmp|var|usr|opt|root|srv|app|etc|mnt|Users|private)(?:/[^\s'\"<>]*)?"
)
_HF_TOKEN_RE = re.compile(r"\bhf_[A-Za-z0-9]{10,}")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{10,}")
_WHITESPACE_RE = re.compile(r"\s+")


def _known_root_patterns() -> list[re.Pattern]:
    roots: set[str] = set()
    candidates = (
        getattr(settings, "BASE_DIR", None),
        getattr(settings, "USER_MODELS_BASE_DIR", None),
        getattr(settings, "LIVE_MODELS_ROOT", None),
        getattr(settings, "MODEL_CACHE_ROOT", None),
        getattr(settings, "HF_HUB_CACHE", None),
        tempfile.gettempdir(),
    )
    for value in candidates:
        if not value:
            continue
        text = str(value).rstrip("\\/")
        if len(text) > 1:
            roots.add(text)
    patterns = []
    for root in sorted(roots, key=len, reverse=True):
        parts = [re.escape(p) for p in re.split(r"[\\/]", root)]
        body = r"[\\/]".join(parts)
        patterns.append(re.compile(body + r"(?:[\\/][^\s'\"<>|]*)?", re.IGNORECASE))
    return patterns


def sanitize(text) -> str:
    """Return *text* made safe to show to a user.

    Removes absolute server paths and anything that looks like a token,
    collapses whitespace, and truncates to about ``MAX_MESSAGE_LEN`` characters.
    """
    text = "" if text is None else str(text)

    for pattern in _known_root_patterns():
        text = pattern.sub("<path>", text)
    text = _WIN_PATH_RE.sub("<path>", text)
    text = _POSIX_PATH_RE.sub("<path>", text)

    platform_token = (getattr(settings, "HF_PLATFORM_TOKEN", "") or "").strip()
    if len(platform_token) >= 8:
        text = text.replace(platform_token, "[hidden]")
    text = _HF_TOKEN_RE.sub("hf_[hidden]", text)
    text = _BEARER_RE.sub("Bearer [hidden]", text)

    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > MAX_MESSAGE_LEN:
        text = text[: MAX_MESSAGE_LEN - 3].rstrip() + "..."
    return text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Run bookkeeping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _RunLog:
    """Appends terminal lines to a DiagnosticRun, saving after every line so polling sees progress."""

    def __init__(self, run: DiagnosticRun):
        self.run = run
        self.lines: list[dict] = list(run.lines or [])

    def add(self, level: str, msg) -> None:
        self.lines.append({
            "t": timezone.now().strftime("%H:%M:%S"),
            "level": level,
            "msg": sanitize(msg),
        })
        self.run.lines = self.lines
        self.run.save(update_fields=["lines"])

    def info(self, msg) -> None:
        self.add("info", msg)

    def ok(self, msg) -> None:
        self.add("ok", msg)

    def warn(self, msg) -> None:
        self.add("warn", msg)

    def fail(self, msg) -> None:
        self.add("fail", msg)


class _Findings:
    """Problems collected across the steps, each with one short concrete fix."""

    def __init__(self):
        self.problems: list[tuple[str, str]] = []
        self.platform_issue = False
        self.sandbox_unavailable = False

    def add(self, summary: str, fix: str) -> None:
        summary = sanitize(summary)
        if any(existing == summary for existing, _ in self.problems):
            return
        self.problems.append((summary, sanitize(fix)))


def _prune_old_runs(user_id: int, game_type: str) -> None:
    qs = DiagnosticRun.objects.filter(user_id=user_id, game_type=game_type)
    keep = list(qs.order_by("-created_at", "-id").values_list("id", flat=True)[:KEEP_RUNS_PER_GAME])
    qs.exclude(id__in=keep).delete()


def _finish_with_error(run: DiagnosticRun, run_log: _RunLog, message: str) -> None:
    run_log.fail(message)
    run.state = DiagnosticRun.State.ERROR
    run.finished_at = timezone.now()
    run.save(update_fields=["state", "finished_at"])


def fail_run(run_id: int, message: str) -> None:
    """Mark a queued/running run as errored (used by the task on timeout)."""
    run = DiagnosticRun.objects.filter(pk=run_id).first()
    if run is None or run.state not in (DiagnosticRun.State.QUEUED, DiagnosticRun.State.RUNNING):
        return
    _finish_with_error(run, _RunLog(run), message)
    _prune_old_runs(run.user_id, run.game_type)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _short(sha) -> str:
    sha = (sha or "").strip()
    return sha[:12] if sha else "(none)"


def _step(run_log: _RunLog, number: int, title: str) -> None:
    run_log.info(f"Step {number}/{TOTAL_STEPS} - {title}")


_TRANSIENT_MARKERS = (
    "Could not reach Hugging Face",
    "HuggingFace API error",
    "Unexpected error",
    "huggingface_hub is not installed",
)


def _is_transient(error: str) -> bool:
    return any(marker in error for marker in _TRANSIENT_MARKERS)


_MISSING_RE = re.compile(r"missing files listed in config_\w+\.json:\s*(.*?)\.?\s*$", re.DOTALL)


def _parse_missing(error: str) -> list[str]:
    """Pull the file names out of the profile helper's "missing files listed ..." message."""
    match = _MISSING_RE.search(error or "")
    if not match:
        return []
    return [name.strip() for name in match.group(1).split(",") if name.strip()]


def _fix_for_error(error: str) -> str:
    low = error.lower()
    if "invalid json" in low:
        return "Fix the JSON syntax of the config file in your repo and run diagnostics again."
    if "not found in" in low and "config_" in low:
        return "Add the missing config file to the root of the repo and run diagnostics again."
    if "not found or you lack access" in low or "access denied" in low:
        return (
            f"Check the repo ID and approve '{PLATFORM_ACCOUNT}' in the repo's "
            "Access Settings on Hugging Face."
        )
    return "Fix the problem described above and run diagnostics again."


def _fix_for_contract_problem(problem: str) -> str:
    low = problem.lower()
    if "syntax error" in low:
        return "Fix the syntax error in the named file, push the change, and run diagnostics again."
    if "not valid json" in low or "invalid json" in low:
        return "Fix the JSON syntax of config_model.json."
    if "not a legal move" in low:
        return "Make get_move() return a legal move in UCI notation for the given position."
    if "returned no move" in low:
        return "Make get_move(state, fen, player) return a move string for every position."
    if "load(ctx)" in low or "module import failed" in low or "no model module" in low:
        return "Make sure your module imports cleanly and defines load(ctx) and get_move(state, fen, player)."
    if "data file" in low or "does not exist" in low or "was not found" in low:
        return "Commit the missing file to the correct repo and make sure its name matches config_model.json."
    if "time limit" in low or "exceeded" in low:
        return "Make load() and get_move() faster so the whole check finishes within the time limit."
    return "Fix the reported problem in your model repo, then run diagnostics again."


def _resolve_dirs_readonly(gm: UserGameModel) -> tuple[Path | None, Path | None]:
    """Same model/data directories the platform uses for this model, without downloading anything.

    Mirrors apps.games.local_inference._resolve_for_sandbox, except that the data
    repo is never fetched (that would be a network download from a read-only check).
    """
    from apps.games import local_inference as li

    model_revision = (
        (gm.approved_full_sha or "").strip()
        or (gm.current_repo_sha or "").strip()
        or None
    )
    model_dir, data_dir = li.resolve_model_path(
        gm.user_id, gm.game_type, repo_id=gm.hf_model_repo_id or None, model_revision=model_revision,
    )
    if model_dir is None:
        return None, None

    data_repo_id = (gm.hf_data_repo_id or "").strip()
    if data_dir is None and data_repo_id:
        revision = (
            (gm.approved_data_repo_sha or "").strip()
            or (gm.current_data_repo_sha or "").strip()
            or "main"
        )
        cached = (
            li._models_base() / f"user_{gm.user_id}" / gm.game_type
            / "data_cache" / li._dataset_cache_folder_name(data_repo_id) / revision
        )
        if li._has_real_data_files(cached):
            data_dir = cached
    return model_dir, data_dir


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Steps
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_missing_model_message(user, game_type: str) -> str | None:
    """Return None if the user has a model repo ID for *game_type*, else one clear message to show."""
    gm = user.get_game_model(game_type)
    if gm is not None and (gm.hf_model_repo_id or "").strip():
        return None
    from apps.users.views import GAME_TYPES

    label = next((g["label"] for g in GAME_TYPES if g["type"] == game_type), game_type)
    return (
        f"No {label} model is registered on your account. Open the Gladiator tab, enter your "
        f"Hugging Face model repo ID for {label}, click Connect Repo, then verify ownership. "
        "After that, run diagnostics again."
    )


def _step_registration(L: _RunLog, F: _Findings, user, game_type: str, label: str):
    _step(L, 1, "Registration")
    missing_message = get_missing_model_message(user, game_type)
    gm = user.get_game_model(game_type)
    if missing_message is not None:
        L.fail(missing_message)
        F.add(
            f"No {label} model is registered.",
            "Open the Gladiator tab, enter your model repo ID for this game and save.",
        )
        return None
    L.ok(f"{label} model is registered: {gm.hf_model_repo_id}")
    data_repo = (gm.hf_data_repo_id or "").strip()
    if data_repo:
        L.info(f"Data repo: {data_repo}")
    else:
        L.warn("No data repo ID is saved; the default '<username>/<game>-data' will be assumed.")
    return gm


def _step_repo_access(L: _RunLog, F: _Findings, gm: UserGameModel) -> None:
    _step(L, 2, "Model repo access")
    from apps.users import views as user_views

    error = user_views._check_repo_is_gated(gm.hf_model_repo_id)
    if not error:
        L.ok(f"Model repo '{gm.hf_model_repo_id}' exists, is gated, and the platform account has access.")
        return
    if _is_transient(error):
        L.warn(error)
        L.warn("This looks like a temporary platform or network issue, not a problem with your model.")
        F.platform_issue = True
        return
    L.fail(error)
    low = error.lower()
    if low.startswith("required: grant access"):
        fix = f"Approve '{PLATFORM_ACCOUNT}' in your model repo's Access Settings on Hugging Face."
    elif "was not found" in low:
        fix = "Check the repo ID in the Gladiator tab and make sure the repo exists on Hugging Face."
    elif "is public" in low:
        fix = "Enable 'Access Requests' (gated) in your model repo's Settings on Hugging Face."
    else:
        fix = "Fix the model repo settings on Hugging Face and run diagnostics again."
    F.add(error, fix)


def _step_ownership(L: _RunLog, F: _Findings, gm: UserGameModel) -> None:
    _step(L, 3, "Ownership proof")
    missing = []
    for flag, name in (
        (gm.model_repo_ownership_verified, "model"),
        (gm.data_repo_ownership_verified, "data"),
    ):
        if flag:
            L.ok(f"Ownership of the {name} repo: OK")
        else:
            L.fail(f"Ownership of the {name} repo: missing")
            missing.append(name)
    if gm.is_verified:
        L.ok("Account verification flag: verified")
    else:
        L.warn("Account verification flag: not verified")
    if missing or not gm.is_verified:
        L.info(
            "Add AGL_VERIFY.txt with your verification code to the repo(s) above, "
            "then click 'Verify Ownership' in the Gladiator tab. Diagnostics does not re-run that check."
        )
        F.add(
            "Ownership is not verified for: " + (", ".join(missing) if missing else "this model") + ".",
            "Add AGL_VERIFY.txt to the repo(s) and click 'Verify Ownership' in the Gladiator tab.",
        )


def _step_files(L: _RunLog, F: _Findings, user, gm: UserGameModel) -> None:
    _step(L, 4, "Repository files")
    from apps.users import views as user_views

    try:
        status = user_views._build_breakthrough_file_status(user, gm)
    except Exception:
        log.exception("Diagnostics: file status failed for user=%s game=%s", user.pk, gm.game_type)
        L.warn("Could not read the repository files right now.")
        F.platform_issue = True
        return
    if status is None:
        L.fail("Could not read config_model.json or config_data.json.")
        F.add(
            "Neither config_model.json nor config_data.json could be read.",
            "Add config_model.json to the model repo and config_data.json to the data repo.",
        )
        return

    revision = status.get("revision_used") or ""
    if revision:
        L.info(f"Model repo revision used: {_short(revision)}")
    _report_config(L, F, "config_model.json", "model", status.get("model_files"), status.get("model_error") or "")
    _report_config(L, F, "config_data.json", "data", status.get("data_files"), status.get("data_error") or "")


def _report_config(L: _RunLog, F: _Findings, cfg_name: str, repo_word: str, files, error: str) -> None:
    files = files or []
    if files:
        L.ok(f"{cfg_name} is readable ({len(files)} file(s) listed).")
        absent = []
        for entry in files:
            if entry.get("present"):
                L.ok(f"Present: {entry.get('name')}")
            else:
                L.fail(f"Missing: {entry.get('name')}")
                absent.append(str(entry.get("name")))
        if absent:
            F.add(
                f"The {repo_word} repo is missing file(s) listed in {cfg_name}: {', '.join(absent)}.",
                "Commit the missing file(s) to the repo and run diagnostics again.",
            )
        return

    missing = _parse_missing(error)
    if missing:
        L.ok(f"{cfg_name} is readable ({len(missing)} listed file(s) are missing).")
        for name in missing:
            L.fail(f"Missing: {name}")
        F.add(
            f"The {repo_word} repo is missing file(s) listed in {cfg_name}: {', '.join(missing)}.",
            "Commit the missing file(s) to the repo and run diagnostics again.",
        )
        return

    if error:
        if _is_transient(error):
            L.warn(error)
            F.platform_issue = True
            return
        L.fail(error)
        F.add(f"{cfg_name} could not be read: {error}", _fix_for_error(error))
        return

    L.warn(f"{cfg_name} could not be read or lists no files.")
    F.add(
        f"{cfg_name} could not be read or lists no files.",
        f"Make sure {cfg_name} exists in the {repo_word} repo and lists the files your model needs.",
    )


def _step_version(L: _RunLog, F: _Findings, gm: UserGameModel) -> None:
    _step(L, 5, "Version and integrity")
    from apps.users.integrity import TOURNAMENT_MIN_RATED_GAMES

    L.info(f"Approved SHA: {_short(gm.approved_full_sha)}")
    L.info(f"Current repo SHA: {_short(gm.current_repo_sha)}")
    L.info(f"New (pending) repo SHA: {_short(gm.new_repo_sha)}")
    L.info(f"Repo changed flag: {'yes' if gm.repo_changed else 'no'}")

    if gm.model_integrity_ok:
        L.ok("Model integrity: OK")
    else:
        L.warn("Model integrity: NOT OK (the model changed or needs re-validation)")
        F.add(
            "Model integrity is not OK (the model changed or needs re-validation).",
            "Play rated games with this model so the platform can re-validate it; "
            "if you did not change the repo, contact an administrator.",
        )

    games = gm.rated_games_since_revalidation or 0
    L.info(f"Rated games since last revalidation: {games} / {TOURNAMENT_MIN_RATED_GAMES}")
    if gm.repo_changed and games < TOURNAMENT_MIN_RATED_GAMES:
        remaining = TOURNAMENT_MIN_RATED_GAMES - games
        L.warn(f"Repo-change cooldown is active: {remaining} more rated game(s) needed.")
        F.add(
            f"The model is in the repo-change cooldown ({games} / {TOURNAMENT_MIN_RATED_GAMES} rated games).",
            f"Play {remaining} more rated game(s) with this model.",
        )
    else:
        L.ok("Not in cooldown.")


def _step_status(L: _RunLog, F: _Findings, gm: UserGameModel) -> None:
    _step(L, 6, "Stored contract result")
    status = gm.status
    if status == UserGameModel.ContractStatus.ACTIVE:
        L.ok("Stored status: active")
    elif status == UserGameModel.ContractStatus.PENDING:
        L.warn("Stored status: pending (the contract check has not passed yet)")
        F.add(
            "The stored contract status is pending, so the model cannot be used in games yet.",
            "Add AGL_VERIFY.txt to your repo and click 'Verify Ownership' on the Gladiator tab: "
            "the model check runs as part of that. Waiting alone will not change this.",
        )
    else:
        L.fail("Stored status: failed")
        F.add(
            "The stored contract status is failed.",
            "Fix the problems listed in this report, then save your repos again in the Gladiator tab.",
        )
    if gm.last_error:
        L.warn(f"Last stored error: {gm.last_error}")
    if gm.last_sandbox_error:
        when = ""
        if gm.last_sandbox_error_at:
            when = " (" + gm.last_sandbox_error_at.strftime("%Y-%m-%d %H:%M") + " UTC)"
        L.warn(f"Last sandbox error{when}: {gm.last_sandbox_error}")


def _step_live_test(L: _RunLog, F: _Findings, gm: UserGameModel) -> None:
    _step(L, 7, "Live contract test")

    from apps.games.exceptions import SandboxUnavailableError
    from apps.games.model_check import check_model_verbose

    model_dir, data_dir = _resolve_dirs_readonly(gm)

    if model_dir is None:
        # This is the case where the live test was skipped because files were not yet downloaded
        L.info("Live contract test skipped (files still being downloaded by background task)")
        F.add(
            "The live contract test could not run because the files of your approved revision are still being downloaded.",
            "This is normal right after registration. The test will run automatically in a few seconds. "
            "You can refresh the Diagnostics tab in a moment.",
        )
        return

    if data_dir is None and (gm.hf_data_repo_id or "").strip():
        L.warn("Data files for your data repo are not downloaded yet; the test runs without them.")

    L.info("Running the contract test in the secure sandbox (this can take a few minutes)...")
    try:
        result = check_model_verbose(model_dir, data_dir, gm.game_type)
    except SandboxUnavailableError:
        log.warning(
            "Diagnostics: sandbox unavailable for user=%s game=%s", gm.user_id, gm.game_type, exc_info=True,
        )
        L.warn("The platform sandbox is temporarily unavailable. This is not a problem with your model.")
        F.sandbox_unavailable = True
        return

    # ... (the rest of the function stays exactly the same)
    stage = result.get("stage")
    problems = list(result.get("problems") or [])
    warnings = list(result.get("warnings") or [])
    moves = list(result.get("moves") or [])

    if stage == "manifest":
        for problem in problems:
            L.fail(problem)
    else:
        L.ok("Manifest check passed.")
        if stage == "syntax":
            for problem in problems:
                L.fail(problem)
        else:
            L.ok("Syntax check passed.")
            for move in moves:
                _log_move(L, move)
            for problem in problems:
                if moves and problem.startswith("sample position"):
                    continue
                L.fail(problem)
    for note in warnings:
        L.warn(note)

    for problem in problems:
        F.add(problem, _fix_for_contract_problem(problem))

    if stage == "sandbox" and not problems:
        illegal = [m for m in moves if not m.get("legal")]
        if not moves:
            L.fail("The sandbox returned no sample moves.")
            F.add(
                "The sandbox returned no sample moves.",
                "Make sure get_move() is defined and returns a move, then run diagnostics again.",
            )
        elif illegal:
            F.add(
                "Some sample moves were not legal.",
                "Make get_move() return a legal move in UCI notation for the given position.",
            )
        else:
            L.ok(f"Live contract test passed ({len(moves)}/{len(moves)} sample moves legal).")


def _log_move(L: _RunLog, move: dict) -> None:
    label = f"Sample position {move.get('position')}"
    played = move.get("move")
    if move.get("error"):
        L.fail(f"{label}: your model raised an error: {move['error']}")
    elif not played:
        L.fail(f"{label}: your model returned no move")
    elif move.get("legal"):
        L.ok(f"{label}: your model played {played} (legal)")
    else:
        L.fail(f"{label}: your model played {played} (ILLEGAL move)")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Verdict
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _list_problems(L: _RunLog, F: _Findings) -> None:
    for index, (summary, fix) in enumerate(F.problems, start=1):
        L.fail(f"{index}. {summary}")
        L.info(f"Fix: {fix}")


def _conclude(L: _RunLog, F: _Findings) -> str:
    Verdict = DiagnosticRun.Verdict
    if F.sandbox_unavailable:
        L.warn(
            "RESULT: the live test could not run because the platform sandbox is unavailable "
            "(not a problem with your model); try again later"
        )
        if F.problems:
            L.info("Problems found by the other checks:")
            _list_problems(L, F)
        return Verdict.PLATFORM_ERROR
    if F.problems:
        L.fail("RESULT: problems found")
        _list_problems(L, F)
        return Verdict.FAIL
    if F.platform_issue:
        L.warn(
            "RESULT: some checks could not be completed because of a temporary platform or "
            "network issue (not a problem with your model); try again later"
        )
        return Verdict.PLATFORM_ERROR
    L.ok("RESULT: your model works correctly")
    return Verdict.PASS


def _execute(run: DiagnosticRun, L: _RunLog) -> str:
    user = run.user
    game_type = run.game_type
    label = UserGameModel.GameType(game_type).label
    F = _Findings()

    L.info(f"Diagnostics for {label} started. Read-only: nothing about your model is changed.")

    gm = _step_registration(L, F, user, game_type, label)
    if gm is None:
        return _conclude(L, F)

    _step_repo_access(L, F, gm)
    _step_ownership(L, F, gm)
    _step_files(L, F, user, gm)
    # Steps 2-4 make network calls; re-read the row so steps 5-7 see anything
    # saved meanwhile (ownership verification, preload, etc.). This closes the
    # exact race that caused "repo can't be used" / "model not downloaded" right
    # after registration.
    gm.refresh_from_db()
    _step_version(L, F, gm)
    _step_status(L, F, gm)
    _step_live_test(L, F, gm)
    return _conclude(L, F)


def run_diagnostics(run_id: int) -> None:
    """Execute one queued DiagnosticRun, appending terminal lines as each step completes."""
    run = DiagnosticRun.objects.select_related("user").filter(pk=run_id).first()
    if run is None:
        log.warning("run_diagnostics: DiagnosticRun %s does not exist", run_id)
        return
    if run.state != DiagnosticRun.State.QUEUED:
        log.info("run_diagnostics: run %s is already %s, skipping", run_id, run.state)
        return

    run.state = DiagnosticRun.State.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["state", "started_at"])
    run_log = _RunLog(run)

    try:
        run.verdict = _execute(run, run_log)
        run.state = DiagnosticRun.State.FINISHED
        run.finished_at = timezone.now()
        run.save(update_fields=["state", "verdict", "finished_at"])
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        log.exception("Diagnostics run %s crashed (user=%s game=%s)", run_id, run.user_id, run.game_type)
        _finish_with_error(run, run_log, UNEXPECTED_FAILURE_MESSAGE)
    finally:
        _prune_old_runs(run.user_id, run.game_type)

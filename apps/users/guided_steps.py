"""Guided-step state for the Gladiator tab, derived only from UserGameModel fields."""

STEP_TITLES = (
    "Enter repo",
    "Approve platform account",
    "Add AGL_VERIFY.txt",
    "Verify",
)

_MARKERS = {"done": "[DONE]", "current": "[CURRENT]", "todo": "[TODO]"}

GUEST_STEP_TITLES = (
    "Connect your public repos",
    "Platform model check",
)


def _is_guest_model(gm) -> bool:
    return bool(getattr(getattr(gm, "user", None), "is_guest", False))


def _done_flags(gm):
    if gm is None or not gm.hf_model_repo_id:
        return [False, False, False, False]
    if getattr(gm, "verification_method", "file") != "file":
        # Repo proven by Hugging Face sign-in (guest period): the gated repo,
        # platform approval and AGL_VERIFY.txt steps are still ahead.
        return [True, False, False, False]
    files_ok = gm.is_verified or (
        gm.model_repo_ownership_verified
        and (not gm.hf_data_repo_id or gm.data_repo_ownership_verified)
    )
    verified = gm.is_verified and gm.status != gm.ContractStatus.FAILED
    # A saved repo has already passed the gated + platform-access checks at connect time.
    return [True, True, bool(files_ok), bool(verified)]


def _guest_flags(gm):
    if gm is None or not gm.hf_model_repo_id:
        return [False, False]
    return [True, gm.status == gm.ContractStatus.ACTIVE]


def _steps_from(titles, flags):
    first_open = next((i for i, d in enumerate(flags) if not d), None)
    steps = []
    for i, (title, done) in enumerate(zip(titles, flags)):
        state = "done" if done else ("current" if i == first_open else "todo")
        steps.append({"num": i + 1, "title": title, "state": state, "marker": _MARKERS[state]})
    return steps


def build_steps(gm, guest=False):
    """Return the numbered steps with state done / current / todo.

    Registered accounts always get the four steps; guests get the two public-repo steps.
    """
    if guest or _is_guest_model(gm):
        return _steps_from(GUEST_STEP_TITLES, _guest_flags(gm))
    return _steps_from(STEP_TITLES, _done_flags(gm))


def next_step_line(game_configs, guest=False):
    """One plain line for the top of the tab: 'Next step: ...' or 'All set'.

    Registered games are considered first; with none registered the first
    game's first step is shown.
    """
    registered = [g for g in game_configs if g["model"] is not None]
    candidates = registered or game_configs[:1]
    for g in candidates:
        gm = g["model"]
        current = next((s for s in g["steps"] if s["state"] == "current"), None)
        if current is None:
            continue
        label = g["label"]
        n = current["num"]
        if guest or _is_guest_model(gm):
            if n == 1:
                return (
                    f"Next step: Type your {label} model repo (and data repo, if any), then click Save. "
                    "Your repos must be public."
                )
            if gm is not None and gm.status == gm.ContractStatus.FAILED:
                return f"Next step: Fix the {label} model check error shown below, then click Save again."
            return f"Next step: Wait for the {label} model check to finish (Preparing your model...)."
        if n == 1:
            return f"Next step: Type or paste your {label} model repo, then click the gold button Save and verify."
        if n == 2:
            return (
                "Next step: Click the yellow button Open Access Settings and approve "
                "ArtificialGladiatorLeague on Hugging Face, then click Save and verify."
            )
        if n == 3:
            return (
                f"Next step: Add AGL_VERIFY.txt with the code to each unverified {label} repo "
                "(Copy file name and Copy code buttons), then click Verify Ownership."
            )
        if gm is not None and gm.status == gm.ContractStatus.FAILED:
            return f"Next step: Fix the {label} model check error shown below, then click Save and verify."
        return f"Next step: Click Verify Ownership (or Save and verify) for your {label} model."
    return "All set"

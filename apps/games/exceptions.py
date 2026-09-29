class ModelNotPrecachedError(Exception):
    """Raised when a user model was not pre-downloaded at login.

    This signals that runtime/per-move downloads are disabled and the
    game cannot proceed until the model is pre-cached via login.
    """
    pass


class SandboxUnavailableError(Exception):
    """Raised when the Docker sandbox itself cannot run a move.

    This covers infrastructure faults only (Docker daemon unreachable,
    sandbox image missing, API errors) — never model-quality issues.
    Docker is mandatory: there is no in-process/local fallback, so this
    exception must propagate to the caller instead of being swallowed
    into a random-move fallback.
    """
    pass


class SandboxDataMissingError(Exception):
    """Raised when a user-declared data repo's required file is missing.

    E.g. the model's ``config_model.json`` (or the default) expects a
    ``zone_db_filename`` but that file was not found in ``/data`` (or the
    model dir) after preparing the data directory. This is a data/config
    problem, not a model-quality issue, so it must propagate instead of
    being swallowed into a random-move fallback.
    """
    pass

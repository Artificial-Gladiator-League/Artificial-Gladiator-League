class ModelNotPrecachedError(Exception):
    """Raised when a user model was not pre-downloaded at login.

    This signals that runtime/per-move downloads are disabled and the
    game cannot proceed until the model is pre-cached via login.
    """
    pass

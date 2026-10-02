from logging.handlers import RotatingFileHandler


class SafeRotatingFileHandler(RotatingFileHandler):
    """Skip rotation when Windows refuses the rename (file held by the autoreloader's other process)."""

    def doRollover(self):
        try:
            super().doRollover()
        except PermissionError:
            pass

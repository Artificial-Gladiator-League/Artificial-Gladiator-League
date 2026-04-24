#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys

from dotenv import load_dotenv
load_dotenv()

# When running the development server, avoid heavy startup downloads.
# Default to skipping the startup pre-warm unless explicitly overridden.
try:
    if any("runserver" in a for a in sys.argv[1:]):
        os.environ.setdefault("PREWARM_MODELS", "false")
except Exception:
    pass

# Prevent OpenBLAS memory allocation failures on Windows.
# Must be set before numpy/torch are imported anywhere.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")


def main():
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agladiator.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()

"""Verify the fixed validate_gated_hf_repo rejects all bad inputs."""
import os, sys, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agladiator.settings")
django.setup()

from django.core.exceptions import ValidationError
from apps.users.forms import validate_gated_hf_repo

TESTS = [
    ("nonexistentuser/xyz12345", "hf_abcdefghijklmnop", "nonexistent repo"),
    ("google/gemma-7b",          "hf_abcdefghijklmnop", "public (non-gated) repo"),
    ("Maxlegrec/ChessBot",       "hf_FAKETOKEN1234567890abc", "gated repo + fake token"),
]

for repo, token, label in TESTS:
    print(f"\n--- {label}: repo={repo}, token={token[:12]}... ---")
    try:
        validate_gated_hf_repo(repo, token)
        print("  RESULT: PASSED (no error) — THIS IS A BUG!")
    except ValidationError as ve:
        print(f"  RESULT: REJECTED (correct!)")
        print(f"  Error: {ve.message_dict if hasattr(ve, 'message_dict') else ve.messages}")
    except Exception as e:
        print(f"  RESULT: unexpected {type(e).__name__}: {e}")

print("\n--- All tests done ---")

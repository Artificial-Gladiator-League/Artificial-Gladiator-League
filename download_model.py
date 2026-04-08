import os
import sys

from huggingface_hub import snapshot_download
from huggingface_hub.utils import HfHubHTTPError

REPO_ID = "Maxlegrec/ChessBot"
LOCAL_DIR = "./my-chessbot"

GATED_REPO_FIX = """
ERROR: Gated repo access denied (403 Forbidden).

This usually means your Hugging Face token lacks permission for public gated repos.

To fix, do ONE of the following:

  Option A – Fix your fine-grained token:
    1. Go to https://huggingface.co/settings/tokens
    2. Edit your fine-grained token
    3. CHECK the box: "Read access to contents of all public gated repos you can access"
    4. Save, then re-run:  huggingface-cli login

  Option B – Use a classic READ token instead (easier):
    1. Go to https://huggingface.co/settings/tokens
    2. Create a new token with type "Read"
    3. Run:  huggingface-cli login
    4. Or set the HF_TOKEN environment variable:
         set HF_TOKEN=hf_your_token_here   (Windows)
         export HF_TOKEN=hf_your_token_here (Linux/Mac)
"""


def get_hf_token():
    """Return the HF token from env, or None (huggingface_hub will fall back to cached login)."""
    token = os.getenv("HF_TOKEN")
    if token:
        return token
    return None  # let huggingface_hub use its cached token


def download_model():
    token = get_hf_token()

    try:
        snapshot_download(
            repo_id=REPO_ID,
            local_dir=LOCAL_DIR,
            local_dir_use_symlinks=False,
            ignore_patterns=["*.gitattributes"],
            token=token,
        )
    except HfHubHTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            msg = str(e).lower()
            if "gated" in msg or "fine-grained" in msg or "access" in msg:
                print(GATED_REPO_FIX, file=sys.stderr)
                sys.exit(1)
        # Re-raise unexpected HTTP errors
        raise
    except Exception:
        raise

    print(f"Download complete! Files are in {LOCAL_DIR}")


if __name__ == "__main__":
    download_model()
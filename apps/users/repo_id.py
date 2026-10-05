"""Pure helper that turns a pasted Hugging Face URL into an 'owner/repo' ID."""
import re

_HF_URL = re.compile(r"^https?://(?:www\.)?huggingface\.co/(?P<path>[^?#]*)(?:[?#].*)?$", re.IGNORECASE)

# Page suffixes that may follow owner/repo in a copied browser URL.
_PAGE_SEGMENTS = {"tree", "blob", "resolve", "raw", "commit", "commits", "discussions", "settings"}


def normalize_hf_repo_id(value):
    """Strip whitespace and reduce huggingface.co repo URLs to 'owner/repo'.

    Anything that is not a recognised model/dataset URL (Space URLs, hf.space
    hosts, plain IDs, garbage) is returned stripped but otherwise unchanged so
    the existing validators produce their usual errors.
    """
    value = (value or "").strip()
    m = _HF_URL.match(value)
    if not m:
        return value

    parts = [p for p in m.group("path").split("/") if p]
    if parts and parts[0].lower() == "datasets":
        parts = parts[1:]
    if len(parts) < 2 or parts[0].lower() == "spaces":
        return value
    if len(parts) > 2 and parts[2].lower() not in _PAGE_SEGMENTS:
        return value
    return f"{parts[0]}/{parts[1]}"

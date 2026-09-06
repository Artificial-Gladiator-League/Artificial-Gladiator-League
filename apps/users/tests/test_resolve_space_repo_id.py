"""Unit tests for resolve_space_repo_id() in apps.users.ownership_verification.

Pure string parsing — no network, no DB. Covers both accepted URL formats,
the multi-dash Space-name convention (first dash separates owner from name,
matching the first split tried by _fetch_verify_file_from_space), and
malformed input.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from apps.users.ownership_verification import resolve_space_repo_id


class ResolveSpaceRepoIdTests(SimpleTestCase):
    def test_canonical_spaces_url(self):
        self.assertEqual(
            resolve_space_repo_id("https://huggingface.co/spaces/owner/name"),
            "owner/name",
        )

    def test_gradio_subdomain_url(self):
        self.assertEqual(
            resolve_space_repo_id("https://owner-name.hf.space"),
            "owner/name",
        )

    def test_gradio_subdomain_multi_dash_name(self):
        # First dash separates owner from name (owner/multi-word-name),
        # matching the first candidate split in _fetch_verify_file_from_space.
        self.assertEqual(
            resolve_space_repo_id("https://owner-multi-word-name.hf.space"),
            "owner/multi-word-name",
        )

    def test_malformed_url_returns_empty(self):
        self.assertEqual(resolve_space_repo_id("not a url"), "")
        self.assertEqual(resolve_space_repo_id(""), "")
        self.assertEqual(resolve_space_repo_id(None), "")
        # Subdomain with no dash cannot be split into owner/name.
        self.assertEqual(resolve_space_repo_id("https://ownername.hf.space"), "")

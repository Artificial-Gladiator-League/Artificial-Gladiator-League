"""Repo ID normalizer, connect / Save-and-verify POST flow, guided-steps markup."""
from __future__ import annotations

import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.users.models import UserGameModel
from apps.users.repo_id import normalize_hf_repo_id
from apps.users.views import _check_repo_id_shape

User = get_user_model()

PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)

PROFILE_AI_URL = reverse("users:profile") + "?tab=ai-models"

SHA_FIELDS = (
    "original_model_commit_sha", "last_known_commit_id", "locked_commit_id",
    "approved_full_sha", "current_repo_sha", "new_repo_sha",
    "approved_data_repo_sha", "current_data_repo_sha", "new_data_repo_sha",
    "last_verified_commit", "cached_commit", "pinned_at",
)

# Emoji and pictographs (BMP symbols used as emoji plus the astral planes).
EMOJI_RE = re.compile("[\u2190-\u2BFF\u2600-\u27BF\U0001F000-\U0001FFFF\uFE0F]")


class NormalizeRepoIdTests(SimpleTestCase):
    def test_plain_id_unchanged(self):
        self.assertEqual(normalize_hf_repo_id("owner/repo"), "owner/repo")

    def test_whitespace_stripped(self):
        self.assertEqual(normalize_hf_repo_id("  owner/repo \n"), "owner/repo")

    def test_empty_and_none(self):
        self.assertEqual(normalize_hf_repo_id(""), "")
        self.assertEqual(normalize_hf_repo_id(None), "")
        self.assertEqual(normalize_hf_repo_id("   "), "")

    def test_model_url(self):
        self.assertEqual(normalize_hf_repo_id("https://huggingface.co/owner/repo"), "owner/repo")

    def test_trailing_slash(self):
        self.assertEqual(normalize_hf_repo_id("https://huggingface.co/owner/repo/"), "owner/repo")

    def test_tree_and_blob(self):
        self.assertEqual(normalize_hf_repo_id("https://huggingface.co/owner/repo/tree/main"), "owner/repo")
        self.assertEqual(
            normalize_hf_repo_id("https://huggingface.co/owner/repo/blob/main/README.md"), "owner/repo"
        )

    def test_datasets_prefix(self):
        self.assertEqual(normalize_hf_repo_id("https://huggingface.co/datasets/owner/data"), "owner/data")
        self.assertEqual(
            normalize_hf_repo_id("https://huggingface.co/datasets/owner/data/tree/main/"), "owner/data"
        )

    def test_query_fragment_and_surrounding_space(self):
        self.assertEqual(
            normalize_hf_repo_id("  https://huggingface.co/owner/repo?library=x#files "), "owner/repo"
        )

    def test_http_www_and_case(self):
        self.assertEqual(normalize_hf_repo_id("HTTP://www.HuggingFace.co/Owner/Repo"), "Owner/Repo")

    def test_space_urls_left_for_validator(self):
        for url in (
            "https://huggingface.co/spaces/owner/repo",
            "https://owner-repo.hf.space",
        ):
            normalized = normalize_hf_repo_id(url)
            self.assertEqual(normalized, url)
            self.assertIn("not the Space URL", _check_repo_id_shape(normalized))

    def test_hf_space_suggestion_kept(self):
        err = _check_repo_id_shape(normalize_hf_repo_id("https://alice-mymodel.hf.space/"))
        self.assertIn("not the Space URL", err)
        self.assertIn("alice/mymodel", err)

    def test_unrecognised_urls_unchanged(self):
        for url in (
            "https://huggingface.co/owner",
            "https://huggingface.co/",
            "https://huggingface.co/owner/repo/unknown/x",
            "https://example.com/owner/repo",
        ):
            self.assertEqual(normalize_hf_repo_id(url), url)

    def test_normalized_url_passes_shape_check(self):
        self.assertIsNone(_check_repo_id_shape(normalize_hf_repo_id("https://huggingface.co/owner/repo/tree/main")))


@PLAIN_STATIC
class ConnectPostTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("connector", password="pw")
        self.client.force_login(self.user)
        self.url = reverse("users:profile")
        patches = [
            mock.patch("apps.users.views._check_repo_is_gated", return_value=None),
            mock.patch("apps.users.views._check_data_repo", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        sched = mock.patch("apps.users.views._schedule_model_preload")
        self.schedule = sched.start()
        self.addCleanup(sched.stop)

    def _post(self, **over):
        data = {
            "ai_model_form": "1",
            "action": "connect",
            "game_type": "chess",
            "hf_model_repo_id": "alice/chess-model",
            "hf_data_repo_id": "alice/chess-data",
        }
        data.update(over)
        return self.client.post(self.url, data)

    def _snapshot(self, gm):
        return {f: getattr(gm, f) for f in SHA_FIELDS}

    def test_connect_creates_model_and_redirects_to_tab(self):
        resp = self._post()
        self.schedule.assert_called_once_with(self.user.pk)
        self.assertRedirects(resp, PROFILE_AI_URL, fetch_redirect_response=False)
        gm = UserGameModel.objects.get(user=self.user, game_type="chess")
        self.assertEqual(gm.hf_model_repo_id, "alice/chess-model")
        self.assertEqual(gm.hf_data_repo_id, "alice/chess-data")
        self.assertFalse(gm.is_verified)
        self.assertTrue(gm.verification_code)

    def test_connect_normalizes_urls(self):
        self._post(
            hf_model_repo_id="  https://huggingface.co/alice/chess-model/tree/main ",
            hf_data_repo_id="https://huggingface.co/datasets/alice/chess-data",
        )
        gm = UserGameModel.objects.get(user=self.user, game_type="chess")
        self.assertEqual(gm.hf_model_repo_id, "alice/chess-model")
        self.assertEqual(gm.hf_data_repo_id, "alice/chess-data")

    def test_space_url_still_rejected(self):
        resp = self._post(hf_model_repo_id="https://huggingface.co/spaces/alice/model")
        self.schedule.assert_not_called()
        self.assertRedirects(resp, PROFILE_AI_URL, fetch_redirect_response=False)
        self.assertFalse(UserGameModel.objects.filter(user=self.user).exists())
        msgs = [str(m) for m in resp.wsgi_request._messages]
        self.assertTrue(any("not the Space URL" in m for m in msgs))

    def test_repo_change_resets_same_fields_and_leaves_shas_alone(self):
        gm = UserGameModel.objects.create(
            user=self.user, game_type="chess",
            hf_model_repo_id="alice/old-model", hf_data_repo_id="alice/chess-data",
            approved_full_sha="a" * 40, current_repo_sha="b" * 40, new_repo_sha="c" * 40,
            approved_data_repo_sha="d" * 40, current_data_repo_sha="e" * 40,
            is_verified=True, verification_code="old-code",
        )
        gm.refresh_from_db()
        self._post()
        after_connect = UserGameModel.objects.get(pk=gm.pk)
        self.assertEqual(after_connect.hf_model_repo_id, "alice/chess-model")
        self.assertFalse(after_connect.is_verified)
        self.assertNotEqual(after_connect.verification_code, "old-code")
        self.assertEqual(after_connect.approved_full_sha, "a" * 40)
        self.assertEqual(after_connect.approved_data_repo_sha, "d" * 40)

    def test_unchanged_verified_repo_keeps_state(self):
        UserGameModel.objects.create(
            user=self.user, game_type="chess",
            hf_model_repo_id="alice/chess-model", hf_data_repo_id="alice/chess-data",
            is_verified=True, verification_code="keep",
        )
        self._post()
        gm = UserGameModel.objects.get(user=self.user, game_type="chess")
        self.assertTrue(gm.is_verified)
        self.assertEqual(gm.verification_code, "keep")


@PLAIN_STATIC
class SaveAndVerifyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("saver", password="pw")
        self.client.force_login(self.user)
        self.url = reverse("users:profile")
        for target in ("apps.users.views._check_repo_is_gated", "apps.users.views._check_data_repo"):
            p = mock.patch(target, return_value=None)
            p.start()
            self.addCleanup(p.stop)
        sched = mock.patch("apps.users.views._schedule_model_preload")
        self.schedule = sched.start()
        self.addCleanup(sched.stop)

    def _post(self, **over):
        data = {
            "ai_model_form": "1",
            "action": "connect",
            "save_and_verify": "1",
            "game_type": "chess",
            "hf_model_repo_id": "alice/chess-model",
            "hf_data_repo_id": "alice/chess-data",
        }
        data.update(over)
        return self.client.post(self.url, data)

    def test_verify_runs_after_successful_connect(self):
        with mock.patch(
            "apps.users.ownership_verification.check_full_ownership",
            return_value=(False, "AGL_VERIFY.txt not found"),
        ) as verify:
            resp = self._post()
        self.assertRedirects(resp, PROFILE_AI_URL, fetch_redirect_response=False)
        verify.assert_called_once()
        gm = UserGameModel.objects.get(user=self.user, game_type="chess")
        self.assertEqual(verify.call_args.args[0].pk, gm.pk)
        msgs = [str(m) for m in resp.wsgi_request._messages]
        self.assertTrue(any("repo saved" in m for m in msgs))
        self.assertTrue(any("AGL_VERIFY.txt not found" in m for m in msgs))

    def test_verify_not_called_when_validation_fails(self):
        with mock.patch("apps.users.ownership_verification.check_full_ownership") as verify:
            self._post(hf_model_repo_id="not a repo")
            self._post(hf_model_repo_id="")
            self._post(hf_model_repo_id="https://huggingface.co/spaces/a/b")
            self._post(hf_data_repo_id="bad data id")
        verify.assert_not_called()
        self.assertFalse(UserGameModel.objects.filter(user=self.user).exists())

    def test_verify_not_called_when_gated_check_fails(self):
        with mock.patch("apps.users.views._check_repo_is_gated", return_value="gated problem"), \
                mock.patch("apps.users.ownership_verification.check_full_ownership") as verify:
            self._post()
        verify.assert_not_called()

    def test_verify_not_called_when_repo_owned_by_another_user(self):
        other = User.objects.create_user("other", password="pw")
        UserGameModel.objects.create(
            user=other, game_type="chess", hf_model_repo_id="alice/chess-model",
        )
        with mock.patch("apps.users.ownership_verification.check_full_ownership") as verify:
            self._post()
        verify.assert_not_called()

    def test_plain_connect_never_verifies(self):
        with mock.patch("apps.users.ownership_verification.check_full_ownership") as verify:
            self._post(save_and_verify="")
        verify.assert_not_called()

    def test_no_sha_changes_beyond_plain_connect(self):
        # Two users, same pre-state: plain connect vs Save and verify with a failing verify.
        def make(name):
            u = User.objects.create_user(name, password="pw")
            UserGameModel.objects.create(
                user=u, game_type="chess",
                hf_model_repo_id="alice/old-model", hf_data_repo_id="alice/chess-data",
                approved_full_sha="a" * 40, current_repo_sha="b" * 40, new_repo_sha="c" * 40,
                approved_data_repo_sha="d" * 40, current_data_repo_sha="e" * 40,
                new_data_repo_sha="f" * 40, original_model_commit_sha="1" * 40,
                last_known_commit_id="2" * 40, last_verified_commit="3" * 40,
                is_verified=True, verification_code="old",
            )
            return u

        plain_user, sav_user = make("plainu"), make("savu")
        snapshots = {}
        for user, repo, extra in ((plain_user, "plain/chess-model", ""), (sav_user, "sav/chess-model", "1")):
            self.client.force_login(user)
            with mock.patch(
                "apps.users.ownership_verification.check_full_ownership",
                return_value=(False, "nope"),
            ):
                self._post(hf_model_repo_id=repo, hf_data_repo_id="alice/chess-data", save_and_verify=extra)
            gm = UserGameModel.objects.get(user=user, game_type="chess")
            snapshots[user.username] = {f: getattr(gm, f) for f in SHA_FIELDS}
        self.assertEqual(snapshots["plainu"], snapshots["savu"])


@PLAIN_STATIC
class GuidedStepsMarkupTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("viewer", password="pw")
        self.client.force_login(self.user)

    def _page(self):
        resp = self.client.get(reverse("users:profile"))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    @staticmethod
    def _new_markup(html):
        parts = re.findall(r"<!-- guided:start -->(.*?)<!-- guided:end -->", html, re.S)
        return "\n".join(parts)

    def test_next_step_when_nothing_registered(self):
        html = self._page()
        self.assertIn(
            "Next step: Type or paste your Chess model repo, then click the gold button Save and verify.", html,
        )
        self.assertIn("Save and verify", html)

    def test_no_format_ok_hint(self):
        html = self._page()
        self.assertNotIn("Format OK", html)
        self.assertNotIn("Format: owner/repo-name", html)
        self.assertIn("Not a valid owner/repo-name format.", html)
        self.assertIn("not a Space or other URL", html)

    def test_current_step_names_buttons(self):
        html = self._page()
        self.assertIn(
            "Type or paste your model repo, then click the gold button <strong>Save and verify</strong>.", html,
        )

    def test_step3_text_and_buttons(self):
        UserGameModel.objects.create(
            user=self.user, game_type="chess",
            hf_model_repo_id="viewer/chess-model", verification_code="abc123",
        )
        html = self._page()
        self.assertIn("add the file AGL_VERIFY.txt with the code below", html)
        self.assertIn("<strong>Copy file name</strong>", html)
        self.assertIn("<strong>Copy code</strong>", html)
        self.assertIn("Then click <strong>Verify Ownership</strong>", html)
        self.assertIn(">Copy file name</button>", html)
        self.assertIn(">Copy code</button>", html)

    def test_step4_text(self):
        UserGameModel.objects.create(
            user=self.user, game_type="chess",
            hf_model_repo_id="viewer/chess-model", verification_code="abc123",
            model_repo_ownership_verified=True,
        )
        html = self._page()
        self.assertIn("[CURRENT] 4. Verify", html)
        self.assertIn(
            "Click <strong>Verify Ownership</strong> (or <strong>Save and verify</strong>).", html,
        )
        self.assertIn("Next step: Click Verify Ownership (or Save and verify) for your Chess model.", html)

    def test_next_step_lines_name_buttons(self):
        from types import SimpleNamespace
        from apps.users.guided_steps import next_step_line

        def cfg(num):
            steps = [{"num": i, "state": "current" if i == num else "done"} for i in (1, 2, 3, 4)]
            return [{"label": "Chess", "steps": steps, "model": SimpleNamespace(
                status="active", ContractStatus=SimpleNamespace(FAILED="failed"),
            )}]

        self.assertIn("Open Access Settings", next_step_line(cfg(2)))
        self.assertIn("Save and verify", next_step_line(cfg(2)))
        self.assertIn("Copy file name", next_step_line(cfg(3)))
        self.assertIn("Verify Ownership", next_step_line(cfg(3)))
        self.assertIn("Verify Ownership", next_step_line(cfg(4)))

    def test_states_follow_model_fields(self):
        UserGameModel.objects.create(
            user=self.user, game_type="chess",
            hf_model_repo_id="viewer/chess-model", hf_data_repo_id="viewer/chess-data",
            verification_code="abc123",
        )
        html = self._page()
        self.assertIn("[DONE] 1. Enter repo", html)
        self.assertIn("[DONE] 2. Approve platform account", html)
        self.assertIn("[CURRENT] 3. Add AGL_VERIFY.txt", html)
        self.assertIn("Next step: Add AGL_VERIFY.txt", html)

    def test_all_set(self):
        UserGameModel.objects.create(
            user=self.user, game_type="chess",
            hf_model_repo_id="viewer/chess-model", hf_data_repo_id="viewer/chess-data",
            verification_code="abc123", is_verified=True,
            model_repo_ownership_verified=True, data_repo_ownership_verified=True,
            status=UserGameModel.ContractStatus.ACTIVE,
        )
        html = self._page()
        self.assertIn("[DONE] 4. Verify", html)
        self.assertIn("All set", html)

    def test_new_markup_has_no_emoji(self):
        UserGameModel.objects.create(
            user=self.user, game_type="chess",
            hf_model_repo_id="viewer/chess-model", verification_code="abc123",
        )
        markup = self._new_markup(self._page())
        self.assertIn("guided", markup)
        self.assertIsNone(EMOJI_RE.search(markup))


# Arrows, technical, geometric, dingbats, emoji planes and the variation selector (box drawing excluded).
PICTO_RE = re.compile("[\u2190-\u21FF\u2300-\u23FF\u25A0-\u27BF\u2900-\u2BFF\U0001F000-\U0001FFFF\uFE0F]")
SCANNED_TEMPLATES = (
    "users/profile.html",
    "users/activation_sent.html",
    "core/how_to_upload.html",
    "core/how_it_works.html",
)


@PLAIN_STATIC
class InfoPagesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("reader", password="pw")
        self.client.force_login(self.user)

    def test_pages_render(self):
        for name in ("users:profile", "users:activation_sent", "core:how_to_upload", "core:how_it_works"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)

    def test_template_sources_have_no_emoji_or_pictographs(self):
        from django.template.loader import get_template
        for name in SCANNED_TEMPLATES:
            with open(get_template(name).origin.name, encoding="utf-8") as fh:
                source = fh.read()
            self.assertIsNone(PICTO_RE.search(source), name)

    def test_activation_sent_has_no_register_again(self):
        html = self.client.get(reverse("users:activation_sent")).content.decode()
        self.assertNotIn(reverse("users:register"), html)
        self.assertNotIn("register again", html.replace("do not need to register again", ""))
        self.assertIn("Check your spam or junk folder. The email comes from agladiator.com.", html)
        self.assertIn("Delivery can take up to 10 minutes.", html)
        self.assertIn(f'href="{reverse("core:contact")}"', html)
        self.assertIn("Your username is reserved while you wait, so you do not need to register again.", html)

    def test_how_to_upload_content(self):
        html = self.client.get(reverse("core:how_to_upload")).content.decode()
        for anchor in ("quick-start", "example", "step-model", "step-config", "troubleshooting", "faq"):
            self.assertIn(f'id="{anchor}"', html)
            self.assertIn(f'href="#{anchor}"', html)
        self.assertIn("https://huggingface.co/chaim-duchovny", html)
        for stale in ("UCTSearcher", "predict(", "zone_db", "3,2-3,3", "Profile -> AG Champions", "No Hugging Face token required"):
            self.assertNotIn(stale, html)
        self.assertNotIn("broken", html.lower())
        self.assertIn("load(ctx)", html)
        self.assertIn("get_move(state, fen, player)", html)
        self.assertIn("log out and log in again", html.lower())
        self.assertIn("sandbox is unavailable", html)

    def test_how_it_works_content(self):
        html = self.client.get(reverse("core:how_it_works")).content.decode()
        self.assertIn("kept warm", html)
        self.assertNotIn("brand-new container", html)
        self.assertNotIn("fresh sandbox", html)
        self.assertIn(reverse("core:how_to_upload"), html)

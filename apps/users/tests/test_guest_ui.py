"""Guest mode UI: sign-in blocks, banner, profile variants and info pages (all behind the flag)."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.users.models import UserGameModel

User = get_user_model()
PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
GATED_WORDS = ("ArtificialGladiatorLeague", "AGL_VERIFY", "Gated", "gated")


def make_guest(name="guest_ui000001", hf="alice"):
    return User.objects.create_user(
        name, password=None, is_guest=True, hf_sub=f"sub-{name}", guest_hf_username=hf,
        ai_name=hf, guest_created_at=timezone.now(),
        guest_expires_at=timezone.now() + timezone.timedelta(days=3),
    )


@PLAIN_STATIC
class GuestSignInBlockTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()

    @override_settings(GUEST_MODE_ENABLED=False)
    def test_flag_off_hides_every_guest_entry_point(self):
        for url in (reverse("users:login"), reverse("users:register"), reverse("core:home")):
            html = self.client.get(url).content.decode()
            self.assertNotIn("guestSignIn", html, url)
            self.assertNotIn(reverse("users:guest_hf_start"), html, url)
            self.assertNotIn("Try as Guest", html, url)

    @override_settings(GUEST_MODE_ENABLED=True)
    def test_flag_on_shows_separate_guest_form_on_login_and_register(self):
        for url in (reverse("users:login"), reverse("users:register")):
            html = self.client.get(url).content.decode()
            self.assertIn('id="guestSignIn"', html, url)
            self.assertIn(f'action="{reverse("users:guest_hf_start")}"', html, url)
            self.assertIn('name="guest_consent"', html, url)
            self.assertIn("Continue with Hugging Face", html, url)

    @override_settings(GUEST_MODE_ENABLED=True)
    def test_home_has_guest_link_for_anonymous_only(self):
        html = self.client.get(reverse("core:home")).content.decode()
        self.assertIn("Try as Guest (Hugging Face)", html)
        self.assertIn(reverse("users:login") + "#guestSignIn", html)
        user = User.objects.create_user("regular", password="pw")
        self.client.force_login(user)
        self.assertNotIn("Try as Guest", self.client.get(reverse("core:home")).content.decode())

    @override_settings(GUEST_MODE_ENABLED=True)
    def test_signed_in_users_do_not_see_guest_block(self):
        self.client.force_login(User.objects.create_user("regular", password="pw"))
        resp = self.client.get(reverse("users:login"))
        self.assertNotIn("guestSignIn", resp.content.decode())


@PLAIN_STATIC
@override_settings(GUEST_MODE_ENABLED=True)
class GuestBannerTests(TestCase):
    def setUp(self):
        cache.clear()
        self.guest = make_guest()
        self.client = Client()

    def test_guest_sees_banner_with_upgrade_and_delete(self):
        self.client.force_login(self.guest)
        html = self.client.get(reverse("core:how_it_works")).content.decode()
        self.assertIn('id="guestBanner"', html)
        self.assertIn(reverse("users:guest_upgrade"), html)
        self.assertIn(f'action="{reverse("users:guest_delete_data")}"', html)
        self.assertIn("Delete my guest data", html)
        self.assertIn('name="robots" content="noindex, nofollow"', html)

    def test_registered_user_sees_no_banner_or_noindex(self):
        self.client.force_login(User.objects.create_user("regular", password="pw"))
        html = self.client.get(reverse("core:how_it_works")).content.decode()
        self.assertNotIn("guestBanner", html)
        self.assertNotIn('name="robots"', html)

    def test_guest_gets_no_sign_up_prompts(self):
        self.client.force_login(self.guest)
        html = self.client.get(reverse("core:how_it_works")).content.decode()
        self.assertNotIn("Sign up free", html)


@PLAIN_STATIC
@override_settings(GUEST_MODE_ENABLED=True)
class GuestProfileTests(TestCase):
    def setUp(self):
        cache.clear()
        self.guest = make_guest()
        self.client = Client()
        self.client.force_login(self.guest)
        patcher = mock.patch("apps.users.guest_snapshot.ensure_guest_snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

    def html(self):
        resp = self.client.get(reverse("users:profile"))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_registered_only_sections_are_hidden(self):
        html = self.html()
        for absent in ('id="emailChangeModal"', "Change Password", "Country of residence", 'data-tab="paypal"',
                       'id="tab-paypal"', reverse("users:gdpr"), reverse("users:save_paypal_email")):
            self.assertNotIn(absent, html, absent)
        self.assertIn('id="guestUpgradeCard"', html)
        self.assertIn(reverse("users:guest_upgrade"), html)
        self.assertIn("alice", html)

    def test_gladiator_tab_has_no_gated_or_verification_text(self):
        html = self.html()
        for word in GATED_WORDS:
            self.assertNotIn(word, html, word)
        self.assertIn("must be <strong>public</strong>", html)
        self.assertIn('name="hf_model_repo_id"', html)
        self.assertIn('name="hf_data_repo_id"', html)

    def test_model_status_messages(self):
        gm = UserGameModel.objects.create(
            user=self.guest, game_type="chess", hf_model_repo_id="alice/m", status="pending",
        )
        self.assertIn("Preparing your model", self.html())
        gm.status, gm.last_error = "failed", "get_move missing"
        gm.save()
        html = self.html()
        self.assertIn("Model check failed", html)
        self.assertIn("get_move missing", html)
        gm.status, gm.last_error = "active", ""
        gm.save()
        self.assertIn("Ready", self.html())

    def test_registered_profile_keeps_existing_sections(self):
        user = User.objects.create_user("regular", password="pw", email="r@example.com")
        c = Client()
        c.force_login(user)
        html = c.get(reverse("users:profile")).content.decode()
        for present in ("emailChangeModal", "Change Password", 'id="tab-paypal"', "ArtificialGladiatorLeague"):
            self.assertIn(present, html, present)
        self.assertNotIn("guestAccountCard", html)


@PLAIN_STATIC
class GuestInfoPageTests(TestCase):
    def setUp(self):
        self.client = Client()

    @override_settings(GUEST_MODE_ENABLED=False)
    def test_flag_off_pages_have_no_guest_text(self):
        for name in ("core:how_it_works", "core:how_to_upload", "core:terms"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertNotIn("Play as a Guest", html, name)
            self.assertNotIn("Try first as a guest", html, name)
            self.assertNotIn("Guest accounts", html, name)
        self.assertIn("Ready to Compete? Register, Step by Step", self.client.get(reverse("core:how_it_works")).content.decode())

    @override_settings(GUEST_MODE_ENABLED=True)
    def test_flag_on_pages_describe_guest_flow(self):
        works = self.client.get(reverse("core:how_it_works")).content.decode()
        self.assertIn("Just Want to Try It? Play as a Guest", works)
        self.assertIn("after 2 idle hours", works)
        self.assertIn("Ready to Compete? Register, Step by Step", works)
        self.assertIn("Try first as a guest", self.client.get(reverse("core:how_to_upload")).content.decode())
        terms = self.client.get(reverse("core:terms")).content.decode()
        self.assertIn('id="guest-accounts"', terms)
        self.assertIn("3 days of inactivity", terms)

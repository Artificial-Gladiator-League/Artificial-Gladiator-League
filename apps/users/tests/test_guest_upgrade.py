"""Guest upgrade: same row becomes permanent after an e-mail link; token, expiry, atomicity, reserved prefix."""
import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.games.models import Game
from apps.users.forms import GuestUpgradeForm, RegistrationForm
from apps.users.guest_upgrade import complete_upgrade
from apps.users.models import GuestUpgradeRequest, UserGameModel

User = get_user_model()
PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
SHA = "c" * 40


def form_data(**over):
    data = {
        "username": "alice_real", "password": "a-long-password", "confirm_password": "a-long-password",
        "email": "alice@example.com", "ai_name": "AliceBot", "country": "DE", "country_confirm": "on",
        "names_confirm": "on", "consent": "on", "captcha": "",
    }
    data.update(over)
    return data


def make_guest(name="guest_up000001", hf="alice"):
    user = User.objects.create_user(
        name, password=None, is_guest=True, hf_sub=f"sub-{name}", guest_hf_username=hf, ai_name=hf,
        guest_created_at=timezone.now(), guest_last_seen=timezone.now(),
        guest_expires_at=timezone.now() + timezone.timedelta(days=3),
    )
    UserGameModel.objects.create(
        user=user, game_type="chess", hf_model_repo_id=f"{hf}/m", hf_data_repo_id=f"{hf}/d",
        is_verified=True, verification_method="oauth_namespace", model_repo_ownership_verified=True,
        approved_full_sha=SHA, status="active",
    )
    return user


def messages_of(resp):
    return [str(m) for m in get_messages(resp.wsgi_request)]


@PLAIN_STATIC
@override_settings(GUEST_MODE_ENABLED=True, SITE_URL="https://example.test")
class UpgradeBase(TestCase):
    def setUp(self):
        cache.clear()
        mail.outbox.clear()
        self.guest = make_guest()
        self.client = Client()
        self.client.force_login(self.guest)

    def submit(self, client=None, **over):
        return (client or self.client).post(reverse("users:guest_upgrade"), form_data(**over))

    def link(self):
        match = re.search(r"https://example\.test(\S+)", mail.outbox[-1].body)
        return match.group(1)


class UpgradeFormTests(UpgradeBase):
    def test_reserved_prefix_is_refused_in_registration_and_upgrade(self):
        for form in (RegistrationForm(form_data(username="Guest_zzz")), GuestUpgradeForm(form_data(username="guest_abc"), guest=self.guest)):
            self.assertFalse(form.is_valid())
            self.assertIn("reserved", " ".join(form.errors["username"]))

    def test_registration_still_accepts_normal_usernames(self):
        form = RegistrationForm(form_data(username="guesty", email="g@example.com", ai_name="Other"))
        self.assertTrue(form.is_valid(), form.errors.as_json())

    def test_hf_oauth_complete_refuses_the_reserved_prefix_only(self):
        client = Client()
        session = client.session
        session["hf_oauth_pending"] = {"hf_username": "someone", "access_token_encrypted": "x"}
        session.save()
        resp = client.post(reverse("users:hf_oauth_complete"), {
            "username": "GUEST_hello", "ai_name": "X", "hf_model_repo_id": "someone/m",
        })
        self.assertContains(resp, "reserved for guest accounts")
        self.assertFalse(User.objects.filter(username="GUEST_hello").exists())

    def test_same_rules_as_registration(self):
        User.objects.create_user("taken", password="pw", email="alice@example.com", ai_name="Taken")
        for over, field in (
            ({"email": "ALICE@example.com"}, "email"), ({"username": "TAKEN"}, "username"),
            ({"ai_name": "taken"}, "ai_name"), ({"password": "short", "confirm_password": "short"}, "password"),
            ({"confirm_password": "different-password"}, "confirm_password"), ({"country": "ZZ"}, "country"),
            ({"consent": ""}, "consent"), ({"names_confirm": ""}, "names_confirm"), ({"country_confirm": ""}, "country_confirm"),
        ):
            form = GuestUpgradeForm(form_data(**over), guest=self.guest)
            self.assertFalse(form.is_valid(), over)
            self.assertIn(field, form.errors, over)

    def test_guest_may_keep_the_ai_name_taken_from_hugging_face(self):
        form = GuestUpgradeForm(form_data(ai_name="alice"), guest=self.guest)
        self.assertTrue(form.is_valid(), form.errors.as_json())


class UpgradeRequestTests(UpgradeBase):
    def test_request_stores_hashed_password_and_sends_a_link_without_changing_the_account(self):
        resp = self.submit()
        self.assertEqual(resp["Location"], reverse("users:guest_upgrade_sent"))
        req = GuestUpgradeRequest.objects.get(user=self.guest)
        self.assertNotIn("a-long-password", req.password_hash)
        self.assertTrue(req.password_hash.startswith(("pbkdf2_", "argon2", "bcrypt", "scrypt")))
        self.assertGreater(req.expires_at, timezone.now() + timezone.timedelta(hours=23))
        self.assertEqual(mail.outbox[-1].to, ["alice@example.com"])
        fresh = User.objects.get(pk=self.guest.pk)
        self.assertTrue(fresh.is_guest)
        self.assertEqual(fresh.username, "guest_up000001")
        self.assertEqual(fresh.email, "")

    def test_invalid_form_creates_nothing(self):
        resp = self.submit(consent="")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(GuestUpgradeRequest.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_mail_failure_removes_the_request(self):
        with mock.patch("apps.users.guest_views.EmailMultiAlternatives", create=True), \
                mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            resp = self.submit()
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(GuestUpgradeRequest.objects.exists())

    @override_settings(GUEST_RATE_UPGRADE_PER_USER="1/h")
    def test_rate_limited_per_guest(self):
        self.submit()
        resp = self.submit(username="another_name", email="other@example.com", ai_name="Other")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(GuestUpgradeRequest.objects.count(), 1)

    def test_newer_request_kills_the_older_link(self):
        self.submit()
        old_link = self.link()
        self.submit(username="alice_two", email="alice2@example.com", ai_name="AliceTwo")
        resp = Client().get(old_link)
        self.assertTrue(any("invalid, expired or already used" in m for m in messages_of(resp)))
        self.assertTrue(User.objects.get(pk=self.guest.pk).is_guest)

    def test_only_guests_can_start_an_upgrade(self):
        reg = User.objects.create_user("regular", password="pw")
        c = Client()
        c.force_login(reg)
        self.assertEqual(c.get(reverse("users:guest_upgrade"))["Location"], reverse("users:profile"))
        self.assertIn("/users/login/", Client().get(reverse("users:guest_upgrade"))["Location"])
        self.assertEqual(self.client.get(reverse("users:guest_upgrade")).status_code, 200)


class UpgradeConfirmTests(UpgradeBase):
    def confirm(self, **over):
        self.submit(**over)
        anon = Client()
        return anon, anon.get(self.link())

    def test_same_row_becomes_permanent_and_keeps_repos(self):
        game = Game.objects.create(white=self.guest, black=make_guest("guest_up000002", "bob"), is_guest_game=True)
        anon, resp = self.confirm()
        self.assertEqual(resp["Location"], reverse("users:login"))
        self.assertIn("now permanent", " ".join(messages_of(resp)))
        user = User.objects.get(pk=self.guest.pk)
        self.assertFalse(user.is_guest)
        self.assertEqual((user.username, user.email, user.ai_name), ("alice_real", "alice@example.com", "AliceBot"))
        self.assertTrue(user.check_password("a-long-password"))
        self.assertTrue(user.is_active)
        self.assertEqual((user.country, user.country_locked), ("DE", True))
        self.assertIsNotNone(user.country_set_at)
        self.assertEqual((user.guest_created_at, user.guest_last_seen, user.guest_expires_at), (None, None, None))
        self.assertEqual((user.hf_sub, user.guest_hf_username, user.hf_username), ("sub-guest_up000001", "alice", ""))
        gm = user.get_game_model("chess")
        self.assertEqual((gm.hf_model_repo_id, gm.hf_data_repo_id, gm.approved_full_sha), ("alice/m", "alice/d", SHA))
        self.assertEqual(gm.verification_method, "oauth_namespace")
        self.assertTrue(Game.objects.get(pk=game.pk).is_guest_game)
        self.assertEqual(User.objects.count(), 2)

    def test_no_automatic_login_and_new_credentials_work(self):
        anon, _ = self.confirm()
        self.assertNotIn("_auth_user_id", anon.session)
        self.assertTrue(Client().login(username="alice_real", password="a-long-password"))

    def test_link_works_once(self):
        anon, _ = self.confirm()
        link = self.link()
        resp = Client().get(link)
        self.assertTrue(any("invalid, expired or already used" in m for m in messages_of(resp)))
        self.assertEqual(User.objects.filter(username="alice_real").count(), 1)

    def test_expired_link_is_refused(self):
        self.submit()
        GuestUpgradeRequest.objects.update(expires_at=timezone.now() - timezone.timedelta(minutes=1))
        resp = Client().get(self.link())
        self.assertTrue(any("invalid, expired or already used" in m for m in messages_of(resp)))
        self.assertTrue(User.objects.get(pk=self.guest.pk).is_guest)

    def test_tampered_links_are_refused(self):
        self.submit()
        link = self.link()
        for bad in (link[:-3] + "xyz/", link.replace("/confirm/", "/confirm/AA", 1), "/users/guest/upgrade/confirm/!!/abc/"):
            resp = Client().get(bad)
            self.assertNotEqual(resp.status_code, 500, bad)
        self.assertTrue(User.objects.get(pk=self.guest.pk).is_guest)

    def test_conflict_since_the_request_is_reported_and_nothing_changes(self):
        self.submit()
        User.objects.create_user("alice_real", password="pw", email="x@example.com", ai_name="Zed")
        resp = Client().get(self.link())
        self.assertTrue(any("no longer available" in m for m in messages_of(resp)))
        user = User.objects.get(pk=self.guest.pk)
        self.assertTrue(user.is_guest)
        self.assertEqual(user.country, "")
        self.assertIsNone(GuestUpgradeRequest.objects.get().used_at)

    def test_email_taken_since_the_request(self):
        self.submit()
        User.objects.create_user("someone", password="pw", email="ALICE@example.com", ai_name="Zed2")
        resp = Client().get(self.link())
        self.assertTrue(any("email" in m for m in messages_of(resp)))
        self.assertTrue(User.objects.get(pk=self.guest.pk).is_guest)

    def test_everything_rolls_back_if_the_last_step_fails(self):
        self.submit()
        req = GuestUpgradeRequest.objects.get()
        with mock.patch.object(GuestUpgradeRequest, "save", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                complete_upgrade(req.pk)
        user = User.objects.get(pk=self.guest.pk)
        self.assertTrue(user.is_guest)
        self.assertEqual((user.country, user.username), ("", "guest_up000001"))

    def test_rows_are_locked_while_upgrading(self):
        self.submit()
        req = GuestUpgradeRequest.objects.get()
        with CaptureQueriesContext(connection) as ctx:
            ok, _ = complete_upgrade(req.pk)
        self.assertTrue(ok)
        locked = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"].upper()]
        self.assertGreaterEqual(len(locked), 2)

    def test_second_completion_is_refused(self):
        self.submit()
        req = GuestUpgradeRequest.objects.get()
        self.assertTrue(complete_upgrade(req.pk)[0])
        ok, message = complete_upgrade(req.pk)
        self.assertFalse(ok)
        self.assertIn("already been used", message)

    def test_upgraded_account_loses_guest_restrictions_but_not_tournament_requirements(self):
        from apps.users.integrity import can_join_tournament

        self.confirm()
        user = User.objects.get(pk=self.guest.pk)
        c = Client()
        c.force_login(user)
        self.assertEqual(c.get(reverse("users:gdpr")).status_code, 200)
        ok, reason = can_join_tournament(user, "chess", tournament_type="qa")
        self.assertFalse(ok)
        self.assertIn("AGL_VERIFY.txt", reason)

    def test_old_guest_session_ends_because_the_password_changed(self):
        self.confirm()
        resp = self.client.get(reverse("users:gdpr"))  # still the guest's browser session
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/users/login/", resp["Location"])

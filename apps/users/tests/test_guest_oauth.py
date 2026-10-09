"""Guest sign-in with Hugging Face: start view, callback branch, identity rules, token hygiene."""
import re
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
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
SECRET = "hf_oauth_SECRET_TOKEN_value"
REFUSAL = "log in with your username and password"


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._data, self.text = status, data or {}, "body"

    def json(self):
        return self._data


def _messages(resp):
    return [str(m) for m in get_messages(resp.wsgi_request)]


@PLAIN_STATIC
@override_settings(GUEST_MODE_ENABLED=True, HF_OAUTH_CLIENT_ID="cid", HF_OAUTH_CLIENT_SECRET="csecret")
class GuestOAuthBase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()

    def start(self, client=None, consent=True):
        client = client or self.client
        return client.post(reverse("users:guest_hf_start"), {"guest_consent": "on"} if consent else {})

    def callback(self, client, state, sub="sub-1", name="alice", **extra_info):
        info = {"sub": sub, "preferred_username": name, **extra_info}
        with mock.patch("requests.post", return_value=_Resp(200, {"access_token": SECRET})), \
                mock.patch("requests.get", return_value=_Resp(200, info)):
            return client.get(reverse("users:hf_oauth_callback"), {"state": state, "code": "abc"})

    def sign_in(self, sub="sub-1", name="alice", client=None, **extra_info):
        client = client or Client()
        start = self.start(client)
        state = parse_qs(urlparse(start["Location"]).query)["state"][0]
        return client, self.callback(client, state, sub=sub, name=name, **extra_info)


class GuestStartTests(GuestOAuthBase):
    def test_redirects_to_hf_with_minimal_scopes_and_pkce(self):
        resp = self.start()
        self.assertEqual(resp.status_code, 302)
        url = urlparse(resp["Location"])
        qs = parse_qs(url.query)
        self.assertEqual(url.netloc, "huggingface.co")
        self.assertEqual(qs["scope"], ["openid profile"])
        self.assertEqual(qs["code_challenge_method"], ["S256"])
        session = self.client.session
        self.assertEqual(session["hf_oauth_state"], qs["state"][0])
        self.assertEqual(session["hf_oauth_guest_state"], qs["state"][0])

    def test_requires_post_consent_and_flag(self):
        self.assertEqual(self.client.get(reverse("users:guest_hf_start")).status_code, 405)
        resp = self.start(consent=False)
        self.assertEqual(resp["Location"], reverse("users:login"))
        self.assertNotIn("hf_oauth_state", self.client.session)
        with override_settings(GUEST_MODE_ENABLED=False):
            self.assertEqual(self.start().status_code, 404)

    def test_csrf_is_enforced(self):
        strict = Client(enforce_csrf_checks=True)
        resp = strict.post(reverse("users:guest_hf_start"), {"guest_consent": "on"})
        self.assertEqual(resp.status_code, 403)

    @override_settings(GUEST_RATE_SIGNIN_PER_IP="2/h")
    def test_per_ip_rate_limit(self):
        self.assertIn("huggingface.co", self.start()["Location"])
        self.assertIn("huggingface.co", self.start()["Location"])
        self.assertEqual(self.start()["Location"], reverse("users:login"))

    @override_settings(GUEST_MAX_ACTIVE=1)
    def test_active_guest_cap(self):
        User.objects.create_user("guest_cap00001", password=None, is_guest=True, hf_sub="x")
        self.assertEqual(self.start()["Location"], reverse("users:login"))

    def test_logged_in_users_are_sent_to_the_lobby(self):
        user = User.objects.create_user("reg", password="pw")
        c = Client()
        c.force_login(user)
        resp = self.start(c)
        self.assertEqual(resp["Location"], reverse("games:lobby"))
        self.assertNotIn("hf_oauth_guest_state", c.session)

    def test_guest_cannot_restart_the_flow(self):
        client, _ = self.sign_in()
        self.assertEqual(self.start(client).status_code, 403)


class GuestCallbackTests(GuestOAuthBase):
    def test_creates_a_guest_with_the_specified_shape(self):
        client, resp = self.sign_in(sub="sub-42", name="alice")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("users:profile") + "?tab=ai-models")
        user = User.objects.get(hf_sub="sub-42")
        self.assertTrue(re.fullmatch(r"guest_[a-z0-9]{8}", user.username))
        self.assertTrue(user.is_guest and user.is_active)
        self.assertEqual(user.email, "")
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.ai_name, "alice")
        self.assertEqual(user.guest_hf_username, "alice")
        self.assertEqual(user.hf_username, "")
        self.assertIsNotNone(user.guest_created_at)
        delta = user.guest_expires_at - timezone.now()
        self.assertTrue(timezone.timedelta(days=2, hours=23) < delta <= timezone.timedelta(days=3))
        self.assertEqual(int(client.session["_auth_user_id"]), user.pk)

    def test_token_is_never_stored_or_logged(self):
        with self.assertLogs("apps.users", level="DEBUG") as logs:
            client, _ = self.sign_in(sub="sub-tok", name="tokuser")
        user = User.objects.get(hf_sub="sub-tok")
        self.assertEqual(user.hf_oauth_token_encrypted, "")
        self.assertIsNone(user.hf_oauth_linked_at)
        dump = repr(sorted(user.__dict__.items())) + repr(dict(client.session.items()))
        self.assertNotIn(SECRET, dump)
        self.assertNotIn("access_token", repr(dict(client.session.items())))
        self.assertFalse(any(SECRET in line for line in logs.output))

    def test_returning_guest_is_logged_back_in_not_duplicated(self):
        self.sign_in(sub="sub-7", name="bob")
        first = User.objects.get(hf_sub="sub-7")
        User.objects.filter(pk=first.pk).update(guest_expires_at=timezone.now() + timezone.timedelta(hours=2))
        client, resp = self.sign_in(sub="sub-7", name="bob")
        self.assertEqual(User.objects.filter(hf_sub="sub-7").count(), 1)
        self.assertEqual(int(client.session["_auth_user_id"]), first.pk)
        fresh = User.objects.get(pk=first.pk)
        self.assertGreater(fresh.guest_expires_at, timezone.now() + timezone.timedelta(days=2))

    def test_ai_name_gets_a_suffix_when_taken(self):
        User.objects.create_user("someone", password="pw", ai_name="Carol")
        self.sign_in(sub="sub-c", name="carol")
        guest = User.objects.get(hf_sub="sub-c")
        self.assertRegex(guest.ai_name, r"^carol-[a-z0-9]{4}$")

    def test_registered_identities_are_refused(self):
        cases = {
            "by hf_sub": dict(sub="sub-reg", name="newname", setup=lambda: User.objects.create_user(
                "u1", password="pw", hf_sub="sub-reg")),
            "by linked username": dict(sub="sub-2", name="Linked", setup=lambda: User.objects.create_user(
                "u2", password="pw", hf_username="linked")),
            "by verified repo namespace": dict(sub="sub-3", name="RepoOwner", setup=self._verified_repo_owner),
        }
        for label, case in cases.items():
            case["setup"]()
            client, resp = self.sign_in(sub=case["sub"], name=case["name"])
            self.assertEqual(resp["Location"], reverse("users:login"), label)
            self.assertTrue(any(REFUSAL in m for m in _messages(resp)), label)
            self.assertNotIn("_auth_user_id", client.session, label)
            self.assertFalse(User.objects.filter(is_guest=True, hf_sub=case["sub"]).exists(), label)

    def _verified_repo_owner(self):
        user = User.objects.create_user("u3", password="pw")
        UserGameModel.objects.create(
            user=user, game_type="chess", hf_model_repo_id="repoowner/model", is_verified=True,
        )

    def test_unverified_repo_namespace_does_not_block(self):
        user = User.objects.create_user("u4", password="pw")
        UserGameModel.objects.create(user=user, game_type="chess", hf_model_repo_id="freename/model")
        client, resp = self.sign_in(sub="sub-free", name="freename")
        self.assertEqual(resp["Location"], reverse("users:profile") + "?tab=ai-models")

    @override_settings(GUEST_RATE_SIGNIN_PER_SUB="1/h")
    def test_per_sub_rate_limit(self):
        self.sign_in(sub="sub-rl", name="rl")
        client, resp = self.sign_in(sub="sub-rl", name="rl")
        self.assertEqual(resp["Location"], reverse("users:login"))
        self.assertNotIn("_auth_user_id", client.session)

    @override_settings(GUEST_MAX_ACTIVE=1)
    def test_cap_is_enforced_at_the_callback(self):
        User.objects.create_user("guest_cap00002", password=None, is_guest=True, hf_sub="other")
        with override_settings(GUEST_MAX_ACTIVE=5):
            client = Client()
            start = self.start(client)
        state = parse_qs(urlparse(start["Location"]).query)["state"][0]
        resp = self.callback(client, state, sub="sub-new", name="newbie")
        self.assertEqual(resp["Location"], reverse("users:login"))
        self.assertFalse(User.objects.filter(hf_sub="sub-new").exists())

    def test_incomplete_or_bad_profile_is_rejected(self):
        for info in ({"sub": "", "preferred_username": "ok"}, {"sub": "s", "preferred_username": "bad name!"},
                     {"sub": "s", "preferred_username": ""}):
            client = Client()
            state = parse_qs(urlparse(self.start(client)["Location"]).query)["state"][0]
            with mock.patch("requests.post", return_value=_Resp(200, {"access_token": SECRET})), \
                    mock.patch("requests.get", return_value=_Resp(200, info)):
                resp = client.get(reverse("users:hf_oauth_callback"), {"state": state, "code": "abc"})
            self.assertEqual(resp["Location"], reverse("users:login"))
        self.assertFalse(User.objects.filter(is_guest=True).exists())

    def test_state_mismatch_is_refused(self):
        client = Client()
        self.start(client)
        resp = self.callback(client, "wrong-state")
        self.assertEqual(resp["Location"], reverse("users:login"))
        self.assertFalse(User.objects.filter(is_guest=True).exists())


class RegularFlowUnchangedTests(GuestOAuthBase):
    def test_abandoned_guest_flow_does_not_taint_the_regular_flow(self):
        client = Client()
        self.start(client)  # guest flow started, never finished
        regular = client.get(reverse("users:hf_oauth_start"))
        state = parse_qs(urlparse(regular["Location"]).query)["state"][0]
        self.assertEqual(parse_qs(urlparse(regular["Location"]).query)["scope"], ["openid profile read-repos"])
        resp = self.callback(client, state, sub="sub-regular", name="regularhf")
        self.assertEqual(resp["Location"], reverse("users:hf_oauth_complete"))
        self.assertFalse(User.objects.filter(is_guest=True).exists())
        self.assertIn("hf_oauth_pending", client.session)

    def test_regular_existing_user_still_logs_in_by_username(self):
        user = User.objects.create_user("linkeduser", password="pw", hf_username="linkedhf")
        client = Client()
        state = parse_qs(urlparse(client.get(reverse("users:hf_oauth_start"))["Location"]).query)["state"][0]
        with mock.patch("apps.games.model_preloader.preload_user_models"), \
                mock.patch("apps.users.model_lifecycle.sync_hf_repo_to_local"):
            resp = self.callback(client, state, sub="sub-linked", name="linkedhf")
        self.assertEqual(resp["Location"], reverse("games:lobby"))
        self.assertEqual(int(client.session["_auth_user_id"]), user.pk)

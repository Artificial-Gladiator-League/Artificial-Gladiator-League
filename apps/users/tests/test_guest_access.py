"""Guest access control: deny-by-default HTTP allowlist, WebSocket gate, decorators, helpers."""
import asyncio

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.users import guests
from apps.users.guest_ws import GuestWebSocketGate
from apps.users.integrity import can_join_tournament, is_eligible_for_tournament
from apps.users.models import UserGameModel

User = get_user_model()

PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
MESSAGE = "Create a permanent account to compete"


def make_guest(name="guest_abcd1234", **extra):
    return User.objects.create_user(
        name, password=None, is_guest=True, hf_sub=f"sub-{name}", guest_hf_username=name,
        guest_created_at=timezone.now(), guest_expires_at=timezone.now() + timezone.timedelta(days=3),
        **extra,
    )


@PLAIN_STATIC
@override_settings(GUEST_MODE_ENABLED=True)
class GuestHttpAccessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.guest = make_guest()
        self.client = Client()
        self.client.force_login(self.guest)

    def test_allowed_pages_open_and_noindex(self):
        for url in (reverse("core:how_it_works"), reverse("games:lobby"), reverse("core:leaderboard")):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
            self.assertEqual(resp["X-Robots-Tag"], "noindex, nofollow", url)

    def test_registered_only_pages_are_refused_with_message(self):
        blocked = [
            ("post", reverse("tournaments:join", args=[1])),
            ("get", reverse("users:gdpr")),
            ("get", reverse("users:gdpr_export")),
            ("post", reverse("users:save_paypal_email")),
            ("post", reverse("users:save_country")),
            ("post", reverse("users:change_password")),
            ("post", reverse("users:request_email_change")),
            ("get", reverse("forum:new_topic", args=["general"])),
            ("post", reverse("games:add_comment", args=[1])),
            ("get", reverse("games:chess_mcvs_move")),
            ("get", reverse("tournaments:prize_claim", args=[1])),
            ("get", "/admin/"),
        ]
        for method, url in blocked:
            resp = getattr(self.client, method)(url)
            self.assertEqual(resp.status_code, 403, url)
            self.assertIn(MESSAGE, resp.content.decode(), url)

    def test_read_only_views_refuse_writes(self):
        resp = self.client.post(reverse("tournaments:list"))
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(reverse("core:leaderboard"))
        self.assertEqual(resp.status_code, 403)

    def test_json_clients_get_json(self):
        resp = self.client.post(reverse("users:save_paypal_email"), HTTP_ACCEPT="application/json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json(), {"error": MESSAGE, "guest_blocked": True})

    def test_profile_post_without_gladiator_form_is_refused(self):
        resp = self.client.post(reverse("users:profile"), {"username": "x"})
        self.assertEqual(resp.status_code, 403)

    def test_registered_user_is_unaffected(self):
        other = User.objects.create_user("regular", password="pw")
        c = Client()
        c.force_login(other)
        self.assertEqual(c.get(reverse("users:gdpr")).status_code, 200)
        self.assertNotIn("X-Robots-Tag", c.get(reverse("core:how_it_works")))

    def test_activity_slides_guest_expiry(self):
        User.objects.filter(pk=self.guest.pk).update(guest_expires_at=timezone.now() + timezone.timedelta(hours=1))
        self.client.get(reverse("core:how_it_works"))
        fresh = User.objects.get(pk=self.guest.pk)
        self.assertGreater(fresh.guest_expires_at, timezone.now() + timezone.timedelta(days=2))
        self.assertIsNotNone(fresh.guest_last_seen)

    def test_guest_hidden_from_search_profile_and_leaderboard(self):
        User.objects.filter(pk=self.guest.pk).update(total_games=7, elo_chess=2000)
        viewer = User.objects.create_user("viewer", password="pw")
        c = Client()
        c.force_login(viewer)
        self.assertEqual(c.get(reverse("users:public_profile", args=[self.guest.username])).status_code, 404)
        results = c.get(reverse("users:user_search"), {"q": "guest_"}).json()["results"]
        self.assertEqual(results, [])
        self.assertNotIn(self.guest.username, c.get(reverse("core:leaderboard")).content.decode())
        self.assertNotIn(self.guest.username, c.get(reverse("core:leaderboard_json")).content.decode())

    def test_guest_own_public_profile_redirects_to_profile(self):
        resp = self.client.get(reverse("users:public_profile", args=[self.guest.username]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("users:profile"))


@PLAIN_STATIC
class GuestModeOffTests(TestCase):
    @override_settings(GUEST_MODE_ENABLED=False)
    def test_guest_sessions_end_when_mode_is_disabled(self):
        guest = make_guest("guest_offoff1")
        c = Client()
        c.force_login(guest)
        resp = c.get(reverse("core:how_it_works"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/")
        self.assertNotIn("_auth_user_id", c.session)


class GuestDecoratorTests(TestCase):
    def test_registered_required_blocks_guest_views_directly(self):
        from apps.users import views as user_views
        from apps.tournaments import views as t_views

        rf = RequestFactory()
        for view, args in ((user_views.save_paypal_email, ()), (t_views.join_tournament, (1,))):
            request = rf.post("/x/")
            request.user = make_guest(f"guest_dec{view.__name__[:6]}")
            resp = view(request, *args)
            self.assertEqual(resp.status_code, 403, view.__name__)


class GuestTournamentGateTests(TestCase):
    def test_guest_cannot_join_or_qualify(self):
        guest = make_guest("guest_tour0001")
        UserGameModel.objects.create(
            user=guest, game_type="chess", hf_model_repo_id="g/model", is_verified=True,
            verification_method="oauth_namespace",
        )
        self.assertEqual(can_join_tournament(guest, "chess"), (False, MESSAGE))
        self.assertEqual(is_eligible_for_tournament(guest, "chess"), (False, MESSAGE))

    def test_upgraded_account_needs_file_verification(self):
        user = User.objects.create_user("upgraded", password="pw")
        UserGameModel.objects.create(
            user=user, game_type="chess", hf_model_repo_id="u/model", is_verified=True,
            verification_method="oauth_namespace",
        )
        ok, reason = can_join_tournament(user, "chess", tournament_type="qa")
        self.assertFalse(ok)
        self.assertIn("AGL_VERIFY.txt", reason)

    def test_file_verified_account_is_not_blocked_by_method(self):
        user = User.objects.create_user("filever", password="pw")
        UserGameModel.objects.create(
            user=user, game_type="chess", hf_model_repo_id="f/model", is_verified=True,
        )
        self.assertEqual(can_join_tournament(user, "chess", tournament_type="qa"), (True, "OK"))


class _FakeUser:
    def __init__(self, guest, auth=True):
        self.is_guest, self.is_authenticated = guest, auth


class GuestWebSocketGateTests(SimpleTestCase):
    def _run(self, user, path):
        calls, sent = [], []

        async def inner(scope, receive, send):
            calls.append(scope["path"])

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            sent.append(message)

        asyncio.run(GuestWebSocketGate(inner)(
            {"type": "websocket", "user": user, "path": path}, receive, send,
        ))
        return calls, sent

    @override_settings(GUEST_MODE_ENABLED=True)
    def test_guest_allowed_paths(self):
        for path in ("/ws/games/lobby/", "/ws/games/12/", "/ws/games/12/spectate/", "/ws/presence/"):
            calls, sent = self._run(_FakeUser(True), path)
            self.assertEqual(calls, [path])
            self.assertEqual(sent, [])

    @override_settings(GUEST_MODE_ENABLED=True)
    def test_guest_refused_elsewhere(self):
        calls, sent = self._run(_FakeUser(True), "/ws/tournaments/5/")
        self.assertEqual(calls, [])
        self.assertEqual(sent, [{"type": "websocket.close", "code": 4403}])

    @override_settings(GUEST_MODE_ENABLED=False)
    def test_guest_refused_when_mode_is_off(self):
        calls, sent = self._run(_FakeUser(True), "/ws/games/lobby/")
        self.assertEqual(calls, [])
        self.assertEqual(sent[0]["code"], 4403)

    def test_registered_and_anonymous_pass_through(self):
        for user in (_FakeUser(False), _FakeUser(False, auth=False)):
            calls, sent = self._run(user, "/ws/tournaments/5/")
            self.assertEqual(calls, ["/ws/tournaments/5/"])


class GuestHelperTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_client_ip_uses_remote_addr_by_default(self):
        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="9.9.9.9", REMOTE_ADDR="1.2.3.4")
        self.assertEqual(guests.client_ip(request), "1.2.3.4")

    @override_settings(TRUSTED_PROXY_HEADER="X-Forwarded-For")
    def test_client_ip_uses_last_value_of_trusted_header(self):
        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="6.6.6.6, 5.5.5.5", REMOTE_ADDR="10.0.0.1")
        self.assertEqual(guests.client_ip(request), "5.5.5.5")

    @override_settings(TRUSTED_PROXY_HEADER="CF-Connecting-IP")
    def test_client_ip_cloudflare_and_malformed_fallback(self):
        request = RequestFactory().get("/", HTTP_CF_CONNECTING_IP="7.7.7.7", REMOTE_ADDR="10.0.0.1")
        self.assertEqual(guests.client_ip(request), "7.7.7.7")
        bad = RequestFactory().get("/", HTTP_CF_CONNECTING_IP="not-an-ip", REMOTE_ADDR="10.0.0.1")
        self.assertEqual(guests.client_ip(bad), "10.0.0.1")

    def test_rate_allowed_counts_per_window(self):
        results = [guests.rate_allowed("t", "x", "3/h") for _ in range(5)]
        self.assertEqual(results, [True, True, True, False, False])
        self.assertTrue(guests.rate_allowed("t", "other", "3/h"))

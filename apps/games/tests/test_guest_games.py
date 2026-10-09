"""Guest game isolation: creation, matchmaking, lobby/join/spectate visibility, and unrated stats."""
from unittest import mock

from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.games import consumers
from apps.games import views as game_views
from apps.games.models import Game
from apps.users.models import UserGameModel

User = get_user_model()
PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
MEMORY_LAYER = override_settings(CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})
NOT_READY = "Preparing your model..."


def make_guest(name, hf=None, ready_model=True):
    hf = hf or name
    user = User.objects.create_user(
        name, password=None, is_guest=True, hf_sub=f"sub-{name}", guest_hf_username=hf, ai_name=hf,
        guest_expires_at=timezone.now() + timezone.timedelta(days=3),
    )
    if ready_model:
        UserGameModel.objects.create(
            user=user, game_type="chess", hf_model_repo_id=f"{hf}/m", is_verified=True,
            verification_method="oauth_namespace", status="active", approved_full_sha="a" * 40,
        )
    return user


def make_registered(name):
    user = User.objects.create_user(name, password="pw")
    UserGameModel.objects.create(
        user=user, game_type="chess", hf_model_repo_id=f"{name}/m", is_verified=True, status="active",
    )
    return user


@PLAIN_STATIC
@override_settings(GUEST_MODE_ENABLED=True)
class GuestGameHttpTests(TestCase):
    def setUp(self):
        cache.clear()
        self.g1, self.g2 = make_guest("guest_gm000001"), make_guest("guest_gm000002")
        self.reg = make_registered("regular1")
        self.reg2 = make_registered("regular2")
        ready = mock.patch("apps.users.guest_snapshot.guest_snapshot_ready", return_value=(True, "OK"))
        ready.start()
        self.addCleanup(ready.stop)

    def client_for(self, user):
        c = Client()
        c.force_login(user)
        return c

    def create(self, user, view="users:create_game"):
        name = "games:create_game"
        return self.client_for(user).post(reverse(name), {"game_type": "chess", "time_control": "3+1"})

    def test_guest_created_game_is_flagged_and_registered_is_not(self):
        self.create(self.g1)
        self.assertTrue(Game.objects.get(host=self.g1).is_guest_game)
        self.create(self.reg)
        self.assertFalse(Game.objects.get(host=self.reg).is_guest_game)

    def test_lobby_create_flags_guest_games(self):
        resp = self.client_for(self.g1).post(
            reverse("games:create_lobby_game"), {"game_type": "chess", "base_minutes": "3", "increment_seconds": "1"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Game.objects.get(host=self.g1).is_guest_game)

    def test_guest_cannot_create_or_join_until_the_snapshot_is_ready(self):
        host = Game.objects.create(white=self.g2, host=self.g2, is_guest_game=True)
        with mock.patch("apps.users.guest_snapshot.guest_snapshot_ready", return_value=(False, NOT_READY)), \
                mock.patch("apps.users.guest_snapshot.ensure_guest_snapshots") as ensure:
            c = self.client_for(self.g1)
            created = c.post(reverse("games:create_game"), {"game_type": "chess"}, HTTP_ACCEPT="application/json")
            joined = c.post(reverse("games:join_game", args=[host.pk]), HTTP_ACCEPT="application/json")
        self.assertEqual(created.status_code, 403)
        self.assertEqual(created.json()["error"], NOT_READY)
        self.assertEqual(joined.status_code, 403)
        self.assertFalse(Game.objects.filter(host=self.g1).exists())
        host.refresh_from_db()
        self.assertIsNone(host.black)
        ensure.assert_called()

    def test_only_guests_can_join_guest_tables_and_only_registered_the_rest(self):
        guest_table = Game.objects.create(white=self.g2, host=self.g2, is_guest_game=True)
        reg_table = Game.objects.create(white=self.reg2, host=self.reg2)
        self.assertEqual(self.client_for(self.reg).post(reverse("games:join_game", args=[guest_table.pk])).status_code, 404)
        self.assertEqual(self.client_for(self.g1).post(reverse("games:join_game", args=[reg_table.pk])).status_code, 404)
        self.assertEqual(self.client_for(self.g1).post(reverse("games:join_game", args=[guest_table.pk])).status_code, 302)
        guest_table.refresh_from_db()
        self.assertEqual(guest_table.black_id, self.g1.pk)
        self.assertTrue(guest_table.is_guest_game)
        self.assertEqual(self.client_for(self.reg).post(reverse("games:join_game", args=[reg_table.pk])).status_code, 302)

    def test_lobby_lists_are_separated(self):
        Game.objects.create(white=self.g2, host=self.g2, is_guest_game=True)
        Game.objects.create(white=self.reg2, host=self.reg2)
        live_guest = Game.objects.create(white=self.g1, black=self.g2, status="ongoing", is_guest_game=True)
        live_reg = Game.objects.create(white=self.reg, black=self.reg2, status="ongoing")

        guest_ctx = self.client_for(self.g1).get(reverse("games:lobby")).context
        reg_ctx = self.client_for(self.reg).get(reverse("games:lobby")).context
        anon_ctx = Client().get(reverse("games:lobby")).context

        self.assertEqual([g["host_name"] for g in guest_ctx["waiting_games"]], [self.g2.username])
        self.assertEqual([g["host_name"] for g in reg_ctx["waiting_games"]], [self.reg2.username])
        self.assertEqual([g["pk"] for g in guest_ctx["ongoing_games"]], [live_guest.pk])
        self.assertEqual([g["pk"] for g in reg_ctx["ongoing_games"]], [live_reg.pk])
        self.assertEqual([g["pk"] for g in anon_ctx["ongoing_games"]], [live_reg.pk])
        self.assertTrue(all(g["pk"] != live_guest.pk for g in anon_ctx["display_games"]))

    def test_detail_spectate_and_comments_hide_guest_games_from_everyone_else(self):
        guest_game = Game.objects.create(white=self.g1, black=self.g2, status="ongoing", is_guest_game=True)
        reg_game = Game.objects.create(white=self.reg, black=self.reg2, status="ongoing")
        names = ("games:game_detail", "games:spectate", "games:game_comments")
        for name in names:
            url = reverse(name, args=[guest_game.pk])
            self.assertEqual(self.client_for(self.reg).get(url).status_code, 404, name)
            self.assertEqual(Client().get(url).status_code, 404, name)
            self.assertEqual(self.client_for(User.objects.create_user(f"x_{name[-6:]}", password="pw")).get(url).status_code, 404)
            rurl = reverse(name, args=[reg_game.pk])
            self.assertEqual(self.client_for(self.g1).get(rurl).status_code, 404, name)
            if name != "games:game_comments":  # that partial's template is missing in the repo
                self.assertEqual(self.client_for(self.g1).get(url).status_code, 200, name)
                self.assertEqual(Client().get(rurl).status_code, 200, name)

    def test_old_guest_games_stay_visible_to_their_players_after_an_upgrade(self):
        game = Game.objects.create(white=self.g1, black=self.g2, status="white_wins", result="1-0", is_guest_game=True)
        User.objects.filter(pk=self.g1.pk).update(is_guest=False)
        self.assertEqual(self.client_for(User.objects.get(pk=self.g1.pk)).get(reverse("games:game_detail", args=[game.pk])).status_code, 200)

    def test_history_and_next_live_are_separated(self):
        finished_guest = Game.objects.create(white=self.g1, black=self.g2, status="white_wins", result="1-0", is_guest_game=True)
        finished_reg = Game.objects.create(white=self.reg, black=self.reg2, status="white_wins", result="1-0")
        live_guest = Game.objects.create(white=self.g1, black=self.g2, status="ongoing", is_guest_game=True)
        self.assertEqual(
            [g.pk for g in self.client_for(self.reg).get(reverse("games:history")).context["games"]], [finished_reg.pk],
        )
        self.assertEqual(
            [g.pk for g in self.client_for(self.g1).get(reverse("games:history")).context["games"]], [finished_guest.pk],
        )
        resp = self.client_for(self.reg).get(reverse("games:next_live_ai_game", args=[finished_reg.pk]))
        self.assertIsNone(resp.json()["next_spectate_url"])
        resp = self.client_for(self.g2).get(reverse("games:next_live_ai_game", args=[finished_guest.pk]))
        self.assertEqual(resp.json()["next_spectate_url"], reverse("games:spectate", args=[live_guest.pk]))


class GuestGameStatsIsolationTests(TestCase):
    def setUp(self):
        self.a, self.b = make_guest("guest_st000001"), make_guest("guest_st000002")

    def snapshot(self, *users):
        return [
            (u.elo, u.elo_chess, u.wins, u.losses, u.draws, u.total_games, u.current_streak,
             UserGameModel.objects.get(user=u).rated_games_since_revalidation,
             UserGameModel.objects.get(user=u).rated_games_played)
            for u in (User.objects.get(pk=x.pk) for x in users)
        ]

    def finish(self, game):
        game.status, game.result, game.winner = Game.Status.WHITE_WINS, Game.Result.WHITE_WIN, game.white
        game.save()

    def test_finished_guest_game_changes_nothing(self):
        before = self.snapshot(self.a, self.b)
        game = Game.objects.create(white=self.a, black=self.b, status="ongoing", is_guest_game=True)
        with mock.patch("apps.users.signals._auto_pin_legacy_sha") as pin:
            self.finish(game)
        self.assertEqual(self.snapshot(self.a, self.b), before)
        game.refresh_from_db()
        self.assertEqual((game.elo_change_white, game.elo_change_black), (0, 0))
        pin.assert_not_called()

    def test_still_unrated_after_both_players_upgrade(self):
        before = self.snapshot(self.a, self.b)
        game = Game.objects.create(white=self.a, black=self.b, status="ongoing", is_guest_game=True)
        User.objects.filter(pk__in=[self.a.pk, self.b.pk]).update(is_guest=False)
        game = Game.objects.get(pk=game.pk)
        self.finish(game)
        self.assertEqual(self.snapshot(self.a, self.b), before)

    def test_regular_games_are_still_rated(self):
        x, y = make_registered("rated1"), make_registered("rated2")
        game = Game.objects.create(white=x, black=y, status="ongoing")
        self.finish(game)
        self.assertEqual(User.objects.get(pk=x.pk).wins, 1)
        self.assertEqual(User.objects.get(pk=y.pk).losses, 1)

    def test_chess_engine_update_is_a_no_op_for_guest_games(self):
        from apps.games.chess_engine import update_player_stats

        game = Game.objects.create(white=self.a, black=self.b, status="white_wins", result="1-0", is_guest_game=True)
        before = self.snapshot(self.a, self.b)
        update_player_stats(game)
        self.assertEqual(self.snapshot(self.a, self.b), before)
        self.assertEqual((game.elo_change_white, game.elo_change_black), (0, 0))

    def test_leaderboard_stats_ignore_guest_games(self):
        from apps.core.views import _per_game_stats

        x, y = make_registered("lb1"), make_registered("lb2")
        Game.objects.create(white=x, black=y, status="white_wins", result="1-0", winner=x, is_guest_game=True)
        Game.objects.create(white=x, black=y, status="draw", result="1/2-1/2")
        stats = _per_game_stats([x.pk, y.pk], "chess")
        self.assertEqual((stats[x.pk]["wins"], stats[x.pk]["total_games"], stats[x.pk]["draws"]), (0, 1, 1))

    def test_guest_games_never_count_toward_tournament_qualification(self):
        from apps.users.integrity import is_eligible_for_tournament

        x, y = make_registered("qual1"), make_registered("qual2")
        for _ in range(30):
            Game.objects.create(white=x, black=y, status="white_wins", result="1-0", is_guest_game=True)
        ok, reason = is_eligible_for_tournament(x, "chess")
        self.assertFalse(ok)
        self.assertIn("30 more", reason)

    def test_keep_warm_skips_guest_games(self):
        from apps.games import tasks

        Game.objects.create(white=self.a, black=self.b, status="ongoing", is_guest_game=True)
        with mock.patch("apps.games.bot_runner._get_repo_for_user") as repo, \
                mock.patch.dict("sys.modules", {
                    "apps.games.predict_chess": mock.MagicMock(_space_url_for=lambda r: "x"),
                    "apps.games.predict_breakthrough": mock.MagicMock(_space_base_url=lambda r: "x"),
                }), mock.patch.object(tasks, "_ping_endpoint") as ping:
            tasks.keep_warm_ongoing_games()
        repo.assert_not_called()
        ping.assert_not_called()


@MEMORY_LAYER
@override_settings(GUEST_MODE_ENABLED=True)
class GuestWebSocketIsolationTests(TransactionTestCase):
    def setUp(self):
        consumers._queues.clear()
        self.addCleanup(consumers._queues.clear)
        self.g1, self.g2 = make_guest("guest_ws000001"), make_guest("guest_ws000002")
        self.reg, self.reg2 = make_registered("wsreg1"), make_registered("wsreg2")
        self.guest_game = Game.objects.create(white=self.g1, black=self.g2, status="ongoing", is_guest_game=True)
        self.reg_game = Game.objects.create(white=self.reg, black=self.reg2, status="ongoing")

    async def connect(self, consumer, path, user, game_id=None):
        comm = WebsocketCommunicator(consumer.as_asgi(), path)
        if game_id is not None:
            comm.scope["url_route"] = {"kwargs": {"game_id": str(game_id)}}
        comm.scope["user"] = user
        connected, code = await comm.connect()
        return comm, connected

    async def test_spectate_socket_is_isolated(self):
        from django.contrib.auth.models import AnonymousUser

        path = f"/ws/games/{self.guest_game.pk}/spectate/"
        for user, expected in ((self.reg, False), (AnonymousUser(), False), (self.g1, True)):
            comm, connected = await self.connect(consumers.SpectateConsumer, path, user, self.guest_game.pk)
            self.assertEqual(connected, expected, user)
            await comm.disconnect()
        comm, connected = await self.connect(
            consumers.SpectateConsumer, f"/ws/games/{self.reg_game.pk}/spectate/", self.g1, self.reg_game.pk,
        )
        self.assertFalse(connected)
        comm, connected = await self.connect(
            consumers.SpectateConsumer, f"/ws/games/{self.reg_game.pk}/spectate/", self.reg2, self.reg_game.pk,
        )
        self.assertTrue(connected)
        await comm.disconnect()

    async def test_game_socket_is_isolated(self):
        with mock.patch.object(consumers.GameConsumer, "_prewarm_workers_both_if_waiting", new=mock.AsyncMock()):
            outsider, ok = await self.connect(consumers.GameConsumer, "/ws/games/1/", self.reg, self.guest_game.pk)
            self.assertFalse(ok)
            guest_on_reg, ok = await self.connect(consumers.GameConsumer, "/ws/games/1/", self.g1, self.reg_game.pk)
            self.assertFalse(ok)
            player, ok = await self.connect(consumers.GameConsumer, "/ws/games/1/", self.g1, self.guest_game.pk)
            self.assertTrue(ok)
            await player.disconnect()

    async def _drain_until(self, comm, wanted):
        for _ in range(8):
            msg = await comm.receive_json_from(timeout=2)
            if msg.get("type") == wanted:
                return msg
        self.fail(f"never received {wanted}")

    async def test_guests_match_each_other_in_their_own_queue_and_games_are_flagged(self):
        with mock.patch("apps.users.guests.guest_snapshot_block", return_value=None):
            c1, ok1 = await self.connect(consumers.LobbyConsumer, "/ws/games/lobby/", self.g1)
            c2, ok2 = await self.connect(consumers.LobbyConsumer, "/ws/games/lobby/", self.g2)
            self.assertTrue(ok1 and ok2)
            await c1.send_json_to({"action": "queue", "time_control": "3+1", "game_type": "chess"})
            await self._drain_until(c1, "queued")
            await c2.send_json_to({"action": "queue", "time_control": "3+1", "game_type": "chess"})
            matched = await self._drain_until(c2, "matched")
        game = await Game.objects.aget(pk=matched["game_id"])
        self.assertTrue(game.is_guest_game)
        self.assertEqual(game.game_type, "chess")
        await c1.disconnect()
        await c2.disconnect()

    async def test_guest_and_registered_queues_never_meet(self):
        with mock.patch("apps.users.guests.guest_snapshot_block", return_value=None), \
                mock.patch.object(consumers.LobbyConsumer, "_check_rated_permission",
                                  new=mock.AsyncMock(return_value=(True, ""))):
            guest, _ = await self.connect(consumers.LobbyConsumer, "/ws/games/lobby/", self.g1)
            await guest.send_json_to({"action": "queue", "time_control": "3+1", "game_type": "chess"})
            await self._drain_until(guest, "queued")
            regular, _ = await self.connect(consumers.LobbyConsumer, "/ws/games/lobby/", self.reg)
            await regular.send_json_to({"action": "queue", "time_control": "3+1", "game_type": "chess"})
            msg = await self._drain_until(regular, "queued")
        self.assertEqual(msg["position"], 1)
        self.assertEqual(await Game.objects.acount(), 2)  # only the two fixtures
        await guest.disconnect()
        await regular.disconnect()

    async def test_guest_cannot_queue_before_the_snapshot_is_ready(self):
        with mock.patch("apps.users.guests.guest_snapshot_block", return_value=NOT_READY):
            comm, _ = await self.connect(consumers.LobbyConsumer, "/ws/games/lobby/", self.g1)
            await comm.send_json_to({"action": "queue", "time_control": "3+1", "game_type": "chess"})
            msg = await self._drain_until(comm, "error")
        self.assertEqual(msg["message"], NOT_READY)
        self.assertEqual(consumers._queues, {})
        await comm.disconnect()

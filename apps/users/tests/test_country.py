"""Country of residence: set at registration, locked, staff-only changes, profile flag."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.users.forms import RegistrationForm
from apps.users.models import CountryChangeLog

User = get_user_model()

PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)


def _reg_data(**over):
    data = {
        "username": "newbie", "password": "longenough1", "confirm_password": "longenough1",
        "email": "newbie@example.com", "ai_name": "NewbieAI",
        "country": "IN", "country_confirm": "on", "consent": "on", "captcha": "",
    }
    data.update(over)
    return data


@override_settings(
    RECAPTCHA_TESTING=True,
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "reg-country-tests"}},
)
class RegistrationCountryTest(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # locmem only (overridden above): the 5/h registration limit must not leak between tests
    def test_form_requires_country(self):
        for missing in ("", "ZZ", "XX"):
            form = RegistrationForm(_reg_data(country=missing))
            self.assertFalse(form.is_valid(), missing)
            self.assertIn("country", form.errors)

    def test_form_requires_the_confirmation(self):
        data = _reg_data()
        del data["country_confirm"]
        form = RegistrationForm(data)
        self.assertFalse(form.is_valid())
        self.assertIn("country_confirm", form.errors)

    def test_form_accepts_country_and_confirmation(self):
        form = RegistrationForm(_reg_data())
        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertEqual(form.cleaned_data["country"], "IN")

    def test_form_normalises_case(self):
        form = RegistrationForm(_reg_data(country="il"))
        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertEqual(form.cleaned_data["country"], "IL")

    def test_page_shows_select_warning_and_checkbox(self):
        resp = self.client.get(reverse("users:register"))
        self.assertContains(resp, 'name="country"')
        self.assertContains(resp, 'name="country_confirm"')
        self.assertContains(resp, "cannot be changed later")
        self.assertContains(resp, "India")

    @mock.patch("apps.users.views.EmailMultiAlternatives")
    def test_registration_saves_and_locks_the_country(self, mail):
        resp = self.client.post(reverse("users:register"), _reg_data())
        self.assertEqual(resp.status_code, 302, resp.content.decode()[:300])
        user = User.objects.get(username="newbie")
        self.assertEqual(user.country, "IN")
        self.assertTrue(user.country_locked)
        self.assertIsNotNone(user.country_set_at)
        self.assertTrue(user.show_flag)

    @mock.patch("apps.users.views.EmailMultiAlternatives")
    def test_registration_without_country_creates_nothing(self, mail):
        resp = self.client.post(reverse("users:register"), _reg_data(country=""))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="newbie").exists())

    @mock.patch("apps.users.views.EmailMultiAlternatives")
    def test_registration_without_confirmation_creates_nothing(self, mail):
        data = _reg_data()
        del data["country_confirm"]
        resp = self.client.post(reverse("users:register"), data)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="newbie").exists())


class CountryLockTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("locked", password="pw")
        self.assertTrue(self.user.claim_country("IN"))
        self.client.force_login(self.user)

    def fresh(self):
        return User.objects.get(pk=self.user.pk)

    def test_claim_country_is_one_shot(self):
        self.assertFalse(self.fresh().claim_country("US"))
        self.assertEqual(self.fresh().country, "IN")

    def test_model_save_cannot_change_a_saved_country(self):
        user = self.fresh()
        user.country = "US"
        with self.assertRaises(ValidationError):
            user.save()
        with self.assertRaises(ValidationError):
            user.save(update_fields=["country"])
        with self.assertRaises(ValidationError):
            user.save(update_fields=["country_locked"])
        self.assertEqual(self.fresh().country, "IN")

    def test_a_stale_copy_can_never_overwrite_the_country(self):
        stale = User.objects.get(pk=self.user.pk)
        User.objects.filter(pk=self.user.pk).update(country="")        # simulate an older snapshot
        stale = User.objects.get(pk=self.user.pk)
        User.objects.filter(pk=self.user.pk).update(country="IN")
        stale.elo = 1500
        stale.save()                                                   # full save of a stale copy
        fresh = self.fresh()
        self.assertEqual((fresh.country, fresh.elo), ("IN", 1500))

    def test_profile_post_with_a_crafted_country_is_ignored(self):
        resp = self.client.post(reverse("users:profile"), {
            "username": "locked", "country": "US", "country_locked": "", "country_set_at": "",
        })
        self.assertIn(resp.status_code, (200, 302))
        fresh = self.fresh()
        self.assertEqual((fresh.country, fresh.country_locked), ("IN", True))

    def test_save_country_view_refuses_to_change_a_saved_country(self):
        resp = self.client.post(reverse("users:save_country"), {
            "action": "set", "country": "US", "country_confirm": "1",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.fresh().country, "IN")

    def test_flag_action_never_touches_the_country(self):
        self.client.post(reverse("users:save_country"), {
            "action": "flag", "country": "US", "country_confirm": "1",
        })
        fresh = self.fresh()
        self.assertEqual(fresh.country, "IN")
        self.assertFalse(fresh.show_flag)          # checkbox absent = off

    def test_settings_page_shows_read_only_country_and_support_line(self):
        resp = self.client.get(reverse("users:profile"))
        self.assertContains(resp, "To correct your country, contact support.")
        self.assertContains(resp, "Show my flag on my profile")
        self.assertContains(resp, "India")
        self.assertNotContains(resp, 'name="country"')
        self.assertNotContains(resp, 'name="country_confirm"')


class ExistingUserSetsCountryOnceTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("old", password="pw")
        self.client.force_login(self.user)
        self.url = reverse("users:save_country")

    def fresh(self):
        return User.objects.get(pk=self.user.pk)

    def test_no_forced_prompt_on_login_or_pages(self):
        resp = self.client.get(reverse("tournaments:list"))
        self.assertEqual(resp.status_code, 200)

    def test_settings_page_offers_the_form_with_warning(self):
        resp = self.client.get(reverse("users:profile"))
        self.assertContains(resp, 'name="country"')
        self.assertContains(resp, 'name="country_confirm"')
        self.assertContains(resp, "cannot be changed later")

    def test_set_once_then_locked(self):
        resp = self.client.post(self.url, {"action": "set", "country": "il", "country_confirm": "1"})
        self.assertEqual(resp.status_code, 302)
        user = self.fresh()
        self.assertEqual((user.country, user.country_locked), ("IL", True))
        self.assertIsNotNone(user.country_set_at)

        self.client.post(self.url, {"action": "set", "country": "US", "country_confirm": "1"})
        self.assertEqual(self.fresh().country, "IL")

    def test_confirmation_is_required(self):
        self.client.post(self.url, {"action": "set", "country": "IL"})
        self.assertEqual(self.fresh().country, "")

    def test_invalid_code_is_rejected(self):
        self.client.post(self.url, {"action": "set", "country": "ZZ", "country_confirm": "1"})
        self.assertEqual(self.fresh().country, "")

    def test_get_is_not_allowed_to_set(self):
        self.client.get(self.url + "?action=set&country=IL&country_confirm=1")
        self.assertEqual(self.fresh().country, "")

    def test_next_must_be_local(self):
        resp = self.client.post(self.url, {
            "action": "set", "country": "IL", "country_confirm": "1", "next": "https://evil.example/x",
        })
        self.assertNotIn("evil.example", resp["Location"])

    def test_next_returns_to_the_terms_page(self):
        resp = self.client.post(self.url, {
            "action": "set", "country": "IL", "country_confirm": "1", "next": "/tournaments/5/terms/",
        })
        self.assertEqual(resp["Location"], "/tournaments/5/terms/")


class StaffCountryChangeTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("boss", "boss@example.com", "pw")
        self.user = User.objects.create_user("target", password="pw")
        self.user.claim_country("IN")
        self.client.force_login(self.staff)
        self.changelist = reverse("admin:users_customuser_changelist")

    def fresh(self):
        return User.objects.get(pk=self.user.pk)

    def test_country_is_read_only_in_the_user_admin_form(self):
        resp = self.client.get(reverse("admin:users_customuser_change", args=[self.user.pk]))
        self.assertEqual(resp.status_code, 200)
        for name in ("country", "country_set_at", "country_locked", "show_flag"):
            self.assertNotContains(resp, f'name="{name}"')
            self.assertIn(name, resp.context["adminform"].readonly_fields)

    def test_admin_form_post_cannot_change_the_country(self):
        url = reverse("admin:users_customuser_change", args=[self.user.pk])
        self.client.post(url, {"username": "target", "country": "US", "country_locked": ""})
        self.assertEqual(self.fresh().country, "IN")

    def test_action_page_asks_for_country_and_reason(self):
        resp = self.client.post(self.changelist, {
            "action": "change_locked_country", "_selected_action": [self.user.pk],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "reason")
        self.assertEqual(self.fresh().country, "IN")
        self.assertEqual(CountryChangeLog.objects.count(), 0)

    def test_reason_is_required(self):
        for reason in ("", "   "):
            resp = self.client.post(self.changelist, {
                "action": "change_locked_country", "_selected_action": [self.user.pk],
                "apply": "1", "country": "US", "reason": reason,
            })
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "required")
        self.assertEqual(self.fresh().country, "IN")
        self.assertEqual(CountryChangeLog.objects.count(), 0)

    def test_change_with_reason_is_applied_and_logged(self):
        resp = self.client.post(self.changelist, {
            "action": "change_locked_country", "_selected_action": [self.user.pk],
            "apply": "1", "country": "US", "reason": "Verified passport: moved to the US",
        })
        self.assertEqual(resp.status_code, 302)
        user = self.fresh()
        self.assertEqual((user.country, user.country_locked), ("US", True))
        log = CountryChangeLog.objects.get()
        self.assertEqual(
            (log.user, log.changed_by, log.old_country, log.new_country, log.reason),
            (user, self.staff, "IN", "US", "Verified passport: moved to the US"),
        )
        self.assertIsNotNone(log.changed_at)

    def test_model_helper_requires_a_reason(self):
        with self.assertRaises(ValidationError):
            self.user.change_country_by_staff("US", self.staff, " ")
        self.assertEqual(self.fresh().country, "IN")

    def test_non_staff_cannot_use_the_action(self):
        self.client.force_login(self.user)
        resp = self.client.post(self.changelist, {
            "action": "change_locked_country", "_selected_action": [self.user.pk],
            "apply": "1", "country": "US", "reason": "please",
        })
        self.assertIn(resp.status_code, (302, 403))
        self.assertEqual(self.fresh().country, "IN")
        self.assertEqual(CountryChangeLog.objects.count(), 0)


@PLAIN_STATIC
class ProfileFlagTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("flagged", password="pw")
        self.user.claim_country("IL")
        self.viewer = User.objects.create_user("viewer", password="pw")
        self.client.force_login(self.viewer)

    def profile(self, user=None):
        return self.client.get(reverse("users:public_profile", args=[(user or self.user).username]))

    def test_flag_appears_before_the_username_on_the_profile_page(self):
        html = self.profile().content.decode()
        self.assertIn('<img class="lp-flag"', html)
        self.assertIn("flags/il.gif", html)
        self.assertLess(html.index('<img class="lp-flag"'), html.index('<span>flagged</span>'))

    def test_flag_hidden_when_show_flag_is_off(self):
        User.objects.filter(pk=self.user.pk).update(show_flag=False)
        html = self.profile().content.decode()
        self.assertNotIn('<img class="lp-flag"', html)
        self.assertNotIn("flags/il.gif", html)

    def test_no_flag_without_a_country(self):
        html = self.profile(self.viewer).content.decode()
        self.assertNotIn('<img class="lp-flag"', html)

    def test_own_profile_shows_the_flag_too(self):
        self.client.force_login(self.user)
        self.assertIn('<img class="lp-flag"', self.client.get(reverse("users:profile")).content.decode())

    def test_flag_is_on_the_profile_page_only(self):
        from apps.tournaments.models import Tournament
        from django.utils import timezone
        t = Tournament.objects.create(
            name="Flag Cup", type=Tournament.Type.QA, status=Tournament.Status.OPEN,
            start_time=timezone.now(), capacity=2, rounds_total=1,
        )
        t.players.add(self.user)
        self.client.force_login(self.user)
        for url in (
            reverse("core:home"), reverse("core:leaderboard"), reverse("tournaments:list"),
            reverse("tournaments:detail", args=[t.pk]), reverse("games:lobby"),
            reverse("games:history"), reverse("news:list"), reverse("forum:category_list"),
        ):
            body = self.client.get(url).content.decode()
            self.assertNotIn('<img class="lp-flag"', body, url)
            self.assertNotIn("flags/il.gif", body, url)

    def test_other_places_keep_the_empty_flag_stub(self):
        self.assertEqual(self.user.country_flag, "")

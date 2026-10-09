"""keep_warm_ongoing_games must not raise when the Space URL helpers are missing."""
import sys
from unittest import mock

from django.test import TestCase

from apps.games import tasks


class KeepWarmTests(TestCase):
    def test_missing_helpers_return_without_raising(self):
        tasks._import_failure_logged = False
        with mock.patch.dict(sys.modules, {"apps.games.predict_chess": None}):
            with self.assertLogs("apps.games.tasks", level="DEBUG") as cm:
                first = tasks.keep_warm_ongoing_games()
            second = tasks.keep_warm_ongoing_games()
        self.assertTrue(first.startswith("Skipped"))
        self.assertTrue(second.startswith("Skipped"))
        self.assertEqual(len(cm.records), 1)

"""Dependabot opens weekly vcpkg baseline updates for the root manifest."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]


class DependabotConfigurationTests(unittest.TestCase):
    def test_root_vcpkg_updates_run_weekly_in_the_local_timezone(self) -> None:
        # JSON is valid YAML and lets the contract use the standard-library parser.
        configuration = json.loads(
            (REPOSITORY / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(configuration["version"], 2)
        self.assertEqual(set(configuration), {"version", "updates"})
        self.assertEqual(configuration["updates"], [{
            "package-ecosystem": "vcpkg",
            "directory": "/",
            "open-pull-requests-limit": 1,
            "schedule": {
                "interval": "weekly",
                "day": "monday",
                "time": "09:00",
                "timezone": "Asia/Vladivostok",
            },
        }])

"""Dependabot opens weekly vcpkg baseline updates for the root manifest."""

from __future__ import annotations

import json
from datetime import datetime
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
            "allow": [{"dependency-name": "github.com/microsoft/vcpkg"}],
            "ignore": [{
                "dependency-name": "github.com/microsoft/vcpkg",
                "versions": ["<=2026.10.15"],
            }],
            "schedule": {
                "interval": "weekly",
                "day": "monday",
                "time": "09:00",
                "timezone": "Asia/Vladivostok",
            },
        }])

    def test_baseline_prs_skip_older_releases_and_keep_future_releases_eligible(self) -> None:
        configuration = json.loads((REPOSITORY / ".github/dependabot.yml").read_text(encoding="utf-8"))
        update, = configuration["updates"]
        self.assertEqual(update["allow"], [{"dependency-name": "github.com/microsoft/vcpkg"}])
        ignored, = update["ignore"]
        self.assertEqual(ignored["dependency-name"], "github.com/microsoft/vcpkg")
        constraint, = ignored["versions"]
        cutoff = datetime.strptime(constraint, "<=%Y.%m.%d").date()
        # The pinned registry commit is from 2026-10-15. Dependabot compares
        # release tags rather than commit ancestry, so guard the snapshot date.
        self.assertEqual(cutoff.isoformat(), "2026-10-15")
        for tag, should_ignore in (("2026.07.29", True), ("2026.10.14", True),
                                   ("2026.10.15", True), ("2026.10.16", False),
                                   ("2027.01.01", False)):
            with self.subTest(tag=tag):
                self.assertEqual(datetime.strptime(tag, "%Y.%m.%d").date() <= cutoff, should_ignore)

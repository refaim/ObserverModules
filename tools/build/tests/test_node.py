from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.node import NodeFactory  # noqa: E402
from core.render import TemplateRenderer  # noqa: E402


class NodeFactoryTests(unittest.TestCase):
    def test_runtime_environment_and_cwd_are_signed(self) -> None:
        renderer = TemplateRenderer(BUILD_ROOT / "templates")
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            first.mkdir()
            second.mkdir()

            def create(cwd: Path, value: str):
                factory = NodeFactory(renderer, cwd, {"tool": "1"}, (("SETTING", value),))
                return factory.make(
                    "argv.json",
                    "example",
                    "slot",
                    {"argv": (str(Path(sys.executable).resolve()), "--version")},
                    files={},
                    config={"action": "probe"},
                )

            baseline = create(first, "one")
            changed_environment = create(first, "two")
            changed_cwd = create(second, "one")

        self.assertNotEqual(baseline.uid, changed_environment.uid)
        self.assertNotEqual(baseline.uid, changed_cwd.uid)


if __name__ == "__main__":
    unittest.main(verbosity=2)

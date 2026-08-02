from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import tempfile
import unittest

from core.graph import Graph
from core.node import NodeFactory
from core.render import TemplateRenderer
from graphs.common import BUILD_ROOT, restore_node


@dataclass(frozen=True)
class Toolchain:
    pwsh: Path
    vcpkg_root: Path
    environment: tuple[tuple[str, str], ...]
    identity: tuple[tuple[str, str], ...]


def repository(root: Path) -> Path:
    (root / "build/vcpkg/triplets").mkdir(parents=True)
    (root / "vcpkg.json").write_text("{}\n", encoding="utf-8")
    for flavor in ("", "-asan"):
        (root / f"build/vcpkg/triplets/observer-x64-windows-static{flavor}.cmake").write_text(
            f"triplet{flavor}\n", encoding="utf-8"
        )
    return root


class RestoreNodeTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Toolchain]:
        repo = repository(root / "repo")
        tools = root / "tools"
        vcpkg = tools / "vcpkg"
        vcpkg.mkdir(parents=True)
        pwsh = tools / "pwsh.exe"
        pwsh.write_bytes(b"pwsh-one")
        (vcpkg / "vcpkg.exe").write_bytes(b"vcpkg-one")
        return repo, Toolchain(
            pwsh, vcpkg,
            (("ZED", "last"), ("Path", str(tools))),
            (("pwsh_version", "7.5"), ("unrelated", "first")),
        )

    def test_restore_identity_ignores_parent_factory_and_wrapper_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, first = self.fixture(Path(temporary))
            second = replace(
                first,
                environment=(("PATH", str(first.pwsh.parent)), ("zed", "last")),
                identity=(("unrelated", "second"), ("pwsh_version", "wrapper-noise")),
            )
            renderer = TemplateRenderer(BUILD_ROOT / "templates")
            factories = (
                NodeFactory(renderer, repo / "wrong-one", {"compiler": "one"}, (("NOISE", "one"),)),
                NodeFactory(renderer, repo / "wrong-two", {"compiler": "two"}, (("NOISE", "two"),)),
            )
            pairs = tuple(
                (
                    restore_node(repo, first, factories[0], "x64", flavor=flavor),
                    restore_node(repo, second, factories[1], "x64", flavor=flavor),
                )
                for flavor in ("", "asan")
            )

        for left, right in pairs:
            self.assertEqual(left, right)
        nodes = tuple({node.name: node for pair in pairs for node in pair}.values())
        self.assertEqual(len(nodes), 2)
        Graph(nodes, tuple(node.name for node in nodes), {"restore": 1})

    def test_exact_tool_and_triplet_changes_invalidate_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, toolchain = self.fixture(Path(temporary))
            factory = NodeFactory(
                TemplateRenderer(BUILD_ROOT / "templates"), repo, {"unrelated": "identity"}
            )

            baseline = restore_node(repo, toolchain, factory, "x64")
            toolchain.pwsh.write_bytes(b"pwsh-two")
            pwsh_changed = restore_node(repo, toolchain, factory, "x64")
            (toolchain.vcpkg_root / "vcpkg.exe").write_bytes(b"vcpkg-two")
            vcpkg_changed = restore_node(repo, toolchain, factory, "x64")
            (repo / "build/vcpkg/triplets/observer-x64-windows-static.cmake").write_text(
                "changed triplet\n", encoding="utf-8"
            )
            triplet_changed = restore_node(repo, toolchain, factory, "x64")

        self.assertNotEqual(baseline.uid, pwsh_changed.uid)
        self.assertNotEqual(pwsh_changed.uid, vcpkg_changed.uid)
        self.assertNotEqual(vcpkg_changed.uid, triplet_changed.uid)


if __name__ == "__main__":
    unittest.main()

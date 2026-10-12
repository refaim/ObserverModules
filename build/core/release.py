"""Fail-closed module release gate over published package manifests.

A release is named by a module tag, ``<module>/vX.Y.Z``: the tag selects exactly one module and
its own SemVer lineage in ``src/modules/<module>/VERSION``. This command validates the tag,
compares the module's content identity recorded in ``packages.json`` against the previously
published release of the *same* module lineage, and refuses a release whose version or content
disagree. It also verifies the bytes of the supplied archive assets before a release job
publishes them. The default all-module gate remains available for a repository-wide release.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile

from core.module_version import (
    MODULES,
    ModuleVersionError,
    parse_version,
    read_version,
)
from core.changelog import ChangelogError, latest_notes, release_notes
# The final symbol bundle has to be byte-identical to the one the local packaging route writes,
# so the release gate reuses that deterministic ZIP writer instead of restating it.
from core.package import _zip


# The architectures a module release must publish. Kept here (rather than imported from the
# packaging code) so the gate is an independent oracle for the names it verifies.
ARCHITECTURES = ("x86", "x64")
_TAG_ROOT = "v"
_IDENTITY = re.compile(r"[0-9a-f]{64}")
# A published asset is a plain file name, so any separator or traversal segment is rejected.
_ARCHIVE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.zip")
# An intermediate symbol bundle is small; a member that claims more than this, or a bundle whose
# streamed payload exceeds it, is refused before it can exhaust memory.
_MAXIMUM_SYMBOL_BYTES = 512 * 1024 * 1024
_SYMBOL_CHUNK = 1024 * 1024
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_BOOTSTRAP_NOTE = (
    "no previous release manifest was available, so the module version gate recorded a "
    "bootstrap release instead of comparing content identities"
)


class ReleaseError(RuntimeError):
    """A release tag, manifest, or asset set is malformed or violates the version gate."""


def module_archive(module: str, version: str, architecture: str) -> str:
    """Return the published archive name that carries one module's release version."""

    return f"{module}-{version}-{architecture}.zip"


def symbol_archive(module: str, version: str) -> str:
    """Return the published PDB archive name that carries one module's release version.

    The bundle is one file per module and release; each architecture contributes one
    ``<arch>/<module>.pdb`` entry, so the intermediate per-architecture bundles flatten into it.
    """

    return f"{module}-{version}-pdb.zip"


def expected_archives(
    module: str, version: str, architectures: Sequence[str] = ARCHITECTURES
) -> tuple[str, ...]:
    """Return every archive a subdir-per-architecture module release must publish."""

    return tuple(
        module_archive(module, version, architecture) for architecture in architectures
    ) + (symbol_archive(module, version),)


def parse_tag(tag: str) -> tuple[str, str]:
    """Split one ``<module>/vX.Y.Z`` release tag, rejecting anything malformed."""

    module, separator, version = tag.partition(f"/{_TAG_ROOT}")
    if not separator:
        raise ReleaseError(f"release tag must be <module>/v<X.Y.Z>: {tag!r}")
    if module not in MODULES:
        raise ReleaseError(f"unknown release module: {module!r}")
    try:
        parse_version(version)
    except ModuleVersionError as error:
        raise ReleaseError(f"release tag version must be X.Y.Z: {tag!r}") from error
    return module, version


def validate_tag(repository: Path, tag: str) -> tuple[str, str]:
    """Resolve one release tag and require it to match the module's declared version."""

    module, version = parse_tag(tag)
    try:
        declared = read_version(repository, module)
    except ModuleVersionError as error:
        raise ReleaseError(f"module version file is unreadable for {module!r}") from error
    if declared != version:
        raise ReleaseError(
            f"release tag {tag!r} does not match src/modules/{module}/VERSION ({declared})"
        )
    for language in ("en", "ru"):
        try:
            latest_notes(repository, module, version, language=language)
        except ChangelogError as error:
            raise ReleaseError(
                f"release tag {tag!r} does not match the {module!r} ChangeLog: {error}"
            ) from error
    return module, version


def stable_release_tags(releases: Iterable[str]) -> list[str]:
    """Extract the tag of every published, stable release from ``gh api`` JSON lines.

    The workflow lists every page of the release history as one JSON object per line carrying the
    ``tag``, ``draft`` and ``prerelease`` fields. Draft and prerelease entries, blank lines, and
    records that are not JSON objects with a string tag are ignored, so an unpublished draft can
    never disturb a published lineage.
    """

    tags: list[str] = []
    for line in releases:
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("draft") or record.get("prerelease"):
            continue
        tag = record.get("tag")
        if isinstance(tag, str):
            tags.append(tag)
    return tags


def resolve_previous(
    tag: str, releases: Iterable[str]
) -> str | None:
    """Return the release a *new* publication of ``tag`` must be compared against, if any.

    ``releases`` is the published stable release history of every module; only tags of ``tag``'s
    own module are candidates and the release under test is never mistaken for its own
    predecessor. The latest published release of the lineage is returned whatever its version, so
    publishing an older version than the current head resolves that head and the version gate
    rejects the backwards release, instead of silently bootstrapping it.

    A rerun of an already-published tag never reaches here: it reads the predecessor recorded in
    the immutable release report instead, so history that moved on afterwards (or a hand-inserted
    older release) can never change what the rerun compares against.
    """

    module, version = parse_tag(tag)
    current = parse_version(version)
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for candidate in releases:
        text = candidate.strip()
        try:
            candidate_module, candidate_version = parse_tag(text)
        except ReleaseError:
            continue
        if candidate_module != module:
            continue
        parsed = parse_version(candidate_version)
        if parsed == current:
            continue
        candidates.append((parsed, text))
    return max(candidates)[1] if candidates else None


def recorded_previous_tag(tag: str, report: object) -> str | None:
    """Return the predecessor an already-published release recorded for ``tag``.

    A rerun must compare against the ``previous_version`` written into the immutable
    ``release.json`` by the original publication, never against the mutable release history. The
    report has to describe exactly the tag's own module at the tag's version, and record a
    ``previous_version`` that is either ``null`` (a bootstrap release, which has no predecessor) or
    a canonical version strictly older than the tag.
    """

    module, version = parse_tag(tag)
    if not isinstance(report, dict):
        raise ReleaseError("current release report must be a JSON object")
    records = report.get("modules")
    if not isinstance(records, list):
        raise ReleaseError("current release report must describe its modules")
    matching = [
        record
        for record in records
        if isinstance(record, dict) and record.get("module") == module
    ]
    if len(matching) != 1:
        raise ReleaseError(
            f"current release report must describe exactly one {module!r} module"
        )
    record = matching[0]
    if record.get("version") != version:
        raise ReleaseError(
            f"current release report version does not match the tag for {module!r}"
        )
    if "previous_version" not in record:
        raise ReleaseError(
            f"current release report records no previous_version for {module!r}"
        )
    previous = record["previous_version"]
    if previous is None:
        return None
    try:
        parse_version(previous if isinstance(previous, str) else "")
    except ModuleVersionError as error:
        raise ReleaseError(
            f"current release report records an invalid previous_version for {module!r}"
        ) from error
    if parse_version(previous) >= parse_version(version):
        raise ReleaseError(
            f"current release report previous_version is not older than {module!r} {version}"
        )
    return f"{module}/{_TAG_ROOT}{previous}"


def _plain_archive(name: str) -> str:
    if _ARCHIVE_NAME.fullmatch(name) is None:
        raise ReleaseError(f"release asset name must be a plain zip file name: {name!r}")
    return name


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _record(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ReleaseError("release manifest module record must be an object")
    module, version, content = (
        value.get("module"), value.get("version"), value.get("content")
    )
    if not isinstance(content, str) or _IDENTITY.fullmatch(content) is None:
        raise ReleaseError(f"invalid module content identity for {module!r}")
    try:
        parse_version(version if isinstance(version, str) else "")
    except ModuleVersionError as error:
        raise ReleaseError(f"module version must be X.Y.Z for {module!r}") from error
    return {"content": content, "module": str(module), "version": version}


def _modules(
    document: dict[str, object], *, expected: frozenset[str] | None = frozenset(MODULES)
) -> list[dict[str, str]]:
    records = document.get("modules")
    if not isinstance(records, list):
        raise ReleaseError("release manifest must be a JSON object with modules and archives")
    parsed = [_record(value) for value in records]
    names = {record["module"] for record in parsed}
    if len(names) != len(parsed) or (expected is not None and names != set(expected)):
        raise ReleaseError("release manifest requires the expected module set")
    # Published history is immutable: a previous release may predate a module or outlive a
    # retired one, so only the release under test has to describe the current module set.
    return sorted(
        (record for record in parsed if record["module"] in MODULES),
        key=lambda record: record["module"],
    )


def _archives(document: dict[str, object]) -> list[dict[str, str]]:
    records = document.get("archives")
    if not isinstance(records, list):
        raise ReleaseError("release manifest must be a JSON object with modules and archives")
    parsed = []
    for value in records:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str) or (
            not isinstance(value.get("sha256"), str)
        ):
            raise ReleaseError("invalid release archive record")
        parsed.append({"name": _plain_archive(value["name"]), "sha256": value["sha256"]})
    return parsed


def _unique(archives: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    ordered: dict[str, dict[str, str]] = {}
    for archive in archives:
        if archive["name"] in ordered:
            raise ReleaseError(f"duplicate release archive: {archive['name']}")
        ordered[archive["name"]] = archive
    return [ordered[name] for name in sorted(ordered)]


def load_manifest(
    path: Path, *, expected: frozenset[str] | None = frozenset(MODULES)
) -> dict[str, object]:
    """Read and validate one published ``packages.json`` document."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseError(f"unreadable release manifest: {path}") from error
    if not isinstance(document, dict):
        raise ReleaseError("release manifest must be a JSON object with modules and archives")
    return {
        "archives": _unique(_archives(document)),
        "modules": _modules(document, expected=expected),
    }


def merge_manifests(
    documents: Sequence[dict[str, object]],
    *,
    expected: frozenset[str] | None = frozenset(MODULES),
) -> dict[str, object]:
    """Union per-architecture manifests that must agree about every module.

    ``expected`` is ``None`` for a module release whose packaging may already have selected the
    module, so the manifests only have to agree with each other; the default all-module release
    still requires every manifest to describe the repository's current module set.
    """

    if not documents:
        raise ReleaseError("a release requires at least one release manifest")
    first, *rest = documents
    modules = _modules(first, expected=expected)
    names = [record["module"] for record in modules]
    for document in rest:
        others = _modules(document, expected=expected)
        if [record["module"] for record in others] != names:
            raise ReleaseError("release manifests describe different module sets")
        for left, right in zip(modules, others, strict=True):
            if left != right:
                raise ReleaseError(f"release manifests disagree about {left['module']}")
    archives = _unique(
        archive for document in documents for archive in _archives(document)
    )
    return {"archives": archives, "modules": modules}


def select_module(document: dict[str, object], module: str) -> dict[str, object]:
    """Restrict a merged all-module manifest to the one module a release publishes."""

    modules = [record for record in document["modules"] if record["module"] == module]
    if not modules:
        raise ReleaseError(f"release manifest has no {module!r} record")
    prefix = f"{module}-"
    archives = [
        archive
        for archive in document["archives"]
        if archive["name"].startswith(prefix)
    ]
    return {"archives": archives, "modules": modules}


def _regular_file(path: Path) -> Path:
    """Return ``path`` only when it is a plain file, never a link or another kind of node."""

    try:
        information = os.lstat(path)
    except OSError as error:
        raise ReleaseError(f"release symbol bundle is missing: {path}") from error
    attributes = getattr(information, "st_file_attributes", 0)
    if stat.S_ISLNK(information.st_mode) or attributes & _REPARSE:
        raise ReleaseError(f"release symbol bundle must be a regular file: {path}")
    if not stat.S_ISREG(information.st_mode):
        raise ReleaseError(f"release symbol bundle must be a regular file: {path}")
    return path


def _symbol_member(module: str, member: zipfile.ZipInfo) -> str:
    """Return the architecture of one whitelisted ``<arch>/<module>.pdb`` entry."""

    name = member.filename
    parts = name.split("/")
    if len(parts) != 2 or parts[0] not in ARCHITECTURES or parts[1] != f"{module}.pdb":
        raise ReleaseError(f"release symbol bundle entry must be <arch>/{module}.pdb: {name!r}")
    if stat.S_ISLNK(member.external_attr >> 16):
        raise ReleaseError(f"release symbol bundle entry must be a plain file: {name!r}")
    return parts[0]


def _symbol_bundle_entries(archive: Path, module: str) -> dict[str, bytes]:
    """Read one intermediate symbol bundle as disjoint architecture payloads.

    Only the exact ``<arch>/<module>.pdb`` whitelist is accepted, the archive has to be a real
    ZIP with no duplicate or extra entries, and every payload is streamed under an explicit size
    bound so a crafted bundle cannot exhaust memory or smuggle another module's symbols.
    """

    entries: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                architecture = _symbol_member(module, member)
                if architecture in entries:
                    raise ReleaseError(
                        f"release symbol bundle repeats architecture {architecture}"
                    )
                payload = bytearray()
                with bundle.open(member) as stream:
                    while chunk := stream.read(_SYMBOL_CHUNK):
                        payload.extend(chunk)
                        if len(payload) > _MAXIMUM_SYMBOL_BYTES:
                            raise ReleaseError(
                                f"release symbol bundle entry is too large: {member.filename!r}"
                            )
                entries[architecture] = bytes(payload)
    except ReleaseError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise ReleaseError(f"release symbol bundle is unreadable: {archive}") from error
    if not entries:
        raise ReleaseError(f"release symbol bundle is empty: {archive}")
    return entries


def _strip_symbol_records(
    document: dict[str, object]
) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    """Split a manifest into its non-symbol archives and each module's own symbol record.

    A merged manifest may not repeat an archive name, and the same per-module symbol bundle is
    published beside every architecture manifest, so each manifest's own symbol records are
    removed before the manifests are merged. Only the record whose name is exactly
    ``<module>-<version>-pdb.zip`` for that manifest's module and version is recognised; anything
    else is left in place so a wrong module or version still fails the release.
    """

    wanted = {
        symbol_archive(record["module"], record["version"]): record["module"]
        for record in _modules(document, expected=None)
    }
    remaining: list[dict[str, str]] = []
    symbols: dict[str, dict[str, str]] = {}
    for archive in _archives(document):
        module = wanted.get(archive["name"])
        if module is None:
            remaining.append(archive)
        else:
            symbols[module] = archive
    return remaining, symbols


def _validate_symbol_record(manifest: Path, record: dict[str, str], module: str) -> dict[str, bytes]:
    """Validate one intermediate symbol bundle against its own manifest record."""

    archive = _regular_file(manifest.parent / record["name"])
    if _sha256(archive) != record["sha256"]:
        raise ReleaseError(f"release symbol digest mismatch: {record['name']}")
    return _symbol_bundle_entries(archive, module)


def _linked_node(node: Path) -> bool:
    """Return whether one node exists and is a symbolic link or a Windows reparse point.

    A missing node is not linked: assembling a fresh asset is the normal case, so only nodes that
    already exist have to be inspected.
    """

    try:
        information = os.lstat(node)
    except FileNotFoundError:
        return False
    attributes = getattr(information, "st_file_attributes", 0)
    return stat.S_ISLNK(information.st_mode) or bool(attributes & _REPARSE)


def _plain_symbol_destination(target: Path) -> None:
    """Refuse a linked destination or any linked ancestor before writing the symbol bundle.

    The ZIP writer follows a symlink or reparse point anywhere along the path, so the destination
    and every ancestor up to the filesystem root are inspected on their absolute, non-resolving
    form (a link component is never resolved away). A pre-existing link is refused instead of being
    followed and letting the writer overwrite whatever external file it resolves to.
    """

    destination = Path(os.path.abspath(target))
    for node in (destination, *destination.parents):
        if _linked_node(node):
            raise ReleaseError(f"release symbol bundle destination must not be a link: {node}")


def _assemble_symbols(
    assets: Path, module: str, bundle: str, entries: dict[str, bytes]
) -> Path:
    """Assemble the disjoint architecture payloads into the published symbol bundle."""

    target = assets / bundle
    if target.is_dir():
        raise ReleaseError(f"release symbol bundle is a directory: {target}")
    _plain_symbol_destination(target)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        files: dict[str, Path] = {}
        for architecture in ARCHITECTURES:
            source = root / architecture / f"{module}.pdb"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(entries[architecture])
            files[f"{architecture}/{module}.pdb"] = source
        _zip(target, files)
    return target


def _module_release(
    module: str, version: str, manifests: Sequence[Path], assets: Path
) -> dict[str, object]:
    """Normalise the intermediate symbol bundles, then select one module's release manifest."""

    if not assets.is_dir():
        raise ReleaseError(f"release assets directory must exist: {assets}")
    documents: list[dict[str, object]] = []
    symbols: list[tuple[Path, dict[str, str]]] = []
    for path in manifests:
        document = load_manifest(path, expected=None)
        remaining, records = _strip_symbol_records(document)
        documents.append({"archives": remaining, "modules": document["modules"]})
        symbols.append((path, records))
    current = select_module(merge_manifests(documents, expected=None), module)
    record = current["modules"][0]
    if record["version"] != version:
        raise ReleaseError(
            f"{module} tag version {version} does not match package version {record['version']}"
        )
    collected: dict[str, bytes] = {}
    seen: set[str] = set()
    for path, records in symbols:
        found = records.get(module)
        if found is None:
            continue
        for architecture, payload in _validate_symbol_record(path, found, module).items():
            if architecture in collected:
                raise ReleaseError(
                    f"{module} release ships architecture {architecture} more than once"
                )
            digest = hashlib.sha256(payload).hexdigest()
            if digest in seen:
                raise ReleaseError(
                    f"{module} release repeats symbol bytes for {architecture}"
                )
            seen.add(digest)
            collected[architecture] = payload
    if set(collected) != set(ARCHITECTURES):
        raise ReleaseError(
            f"{module} release requires the {', '.join(ARCHITECTURES)} symbol bundles"
        )
    bundle = symbol_archive(module, version)
    symbol_path = _assemble_symbols(assets, module, bundle, collected)
    current["archives"] = _unique(
        [*current["archives"], {"name": bundle, "sha256": _sha256(symbol_path)}]
    )
    return current


def verify_assets(directory: Path, archives: Sequence[dict[str, str]]) -> None:
    """Fail closed unless the asset directory is exactly the manifest's archive bytes."""

    if not directory.is_dir():
        raise ReleaseError(f"release assets directory must exist: {directory}")
    expected = {archive["name"]: archive["sha256"] for archive in archives}
    found = {path.name for path in directory.iterdir() if path.is_file()}
    if found != set(expected):
        missing = sorted(set(expected) - found)
        extra = sorted(found - set(expected))
        raise ReleaseError(
            f"release assets do not match the module archives; missing={missing} extra={extra}"
        )
    for name, digest in expected.items():
        if _sha256(directory / name) != digest:
            raise ReleaseError(f"release asset digest mismatch: {name}")


def _decide(current: dict[str, str], previous: dict[str, dict[str, str]] | None) -> dict[str, object]:
    module, version, content = current["module"], current["version"], current["content"]
    decision = {**current, "previous_version": None, "status": "bootstrap"}
    if previous is None:
        return decision
    earlier = previous.get(module)
    if earlier is None:
        return {**decision, "status": "new"}
    decision["previous_version"] = earlier["version"]
    order = (parse_version(version) > parse_version(earlier["version"])) - (
        parse_version(version) < parse_version(earlier["version"])
    )
    changed = content != earlier["content"]
    if order < 0:
        raise ReleaseError(
            f"{module} version {version} is older than {earlier['version']}"
        )
    if order == 0 and changed:
        raise ReleaseError(f"{module} content changed without a version bump")
    if order > 0 and not changed:
        raise ReleaseError(f"{module} version bumped without a content change")
    return {**decision, "status": "released" if changed else "unchanged"}


def evaluate(
    current: dict[str, object],
    previous: dict[str, object] | None,
    *,
    expected: frozenset[str] = frozenset(MODULES),
) -> dict[str, object]:
    """Return the recorded per-module release decisions or fail the release."""

    earlier = None if previous is None else {
        record["module"]: record for record in _modules(previous, expected=None)
    }
    modules = [_decide(record, earlier) for record in _modules(current, expected=expected)]
    return {
        "bootstrap": previous is None,
        "modules": modules,
        "note": _BOOTSTRAP_NOTE if previous is None else "",
        "title": ", ".join(
            f"{record['module']} {record['version']}" for record in modules
        ),
    }


def render_notes(report: dict[str, object], notes: str = "") -> str:
    """Render the release body, appending the module's human ChangeLog entry when given."""

    rows = "\n".join(
        f"| {record['module']} | {record['version']} | {record['status']} |"
        for record in report["modules"]
    )
    note = report["note"]
    body = (
        "Per-module release versions.\n\n"
        "| Module | Version | Gate |\n|---|---|---|\n"
        f"{rows}\n" + (f"\nThe module version gate reported: {note}.\n" if note else "")
    )
    if notes:
        body += f"\n{notes}\n"
    return body


def _write(path: Path, document: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def _notes(output: Path, report: dict[str, object], notes: str = "") -> None:
    with (output / "notes.md").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(render_notes(report, notes))


def _tag(args: argparse.Namespace) -> None:
    module, version = validate_tag(args.repository, args.tag)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"module={module}\nversion={version}\n")


def _previous(args: argparse.Namespace) -> None:
    if not args.current_exists and args.current_report is not None:
        raise ReleaseError("--current-report requires --current-exists")
    if args.current_exists:
        if args.current_report is None:
            raise ReleaseError(
                "a rerun of a published tag requires --current-report so its predecessor is "
                "never reconstructed from history"
            )
        try:
            report = json.loads(args.current_report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReleaseError(
                f"unreadable current release report: {args.current_report}"
            ) from error
        predecessor = recorded_previous_tag(args.tag, report)
    elif args.releases is not None:
        lines = args.releases.read_text(encoding="utf-8").splitlines()
        predecessor = resolve_previous(args.tag, stable_release_tags(lines))
    else:
        raise ReleaseError("a new publication requires --releases")
    with args.output.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(f"{predecessor or ''}\n")


def _gate(args: argparse.Namespace) -> None:
    if not args.output.is_dir():
        raise ReleaseError(f"release output directory must exist: {args.output}")
    previous = (
        None if args.previous is None else load_manifest(args.previous, expected=None)
    )
    human_notes = ""
    if args.module is None:
        current = merge_manifests([load_manifest(path) for path in args.manifests])
        report = evaluate(current, previous)
    else:
        if args.tag is None or args.repository is None or args.assets is None:
            raise ReleaseError("a module release requires --tag, --repository, and --assets")
        tag_module, version = validate_tag(args.repository, args.tag)
        if tag_module != args.module:
            raise ReleaseError(
                f"release tag {args.tag!r} selects {tag_module!r}, not {args.module!r}"
            )
        if previous is not None and all(
            record["module"] != args.module for record in previous["modules"]
        ):
            raise ReleaseError(
                f"previous release manifest does not describe {args.module!r}"
            )
        current = _module_release(args.module, version, args.manifests, args.assets)
        if {archive["name"] for archive in current["archives"]} != set(
            expected_archives(args.module, version)
        ):
            raise ReleaseError(
                f"{args.module} release archives must be exactly its module and symbol packages"
            )
        verify_assets(args.assets, current["archives"])
        # ``validate_tag`` already refused a ChangeLog that disagrees with the tag, so this read
        # only has to supply the human notes for the published body.
        human_notes = release_notes(args.repository, args.module, version)
        report = evaluate(current, previous, expected=frozenset({args.module}))
    _write(args.output / "packages.json", current)
    _write(args.output / "release.json", report)
    _notes(args.output, report, human_notes)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(required=True, dest="command")
    tag = commands.add_parser("tag")
    tag.add_argument("--repository", required=True, type=Path)
    tag.add_argument("--tag", required=True)
    tag.add_argument("--github-output", type=Path)
    previous = commands.add_parser("previous")
    previous.add_argument("--tag", required=True)
    previous.add_argument("--releases", type=Path)
    previous.add_argument("--output", required=True, type=Path)
    previous.add_argument(
        "--current-exists",
        action="store_true",
        help="the exact tag is already published, so the rerun reuses its recorded predecessor",
    )
    previous.add_argument(
        "--current-report",
        type=Path,
        help="the published release.json whose recorded previous_version a rerun compares against",
    )
    gate = commands.add_parser("gate")
    gate.add_argument("--output", required=True, type=Path)
    gate.add_argument("--previous", type=Path)
    gate.add_argument("--module", choices=MODULES)
    gate.add_argument("--tag")
    gate.add_argument("--repository", type=Path)
    gate.add_argument("--assets", type=Path)
    gate.add_argument("manifests", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if args.command == "tag":
        _tag(args)
    elif args.command == "previous":
        _previous(args)
    else:
        _gate(args)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

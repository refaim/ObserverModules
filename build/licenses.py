"""SPDX/vcpkg license adapter for the unpackrat Observer modules.

Two console operations live here and are driven by ``build/main.py``:

``license-inputs`` reads the *existing* Release vcpkg restore, never restoring,
installing, building or downloading, and exports per architecture the selected
target and host build-helper packages together with their raw emitted SBOM and
copyright bytes.

``license-prepare`` cross-checks the two per-architecture exports, validates them
against the reviewed component inventory and writes one canonical SPDX-2.3 root
document plus the ORT config/curation, provenance and verified source-artifact
set.

The module is data plumbing, not a license engine.  Emitted expressions are
preserved verbatim, every generated dependency package concludes
``NOASSERTION`` so ORT's source scanner is never masked by a curated
conclusion, and SPDX ``OR``/``AND`` algebra is evaluated by ORT.  Reviewed
``OR`` choices travel as explicit ORT ``license_choices`` data instead of being
folded into the conclusion.
"""

from __future__ import annotations

import hashlib
import fnmatch
import io
import json
import os
import re
import subprocess
import tarfile
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


SCHEMA_INPUTS = "unpackrat-license-inputs/1"
SCHEMA_PREPARED = "unpackrat-license-prepared/1"
SCHEMA_INVENTORY = "unpackrat-license-inventory/3"
SCHEMA_ATTESTATION = "unpackrat-license-attestation/1"
HOST_TRIPLET = "x64-windows"
ARCHITECTURES = ("x86", "x64")
NOASSERTION = "NOASSERTION"
PACKAGE_TYPE = "SpdxDocumentFile"
SUPPLIER = "Organization: vcpkg"
SOURCE_SERVER = "http://127.0.0.1:8765/sources/"
HASH_ALGORITHM = "SHA-512"
PACKAGE_CONFIGURATION_DIR = "package-configurations"
ATTESTATION_FILE = "release-attestation.json"
# The tracked files that select *what* a vcpkg restore installs: the manifest and the owned overlay
# recipes, patches and triplets.  Their content identity, not the archive URL or the declared
# license, is what binds the selected build options, so a recipe change that enables excluded code
# fails review while an ordinary registry version bump (a manifest edit) does not.
BUILD_INPUT_FILES = frozenset({"vcpkg.json", "vcpkg-configuration.json"})
BUILD_INPUT_TREES = ("build/vcpkg",)
_HASH_CHUNK = 1 << 20
_GIT_ARCHIVE = re.compile(r"^git\+(https?)://github\.com/([^/@]+)/([^/@]+)@(.+)$")
_SPDX_ID = re.compile(r"[^A-Za-z0-9.\-]")
_REGEX_META = re.compile(r"([.^$*+?()\[\]{}|\\])")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
# Only these exact hosts are the documented official GitHub source origins. A substring match on
# ``github`` would let ``github.attacker.example`` or ``github.com@evil.example`` impersonate the
# real host, so the netloc is compared exactly, including any port or userinfo.
_OFFICIAL_GITHUB_HOSTS = frozenset(
    {"github.com", "raw.githubusercontent.com", "codeload.github.com"}
)
_ARCHIVE_SUFFIXES = (
    ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".zip", ".tar",
)


class LicenseError(Exception):
    """A fail-closed violation in the license export or preparation pipeline."""


Fetch = Callable[[str], bytes]
Collector = Callable[[Path, Path], tuple[object, object]]


def _sha512_bytes(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _read_bytes(path: Path, what: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise LicenseError(f"missing {what}: {path}") from error


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _load_json(path: Path, what: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise LicenseError(f"missing {what}: {path}") from error
    except json.JSONDecodeError as error:
        raise LicenseError(f"{what} is not valid JSON: {path}: {error}") from error


@dataclass(frozen=True)
class ListedPackage:
    """One entry from ``vcpkg list --x-json``."""

    name: str
    triplet: str
    version: str
    port_version: int
    features: tuple[str, ...]


@dataclass(frozen=True)
class PackageInfo:
    """One entry from ``vcpkg x-package-info --x-installed --x-json``."""

    triplet: str
    abi: str
    dependencies: tuple[str, ...]


@dataclass(frozen=True)
class InstalledPackage:
    """A selected installed package: listing facts joined with database facts."""

    name: str
    triplet: str
    version: str
    port_version: int
    features: tuple[str, ...]
    abi: str
    depends: tuple[str, ...]

    @property
    def full_version(self) -> str:
        return self.version if self.port_version == 0 else f"{self.version}#{self.port_version}"


def _require_mapping(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LicenseError(f"{what} is not a JSON object")
    return value


def _require_string(mapping: Mapping[str, object], key: str, what: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise LicenseError(f"{what} has no {key!r} string")
    return value


def parse_installed_list(document: object) -> tuple[ListedPackage, ...]:
    """Read the ``vcpkg list --x-json`` map of installed specs to package facts."""

    listing = _require_mapping(document, "vcpkg list --x-json")
    packages: list[ListedPackage] = []
    for spec, raw in listing.items():
        entry = _require_mapping(raw, f"installed package entry {spec}")
        name = _require_string(entry, "package_name", str(spec))
        triplet = _require_string(entry, "triplet", name)
        version = _require_string(entry, "version", name)
        features = entry.get("features", [])
        if not isinstance(features, list) or not all(isinstance(item, str) for item in features):
            raise LicenseError(f"installed package {name} has a malformed features list")
        port_version = entry.get("port_version", 0)
        if not isinstance(port_version, int):
            raise LicenseError(f"installed package {name} has a malformed port_version")
        packages.append(ListedPackage(name, triplet, version, port_version, tuple(features)))
    return tuple(packages)


def parse_installed_info(document: object) -> dict[str, PackageInfo]:
    """Read the ``vcpkg x-package-info --x-installed --x-json`` results map."""

    root = _require_mapping(document, "vcpkg x-package-info --x-json")
    results = root.get("results")
    if not isinstance(results, Mapping):
        raise LicenseError("vcpkg x-package-info --x-json has no results object")
    infos: dict[str, PackageInfo] = {}
    for spec, raw in results.items():
        entry = _require_mapping(raw, f"installed info {spec}")
        triplet = _require_string(entry, "triplet", str(spec))
        abi = _require_string(entry, "abi", str(spec))
        dependencies = entry.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            raise LicenseError(f"installed info {spec} has a malformed dependencies list")
        infos[str(spec)] = PackageInfo(triplet, abi, tuple(dependencies))
    return infos


def parse_installed(document: object, info: object) -> tuple[InstalledPackage, ...]:
    """Join the listing and the database JSON, rejecting any omitted or extra node."""

    facts = parse_installed_list(document)
    infos = parse_installed_info(info)
    packages: list[InstalledPackage] = []
    for fact in facts:
        spec = f"{fact.name}:{fact.triplet}"
        record = infos.get(spec)
        if record is None:
            raise LicenseError(f"installed package {spec} has no x-package-info statement")
        if record.triplet != fact.triplet:
            raise LicenseError(
                f"x-package-info triplet {record.triplet} disagrees with {fact.triplet}"
            )
        packages.append(
            InstalledPackage(
                name=fact.name, triplet=fact.triplet, version=fact.version,
                port_version=fact.port_version, features=fact.features,
                abi=record.abi, depends=record.dependencies,
            )
        )
    listed = {f"{fact.name}:{fact.triplet}" for fact in facts}
    for spec in sorted(infos):
        if spec not in listed:
            raise LicenseError(f"x-package-info reported unlisted spec: {spec}")
    return tuple(packages)


def target_triplet(architecture: str) -> str:
    return f"unpackrat-{architecture}-windows-static"


def is_target_triplet(triplet: str) -> bool:
    return triplet.startswith("unpackrat-") and triplet.endswith("-windows-static")


def select_installed(
    packages: Sequence[InstalledPackage], architecture: str
) -> tuple[InstalledPackage, ...]:
    """Select the target and host packages, rejecting any foreign triplet tree."""

    target = target_triplet(architecture)
    selected: list[InstalledPackage] = []
    for package in packages:
        if package.triplet in (target, HOST_TRIPLET):
            selected.append(package)
        else:
            raise LicenseError(
                f"installed {package.name}:{package.triplet} is neither {target} nor {HOST_TRIPLET}"
            )
    if not selected:
        raise LicenseError(f"no installed packages found for {target} or {HOST_TRIPLET}")
    _require_resolved_dependencies(selected)
    return tuple(sorted(selected, key=lambda item: (item.triplet, item.name)))


def _require_resolved_dependencies(selected: Sequence[InstalledPackage]) -> None:
    installed = {(package.name, package.triplet) for package in selected}
    for package in selected:
        for dependency in package.depends:
            name, separator, triplet = dependency.partition(":")
            key = (name.strip(), triplet.strip() if separator else package.triplet)
            if key not in installed:
                raise LicenseError(f"unresolved dependency {dependency} required by {package.name}")


def vcpkg_root(repository: Path | str, architecture: str) -> Path:
    return Path(repository) / "out" / "native" / "vcpkg_installed" / architecture


def vcpkg_collector(vcpkg: Path | str) -> Collector:
    """Build a collector that reuses the vcpkg JSON CLI against an existing install root.

    The plain ``vcpkg`` ``status`` database can lag the current install because it is
    only a checkpoint; the authoritative installed set lives in the ``vcpkg list`` and
    ``vcpkg x-package-info`` JSON, so both read operations are the decision source and
    no local database parser is used.
    """

    def collect(root: Path, repository: Path) -> tuple[object, object]:
        install_root = f"--x-install-root={root}"
        listing = _vcpkg_json([vcpkg, "list", "--x-json", install_root], repository)
        specs = sorted(listing) if isinstance(listing, Mapping) else []
        info = _vcpkg_installed_info_json(
            [
                vcpkg, "x-package-info", "--x-installed", "--x-json", "--x-transitive",
                install_root, *specs,
            ],
            repository,
        )
        return listing, info

    return collect


def _vcpkg_command(arguments: Sequence[object]) -> str:
    return " ".join(os.fspath(argument) for argument in arguments)


def _parse_vcpkg_json(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise LicenseError(f"vcpkg did not return valid JSON: {error}") from error


def _vcpkg_json(arguments: Sequence[object], repository: Path) -> object:
    """Run a vcpkg JSON command that must exit 0, such as ``list``."""

    try:
        completed = subprocess.run(
            [os.fspath(argument) for argument in arguments],
            capture_output=True, text=True, check=True, cwd=os.fspath(repository),
        )
    except FileNotFoundError as error:
        raise LicenseError("vcpkg is unavailable for the license export") from error
    except subprocess.CalledProcessError as error:
        raise LicenseError(
            f"vcpkg {_vcpkg_command(arguments)} failed: {error.stderr.strip()}"
        ) from error
    return _parse_vcpkg_json(completed.stdout)


def _vcpkg_installed_info_json(arguments: Sequence[object], repository: Path) -> object:
    """Run the experimental ``x-package-info`` query, tolerating its exit-1 success.

    The pinned tool (``microsoft/vcpkg-tool`` @
    ``98d7cb0cf1f4686a3e43aa5672b6230c1d56bce8``, ``src/vcpkg/commands.package-info.cpp``)
    writes the complete ``results`` JSON to stdout and then returns from
    ``command_package_info_and_exit`` without an exit success, so the process exits 1 on a
    fully successful query with an empty stderr.  Accept exactly that shape: status 1, no
    stderr diagnostic, and a parseable body.  Every other status, any stderr text, and an
    unparseable body still fail closed; ``vcpkg list`` keeps its strict exit-0 contract.
    """

    try:
        completed = subprocess.run(
            [os.fspath(argument) for argument in arguments],
            capture_output=True, text=True, check=True, cwd=os.fspath(repository),
        )
    except FileNotFoundError as error:
        raise LicenseError("vcpkg is unavailable for the license export") from error
    except subprocess.CalledProcessError as error:
        if error.returncode != 1 or (error.stderr or "").strip():
            raise LicenseError(
                f"vcpkg {_vcpkg_command(arguments)} failed: {(error.stderr or '').strip()}"
            ) from error
        return _parse_vcpkg_json(error.stdout or "")
    return _parse_vcpkg_json(completed.stdout)


@dataclass(frozen=True)
class Resource:
    name: str
    url: str
    sha512: str


@dataclass(frozen=True)
class PortSbom:
    port: str
    version: str
    abi: str
    expression: str
    download_location: str
    purl: str
    resources: tuple[Resource, ...]
    files: tuple[Mapping[str, object], ...]


def parse_sbom(document: Mapping[str, object]) -> PortSbom:
    """Extract the port, binary and resource facts from an emitted vcpkg SBOM."""

    if document.get("spdxVersion") != "SPDX-2.3":
        raise LicenseError(f"unsupported SBOM SPDX version: {document.get('spdxVersion')!r}")
    packages = document.get("packages")
    if not isinstance(packages, list):
        raise LicenseError("SBOM has no packages array")
    by_id = {
        package.get("SPDXID"): package for package in packages if isinstance(package, dict)
    }
    port = by_id.get("SPDXRef-port")
    binary = by_id.get("SPDXRef-binary")
    if not isinstance(port, dict) or not isinstance(binary, dict):
        raise LicenseError("SBOM lacks the SPDXRef-port/SPDXRef-binary packages")
    resources: list[Resource] = []
    for package in packages:
        if not isinstance(package, dict) or not str(package.get("SPDXID", "")).startswith(
            "SPDXRef-resource-"
        ):
            continue
        checksums = package.get("checksums") or []
        sha512 = next(
            (
                item.get("checksumValue")
                for item in checksums
                if isinstance(item, dict) and item.get("algorithm") == "SHA512"
            ),
            None,
        )
        if not sha512:
            raise LicenseError(f"SBOM resource {package.get('name')} has no SHA512 checksum")
        resources.append(
            Resource(
                name=str(package.get("name", "")),
                url=str(package.get("downloadLocation", "")),
                sha512=str(sha512),
            )
        )
    external = port.get("externalRefs") or []
    purl = next(
        (
            item.get("referenceLocator")
            for item in external
            if isinstance(item, dict) and item.get("referenceType") == "purl"
        ),
        "",
    )
    files = tuple(item for item in document.get("files", []) if isinstance(item, dict))
    return PortSbom(
        port=str(port.get("name", "")),
        version=str(port.get("versionInfo", "")),
        abi=str(binary.get("versionInfo", "")),
        expression=str(port.get("licenseConcluded", "")),
        download_location=str(port.get("downloadLocation", "")),
        purl=str(purl or ""),
        resources=tuple(resources),
        files=files,
    )


def github_archive_url(url: str) -> str | None:
    """Convert a ``git+https://github.com/owner/repo@ref`` resource URI to an archive URL.

    Returns ``None`` for a non-GitHub or non-git resource, for example a
    ``raw.githubusercontent.com`` single header, which is used unchanged.
    """

    match = _GIT_ARCHIVE.match(url)
    if match is None:
        return None
    scheme, owner, repo, ref = match.groups()
    if repo.endswith(".git"):
        repo = repo[:-4]
    return f"{scheme}://github.com/{owner}/{repo}/archive/{ref}.tar.gz"


def source_location(resource: Resource) -> str:
    """Resolve a resource to the URL whose bytes the recorded SHA512 covers."""

    archive = github_archive_url(resource.url)
    return archive if archive is not None else resource.url


def git_provenance(repository: Path | str) -> dict[str, object]:
    """Bind an export to the exact checked-out commit and any hosted-run identity."""

    return {
        "sourceCommit": _git(repository, "rev-parse", "HEAD"),
        "sourceTree": _git(repository, "rev-parse", "HEAD^{tree}"),
        "runId": os.environ.get("GITHUB_RUN_ID"),
        "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
    }


def _git(repository: Path | str, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(repository), *arguments],
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError as error:
        raise LicenseError("git is unavailable, so the export cannot be bound to a commit") from error
    except subprocess.CalledProcessError as error:
        raise LicenseError(
            f"git {' '.join(arguments)} failed in {repository}: {error.stderr.strip()}"
        ) from error
    return result.stdout.strip()


def _require_empty(output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise LicenseError(f"refusing to reuse a non-empty output directory: {output}")


def _build_input_members(repository: Path | str) -> dict[str, bytes]:
    """Read every tracked file that selects what the vcpkg restore installs."""

    repository = Path(repository)
    members: dict[str, bytes] = {}
    for path in _git_tracked(repository):
        if path in BUILD_INPUT_FILES or any(
            path == tree or path.startswith(tree + "/") for tree in BUILD_INPUT_TREES
        ):
            members[path] = _read_bytes(_contained_path(repository, path), f"build input {path}")
    return members


def build_input_identity(repository: Path | str) -> dict[str, str]:
    """Deterministic content identity of each tracked vcpkg build-option input."""

    return {
        path: _sha256_bytes(data) for path, data in _build_input_members(repository).items()
    }


def build_input_fingerprint(repository: Path | str) -> str:
    """One digest over the reviewed build-option inputs of the current checkout."""

    identity = build_input_identity(repository)
    if not identity:
        raise LicenseError(
            "no tracked vcpkg build inputs are available, so the restore cannot be attested"
        )
    digest = hashlib.sha256()
    for path in sorted(identity):
        digest.update(path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(identity[path].encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()


def attestation_path(repository: Path | str, architecture: str) -> Path:
    return vcpkg_root(repository, architecture) / ATTESTATION_FILE


def invalidate_attestation(repository: Path | str, architecture: str) -> None:
    """Drop any prior Release attestation before the shared restore can be mutated.

    A Debug or analysis restore writes the *same* installed tree a Release build attests, and a
    failed Release restore leaves that tree half-mutated, so no prior success stamp may survive
    *any* operation on the shared root.  The isolated ASan restore owns a different tree, so the
    caller only reaches this for the shared root and never touches an unrelated attestation.
    """

    if architecture not in ARCHITECTURES:
        raise LicenseError(f"unknown architecture: {architecture}")
    attestation_path(repository, architecture).unlink(missing_ok=True)


def write_attestation(
    repository: Path | str, architecture: str, *, provenance: Mapping[str, object] | None = None
) -> dict[str, object]:
    """Record a successful Release restore/build of this checkout next to the restore.

    The driver calls this only after a Release build has finished, so its presence proves this
    restore was produced by real Release work for the current commit and the current build inputs.
    """

    if architecture not in ARCHITECTURES:
        raise LicenseError(f"unknown architecture: {architecture}")
    document: dict[str, object] = {
        "schema": SCHEMA_ATTESTATION,
        "architecture": architecture,
        "triplet": target_triplet(architecture),
        "buildInputFingerprint": build_input_fingerprint(repository),
        "provenance": dict(provenance if provenance is not None else git_provenance(repository)),
    }
    _write_json(attestation_path(repository, architecture), document)
    return document


def _validate_attestation(
    document: object, repository: Path | str, architecture: str, *,
    provenance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Re-derive every recorded fact from the checkout and compare rather than trusting the source.

    A stale restore produced for an earlier commit, a different CI run, or the same commit with
    changed build inputs must never be presented as current.  Both the loaded file and a supplied
    attestation document go through this one complete check, so neither path can skip a fact.
    """

    repository = Path(repository)
    if not isinstance(document, Mapping):
        raise LicenseError("the Release build attestation is not an object")
    if document.get("schema") != SCHEMA_ATTESTATION:
        raise LicenseError(f"not a {SCHEMA_ATTESTATION} attestation")
    if str(document.get("architecture")) != architecture:
        raise LicenseError(
            f"the Release attestation records architecture {document.get('architecture')!r}, "
            f"not {architecture!r}"
        )
    expected_triplet = target_triplet(architecture)
    if str(document.get("triplet")) != expected_triplet:
        raise LicenseError(
            f"the Release attestation records triplet {document.get('triplet')!r}, "
            f"not {expected_triplet!r}"
        )
    if str(document.get("buildInputFingerprint")) != build_input_fingerprint(repository):
        raise LicenseError(
            "the vcpkg build inputs changed after the Release restore; re-run the Release build "
            "before exporting the license inputs"
        )
    expected = dict(provenance if provenance is not None else git_provenance(repository))
    recorded = _require_mapping(document.get("provenance"), "attestation provenance")
    for key in ("sourceCommit", "sourceTree", "runId", "runAttempt"):
        if recorded.get(key) != expected.get(key):
            raise LicenseError(
                f"the Release restore is bound to {key}={recorded.get(key)!r} but this checkout and "
                f"run are {expected.get(key)!r}; the restore is stale and must be rebuilt"
            )
    return dict(document)


def load_attestation(
    repository: Path | str, architecture: str, *, provenance: Mapping[str, object] | None = None
) -> dict[str, object]:
    """Prove the existing restore came from a successful Release build of this checkout."""

    repository = Path(repository)
    path = attestation_path(repository, architecture)
    document = _load_json(path, "Release build attestation")
    return _validate_attestation(document, repository, architecture, provenance=provenance)


def export_inputs(
    repository: Path | str, architecture: str, output: Path | str, *,
    collect: Collector, attestation: Mapping[str, object] | None = None,
) -> Path:
    """Export the selected installed packages of the attested Release restore for one architecture.

    The existing restore is only exported when this checkout carries a successful Release build
    attestation for it; a missing or stale attestation fails closed instead of stamping the current
    HEAD onto a restore that was produced for a different commit, run or set of build inputs.  A
    supplied attestation is validated through the same complete check as a loaded one, so it is not
    a bypass.  The attested build-input identity is carried into the export so ``prepare`` can bind
    it back to the actual current checkout before it fetches anything.
    """

    if architecture not in ARCHITECTURES:
        raise LicenseError(f"unknown architecture: {architecture}")
    repository = Path(repository)
    output = Path(output)
    root = vcpkg_root(repository, architecture)
    listing, info = collect(root, repository)
    selected = select_installed(parse_installed(listing, info), architecture)
    record = (
        _validate_attestation(attestation, repository, architecture)
        if attestation is not None
        else load_attestation(repository, architecture)
    )
    resolved = dict(_require_mapping(record.get("provenance"), "attestation provenance"))
    _require_empty(output)
    output.mkdir(parents=True, exist_ok=True)
    packages = [_export_package(root, package, output) for package in selected]
    _write_json(
        output / "installed.json",
        {
            "schema": SCHEMA_INPUTS,
            "architecture": architecture,
            "targetTriplet": target_triplet(architecture),
            "hostTriplet": HOST_TRIPLET,
            "provenance": dict(resolved),
            "buildInputs": build_input_identity(repository),
            "packages": packages,
        },
    )
    return output


def _export_package(root: Path, package: InstalledPackage, output: Path) -> dict[str, object]:
    share = root / package.triplet / "share" / package.name
    sbom_path = share / "vcpkg.spdx.json"
    sbom_bytes = _read_bytes(sbom_path, f"SBOM for {package.name}")
    sbom = parse_sbom(_load_json(sbom_path, f"SBOM for {package.name}"))
    if sbom.port != package.name:
        raise LicenseError(f"SBOM port {sbom.port} does not match installed package {package.name}")
    installed_full = package.full_version
    if sbom.version != installed_full:
        raise LicenseError(
            f"version mismatch for {package.name}: installed {installed_full}, SBOM {sbom.version}"
        )
    if sbom.abi != package.abi:
        raise LicenseError(f"ABI mismatch for {package.name}: installed {package.abi}, SBOM {sbom.abi}")
    copyright_bytes = _read_bytes(share / "copyright", f"copyright notice for {package.name}")
    sbom_relative = f"sbom/{package.name}.spdx.json"
    copyright_relative = f"copyright/{package.name}.txt"
    _write_bytes(output / sbom_relative, sbom_bytes)
    _write_bytes(output / copyright_relative, copyright_bytes)
    share_files = _export_share_files(share, package, sbom, output)
    return {
        "name": package.name,
        "version": package.version,
        "portVersion": package.port_version,
        "fullVersion": installed_full,
        "triplet": package.triplet,
        "kind": "target" if is_target_triplet(package.triplet) else "host",
        "features": list(package.features),
        "abi": package.abi,
        "purl": sbom.purl,
        "downloadLocation": sbom.download_location,
        "declaredExpression": sbom.expression,
        "sbom": {"path": sbom_relative, "sha256": _hash_file(sbom_path, "sha256")},
        "copyright": {
            "path": copyright_relative,
            "sha256": _hash_file(share / "copyright", "sha256"),
        },
        "resources": [
            {
                "name": resource.name,
                "url": resource.url,
                "archiveUrl": github_archive_url(resource.url),
                "sha512": resource.sha512,
            }
            for resource in sbom.resources
        ],
        "shareFiles": share_files,
        "depends": list(package.depends),
    }


def _sbom_share_members(sbom: PortSbom, name: str) -> dict[str, str]:
    """The exact share-file members and SHA256 digests the raw SBOM selects for a port.

    A target package with source resources carries no host-helper share tree, so it selects
    nothing; otherwise the authoritative set is exactly the ``./share/<name>/`` file entries of the
    emitted SBOM, keyed by their path relative to that share directory.  This one derivation is the
    authority both the export and the export verification reuse.
    """

    if sbom.resources:
        return {}
    prefix = f"./share/{name}/"
    recorded: dict[str, str] = {}
    for entry in sbom.files:
        file_name = str(entry.get("fileName", ""))
        if not file_name.startswith(prefix):
            continue
        digest = next(
            (
                item.get("checksumValue")
                for item in entry.get("checksums", [])
                if isinstance(item, dict) and item.get("algorithm") == "SHA256"
            ),
            None,
        )
        if not digest:
            raise LicenseError(f"host helper {name} file {file_name} has no SHA256")
        recorded[file_name[len(prefix):]] = str(digest)
    return recorded


def _export_share_files(
    share: Path, package: InstalledPackage, sbom: PortSbom, output: Path
) -> list[dict[str, str]]:
    """Copy and hash the installed share sources of a host build helper.

    Both the read inside the installed share tree and the write inside the export are anchored with
    the containing-path check, so a raw SBOM ``fileName`` that tries to leave either root is
    rejected before any byte is read or written.
    """

    recorded = _sbom_share_members(sbom, package.name)
    exported: list[dict[str, str]] = []
    for relative in sorted(recorded):
        data = _read_bytes(
            _contained_path(share, relative), f"host helper share file {package.name}/{relative}"
        )
        if _sha256_bytes(data) != recorded[relative]:
            raise LicenseError(f"share file checksum mismatch for {package.name}/{relative}")
        target = _contained_path(output / "share" / package.name, relative)
        _write_bytes(target, data)
        exported.append({"path": relative, "sha256": recorded[relative]})
    return exported


def load_export(directory: Path | str) -> dict[str, object]:
    directory = Path(directory)
    document = _load_json(directory / "installed.json", "license-inputs export")
    if not isinstance(document, dict) or document.get("schema") != SCHEMA_INPUTS:
        raise LicenseError(f"not a {SCHEMA_INPUTS} export: {directory}")
    packages = document.get("packages")
    if not isinstance(packages, list):
        raise LicenseError(f"{SCHEMA_INPUTS} export has no packages array: {directory}")
    for package in packages:
        _verify_export_package(directory, _require_mapping(package, "exported package"))
    document["@directory"] = directory
    return document


def _verified_record(
    directory: Path, package: Mapping[str, object], key: str, name: str
) -> bytes:
    """Read a recorded raw artifact and prove its bytes still match the recorded SHA256."""

    record = _require_mapping(package.get(key), f"{key} record of {name}")
    relative = _require_string(record, "path", f"{key} record of {name}")
    digest = _require_string(record, "sha256", f"{key} record of {name}")
    data = _read_bytes(_contained_path(directory, relative), f"exported {key} of {name}")
    if _sha256_bytes(data) != digest:
        raise LicenseError(f"exported {key} of {name} does not match its recorded SHA256")
    return data


def _verify_export_package(directory: Path, package: Mapping[str, object]) -> None:
    """Reparse the authoritative raw SBOM and cross-check every exported fact."""

    name = _require_string(package, "name", "exported package")
    raw = _verified_record(directory, package, "sbom", name)
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LicenseError(f"exported SBOM of {name} is not valid JSON: {error}") from error
    sbom = parse_sbom(_require_mapping(document, f"raw SBOM of {name}"))
    if sbom.port != name:
        raise LicenseError(f"exported SBOM port {sbom.port} disagrees with {name}")
    for field, actual in (
        ("abi", sbom.abi),
        ("declaredExpression", sbom.expression),
        ("downloadLocation", sbom.download_location),
        ("purl", sbom.purl),
    ):
        expected = str(package.get(field, ""))
        if actual != expected:
            raise LicenseError(
                f"exported {field} of {name} ({expected!r}) disagrees with its raw SBOM ({actual!r})"
            )
    _verify_full_version(package, sbom, name)
    _verify_export_resources(package, sbom, name)
    _verified_record(directory, package, "copyright", name)
    _verify_export_shares(directory, package, sbom, name)


def _verify_export_shares(
    directory: Path, package: Mapping[str, object], sbom: PortSbom, name: str
) -> None:
    """The exported share members must equal the raw SBOM's own selection, bytes and all.

    The manifest's retained ``shareFiles`` list is only a summary; the authoritative membership and
    digests come from the same raw SBOM the export preserved, so dropping a member from every
    manifest cannot narrow the audited set, and rewriting a member's bytes together with its
    summary digest still disagrees with the SBOM and fails.
    """

    entries = package.get("shareFiles")
    if not isinstance(entries, list):
        raise LicenseError(f"exported package {name} has no shareFiles array")
    recorded: dict[str, str] = {}
    for entry in entries:
        share = _require_mapping(entry, f"share file entry of {name}")
        relative = _require_string(share, "path", f"share file of {name}")
        recorded[relative] = _require_string(share, "sha256", f"share file of {name}")
    expected = _sbom_share_members(sbom, name)
    if recorded != expected:
        raise LicenseError(f"exported share files of {name} disagree with its raw SBOM")
    for relative, digest in expected.items():
        data = _read_bytes(
            _contained_path(directory / "share" / name, relative), f"share file of {name}"
        )
        if _sha256_bytes(data) != digest:
            raise LicenseError(f"exported share file of {name} does not match its recorded SHA256")


def _verify_full_version(package: Mapping[str, object], sbom: PortSbom, name: str) -> None:
    """The recorded identity must agree with the raw SBOM that backs it.

    The pinned vcpkg-tool renders the installed port revision into the SBOM port
    ``versionInfo`` as the full ``base#port`` identity (``1.3.2#1`` for zlib 1.3.2 port 1)
    and records the bare ``base`` for a port that declares no revision.  A record has to
    reproduce the SBOM's identity exactly: the recorded base is the bare version, ``#`` is
    the vcpkg port-revision delimiter and never part of a base, and the recorded
    ``base#port`` must equal the raw SBOM byte for byte, so a hand-edited record can
    neither drop, invent nor relabel a revision.
    """

    port_version = package.get("portVersion")
    if not isinstance(port_version, int) or port_version < 0:
        raise LicenseError(f"exported package {name} has a malformed portVersion")
    base = _require_string(package, "version", f"exported package {name}")
    if "#" in base:
        raise LicenseError(
            f"exported base version of {name} records {base!r}, which carries a port "
            f"revision delimiter instead of a bare base"
        )
    full = base if port_version == 0 else f"{base}#{port_version}"
    if str(package.get("fullVersion")) != full:
        raise LicenseError(
            f"exported fullVersion of {name} disagrees with {base}+port{port_version}"
        )
    if sbom.version != full:
        raise LicenseError(
            f"the raw SBOM of {name} records version {sbom.version!r}, which is not the "
            f"recorded full identity {full!r}"
        )


def _verify_export_resources(
    package: Mapping[str, object], sbom: PortSbom, name: str
) -> None:
    resources = package.get("resources")
    if not isinstance(resources, list):
        raise LicenseError(f"exported package {name} has no resources array")
    recorded: list[tuple[str, str, str]] = []
    for entry in resources:
        resource = _require_mapping(entry, f"resource of {name}")
        url = _require_string(resource, "url", f"resource of {name}")
        recorded.append(
            (
                _require_string(resource, "name", f"resource of {name}"),
                url,
                _require_string(resource, "sha512", f"resource of {name}"),
            )
        )
        if resource.get("archiveUrl") != github_archive_url(url):
            raise LicenseError(f"exported archive URL of {name} disagrees with its raw resource URL")
    actual = [(item.name, item.url, item.sha512) for item in sbom.resources]
    if recorded != actual:
        raise LicenseError(f"exported resources of {name} disagree with its raw SBOM")


def load_inventory(path: Path | str) -> dict[str, object]:
    document = _load_json(Path(path), "reviewed license inventory")
    if not isinstance(document, dict) or document.get("schema") != SCHEMA_INVENTORY:
        raise LicenseError(f"not a {SCHEMA_INVENTORY} inventory: {path}")
    if not isinstance(document.get("components"), list):
        raise LicenseError("license inventory has no components array")
    return document


def prepare(
    inputs: Path | str, output: Path | str, *, inventory_path: Path | str,
    repository: Path | str, fetch: Fetch | None = None,
    provenance: Mapping[str, object] | None = None,
) -> Path:
    """Verify both exports and write the canonical ORT input set."""

    inputs = Path(inputs)
    output = Path(output)
    repository = Path(repository)
    inventory = load_inventory(inventory_path)
    exports = {architecture: load_export(inputs / architecture) for architecture in ARCHITECTURES}
    expected = dict(provenance if provenance is not None else git_provenance(repository))
    _require_same_run(exports, expected)
    _require_build_inputs(exports, repository)
    components = _check_components(exports, inventory, repository)
    _require_empty(output)
    output.mkdir(parents=True, exist_ok=True)
    resolved_fetch = fetch if fetch is not None else default_fetch
    sources = _write_sources(components, output, resolved_fetch)
    _resolve_source_versions(components, sources)
    _write_raw(exports, output)
    _write_notices(inventory, repository, output)
    provenance = _write_provenance(exports, components, sources, output)
    _write_spdx(inventory, components, sources, provenance, output)
    _write_ort_config(components, repository, output)
    _write_curations(components, sources, output)
    _copy_package_configurations(repository, output)
    return output


def _resolve_source_versions(
    components: Sequence[dict[str, object]], sources: Mapping[str, Mapping[str, object]]
) -> None:
    """Bind an adopted repository source to the content address of its bytes."""

    for component in components:
        if str(component["kind"]) == "adopted-source":
            component["version"] = str(sources[str(component["name"])]["sha256"])


def _copy_package_configurations(repository: Path, output: Path) -> None:
    """Copy reviewed package configurations verbatim; never invent a native field."""

    directory = repository / ".ort" / PACKAGE_CONFIGURATION_DIR
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.yml")):
        data = _read_bytes(path, f"package configuration {path.name}")
        _write_bytes(output / PACKAGE_CONFIGURATION_DIR / path.name, data)


def _require_same_run(
    exports: Mapping[str, Mapping[str, object]], expected: Mapping[str, object]
) -> None:
    """Bind both exports to the *actual* checkout and CI run, not merely to each other."""

    for architecture in ARCHITECTURES:
        actual = dict(_require_mapping(exports[architecture]["provenance"], f"{architecture} provenance"))
        if actual != dict(expected):
            raise LicenseError(
                f"the {architecture} export is bound to a different checkout or run than the "
                f"current one: {actual} != {dict(expected)}"
            )


def _require_build_inputs(
    exports: Mapping[str, Mapping[str, object]], repository: Path
) -> None:
    """Bind both exports to the current checkout's reviewed build-option content *before* any fetch.

    The recorded per-architecture provenance only names the commit and CI run; it does not detect an
    uncommitted change to the manifest, recipe or triplet that selects what the restore installs.
    Re-deriving the build-input identity from the live checkout and requiring every export to match
    closes that gap, so a stale export cannot fold a changed build scope into the audited set even
    when the approved library versions themselves are unchanged.  Both exports must match the same
    live identity, which is also the architecture parity this enforces.
    """

    expected = build_input_identity(repository)
    for architecture in ARCHITECTURES:
        record = _require_mapping(
            exports[architecture].get("buildInputs"), f"{architecture} build inputs"
        )
        actual = {str(path): str(digest) for path, digest in record.items()}
        if actual != expected:
            raise LicenseError(
                f"the {architecture} export was produced from different vcpkg build inputs than "
                f"this checkout; re-run the Release build and export the license inputs again"
            )


def _check_components(
    exports: Mapping[str, Mapping[str, object]], inventory: Mapping[str, object],
    repository: Path,
) -> list[dict[str, object]]:
    inventory_by_name: dict[str, Mapping[str, object]] = {}
    adopted: list[Mapping[str, object]] = []
    own_sources: list[Mapping[str, object]] = []
    for component in inventory["components"]:
        if not isinstance(component, dict):
            continue
        if component.get("pendingNotice"):
            raise LicenseError(
                f"{component.get('name')} still declares a pending notice; the notice must be present"
            )
        if component.get("kind") == "vcpkg-package":
            inventory_by_name[str(component["vcpkgName"])] = component
        elif component.get("kind") == "adopted-source":
            adopted.append(component)
        elif component.get("kind") == "own-source":
            own_sources.append(component)
        else:
            raise LicenseError(
                f"unknown inventory component kind {component.get('kind')!r}"
            )
    by_name: dict[str, dict[str, list[dict[str, object]]]] = {}
    for architecture in ARCHITECTURES:
        for package in exports[architecture]["packages"]:
            by_name.setdefault(str(package["name"]), {}).setdefault(
                str(package["kind"]), []
            ).append(package)
    left = {(str(p["name"]), str(p["kind"])) for p in exports[ARCHITECTURES[0]]["packages"]}
    right = {(str(p["name"]), str(p["kind"])) for p in exports[ARCHITECTURES[1]]["packages"]}
    if left != right:
        raise LicenseError("package selection differs between x86 and x64")
    components: list[dict[str, object]] = []
    for name in sorted(by_name):
        kinds = by_name[name]
        entries = kinds.get("target", []) + kinds.get("host", [])
        _require_parity(name, entries)
        reference = entries[0]
        inventory_component = inventory_by_name.get(name)
        if inventory_component is None:
            raise LicenseError(f"installed package {name} is absent from the reviewed inventory")
        _require_features(name, reference, inventory_component)
        _require_origin(name, reference, inventory_component)
        _require_build_configuration(name, inventory_component, repository)
        scope = str(inventory_component["scope"]) if kinds.get("target") else "build"
        components.append({
            "name": name,
            "version": str(reference["fullVersion"]),
            "expression": _resolve_declared(name, reference, inventory_component),
            "licenseChoice": _license_choice(inventory_component),
            "scope": scope,
            "origin": str(inventory_component["source"]["origin"]),
            "resources": list(reference["resources"]),
            "shareFiles": list(reference["shareFiles"]),
            "shareData": _share_data(
                exports[ARCHITECTURES[0]]["@directory"], name, reference["shareFiles"]
            ),
            "depends": sorted({str(item) for entry in entries for item in entry["depends"]}),
            "kind": "vcpkg-package",
            "staticallyLinked": kinds.get("target") is not None and scope != "build",
            "resolution": None,
        })
    _require_inventory_installed(inventory, by_name)
    for component in adopted:
        components.extend(_adopted_components(component, repository))
    claimed = _claimed_members(adopted)
    for component in own_sources:
        components.append(_own_source_component(component, repository, claimed))
    return components


def _require_origin(
    name: str, reference: Mapping[str, object], component: Mapping[str, object]
) -> None:
    """Reject a source swap that keeps the license but moves the upstream project."""

    resources = reference["resources"]
    if not resources:
        return
    identities = {_source_identity(str(resource["url"])) for resource in resources}
    if len(identities) != 1:
        raise LicenseError(f"{name} spans several source origins: {sorted(identities)}")
    approved = str(component["source"].get("identity"))
    actual = identities.pop()
    if approved != actual:
        raise LicenseError(
            f"{name} source origin is {actual} but the inventory approves {approved}"
        )


def _adopted_components(
    component: Mapping[str, object], repository: Path
) -> list[dict[str, object]]:
    """Turn an incorporated repository source into a content-addressed component."""

    name = str(component["name"])
    source = component.get("source")
    source = source if isinstance(source, Mapping) else {}
    if str(source.get("origin")) != "repository":
        return []
    if not component.get("incorporated"):
        return []
    members = component.get("members")
    if not isinstance(members, list) or not members:
        raise LicenseError(f"incorporated adopted source {name} declares no members")
    files: dict[str, bytes] = {}
    for member in members:
        path = _contained_path(repository, str(member))
        files[str(member)] = _read_bytes(path, f"{name} source member {member}")
    return [_repository_component(name, component, files)]


def _repository_component(
    name: str, component: Mapping[str, object], files: Mapping[str, bytes]
) -> dict[str, object]:
    """Shape one repository-owned source into a content-addressed, scanned component."""

    scope = str(component["scope"])
    return {
        "name": name,
        "version": "",
        "expression": str(component["declaredExpression"]),
        "licenseChoice": _license_choice(component),
        "scope": scope,
        "origin": "repository",
        "resources": [],
        "shareFiles": [],
        "shareData": dict(files),
        "depends": [],
        "kind": "adopted-source",
        "staticallyLinked": scope != "build",
        "resolution": _observer_resolution(name, component, files),
    }


def _claimed_members(adopted: Sequence[Mapping[str, object]]) -> set[str]:
    """Every path an incorporated repository source already owns outright."""

    claimed: set[str] = set()
    for component in adopted:
        source = component.get("source")
        if (
            isinstance(source, Mapping)
            and str(source.get("origin")) == "repository"
            and component.get("incorporated")
        ):
            claimed.update(str(member) for member in component.get("members", []))
    return claimed


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    """Whether [path] matches any documented exclusion glob."""

    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _git_tracked(repository: Path | str) -> tuple[str, ...]:
    """The exhaustive, deterministic set of code under version control."""

    output = _git(repository, "ls-files", "-z")
    return tuple(sorted(path for path in output.split("\x00") if path))


def _own_source_component(
    component: Mapping[str, object], repository: Path, claimed: set[str]
) -> dict[str, object]:
    """Gather every tracked file not already owned or documented into one scanned component.

    Membership comes from Git metadata, so untracked working files can never slip into the
    audited source, and any new tracked file that is neither claimed by an adopted source nor
    covered by a documented exclusion fails closed until the inventory is reviewed.
    """

    name = str(component["name"])
    roots = tuple(str(root) for root in component.get("roots", []))
    include = {str(item) for item in component.get("include", [])}
    exclude = tuple(str(pattern) for pattern in component.get("exclude", []))
    if not roots and not include:
        raise LicenseError(f"own-source component {name} declares no roots or includes")
    tracked = _git_tracked(repository)
    files: dict[str, bytes] = {}
    accounted: set[str] = set()
    for path in tracked:
        if path in claimed or _matches_any(path, exclude):
            accounted.add(path)
        elif path in include or any(
            path == root or path.startswith(root + "/") for root in roots
        ):
            files[path] = _read_bytes(
                _contained_path(repository, path), f"own-source member {path}"
            )
            accounted.add(path)
    missing = sorted(set(tracked) - accounted)
    if missing:
        raise LicenseError(
            f"tracked files outside every scanned component of {name} need review: {missing}"
        )
    return _repository_component(name, component, files)


def _observer_resolution(
    name: str, component: Mapping[str, object], files: Mapping[str, bytes]
) -> dict[str, str] | None:
    """Emit the reviewed exact LGPL resolution only for the approved source snapshot."""

    policy = component.get("policyResolution")
    if not isinstance(policy, Mapping):
        return None
    approved = policy.get("approvedMembers")
    if not isinstance(approved, Mapping):
        raise LicenseError(f"{name} policy resolution has no approvedMembers map")
    actual = {member: _sha256_bytes(data) for member, data in files.items()}
    if dict(approved) != actual:
        raise LicenseError(
            f"{name} source changed since review; the exact LGPL resolution must be re-approved"
        )
    return {
        "reason": str(policy["reason"]),
        "license": str(policy["license"]),
        "comment": str(policy["comment"]),
    }


def _share_data(
    directory: object, name: str, share_files: Sequence[Mapping[str, object]]
) -> dict[str, bytes]:
    if not share_files:
        return {}
    root = Path(str(directory))
    return {
        str(entry["path"]): _read_bytes(
            _contained_path(root / "share" / name, str(entry["path"])),
            f"share file {name}/{entry['path']}",
        )
        for entry in share_files
    }


def _require_parity(name: str, entries: Sequence[Mapping[str, object]]) -> None:
    versions = {str(entry["version"]) for entry in entries}
    full_versions = {str(entry["fullVersion"]) for entry in entries}
    expressions = {str(entry["declaredExpression"]) for entry in entries}
    features = {tuple(sorted(str(item) for item in entry["features"])) for entry in entries}
    hashes = {
        tuple(sorted(str(resource["sha512"]) for resource in entry["resources"]))
        for entry in entries
    }
    shares = {
        tuple(sorted((str(item["path"]), str(item["sha256"])) for item in entry["shareFiles"]))
        for entry in entries
    }
    if (
        len(versions) != 1
        or len(full_versions) != 1
        or len(expressions) != 1
        or len(hashes) != 1
        or len(features) != 1
        or len(shares) != 1
    ):
        raise LicenseError(f"package {name} disagrees between architectures")


def _require_features(
    name: str, reference: Mapping[str, object], component: Mapping[str, object]
) -> None:
    """Reject any installed feature selection the reviewed inventory has not approved."""

    approved = sorted(str(item) for item in component.get("features", []))
    emitted = sorted(str(item) for item in reference["features"])
    if emitted != approved:
        raise LicenseError(
            f"{name} installed features {emitted} need review against approved {approved}"
        )


def _require_build_configuration(
    name: str, component: Mapping[str, object], repository: Path
) -> None:
    """Bind a curated component to the exact reviewed recipe/triplet/patch content.

    A source URL and hash bind the upstream archive, and the declared expression binds the license,
    but neither binds the *selected build options*.  For a curated component the inventory records
    the reviewed content identity of every build-input file that selects what is compiled, so a
    change that keeps the same archive, license and features while enabling previously excluded code
    changes a digest and must fail for renewed review.
    """

    guard = component.get("buildConfiguration")
    if guard is None:
        return
    if not isinstance(guard, Mapping) or not guard:
        raise LicenseError(f"{name} declares an empty build-configuration guard")
    approved = {str(path): str(digest) for path, digest in guard.items()}
    identity = build_input_identity(repository)
    actual = {path: identity[path] for path in approved if path in identity}
    if actual != approved:
        missing = sorted(set(approved) - set(actual))
        detail = (
            f"unavailable build inputs {missing}"
            if missing
            else "the recipe/triplet/patch content changed"
        )
        raise LicenseError(
            f"{name} selected build options no longer match the reviewed configuration "
            f"({detail}); the configuration must be re-reviewed"
        )


def _resolve_declared(
    name: str, reference: Mapping[str, object], component: Mapping[str, object]
) -> str:
    """Return the declared expression to publish, applying only a scoped correction."""

    if str(component["source"]["origin"]) == "reference":
        raise LicenseError(f"{name} is an unincorporated reference but is installed")
    emitted = str(reference["declaredExpression"])
    if emitted == NOASSERTION:
        correction = component.get("correction")
        if not isinstance(correction, dict):
            raise LicenseError(f"{name} emits NOASSERTION without a version+hash scoped correction")
        _require_correction(name, reference, correction)
        return str(correction["license"])
    approved = str(component["declaredExpression"])
    if emitted != approved:
        raise LicenseError(
            f"{name} license changed to {emitted} but the inventory approves {approved}"
        )
    return emitted


def _require_correction(
    name: str, reference: Mapping[str, object], correction: Mapping[str, object]
) -> None:
    if str(correction.get("version")) != str(reference["fullVersion"]):
        raise LicenseError(
            f"{name} NOASSERTION correction version does not match {reference['fullVersion']}"
        )
    hashes = {str(resource["sha512"]) for resource in reference["resources"]}
    if str(correction.get("sha512")) not in hashes:
        raise LicenseError(f"{name} NOASSERTION correction hash matches no emitted resource")


def _license_choice(component: Mapping[str, object]) -> dict[str, str] | None:
    choice = component.get("licenseChoice")
    if not isinstance(choice, dict):
        return None
    return {
        "given": str(choice["given"]),
        "choice": str(choice["choice"]),
        "comment": str(choice.get("comment", "")),
    }


def _require_inventory_installed(
    inventory: Mapping[str, object], by_name: Mapping[str, object]
) -> None:
    for component in inventory["components"]:
        if not isinstance(component, dict) or component.get("kind") != "vcpkg-package":
            continue
        vcpkg_name = str(component.get("vcpkgName"))
        if vcpkg_name not in by_name:
            raise LicenseError(f"reviewed inventory package {vcpkg_name} is not installed")


def deterministic_tar(members: Mapping[str, bytes]) -> bytes:
    """Build a byte-identical uncompressed tar for the same logical members.

    The archive stays uncompressed on purpose.  The same logical members produced
    different gzip output across runtimes even though the uncompressed tar payload was
    byte-identical; the exact cause of that cross-runtime gzip difference was not
    identified, so a gzip layer cannot back a portable content address.
    """

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for member_name in sorted(members):
            data = members[member_name]
            info = tarfile.TarInfo(member_name)
            info.size = len(data)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _archive_extension(url: str) -> str | None:
    """Return the verifiable single-archive extension of [url], or None for raw bytes."""

    path = urllib.parse.urlsplit(url).path.lower()
    for suffix in _ARCHIVE_SUFFIXES:
        if path.endswith(suffix):
            return suffix
    return None


def _source_identity(url: str) -> str:
    """Reduce a resource URI to a stable ``host/owner/repo`` upstream identity."""

    cleaned = url[4:] if url.startswith("git+") else url
    split = urllib.parse.urlsplit(cleaned)
    host = split.netloc.lower()
    segments = [segment for segment in split.path.split("/") if segment]
    if host in _OFFICIAL_GITHUB_HOSTS:
        if len(segments) < 2:
            raise LicenseError(f"official source URL is not an owner/repository path: {url}")
        owner = segments[0]
        repo = segments[1].split("@")[0]
        if repo.endswith(".git"):
            repo = repo[:-4]
        return f"github.com/{owner}/{repo}".lower()
    if not host:
        raise LicenseError(f"source URL has no host: {url}")
    return f"{host}/{'/'.join(segments[:2])}".lower()


def _contained_path(root: Path, relative: str) -> Path:
    """Resolve [relative] under [root], rejecting any Windows/POSIX escape before touching it."""

    text = str(relative)
    posix = PurePosixPath(text)
    if not text or "\\" in text or "\x00" in text:
        raise LicenseError(f"path {text!r} is not a portable relative path under {root}")
    if posix.is_absolute() or text.startswith("//") or _WINDOWS_DRIVE.match(text):
        raise LicenseError(f"path {text!r} escapes {root}")
    if ".." in posix.parts:
        raise LicenseError(f"path {text!r} escapes {root}")
    root_resolved = root.resolve()
    target = (root / posix).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise LicenseError(f"path {text!r} escapes {root}")
    return target


def _source_artifact_name(sha256: str, extension: str) -> str:
    """Name a source artifact by the content address of its actual bytes."""

    return f"{sha256}{extension}"


def _write_sources(
    components: Sequence[Mapping[str, object]], output: Path, fetch: Fetch
) -> dict[str, dict[str, object]]:
    sources: dict[str, dict[str, object]] = {}
    directory = output / "sources"
    directory.mkdir(parents=True, exist_ok=True)
    for component in components:
        data, extension = _source_bytes(component, fetch)
        sha256 = _sha256_bytes(data)
        name = _source_artifact_name(sha256, extension)
        (directory / name).write_bytes(data)
        sources[str(component["name"])] = {
            "artifact": name,
            "sha512": _sha512_bytes(data),
            "sha256": sha256,
        }
    return sources


def _source_bytes(component: Mapping[str, object], fetch: Fetch) -> tuple[bytes, str]:
    if str(component.get("kind")) == "adopted-source":
        members = {str(path): data for path, data in component["shareData"].items()}
        return deterministic_tar(members), ".tar"
    share_data = component["shareData"]
    if share_data:
        members = {
            f"{component['name']}/{path}": _verify_share(component, path, data)
            for path, data in share_data.items()
        }
        return deterministic_tar(members), ".tar"
    resources = component["resources"]
    if not resources:
        raise LicenseError(f"component {component['name']} has no verifiable source artifact")
    return _fetch_source(component, resources, fetch)


def _verify_share(component: Mapping[str, object], path: str, data: bytes) -> bytes:
    recorded = {str(entry["path"]): str(entry["sha256"]) for entry in component["shareFiles"]}
    if _sha256_bytes(data) != recorded.get(path):
        raise LicenseError(f"share file checksum mismatch for {component['name']}/{path}")
    return data


def _fetch_source(
    component: Mapping[str, object], resources: Sequence[Mapping[str, object]], fetch: Fetch
) -> tuple[bytes, str]:
    if len(resources) == 1:
        url = source_location(_resource(resources[0]))
        extension = _archive_extension(url)
        if extension is not None:
            data = fetch(url)
            if _sha512_bytes(data) != resources[0]["sha512"]:
                raise LicenseError(f"source archive checksum mismatch for {component['name']}")
            return data, extension
    members: dict[str, bytes] = {}
    for resource in resources:
        data = fetch(source_location(_resource(resource)))
        if _sha512_bytes(data) != resource["sha512"]:
            raise LicenseError(
                f"source checksum mismatch for {component['name']}/{resource['name']}"
            )
        members[Path(str(resource["name"])).name] = data
    return deterministic_tar(members), ".tar"


def _resource(entry: Mapping[str, object]) -> Resource:
    return Resource(str(entry["name"]), str(entry["url"]), str(entry["sha512"]))


def default_fetch(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:  # noqa: S310 - fixed https sources
        return response.read()


def _write_raw(exports: Mapping[str, Mapping[str, object]], output: Path) -> None:
    for architecture in ARCHITECTURES:
        source = Path(str(exports[architecture]["@directory"]))
        for package in exports[architecture]["packages"]:
            for key in ("sbom", "copyright"):
                relative = str(package[key]["path"])
                data = _read_bytes(
                    _contained_path(source, relative), f"{key} for {package['name']}"
                )
                _write_bytes(output / "raw" / architecture / relative, data)
            for entry in package["shareFiles"]:
                relative = f"share/{package['name']}/{entry['path']}"
                data = _read_bytes(
                    _contained_path(source, relative), f"share file for {package['name']}"
                )
                _write_bytes(output / "raw" / architecture / relative, data)


def _write_notices(inventory: Mapping[str, object], repository: Path, output: Path) -> None:
    wanted: set[str] = set()
    for component in inventory["components"]:
        if isinstance(component, dict):
            wanted.update(str(name) for name in component.get("notices", []))
    wanted.update(str(name) for name in inventory["rootPackage"].get("notices", []))
    for notice in sorted(wanted):
        data = _read_bytes(repository / notice, f"license notice {notice}")
        _write_bytes(output / "notices" / Path(notice).name, data)


def _write_provenance(
    exports: Mapping[str, Mapping[str, object]],
    components: Sequence[Mapping[str, object]],
    sources: Mapping[str, Mapping[str, object]],
    output: Path,
) -> dict[str, object]:
    provenance = {
        "schema": SCHEMA_PREPARED,
        "provenance": dict(exports[ARCHITECTURES[0]]["provenance"]),
        "architectures": list(ARCHITECTURES),
        "components": [
            {
                "name": component["name"],
                "version": component["version"],
                "declaredExpression": component["expression"],
                "scope": component["scope"],
                "origin": component["origin"],
                "resources": [
                    {
                        "url": resource["url"],
                        "archiveUrl": source_location(_resource(resource)),
                        "sha512": resource["sha512"],
                    }
                    for resource in component["resources"]
                ],
                "source": sources.get(str(component["name"])),
            }
            for component in components
        ],
    }
    _write_json(output / "provenance.json", provenance)
    return provenance


def _spdx_id(name: str) -> str:
    return "SPDXRef-Package-" + _SPDX_ID.sub("-", name)


def _package_id(component: Mapping[str, object]) -> str:
    return f"{PACKAGE_TYPE}:vcpkg:{component['name']}:{component['version']}"


def _write_spdx(
    inventory: Mapping[str, object],
    components: Sequence[Mapping[str, object]],
    sources: Mapping[str, Mapping[str, object]],
    provenance: Mapping[str, object],
    output: Path,
) -> None:
    root = inventory["rootPackage"]
    root_id = _spdx_id(str(root["name"]))
    packages: list[dict[str, object]] = [
        {
            "name": str(root["name"]),
            "SPDXID": root_id,
            "versionInfo": "",
            "packageFileName": "",
            "downloadLocation": NOASSERTION,
            "licenseDeclared": str(root["license"]),
            "licenseConcluded": NOASSERTION,
            "copyrightText": NOASSERTION,
        }
    ]
    relationships: list[dict[str, str]] = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": root_id,
        }
    ]
    ids = {str(component["name"]): _spdx_id(str(component["name"])) for component in components}
    linked = {str(component["name"]): bool(component["staticallyLinked"]) for component in components}
    for component in components:
        name = str(component["name"])
        package_id = ids[name]
        artifact = str(sources[name]["artifact"])
        packages.append({
            "name": name,
            "SPDXID": package_id,
            "versionInfo": str(component["version"]),
            "packageFileName": artifact,
            "downloadLocation": f"{SOURCE_SERVER}{artifact}",
            "supplier": SUPPLIER,
            "licenseDeclared": str(component["expression"]),
            "licenseConcluded": NOASSERTION,
            "copyrightText": NOASSERTION,
            "comment": f"scope={component['scope']}; origin={component['origin']}",
        })
        scope = str(component["scope"])
        if scope == "build":
            relationships.append({
                "spdxElementId": package_id,
                "relationshipType": "BUILD_DEPENDENCY_OF",
                "relatedSpdxElement": root_id,
            })
        elif scope == "test":
            relationships.append({
                "spdxElementId": package_id,
                "relationshipType": "TEST_DEPENDENCY_OF",
                "relatedSpdxElement": root_id,
            })
        if bool(component["staticallyLinked"]):
            relationships.append({
                "spdxElementId": root_id,
                "relationshipType": "STATIC_LINK",
                "relatedSpdxElement": package_id,
            })
        for dependency in component["depends"]:
            dependency_name = str(dependency).partition(":")[0]
            if dependency_name not in ids:
                raise LicenseError(
                    f"dependency {dependency} of {name} is not a reviewed component"
                )
            relationships.append({
                "spdxElementId": package_id,
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": ids[dependency_name],
            })
            if linked[dependency_name]:
                relationships.append({
                    "spdxElementId": package_id,
                    "relationshipType": "STATIC_LINK",
                    "relatedSpdxElement": ids[dependency_name],
                })
    relationships.extend(
        {
            "spdxElementId": root_id,
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": package_id,
        }
        for package_id in ids.values()
    )
    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "documentNamespace": _namespace(provenance),
        "name": str(root["name"]),
        "creationInfo": {
            "creators": ["Tool: unpackrat-license-prepare"],
            "created": "1970-01-01T00:00:00Z",
        },
        "packages": packages,
        "relationships": relationships,
    }
    _write_json(output / "unpackrat.spdx.json", document)


def _namespace(provenance: Mapping[str, object]) -> str:
    commit = str(provenance["provenance"].get("sourceCommit", "unknown"))[:12]
    return f"https://spdx.org/spdxdocs/unpackrat-{commit}"


def _write_ort_config(
    components: Sequence[Mapping[str, object]], repository: Path, output: Path
) -> None:
    base_path = repository / ".ort.yml"
    base = _load_json(base_path, "base .ort.yml") if base_path.exists() else {}
    if not isinstance(base, dict):
        raise LicenseError(f"base {base_path.name} is not a JSON/YAML object")
    section = base.get("license_choices")
    section = section if isinstance(section, dict) else {}
    previous = section.get("package_license_choices")
    section["package_license_choices"] = (
        previous if isinstance(previous, list) else []
    ) + _package_license_choices(components)
    base["license_choices"] = section
    resolutions = base.get("resolutions")
    resolutions = resolutions if isinstance(resolutions, dict) else {}
    previous_violations = resolutions.get("rule_violations")
    resolutions["rule_violations"] = (
        previous_violations if isinstance(previous_violations, list) else []
    ) + _rule_violations(components)
    base["resolutions"] = resolutions
    _write_json(output / ".ort.yml", base)


def _package_license_choices(
    components: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    choices: list[dict[str, object]] = []
    for component in components:
        choice = component["licenseChoice"]
        if choice is None:
            continue
        choices.append({
            "package_id": _package_id(component),
            "license_choices": [{"given": choice["given"], "choice": choice["choice"]}],
        })
    return choices


def _literal(text: str) -> str:
    """Escape regex metacharacters so a resolution message matches literally."""

    return _REGEX_META.sub(r"\\\1", text)


def _rule_violations(
    components: Sequence[Mapping[str, object]]
) -> list[dict[str, str]]:
    """Build the reviewed exact-message violations bound to the actual artifact ID."""

    violations: list[dict[str, str]] = []
    for component in components:
        resolution = component.get("resolution")
        if not resolution:
            continue
        message = _literal(
            f"The dependency '{_package_id(component)}' is statically linked and licensed under "
            f"the ScanCode 'copyleft-limited' categorized license {resolution['license']}."
        )
        violations.append({
            "message": message,
            "reason": str(resolution["reason"]),
            "comment": str(resolution["comment"]),
        })
    return violations


def _write_curations(
    components: Sequence[Mapping[str, object]],
    sources: Mapping[str, Mapping[str, object]],
    output: Path,
) -> None:
    curations = [
        {
            "id": _package_id(component),
            "curations": {
                "source_artifact": {
                    "url": f"{SOURCE_SERVER}{sources[str(component['name'])]['artifact']}",
                    "hash": {
                        "value": sources[str(component["name"])]["sha512"],
                        "algorithm": HASH_ALGORITHM,
                    },
                },
                "source_code_origins": ["ARTIFACT"],
            },
        }
        for component in components
    ]
    _write_json(output / "package-curations.yml", curations)

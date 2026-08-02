# IX-derived local build DAG

This document records the owner-approved target for replacing the large PowerShell
orchestration layer. The decision was finalized on 2026-08-01 after studying
[`pg83/ix`](https://github.com/pg83/ix) at revision
`66726a904152246fbef8b27e26e878840f6d7fb7`. Implementation follows strict TDD.

## Scope

MSBuild remains the native Windows compile/link backend. The new layer only renders
recipes, signs their complete inputs, constructs a fine-grained DAG, executes ready
nodes concurrently, and caches successful outputs. Replacing MSBuild with direct
`cl.exe`/`link.exe` orchestration is out of scope.

The existing `build.ps1` command contract remains authoritative until the new system
has result parity for the complete local verification matrix. The coarse and fine DAG
experiments under `build/` are frozen fallback/oracle code, not the foundation of the
new implementation.

Prefer a mature, focused open-source library or the Python standard library over
first-party infrastructure whenever it provides the required contract. Dependencies
are pinned exactly with `uv`; custom code is reserved for ObserverModules-specific
policy or guarantees unavailable from an existing component.

## Kept from IX

- Jinja recipe inheritance;
- `in_dir`/`out_dir` node semantics;
- content-addressed immutable outputs;
- MD5 identities and touch-marker cache hits;
- demand-driven `asyncio` graph traversal;
- exactly one named resource pool per node;
- dependency UID propagation into dependent identities.

## Windows adaptations

- Jinja renders PowerShell recipes instead of POSIX shell recipes. MSBuild, vcpkg,
  clang-tidy, fuzzers, UMDH, audit tools, and packaging remain ordinary recipe tools.
- `StrictUndefined` makes a missing template value fail before signing or execution.
- PowerShell values use a dedicated single-quote filter; recipes do not interpolate
  unquoted paths or user-controlled values.
- Windows Job Objects replace POSIX process groups so cancellation terminates the
  complete `pwsh -> MSBuild -> cl/link` process tree.
- A per-UID interprocess lock prevents concurrent local processes from publishing the
  same CAS entry.
- Every mutable path is confined below the repository output root. Escapes and existing
  reparse-point components or leaves are rejected before mutation.
- The Linux isolation model is not claimed on Windows. Path confinement and Job Objects
  provide narrower, explicit guarantees rather than pretending to reproduce `unshare`.

The local concurrency model assumes cooperating build processes using the UID lock. It
does not claim protection from another same-user process maliciously replacing path
components during a filesystem operation; that stronger boundary would require
handle-relative Win32 filesystem operations or an OS sandbox. The driver therefore
validates confinement and existing reparse points before mutation and uses exclusive
creation, without duplicating speculative post-operation TOCTOU checks.

MD5 is retained deliberately for compatibility with the IX content-identity model. It
identifies local deterministic build inputs; it is not presented as a cryptographic
integrity boundary for untrusted remote artifacts.

## Repository layout

```text
ObserverModules/
|-- build.ps1
|-- tools/
|   `-- build/
|       |-- pyproject.toml
|       |-- uv.lock
|       |-- main.py
|       |-- core/
|       |   |-- execute.py, graph.py, recipe.py, render.py, sign.py
|       |   |-- paths.py, runtime.py, store.py
|       |   |-- sarif.py, toolchain.py
|       |   `-- windows_job.py, windows_process.py
|       |-- graphs/
|       |   `-- analysis.py
|       |-- templates/
|       |   |-- base.json, script.json, argv.json
|       |   |-- pwsh.ps1, msbuild.ps1, analysis.ps1
|       |   `-- msvc-analyze.ps1, clang-tidy.ps1, vcpkg.ps1
|       `-- tests/
|-- out/
|   |-- cas/
|   |   `-- <md5>-<node>/
|   |       |-- out/
|   |       |-- log.txt
|   |       `-- touch
|   `-- work/
|       |-- <run-id>/
|       `-- .locks/
|-- src/
`-- docs/
```

Templates remain flat while the set is small. A maintained leaf recipe should normally
be 2-15 lines because setup, error handling, logging, and common argv live in inherited
base templates. Generated scripts may be longer; they are disposable execution data.
Subdirectories are introduced only when real template families require them.

`out/` contains only two top-level entries and is ignored by Git:

- `out/cas` contains UID-addressed outputs, their successful-task log, and a zero-byte
  `touch` marker;
- `out/work` contains in-flight compiler intermediates, failed-task evidence,
  quarantined incomplete entries, and internal lock files. A successful node removes
  its exact scratch directory immediately; successful run directories therefore do
  not accumulate beside the CAS.

There is no speculative top-level `results` or `trash` directory. Commands print exact
result paths. An incomplete CAS entry has no marker, is never a cache hit, and is moved
under the locked work area or safely removed before rebuilding.

## Rendering and identity

A logical node selects a recipe and passes explicit validated values such as project,
source, architecture, configuration, tool paths, `in_dir`, and `out_dir`. Jinja expands
the complete inheritance chain before any process starts.

As in IX, the rendered descriptor contains a literal `exec` argv array and signed
`data`. PowerShell uses a constant `-Command` wrapper that constructs a script block
from the exact UTF-8 recipe bytes received on stdin. Parser and terminating errors
therefore produce a nonzero process result; stdin remains recipe transport, not
interactive input. This avoids a temporary script pathname in the identity and
therefore avoids a UID/path cycle. Concrete CAS/work paths are derived after signing
and supplied through validated process environment values.

The canonical MD5 input includes:

- rendered recipe bytes;
- literal argv and relevant environment/configuration values;
- logical input paths and exact input bytes;
- dependency UIDs;
- toolchain and target-platform identity;
- the recipe/executor schema identity.

Mapping order cannot affect the digest. A change to any signed field must change the
UID. A cache hit requires both the expected CAS entry and its touch marker.
The readable node-name suffix in `<md5>-<node>` is diagnostic only, as in IX's
`<uid>-<package_name>` layout; the MD5 is the content identity.

## Graph and failure model

Nodes are split at the smallest safe independently executable unit. The production
verify graph includes separate project/configuration builds, test shards, MSVC analysis
and clang-tidy translation units, SARIF normalization and fan-in, fuzz targets, leak
scenario/mode work, audit tool/module work, and package/smoke units.

Every node names one pool. Initial pool classes are `full`, `slot`, `fuzz`, `umdh`,
`binskim`, and `misc`; measured resource behavior determines capacities. Pools constrain
actual contention instead of imposing phase-wide ordering edges.

Infrastructure failure or cancellation produces no touch marker. Diagnostic tools may
write their raw report and an explicit status result even when findings exist; a later
semantic gate consumes those reports and fails the build after evidence is preserved.

## WSL2 extension

The Python graph/signing core is platform-neutral. A future WSL2 workflow adds a common
`sh.sh` base and short `.sh` counterparts for portable actions such as clang-tidy,
fuzzing, sanitizers, and Mull. PowerShell is not translated automatically into shell.

The Linux driver runs once inside WSL2 and executes the DAG natively with POSIX process
groups. Windows must not launch one `wsl.exe` process per node. Platform, shell recipe,
and toolchain identity are signed, so Windows and Linux outputs cannot alias. The WSL2
workflow is a local quality backend for the portable parser core; shipping DLLs remain
Windows/MSVC artifacts.

## TDD migration order

Steps 1-3 are proven and the analysis portion of step 4 is complete for every supported
project/TU/architecture occurrence. The same per-leaf shape remains
`/analyze || clang-tidy -> normalize || normalize -> merge -> semantic gate`; the complete
533-node three-architecture graph is a 4.38-second warm cache hit. Expansion remains
data-driven and does not switch the root entry point.

1. Prove Jinja inheritance, `StrictUndefined`, PowerShell quoting, and canonical MD5.
2. Prove graph validation, demand execution, pool behavior, touch cache hits, confined
   paths, interprocess locks, and Job Object cancellation with tool-free unit tests.
3. Implement one real x64 vertical slice for `src/modules/renpy/pickle.cpp`:
   MSVC `/analyze` and clang-tidy in parallel, deterministic SARIF normalization, and a
   final semantic gate. A second identical run must be all cache hits; changing an input
   must invalidate only its consumers.
4. Expand to every first-party translation unit, then split fuzzing, leaks, audits,
   packaging, and the remaining verification stages. The TU expansion is complete; the
   other graph families are in progress.
5. Establish full result parity and benchmark cold/warm local runs against the current
   `build.ps1 verify` baseline.
6. Switch the root entry point only after parity, then remove superseded graph and
   PowerShell code. The checkpoint commit keeps that cleanup recoverable.

No tool is installed or updated automatically. `uv` owns the pinned Python/Jinja
environment once the already-approved local prerequisite is available.

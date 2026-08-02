# Experimental build graph driver

> **Historical baseline:** the owner approved the replacement design in
> [`docs/ix-build-adaptation.md`](../docs/ix-build-adaptation.md) on 2026-08-01.
> This coarse driver and the later fine-graph spikes remain only as benchmark/oracle
> code until the new implementation proves parity. Do not extend them into production.

`graph_driver.py` is a stdlib-only outer DAG experiment. It schedules existing
`build.ps1` commands; it does not replace their MSBuild, vcpkg, analysis, test, or
packaging implementation. Nothing calls the driver from the default build entrypoint.

MSBuild is the retained native compile/link backend. A direct Python-to-compiler driver
is out of scope; a Ninja-backed prototype would require new build-only evidence showing
a material native critical-path problem before it is considered.

## Reproducible invocation

The experiment was verified with uv-managed CPython 3.14.6. This invocation pins the
exact interpreter, disables Python downloads, disables network access, ignores uv
project configuration, and does not install project dependencies:

```powershell
uv run --no-python-downloads --offline --no-config `
  --cache-dir .artifacts\uv-graph-cache --python 3.14.6 `
  python build\graph_driver.py plan --graph observer-verify-shadow
```

If CPython 3.14.6 is not already available to uv, the command fails instead of
installing or updating it. The driver itself has no third-party Python dependencies.
The synthetic profile's `{python}` token expands to the absolute current
`sys.executable`, so child commands neither search `PATH` nor recursively invoke uv.

Add `--json` for a stable machine-readable plan. Variables are explicit:

```powershell
uv run --no-python-downloads --offline --no-config `
  --cache-dir .artifacts\uv-graph-cache --python 3.14.6 `
  python build\graph_driver.py plan --graph observer-verify-shadow `
  --set arch=x64 --set fuzz_seconds=60 --json
```

`run` is deliberately separate from `plan`; running `observer-verify-shadow` invokes
the real, expensive leaf gates. Per-node output is written below
`.artifacts/graph/<graph>/logs`, and cache state below the adjacent `state` directory.
Use `--jobs 3` for this profile; higher widths have not been justified on the current
host because each native leaf may already parallelize internally through MSBuild.

## Execution contract

- The plan is a deterministic, name-stable topological order. Cycles and unknown
  dependencies are rejected before any command starts.
- Commands are JSON argv arrays and execute with `shell=False`, a fixed workspace,
  closed stdin, and combined stdout/stderr in a node-specific log.
- A failed node blocks every transitive dependent. Independent ready nodes may finish.
- Each node names one resource pool. Pool capacities and `--jobs` are both enforced
  within a driver process.
- A fingerprint includes argv, pool, cache policy, output declarations, explicit
  fingerprint tokens, SHA-256 records for matched inputs, and dependency fingerprints.
- A cacheable node is skipped only when its successful state fingerprint matches and
  every explicit output still exists. Cacheable nodes without outputs are invalid.
- `cacheable: false` gates always run, even when their fingerprints are unchanged.

The Observer shadow profile keeps every native gate non-cacheable. It first completes
`doctor`, then one serial `restore -RestoreFlavor all`, and then `source-checks`. The
restore prepares both normal and ASan x64 install roots before any parallel leaf starts;
every later leaf passes `-SkipDependencyRestore`, so concurrent vcpkg processes cannot
race on the manifest lock. The public CLI still restores by default, and a direct
`restore -SkipDependencyRestore` invocation is rejected as a false-success hazard.

After source checks, the graph fans out into independently safe branches:

- Debug tests precede compiler analysis because both reuse the Debug object tree.
- Release tests, coverage, and UBSan have configuration-separated object trees and may
  overlap the Debug/analysis branch.
- The coarse pilot serializes ASan before fuzzing. The completed output-scope audit found
  that this edge is not required: ASan and Fuzz use configuration-separated object and
  binary trees, and the restored dependency roots are read-only during both gates.

The five branch tails join before UMDH, so leak instrumentation never overlaps another
heavy gate, and packaging runs last so its destructive staging cleanup cannot race
another gate. The `normal-x64` pool capacity is three, the ASan pool remains one, and
the recommended global `--jobs 3` caps total external concurrency at three.

The current full profile defaults to x64 because UBSan and UMDH leak execution are
x64-specific; an all-architecture profile should split build-only ARM64 work from
host-runnable gates instead of merely overriding `arch=all`.

The profile covers `doctor`, source checks, Debug and Release tests, compiler analysis,
coverage, ASan, UBSan, UMDH leaks, all-format fuzzing, and packaging. Packaging performs
the Release binary audit itself, so a separate `audit-binaries` node would duplicate work.

## Synthetic cache benchmark

The checked-in `synthetic-cache-benchmark` profile has one prepare node, two parallel
cacheable branches, and a non-cacheable terminal gate. On the development host on
2026-08-01:

| Run | Result | Driver time |
| --- | --- | ---: |
| Sequential cold (`--jobs 1`) | all four nodes executed | 0.512 s |
| DAG cold (`--jobs 3`) | compile branches overlapped | 0.383 s |
| DAG warm (`--jobs 3`) | three cache hits; gate still executed | 0.070 s |

The scheduler supplied a 1.34x cold improvement, and the warm run was 7.31x faster than
the sequential cold baseline. These synthetic results demonstrate concurrency and
cache behavior only; they are not native build performance claims.

## Real x64 verify benchmark

The full local x64 gate was measured on the same warm development worktree. Both
successful runs executed every host-capable gate with no deferrals:

| Run | Result | Wall time |
| --- | --- | ---: |
| Sequential `build.ps1 verify` | green | 1121.262 s |
| Coarse DAG (`--jobs 3`) | green | 834.592 s |

The coarse graph saved 286.670 seconds (25.6%, 1.34x). Its single
`compiler-analysis` leaf still took 673.687 seconds, while all-format fuzzing took
253.546 seconds and the leak gate took 92.583 seconds. Those timings establish the next
step: replace monolithic gate leaves with project, translation-unit, fuzz-target,
leak-scenario, audit-tool, and package-unit fan-out/fan-in. The measured coarse profile
is a baseline, not the target architecture.

## Size and replacement projection

The implementation was reduced from 756 to 533 production lines after review. Its
remaining groups are approximately: 116 lines of models/path validation, 195 lines of
graph validation/topological planning/content fingerprints, 129 lines of safe process
execution/scheduling/cache/log handling, and 93 lines of JSON expansion plus CLI.
Cycle diagnostics, failure propagation, path confinement, and `shell=False` execution
were retained rather than compressed away.

This pilot does **not** currently reduce repository LOC: it adds 533 driver lines plus
the profile while replacing none of the Windows leaf implementation or the default
entrypoint. The decomposed implementation currently has a 1,531-line internal entrypoint
and 2,321 PowerShell production lines including its root forwarder and `build/lib` files.
Replacing only `verify.ps1` and `verify-routing.ps1` with this outer scheduler would add
roughly 550 net production/config lines, so adoption on LOC grounds would fail.

A full replacement would still need Visual Studio discovery, MSBuild/vcpkg execution,
coverage/sanitizer/UMDH/audit/package implementations, and their tests. The pilot has not
ported enough of that surface to support a credible full-replacement LOC estimate. Native
timing and a bounded leaf-port spike must justify any further migration.

## Do the recipes need templating?

Not yet. The observed repetition is limited to the `pwsh ... build.ps1` argv prefix,
shared input glob sets, and similar gate records. If this profile grows, schema-native
command prefixes, named input sets, and node defaults would remove those repetitions
while preserving reviewable normalized JSON.

Dependencies, pool selection, cacheability, outputs, and security-sensitive gate flags
should remain explicit. Jinja would make the executable graph harder to review and add
another runtime. Only a much larger architecture/configuration matrix would justify
generation; even then, prefer a typed stdlib Python generator that emits and validates
normalized JSON over a text-template language.

## Experimental limitations

Pool limits and cache state are process-local; concurrent driver processes are not
locked against each other. The cache is local and only as complete as each node's
declared inputs and outputs. Environment variables are inherited. Production adoption
would additionally require cancellation policy, interprocess locking, complete graph
profiles for x86/x64/ARM64 routing, and the same source-quality coverage expected of
the existing build scripts.

Accordingly the coarse pilot proves useful scheduling but is not yet production-ready.
Its full x64 run is green and materially faster, while the measurements show that
monolithic analysis and dynamic leaves still hide most available parallelism.
`build.ps1` remains the correct default until the fine-grained graph has equivalent
local evidence for the supported architecture matrix.

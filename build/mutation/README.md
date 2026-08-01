# Portable mutation gate

This directory contains the Linux-only mutation-test slice for the already portable
Ren'Py Pickle parser. It is intentionally outside the shipped build graph:

- release DLLs remain MSVC/MSBuild-built Windows binaries;
- repository CMake is not introduced;
- `run.sh` compiles only `pickle.cpp`, its existing Catch2 unit tests, and the
  platform-neutral test entry point;
- Mull is filtered to the first-party `pickle.cpp` implementation, so Catch2,
  test code, and vcpkg sources are not mutation targets.

The CI workflow installs exact Mull `0.34.0` for LLVM 19 from its signed repository,
checks the repository-key fingerprint before installation, and checks out vcpkg at
the repository manifest baseline. Local tool installation is not performed by any
repository command and still requires owner approval.

Mull runs in strict mode with a mutation-score threshold of 100. The separate
`validate_report.py` gate also rejects an empty report and accepts only `Killed`
statuses. This closes the otherwise misleading case where a run with no discovered
mutants can report an infinite score. Mutation Testing Elements JSON and the full
CI log are retained as workflow artifacts before the job enforces failure.

The Windows development host can validate the report policy and static workflow
contract without Mull:

```powershell
uv run --no-python-downloads --offline --no-config --python 3.14.6 `
  python -m unittest build.tests.test_mutation_report -v
pwsh -NoProfile -File build/tests/mutation-ci-contract.Tests.ps1
```

The first GitHub run remains the execution proof for the Ubuntu package repository,
Clang/Mull plugin ABI, direct Catch2 link, and actual surviving-mutant inventory.
Any surviving non-equivalent mutant is a test defect. Equivalent mutants require
explicit owner-reviewed disposition; they are not hidden by weakening the gate.

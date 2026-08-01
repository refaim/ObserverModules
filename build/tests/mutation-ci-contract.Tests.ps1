#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$workflowPath = Join-Path $repositoryRoot '.github\workflows\mutation.yml'
$runnerPath = Join-Path $repositoryRoot 'build\mutation\run.sh'
$configPath = Join-Path $repositoryRoot 'build\mutation\mull.yml'
$mainPath = Join-Path $repositoryRoot 'src\tests\mutation\main.cpp'

foreach ($requiredPath in @($workflowPath, $runnerPath, $configPath, $mainPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Mutation CI contract file is missing: $requiredPath"
    }
}

$workflow = Get-Content -Raw -LiteralPath $workflowPath
$runner = Get-Content -Raw -LiteralPath $runnerPath
$config = Get-Content -Raw -LiteralPath $configPath
$portableMain = Get-Content -Raw -LiteralPath $mainPath

foreach ($trigger in @('push:', 'pull_request:', 'schedule:', 'workflow_dispatch:')) {
    if ($workflow -notmatch [regex]::Escape($trigger)) {
        throw "Mutation workflow must expose the '$trigger' trigger."
    }
}
if ($workflow -notmatch 'runs-on:\s*ubuntu-24\.04') {
    throw 'Mutation testing must run on the pinned Ubuntu 24.04 image.'
}
if ($workflow -notmatch '(?ms)^permissions:\s*\r?\n\s+contents:\s*read\s*$') {
    throw 'Mutation workflow must declare least-privilege contents:read permissions.'
}
if ($workflow -match 'security-events:\s*write|checks:\s*write|contents:\s*write') {
    throw 'Mutation workflow must not request write permissions.'
}

$expectedPins = @(
    "MULL_VERSION: '0.34.0'",
    "MULL_LLVM: '19'",
    "VCPKG_COMMIT: '9e593bb18ea69cc5095e012465dcd675a822ed0d'",
    "MULL_REPOSITORY_FINGERPRINT: '6975C1E8A078A727081F4B7541DB35380DE6BD6F'"
)
foreach ($pin in $expectedPins) {
    if (-not $workflow.Contains($pin)) {
        throw "Mutation workflow is missing the exact dependency pin: $pin"
    }
}
if ($workflow -match '(?m)curl[^\r\n]*\|\s*(sudo\s+)?(bash|sh)') {
    throw 'Mutation workflow must not pipe downloaded content into a shell.'
}
if ($workflow -notmatch 'gpg\s+--show-keys\s+--with-colons' -or $workflow -notmatch 'MULL_REPOSITORY_FINGERPRINT') {
    throw 'Mutation workflow must verify the repository signing-key fingerprint before installation.'
}
if ($workflow -notmatch 'mull-\$\{MULL_LLVM\}=\$\{MULL_VERSION\}') {
    throw 'Mutation workflow must install the exact Mull package version.'
}
if ($workflow -notmatch 'repository:\s*microsoft/vcpkg' -or $workflow -notmatch 'ref:\s*\$\{\{ env\.VCPKG_COMMIT \}\}') {
    throw 'Mutation workflow must obtain vcpkg at the manifest baseline commit.'
}
if (
    -not $workflow.Contains("- 'build/tests/test_mutation_report.py'") -or
    -not $workflow.Contains('python3 -m unittest build.tests.test_mutation_report -v')
) {
    throw 'Mutation workflow must run the report-validator regression suite when that suite changes.'
}

if ($workflow -notmatch '(?ms)id:\s*mutation.*?continue-on-error:\s*true') {
    throw 'Mutation execution must retain control so reports can be archived on failure.'
}
if (
    -not $workflow.Contains('mkdir -p .artifacts/reports/mutation') -or
    -not $workflow.Contains('> .artifacts/reports/mutation/ci.log 2>&1')
) {
    throw 'Verbose mutation output must be retained as evidence, including infrastructure failures.'
}
if ($workflow -notmatch '(?ms)if:\s*always\(\).*?uses:\s*actions/upload-artifact@v7') {
    throw 'Mutation evidence must always be archived.'
}
if ($workflow -notmatch '(?ms)if:\s*steps\.mutation\.outcome\s*==\s*''failure''.*?exit\s+1') {
    throw 'Surviving mutants or mutation infrastructure failures must fail the workflow after archival.'
}

if ($config -notmatch '(?m)^\s*-\s+cxx_default\s*$') {
    throw 'The initial mutation scope must enable the stable cxx_default operator group.'
}
if ($config -notmatch 'src/modules/renpy/pickle\\\.cpp\$') {
    throw 'The Mull include path must be confined to the portable first-party pickle implementation.'
}
if ($config -match 'includeNotCovered:\s*true|dryRunEnabled:\s*true') {
    throw 'Mutation configuration must execute only reached mutants, never dry-run them.'
}

foreach ($source in @(
    'src/modules/renpy/pickle.cpp',
    'src/tests/unit/pickle.cpp',
    'src/tests/mutation/main.cpp'
)) {
    if (-not $runner.Contains($source)) {
        throw "Portable mutation build must compile $source."
    }
}
foreach ($requiredArgument in @(
    '-fpass-plugin=/usr/lib/mull-ir-frontend-${MULL_LLVM}',
    '--reporters Elements',
    '--mutation-score-threshold 100',
    '--strict',
    '--no-output',
    'validate_report.py'
)) {
    if (-not $runner.Contains($requiredArgument)) {
        throw "Mutation runner is missing required argument: $requiredArgument"
    }
}
if ($runner -notmatch '(?m)^set -euo pipefail$') {
    throw 'Mutation runner must fail closed on command, variable, and pipeline errors.'
}
if ($runner -match '(?i)cmake|msbuild|\.\s*[/\\]build\.ps1') {
    throw 'Portable mutation testing must not enter the Windows/MSBuild production graph.'
}
if ($runner -notmatch '(?m)^"\$\{test_binary\}"$') {
    throw 'The unmutated portable test binary must pass before Mull runs.'
}
if ($portableMain -notmatch 'Catch::Session\(\)\.run\(argc, argv\)') {
    throw 'The portable test entry point must run the existing Catch2 tests without Windows setup.'
}

Write-Output 'Mutation CI static contract is valid.'

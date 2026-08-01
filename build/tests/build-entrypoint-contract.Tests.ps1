#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-PowerShellAst {
    param([Parameter(Mandatory)][string] $Path)

    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref] $tokens, [ref] $errors)
    if ($errors.Count -ne 0) {
        $messages = @($errors | ForEach-Object Message)
        throw "PowerShell parse failed for '$Path': $($messages -join '; ')"
    }
    return $ast
}

function Assert-SequenceEqual {
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][object[]] $Actual,
        [Parameter(Mandatory)][AllowEmptyCollection()][object[]] $Expected,
        [Parameter(Mandatory)][string] $Description
    )

    if ($Actual.Count -ne $Expected.Count) {
        throw "$Description count differs: actual=$($Actual.Count), expected=$($Expected.Count)."
    }
    for ($index = 0; $index -lt $Expected.Count; ++$index) {
        if (-not [object]::Equals($Actual[$index], $Expected[$index])) {
            throw "$Description differs at index ${index}: actual='$($Actual[$index])', expected='$($Expected[$index])'."
        }
    }
}

function Get-HelpOutput {
    param(
        [Parameter(Mandatory)][string] $PowerShellPath,
        [Parameter(Mandatory)][string] $ScriptPath
    )

    $output = @(& $PowerShellPath -NoLogo -NoProfile -File $ScriptPath help 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Help command failed for '$ScriptPath' with exit code $LASTEXITCODE."
    }
    return ((@($output | ForEach-Object { $_.ToString() }) -join "`n") -replace "`r`n?", "`n").TrimEnd()
}

$repositoryRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$rootEntrypoint = Join-Path $repositoryRoot 'build.ps1'
$internalEntrypoint = Join-Path $repositoryRoot 'build\build.ps1'
$commonLibrary = Join-Path $repositoryRoot 'build\lib\common.ps1'
$powerShellPath = Join-Path $PSHOME 'pwsh.exe'

$rootText = Get-Content -Raw -LiteralPath $rootEntrypoint
$expectedForwarder = "& (Join-Path `$PSScriptRoot 'build\build.ps1') @args"
if (-not $rootText.Contains($expectedForwarder, [StringComparison]::Ordinal)) {
    throw 'The root build.ps1 entrypoint no longer forwards arguments unchanged.'
}

$internalAst = Read-PowerShellAst -Path $internalEntrypoint
$commandParameter = @(
    $internalAst.ParamBlock.Parameters |
        Where-Object { $_.Name.VariablePath.UserPath -eq 'Command' }
)
if ($commandParameter.Count -ne 1) {
    throw 'The internal entrypoint must expose exactly one Command parameter.'
}
$validateSet = @(
    $commandParameter[0].Attributes |
        Where-Object { $_.TypeName.FullName -eq 'ValidateSet' }
)
if ($validateSet.Count -ne 1) {
    throw 'The Command parameter must retain one ValidateSet contract.'
}
$actualCommands = @($validateSet[0].PositionalArguments | ForEach-Object { $_.SafeGetValue() })
$expectedCommands = @(
    'help',
    'doctor',
    'restore',
    'build',
    'test',
    'source-checks',
    'compiler-analysis',
    'test-coverage',
    'test-asan',
    'test-ubsan',
    'test-leaks',
    'fuzz',
    'audit-binaries',
    'package',
    'verify',
    'clean'
)
Assert-SequenceEqual -Actual $actualCommands -Expected $expectedCommands -Description 'Command ValidateSet'

$restoreFlavorParameter = @(
    $internalAst.ParamBlock.Parameters |
        Where-Object { $_.Name.VariablePath.UserPath -eq 'RestoreFlavor' }
)
if ($restoreFlavorParameter.Count -ne 1) {
    throw 'The internal entrypoint must expose exactly one RestoreFlavor parameter.'
}
$restoreFlavorValidateSet = @(
    $restoreFlavorParameter[0].Attributes |
        Where-Object { $_.TypeName.FullName -eq 'ValidateSet' }
)
if ($restoreFlavorValidateSet.Count -ne 1) {
    throw 'RestoreFlavor must retain one ValidateSet contract.'
}
Assert-SequenceEqual `
    -Actual @($restoreFlavorValidateSet[0].PositionalArguments | ForEach-Object { $_.SafeGetValue() }) `
    -Expected @('default', 'asan', 'all') `
    -Description 'RestoreFlavor ValidateSet'

$skipRestoreParameter = @(
    $internalAst.ParamBlock.Parameters |
        Where-Object { $_.Name.VariablePath.UserPath -eq 'SkipDependencyRestore' }
)
if ($skipRestoreParameter.Count -ne 1 -or
    $skipRestoreParameter[0].StaticType -ne [System.Management.Automation.SwitchParameter]) {
    throw 'The internal entrypoint must expose one SkipDependencyRestore switch.'
}

$expectedHelp = @'
ObserverModules build entry point

  doctor                         inspect the complete toolchain
  restore  -Arch <list|all>      restore pinned static vcpkg dependencies
  build    -Arch <list|all>      build modules and tests
  test     -Arch <list>          build and run deterministic/corpus tests
  source-checks -Arch <list|all> clang-format, Cppcheck, PSScriptAnalyzer
  compiler-analysis -Arch <list> MSVC /analyze plus clang-tidy
  test-coverage -Arch <list>     run tests and enforce llvm-cov source coverage
  test-asan -Arch <x86|x64>      build dependencies/code and run tests with MSVC ASan
  test-ubsan -Arch x64           build and run tests with clang-cl UBSan
  test-leaks -Arch x64           run UMDH operation and DLL-lifecycle leak checks
  fuzz     -Arch x64             build and run one or all libFuzzer targets
  audit-binaries -Arch <list|all> build and inspect Release PE files with dumpbin
  package  -Arch <list|all>      module ZIPs plus one combined PDB ZIP
  verify   -Arch <list|all>      complete host-capable gate with explicit native deferrals
  clean                           remove .artifacts

Options:
  -Config Debug|Release
  -Corpus <path>
  -RestoreFlavor default|asan|all  restore only; "all" prepares serial DAG dependency flavors
  -SkipDependencyRestore          DAG leaf only; dependencies must already be restored
  -FuzzSeconds <seconds>
  -FuzzTarget <name|all>         pickle, renpy, rpgmaker, zanzarah, or all (default)
  -LeakWarmup <rounds>
  -LeakIterations <rounds per window>
  -LeakWindows <3..10>
  -LeakToleranceBytes <bytes>    defaults to zero; use only for a reviewed stack-specific exception
  -CoverageThreshold <0..100>    defaults to 100 for source lines and branches
'@.TrimEnd()
$rootHelp = Get-HelpOutput -PowerShellPath $powerShellPath -ScriptPath $rootEntrypoint
$internalHelp = Get-HelpOutput -PowerShellPath $powerShellPath -ScriptPath $internalEntrypoint
if (-not $rootHelp.Equals($expectedHelp, [StringComparison]::Ordinal)) {
    throw 'The public root help output changed.'
}
if (-not $internalHelp.Equals($expectedHelp, [StringComparison]::Ordinal)) {
    throw 'The internal help output differs from the public CLI contract.'
}

$entrypointText = Get-Content -Raw -LiteralPath $internalEntrypoint
$dotSourceMatches = [regex]::Matches(
    $entrypointText,
    "(?m)^\.\s+\(Join-Path\s+\`$script:BuildRoot\s+'lib\\([^']+)'\)\s*`$"
)
$actualLoadOrder = @($dotSourceMatches | ForEach-Object { $_.Groups[1].Value })
Assert-SequenceEqual `
    -Actual $actualLoadOrder `
    -Expected @(
        'package-manifest.ps1',
        'analysis-reporting.ps1',
        'common.ps1',
        'package-smoke.ps1',
        'verify-routing.ps1',
        'packaging.ps1',
        'verify.ps1'
    ) `
    -Description 'Entrypoint library load order'

if (-not (Test-Path -LiteralPath $commonLibrary -PathType Leaf)) {
    throw "Common build library is missing: $commonLibrary"
}
$commonAst = Read-PowerShellAst -Path $commonLibrary
$expectedCommonFunctions = @(
    'Write-Step',
    'Invoke-Native',
    'Invoke-NativeCapture',
    'Get-RequestedArchitecture',
    'Get-MSBuildPlatform',
    'Get-VcpkgTriplet',
    'Get-BinaryDirectory',
    'Resolve-UserPath'
)
$actualCommonFunctions = @(
    $commonAst.FindAll(
        { param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] },
        $false
    ) |
        ForEach-Object Name
)
Assert-SequenceEqual -Actual $actualCommonFunctions -Expected $expectedCommonFunctions -Description 'Common helper surface'

$entrypointFunctionNames = @(
    $internalAst.FindAll(
        { param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] },
        $false
    ) |
        ForEach-Object Name
)
$duplicateCommonFunctions = @($expectedCommonFunctions | Where-Object { $_ -in $entrypointFunctionNames })
if ($duplicateCommonFunctions.Count -ne 0) {
    throw "Common helpers remain duplicated in the entrypoint: $($duplicateCommonFunctions -join ', ')."
}

$restoreFunction = @(
    $internalAst.FindAll(
        {
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq 'Invoke-Restore'
        },
        $false
    )
)
if ($restoreFunction.Count -ne 1) {
    throw 'The entrypoint must define exactly one Invoke-Restore function.'
}
$restoreFunctionText = $restoreFunction[0].Extent.Text
$skipGuardIndex = $restoreFunctionText.IndexOf('if ($SkipDependencyRestore)', [StringComparison]::Ordinal)
$skipReturnIndex = $restoreFunctionText.IndexOf('return', $skipGuardIndex, [StringComparison]::Ordinal)
$toolDiscoveryIndex = $restoreFunctionText.IndexOf('Resolve-Vcpkg', [StringComparison]::Ordinal)
if ($skipGuardIndex -lt 0 -or $skipReturnIndex -lt $skipGuardIndex -or $toolDiscoveryIndex -lt $skipReturnIndex) {
    throw 'SkipDependencyRestore must return before vcpkg discovery and execution.'
}
if ($restoreFunctionText -notmatch "'all'\) \{ @\('default', 'asan'\) \}") {
    throw 'RestoreFlavor all must expand to serial default and ASan restores.'
}

$rejectedRestoreOutput = @(
    & $powerShellPath -NoLogo -NoProfile -File $internalEntrypoint `
        restore -Arch x64 -SkipDependencyRestore 2>&1
)
if ($LASTEXITCODE -eq 0 -or
    ($rejectedRestoreOutput -join "`n") -notmatch 'cannot be used with the restore command') {
    throw 'The restore command must reject SkipDependencyRestore instead of reporting a false success.'
}

$script:BuildRoot = Join-Path $repositoryRoot 'build'
$script:RepositoryRoot = $repositoryRoot
$script:AggregateProject = Join-Path $script:BuildRoot 'ObserverModules.proj'
$script:ArtifactsRoot = Join-Path $repositoryRoot '.artifacts'
$script:KnownArchitectures = @('x86', 'x64', 'arm64')
. $commonLibrary

Assert-SequenceEqual `
    -Actual @(Get-RequestedArchitecture -Requested @('all')) `
    -Expected @('x86', 'x64', 'arm64') `
    -Description 'All-architecture expansion'
Assert-SequenceEqual `
    -Actual @(
        Get-MSBuildPlatform -Architecture 'x86'
        Get-MSBuildPlatform -Architecture 'x64'
        Get-MSBuildPlatform -Architecture 'arm64'
    ) `
    -Expected @('Win32', 'x64', 'ARM64') `
    -Description 'MSBuild platform mapping'
Assert-SequenceEqual `
    -Actual @(
        Get-VcpkgTriplet -Architecture 'x64'
        Get-VcpkgTriplet -Architecture 'x64' -Flavor 'asan'
    ) `
    -Expected @('observer-x64-windows-static', 'observer-x64-windows-static-asan') `
    -Description 'vcpkg triplet mapping'

$expectedBinaryDirectory = Join-Path $script:ArtifactsRoot 'bin\x64\Release'
$actualBinaryDirectory = Get-BinaryDirectory -Architecture 'x64' -Configuration 'Release'
if (-not $actualBinaryDirectory.Equals($expectedBinaryDirectory, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Get-BinaryDirectory no longer uses the explicit build context.'
}
$expectedUserPath = [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot 'docs'))
$actualUserPath = Resolve-UserPath -Path 'docs'
if (-not $actualUserPath.Equals($expectedUserPath, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Resolve-UserPath no longer resolves relative to the repository root.'
}
$capture = Invoke-NativeCapture `
    -FilePath $powerShellPath `
    -Arguments @('-NoLogo', '-NoProfile', '-Command', "Write-Output 'common-capture'")
Assert-SequenceEqual -Actual @($capture) -Expected @('common-capture') -Description 'Native capture helper'

Write-Host '[OK] Build entrypoint CLI and common-library scope contracts are stable.'

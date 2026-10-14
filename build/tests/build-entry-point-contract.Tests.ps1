#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# PowerShell parses an unquoted `x86,x64` in argument mode as an array literal, so an entry
# point that forwards `@args` verbatim splats it into several separate driver arguments and every
# documented multi-value example fails with `unrecognized arguments`. These cases pin the forwarding
# contract of the root launcher: the driver must receive exactly one argument per written argument.

$repositoryRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$entryPoint = Join-Path $repositoryRoot 'build.ps1'
$driverScript = Join-Path (Join-Path $repositoryRoot 'build') 'main.py'
$powershell = [System.Environment]::ProcessPath
$invocation = $entryPoint.Replace("'", "''")

# The seam refuses to report a successful build, so a leaked variable cannot pass as one.
$argvSeamExitCode = 97

function Get-ForwardedArgv {
    param(
        [Parameter(Mandatory)][string[]] $ProcessArgument,
        [Parameter(Mandatory)][string] $Description
    )

    $previous = $env:UNPACKRAT_BUILD_PRINT_ARGV
    try {
        $env:UNPACKRAT_BUILD_PRINT_ARGV = '1'
        $output = & $powershell -NoLogo -NoProfile -NonInteractive @ProcessArgument
    }
    finally {
        $env:UNPACKRAT_BUILD_PRINT_ARGV = $previous
    }

    if ($LASTEXITCODE -ne $argvSeamExitCode) {
        throw ("The entry point must refuse to build and exit ${argvSeamExitCode} while reporting the driver " +
            "argv, but exited ${LASTEXITCODE} for: $Description")
    }
    $reported = (@($output) -join "`n").Trim()
    if (-not $reported.StartsWith('[', [StringComparison]::Ordinal)) {
        throw ("build.ps1 must report the driver argv as JSON when UNPACKRAT_BUILD_PRINT_ARGV is '1' " +
            "for '$Description'; reported: $reported")
    }
    $argv = @($reported | ConvertFrom-Json)
    if ($argv.Count -eq 0 -or $argv[0] -ne $driverScript) {
        throw "The entry point must forward the pinned driver script for: $Description"
    }
    return @($argv | Select-Object -Skip 1)
}

function Assert-CommandLine {
    param(
        [Parameter(Mandatory)][string] $CommandLine,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]] $Expected
    )

    # -Command reproduces the parsing a console or CI step performs; -File would flatten every
    # argument to a string and hide the array-literal defect this contract exists for. -Command
    # reports only success or failure by itself, so the launcher's own code is republished.
    $actual = @(Get-ForwardedArgv -ProcessArgument @('-Command', "& '$invocation' $CommandLine; exit `$LASTEXITCODE") `
        -Description $CommandLine)
    $separator = [char]31
    if ([string]::Join($separator, $actual) -ne [string]::Join($separator, $Expected)) {
        throw ("'$CommandLine' must reach the driver as [$([string]::Join('] [', $Expected))] " +
            "but reached it as [$([string]::Join('] [', $actual))].")
    }
}

Assert-CommandLine -CommandLine 'build -Arch x86,x64 -Config Release' `
    -Expected @('build', '-Arch', 'x86,x64', '-Config', 'Release')

Assert-CommandLine -CommandLine "build -Arch 'x86,x64' -Config Release" `
    -Expected @('build', '-Arch', 'x86,x64', '-Config', 'Release')

Assert-CommandLine -CommandLine 'test -Arch x86,x64 -Config Debug' `
    -Expected @('test', '-Arch', 'x86,x64', '-Config', 'Debug')

Assert-CommandLine -CommandLine 'build -Arch all -Config Debug,Release' `
    -Expected @('build', '-Arch', 'all', '-Config', 'Debug,Release')

Assert-CommandLine -CommandLine 'ci-arch -Arch x64 -Corpus C:\corpus -Jobs 8' `
    -Expected @('ci-arch', '-Arch', 'x64', '-Corpus', 'C:\corpus', '-Jobs', '8')

Assert-CommandLine -CommandLine 'package -Module renpy,rpgmaker -Output out\packages' `
    -Expected @('package', '-Module', 'renpy,rpgmaker', '-Output', 'out\packages')

# build.cmd routes through `pwsh -File`, where every argument already arrives as one string.
$actual = @(Get-ForwardedArgv -ProcessArgument (@('-File', $entryPoint) + @('ci-source', '-Jobs', '4')) `
    -Description 'build.cmd shim')
if ([string]::Join([char]31, $actual) -ne [string]::Join([char]31, @('ci-source', '-Jobs', '4'))) {
    throw 'build.cmd must forward every argument unchanged.'
}

# The launcher must never silently download a Python interpreter: the pinned environment is
# provisioned explicitly, and a local run must refuse an automatic download rather than fetch one.
$launcher = Get-Content -Raw -LiteralPath $entryPoint
foreach ($flag in @('--frozen', '--no-sync', '--no-python-downloads')) {
    if ($launcher -notmatch [Regex]::Escape($flag)) {
        throw "build.ps1's uv run invocation must pass $flag so a missing interpreter fails instead of being downloaded."
    }
}

Write-Host '[OK] The root entry point forwards every documented argument form unchanged.'

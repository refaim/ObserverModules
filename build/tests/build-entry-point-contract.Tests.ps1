#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# PowerShell parses an unquoted `x86,x64` in argument mode as an array literal, so an entry
# point that forwards `@args` verbatim splats it into three separate driver arguments and every
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

function Assert-ForwardedArgv {
    param(
        [Parameter(Mandatory)][string[]] $ProcessArgument,
        [Parameter(Mandatory)][string] $Description,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]] $Expected
    )

    $actual = @(Get-ForwardedArgv -ProcessArgument $ProcessArgument -Description $Description)
    $separator = [char]31
    if ([string]::Join($separator, $actual) -ne [string]::Join($separator, $Expected)) {
        throw ("'$Description' must reach the driver as [$([string]::Join('] [', $Expected))] " +
            "but reached it as [$([string]::Join('] [', $actual))].")
    }
}

function Assert-CommandLine {
    param(
        [Parameter(Mandatory)][string] $CommandLine,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]] $Expected
    )

    # -Command reproduces the parsing a console or CI step performs; -File would flatten every
    # argument to a string and hide the array-literal defect this contract exists for. -Command
    # reports only success or failure by itself, so the launcher's own code is republished.
    Assert-ForwardedArgv -ProcessArgument @('-Command', "& '$invocation' $CommandLine; exit `$LASTEXITCODE") `
        -Description $CommandLine -Expected $Expected
}

# An empty command line reaches the driver as no arguments at all, which is how the launcher
# forwards the documented help path. Assert-CommandLine cannot express it: its mandatory string
# parameters reject the empty string.
Assert-ForwardedArgv -ProcessArgument @('-Command', "& '$invocation'; exit `$LASTEXITCODE") `
    -Description 'no arguments' -Expected @()

Assert-CommandLine -CommandLine 'help' -Expected @('help')

Assert-CommandLine -CommandLine 'doctor' -Expected @('doctor')

Assert-CommandLine -CommandLine 'build -Arch x86,x64 -Config Release' `
    -Expected @('build', '-Arch', 'x86,x64', '-Config', 'Release')

Assert-CommandLine -CommandLine "build -Arch 'x86,x64' -Config Release" `
    -Expected @('build', '-Arch', 'x86,x64', '-Config', 'Release')

Assert-CommandLine -CommandLine 'test -Arch x86,x64 -Config Debug' `
    -Expected @('test', '-Arch', 'x86,x64', '-Config', 'Debug')

Assert-CommandLine -CommandLine 'test -Arch x64 -Config Debug' `
    -Expected @('test', '-Arch', 'x64', '-Config', 'Debug')

# Generality: every multi-value option, not only -Arch, survives the unquoted comma form.
Assert-CommandLine -CommandLine 'restore -Arch x86,x64 -RestoreFlavor default,asan' `
    -Expected @('restore', '-Arch', 'x86,x64', '-RestoreFlavor', 'default,asan')

Assert-CommandLine -CommandLine 'build -Arch all -Config Debug,Release' `
    -Expected @('build', '-Arch', 'all', '-Config', 'Debug,Release')

Assert-CommandLine -CommandLine 'fuzz -Arch x64 -FuzzTarget pickle,renpy,rpgmaker,zanzarah -FuzzSeconds 60' `
    -Expected @('fuzz', '-Arch', 'x64', '-FuzzTarget', 'pickle,renpy,rpgmaker,zanzarah', '-FuzzSeconds', '60')

Assert-CommandLine -CommandLine 'fuzz -Arch x86,x64 -FuzzTarget all -FuzzSeconds 60' `
    -Expected @('fuzz', '-Arch', 'x86,x64', '-FuzzTarget', 'all', '-FuzzSeconds', '60')

Assert-CommandLine -CommandLine 'clean -CleanMode reports' -Expected @('clean', '-CleanMode', 'reports')

# The complete CI command line, including options, integers, and a quoted path.
Assert-CommandLine -CommandLine ("verify-arch -Arch x64 -ExportDir 'C:\evidence dir\x64' " +
    '-FuzzSeconds 5 -LeakWarmup 1 -LeakIterations 1 -LeakWindows 3 -LeakToleranceBytes 0 ' +
    '-Priority normal -Jobs 8 -Corpus C:\corpus') `
    -Expected @('verify-arch', '-Arch', 'x64', '-ExportDir', 'C:\evidence dir\x64',
        '-FuzzSeconds', '5', '-LeakWarmup', '1', '-LeakIterations', '1', '-LeakWindows', '3',
        '-LeakToleranceBytes', '0', '-Priority', 'normal', '-Jobs', '8',
        '-Corpus', 'C:\corpus')

# build.cmd routes through `pwsh -File`, where every argument already arrives as one string.
Assert-ForwardedArgv -ProcessArgument (@('-File', $entryPoint) +
    @('build', '-Arch', 'x86,x64', '-Config', 'Release')) `
    -Description 'build.cmd shim' `
    -Expected @('build', '-Arch', 'x86,x64', '-Config', 'Release')

Write-Host '[OK] The root entry point forwards every documented argument form unchanged.'

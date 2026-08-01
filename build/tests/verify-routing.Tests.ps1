#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$routingModule = Join-Path $repositoryRoot 'build\lib\verify-routing.ps1'
if (-not (Test-Path -LiteralPath $routingModule -PathType Leaf)) {
    throw 'Verify routing planner is missing.'
}
. $routingModule

function Assert-SetEqual {
    param(
        [Parameter(Mandatory)][string[]] $Actual,
        [Parameter(Mandatory)][string[]] $Expected,
        [Parameter(Mandatory)][string] $Description
    )

    if (@(Compare-Object -ReferenceObject $Expected -DifferenceObject $Actual).Count -ne 0) {
        throw "$Description differs from the expected routing contract."
    }
}

$x64Plan = Get-VerifyRoutingPlan -HostArchitecture x64 -RequestedArchitectures @('x86', 'x64', 'arm64')
Assert-SetEqual -Description 'Requested architectures' -Actual @($x64Plan.RequestedArchitectures) -Expected @(
    'x86', 'x64', 'arm64'
)
Assert-SetEqual -Description 'Build matrix' -Actual @($x64Plan.Builds | ForEach-Object { "$($_.Architecture):$($_.Configuration)" }) -Expected @(
    'x86:Debug', 'x86:Release', 'x64:Debug', 'x64:Release', 'arm64:Debug', 'arm64:Release'
)
Assert-SetEqual -Description 'Runnable deterministic tests' -Actual @($x64Plan.TestRuns | ForEach-Object { "$($_.Architecture):$($_.Configuration)" }) -Expected @(
    'x86:Debug', 'x86:Release', 'x64:Debug', 'x64:Release'
)
Assert-SetEqual -Description 'Specialist gates' -Actual @($x64Plan.SpecialistGates | ForEach-Object { "$($_.Name):$($_.Architecture)" }) -Expected @(
    'coverage:x64', 'asan:x86', 'asan:x64', 'ubsan:x64', 'leaks:x64', 'formats:x64'
)
Assert-SetEqual -Description 'Package content checks' -Actual @($x64Plan.PackageContentArchitectures) -Expected @(
    'x86', 'x64', 'arm64'
)
Assert-SetEqual -Description 'Package runtime checks' -Actual @($x64Plan.PackageRuntimeArchitectures) -Expected @(
    'x86', 'x64'
)

$deferredKeys = @($x64Plan.Deferred | ForEach-Object { "$($_.Gate):$($_.Architecture)" })
Assert-SetEqual -Description 'Deferred native work' -Actual $deferredKeys -Expected @('tests:arm64', 'package-runtime:arm64')
if (@($x64Plan.Deferred | Where-Object { [string]::IsNullOrWhiteSpace($_.Reason) }).Count -ne 0) {
    throw 'Every deferred verify action must include a reason.'
}

$currentHost = Get-CurrentVerifyHostArchitecture
if ($currentHost -notin @('x86', 'x64', 'arm64')) {
    throw "The current verify host architecture is unsupported: $currentHost"
}

$arm64Plan = Get-VerifyRoutingPlan -HostArchitecture arm64 -RequestedArchitectures @('arm64')
Assert-SetEqual -Description 'Native ARM64 tests' -Actual @($arm64Plan.TestRuns | ForEach-Object { "$($_.Architecture):$($_.Configuration)" }) -Expected @(
    'arm64:Debug', 'arm64:Release'
)
Assert-SetEqual -Description 'Native ARM64 package smoke' -Actual @($arm64Plan.PackageRuntimeArchitectures) -Expected @('arm64')
if ($arm64Plan.Deferred.Count -ne 0) {
    throw 'A native ARM64 host must not defer requested ARM64 runtime checks.'
}

Write-Host '[OK] Verify routing preserves build coverage and reports non-native runtime work explicitly.'

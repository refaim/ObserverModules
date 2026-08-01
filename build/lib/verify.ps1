#requires -Version 7.4

Set-StrictMode -Version Latest

function Invoke-Verify {
    param(
        [Parameter(Mandatory)][string[]] $Architectures,
        [Parameter()][string] $CorpusPath,
        [Parameter(Mandatory)][double] $RequiredCoverageThreshold,
        [Parameter(Mandatory)][int] $RequiredFuzzSeconds,
        [Parameter(Mandatory)][int] $RequiredLeakWarmup,
        [Parameter(Mandatory)][int] $RequiredLeakIterations,
        [Parameter(Mandatory)][int] $RequiredLeakWindows,
        [Parameter(Mandatory)][int64] $RequiredLeakToleranceBytes
    )

    $plan = Get-VerifyRoutingPlan `
        -HostArchitecture (Get-CurrentVerifyHostArchitecture) `
        -RequestedArchitectures $Architectures

    Invoke-Restore -Architectures $plan.RequestedArchitectures
    Invoke-Lint -Architectures $plan.RequestedArchitectures

    foreach ($buildGroup in @($plan.Builds | Group-Object Configuration)) {
        $buildArchitectures = @($buildGroup.Group | ForEach-Object Architecture)
        Invoke-Build -Architectures $buildArchitectures -Configuration $buildGroup.Name
    }
    foreach ($testRun in @($plan.TestRuns)) {
        Invoke-TestExecutable `
            -Architecture $testRun.Architecture `
            -Configuration $testRun.Configuration `
            -CorpusPath $CorpusPath
    }

    Invoke-CodeAnalysis -Architectures $plan.RequestedArchitectures

    foreach ($gateGroup in @($plan.SpecialistGates | Group-Object Name)) {
        $gateArchitectures = @($gateGroup.Group | ForEach-Object Architecture)
        switch ($gateGroup.Name) {
            'coverage' {
                Invoke-Coverage `
                    -Architectures $gateArchitectures `
                    -CorpusPath $CorpusPath `
                    -Threshold $RequiredCoverageThreshold
            }
            'asan' { Invoke-ASan -Architectures $gateArchitectures }
            'ubsan' { Invoke-Ubsan -Architectures $gateArchitectures }
            'leaks' {
                Invoke-LeakTest `
                    -Architectures $gateArchitectures `
                    -Warmup $RequiredLeakWarmup `
                    -Iterations $RequiredLeakIterations `
                    -Windows $RequiredLeakWindows `
                    -ToleranceBytes $RequiredLeakToleranceBytes
            }
            'formats' {
                Invoke-Fuzz `
                    -Architectures $gateArchitectures `
                    -Seconds $RequiredFuzzSeconds `
                    -TargetName 'all'
            }
            default { throw "Unknown verify specialist gate: $($gateGroup.Name)" }
        }
    }

    Invoke-Package -Architectures $plan.PackageContentArchitectures

    if ($plan.Deferred.Count -ne 0) {
        Write-Step 'Runtime checks deferred to native runners'
        foreach ($deferred in @($plan.Deferred)) {
            Write-Host "[DEFERRED] $($deferred.Gate) $($deferred.Architecture): $($deferred.Reason)"
        }
    }
    Write-Host "[OK] Verify completed every host-capable gate; deferred native checks: $($plan.Deferred.Count)."
}

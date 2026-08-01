#requires -Version 7.4

Set-StrictMode -Version Latest

function Get-CurrentVerifyHostArchitecture {
    $hostArchitecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
    if ($hostArchitecture -notin @('x86', 'x64', 'arm64')) {
        throw "The current host architecture is unsupported: $hostArchitecture"
    }
    return $hostArchitecture
}

function Test-VerifyArchitectureRunnable {
    param(
        [Parameter(Mandatory)][ValidateSet('x86', 'x64', 'arm64')][string] $HostArchitecture,
        [Parameter(Mandatory)][ValidateSet('x86', 'x64', 'arm64')][string] $TargetArchitecture
    )

    switch ($HostArchitecture) {
        'x86' { return $TargetArchitecture -eq 'x86' }
        'x64' { return $TargetArchitecture -in @('x86', 'x64') }
        'arm64' { return $TargetArchitecture -in @('x86', 'x64', 'arm64') }
    }
}

function Get-VerifyRoutingPlan {
    param(
        [Parameter(Mandatory)][ValidateSet('x86', 'x64', 'arm64')][string] $HostArchitecture,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string[]] $RequestedArchitectures
    )

    $knownArchitectures = @('x86', 'x64', 'arm64')
    $requested = @($RequestedArchitectures | Select-Object -Unique)
    $unsupported = @($requested | Where-Object { $_ -notin $knownArchitectures })
    if ($unsupported.Count -ne 0) {
        throw "Unsupported verify architecture(s): $($unsupported -join ', ')"
    }

    $builds = [System.Collections.Generic.List[object]]::new()
    $testRuns = [System.Collections.Generic.List[object]]::new()
    $specialistGates = [System.Collections.Generic.List[object]]::new()
    $packageRuntimeArchitectures = [System.Collections.Generic.List[string]]::new()
    $deferred = [System.Collections.Generic.List[object]]::new()

    foreach ($architecture in $requested) {
        foreach ($configuration in @('Debug', 'Release')) {
            $builds.Add([pscustomobject]@{
                    Architecture = $architecture
                    Configuration = $configuration
                })
            if (Test-VerifyArchitectureRunnable -HostArchitecture $HostArchitecture -TargetArchitecture $architecture) {
                $testRuns.Add([pscustomobject]@{
                        Architecture = $architecture
                        Configuration = $configuration
                    })
            }
        }

        if (Test-VerifyArchitectureRunnable -HostArchitecture $HostArchitecture -TargetArchitecture $architecture) {
            $packageRuntimeArchitectures.Add($architecture)
        } else {
            $deferred.Add([pscustomobject]@{
                    Gate = 'tests'
                    Architecture = $architecture
                    Reason = "The $HostArchitecture host cannot execute $architecture test binaries; use a native $architecture runner."
                })
            $deferred.Add([pscustomobject]@{
                    Gate = 'package-runtime'
                    Architecture = $architecture
                    Reason = "The $HostArchitecture host can validate $architecture package contents but cannot load its DLL."
                })
        }
    }

    if ('x64' -in $requested) {
        foreach ($name in @('coverage', 'ubsan', 'leaks', 'formats')) {
            $specialistGates.Add([pscustomobject]@{ Name = $name; Architecture = 'x64' })
        }
    }
    foreach ($architecture in @('x86', 'x64')) {
        if ($architecture -in $requested) {
            $specialistGates.Add([pscustomobject]@{ Name = 'asan'; Architecture = $architecture })
        }
    }

    return [pscustomobject]@{
        RequestedArchitectures = @($requested)
        Builds = @($builds)
        TestRuns = @($testRuns)
        SpecialistGates = @($specialistGates)
        PackageContentArchitectures = @($requested)
        PackageRuntimeArchitectures = @($packageRuntimeArchitectures)
        Deferred = @($deferred)
    }
}

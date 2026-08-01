#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$leafLibrary = Join-Path $repositoryRoot 'build\lib\dynamic-graph-leaves.ps1'
$probeSource = Join-Path $repositoryRoot 'src\tests\leaks\probe.cpp'

if (-not (Test-Path -LiteralPath $leafLibrary -PathType Leaf)) {
    throw "Dynamic graph leaf library is missing: $leafLibrary"
}
. $leafLibrary

function Assert-Equal {
    param(
        [Parameter(Mandatory)] $Actual,
        [Parameter(Mandatory)] $Expected,
        [Parameter(Mandatory)][string] $Description
    )

    if ($Actual -cne $Expected) {
        throw "$Description expected '$Expected', received '$Actual'."
    }
}

function Assert-Throw {
    param(
        [Parameter(Mandatory)][scriptblock] $Action,
        [Parameter(Mandatory)][string] $Description
    )

    try {
        & $Action
    }
    catch {
        return
    }
    throw "Expected failure: $Description"
}

$targetSpecs = @(Get-DynamicFuzzTargetSpec)
Assert-Equal -Actual $targetSpecs.Count -Expected 4 -Description 'Fuzz target count'
Assert-Equal -Actual (($targetSpecs.Name | Sort-Object) -join ',') -Expected 'pickle,renpy,rpgmaker,zanzarah' -Description 'Fuzz target names'
Assert-Equal -Actual ($targetSpecs | Where-Object Name -eq 'pickle').MaxLength -Expected 262144 -Description 'Pickle maximum input length'
foreach ($spec in @($targetSpecs | Where-Object Name -ne 'pickle')) {
    Assert-Equal -Actual $spec.MaxLength -Expected 1048576 -Description "$($spec.Name) maximum input length"
}
Assert-DynamicRunId -RunId 'local-2026.08.01_01'
Assert-Throw -Description 'unsafe dynamic run id' -Action {
    Assert-DynamicRunId -RunId '..\escape'
}

$replayArguments = Get-DynamicFuzzArgumentList `
    -Phase seed-replay `
    -TargetName pickle `
    -InputPath @('C:\seeds\one', 'C:\seeds\two') `
    -ArtifactDirectory 'C:\artifacts\replay'
Assert-Equal -Actual $replayArguments[0] -Expected 'C:\seeds\one' -Description 'Replay first seed'
Assert-Equal -Actual $replayArguments[1] -Expected 'C:\seeds\two' -Description 'Replay second seed'
if ($replayArguments -notcontains '-max_len=262144' -or $replayArguments -match '^-max_total_time=') {
    throw 'Replay arguments do not use libFuzzer individual-file mode with the target maximum length.'
}

$timedArguments = Get-DynamicFuzzArgumentList `
    -Phase timed `
    -TargetName renpy `
    -InputPath @('C:\corpus\renpy') `
    -ArtifactDirectory 'C:\artifacts\timed' `
    -Seconds 17
if ($timedArguments -notcontains '-max_total_time=17' -or $timedArguments -notcontains '-use_value_profile=1') {
    throw 'Timed fuzz arguments do not enforce the requested duration and value profile.'
}
Assert-Throw -Description 'timed fuzz without a duration' -Action {
    Get-DynamicFuzzArgumentList -Phase timed -TargetName renpy -InputPath @('C:\corpus') -ArtifactDirectory 'C:\out'
}

$temporaryRoot = Join-Path $repositoryRoot ".artifacts\dynamic-leaf-contract-$([guid]::NewGuid().ToString('N'))"
$seedRoot = Join-Path $temporaryRoot 'seeds'
$corpusRoot = Join-Path $temporaryRoot 'corpus'
New-Item -ItemType Directory -Path $seedRoot -Force | Out-Null
[System.IO.File]::WriteAllText((Join-Path $seedRoot 'hex-seed.hex'), '00 7f FF')
[System.IO.File]::WriteAllBytes((Join-Path $seedRoot 'raw.seed'), [byte[]]@(1, 2, 3))
try {
    [void](Initialize-DynamicFuzzCorpus -SeedDirectory $seedRoot -CorpusDirectory $corpusRoot)
    Assert-Equal -Actual ([Convert]::ToHexString([System.IO.File]::ReadAllBytes((Join-Path $corpusRoot 'hex-seed')))) -Expected '007FFF' -Description 'Decoded hex seed'
    Assert-Equal -Actual ([Convert]::ToHexString([System.IO.File]::ReadAllBytes((Join-Path $corpusRoot 'raw.seed')))) -Expected '010203' -Description 'Copied raw seed'
    [System.IO.File]::WriteAllText((Join-Path $seedRoot 'invalid.hex'), 'ABC')
    Assert-Throw -Description 'odd-length hexadecimal seed' -Action {
        Initialize-DynamicFuzzCorpus -SeedDirectory $seedRoot -CorpusDirectory (Join-Path $temporaryRoot 'invalid')
    }
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}

$leafRoot = Join-Path $repositoryRoot ".artifacts\dynamic-leaf-invoke-$([guid]::NewGuid().ToString('N'))"
$leafSeeds = Join-Path $leafRoot 'seeds'
$fakeFuzzer = Join-Path $leafRoot 'fuzz-pickle.exe'
$fakeProbe = Join-Path $leafRoot 'leak-probe.exe'
New-Item -ItemType Directory -Path $leafSeeds -Force | Out-Null
[System.IO.File]::WriteAllBytes((Join-Path $leafSeeds 'none.pickle'), [byte[]]@([char]'N', [char]'.'))
[System.IO.File]::WriteAllText($fakeFuzzer, 'fake')
[System.IO.File]::WriteAllText($fakeProbe, 'fake')
try {
    $fuzzInvocations = [System.Collections.Generic.List[object]]::new()
    $fuzzInvoker = {
        param($FilePath, $Arguments, $WorkingDirectory, $LogPath)
        $fuzzInvocations.Add([pscustomobject]@{
                FilePath = $FilePath
                Arguments = @($Arguments)
                WorkingDirectory = $WorkingDirectory
                LogPath = $LogPath
            })
        return 0
    }.GetNewClosure()
    $fuzzEvidence = Invoke-DynamicFuzzLeaf `
        -Phase seed-replay `
        -TargetName pickle `
        -FuzzerPath $fakeFuzzer `
        -SeedDirectory $leafSeeds `
        -CorpusDirectory (Join-Path $leafRoot 'persistent-corpus') `
        -RunDirectory (Join-Path $leafRoot 'replay-run') `
        -WorkingDirectory $leafRoot `
        -Seconds 9 `
        -NativeInvoker $fuzzInvoker
    Assert-Equal -Actual $fuzzInvocations.Count -Expected 1 -Description 'Fuzz leaf native invocation count'
    Assert-Equal -Actual $fuzzEvidence.phase -Expected 'seed-replay' -Description 'Fuzz leaf evidence phase'
    if ($fuzzInvocations[0].Arguments -notcontains '-max_len=262144') {
        throw 'Fuzz leaf did not pass the selected target bound to its native adapter.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $leafRoot 'replay-run\result.json') -PathType Leaf)) {
        throw 'Fuzz leaf did not write its isolated evidence file.'
    }

    $probeCapture = {
        param($FilePath, $Arguments, $WorkingDirectory)
        $null = $FilePath, $WorkingDirectory
        if ($Arguments -notcontains '--scenario' -or $Arguments -notcontains 'malformed') {
            throw 'Leak preflight adapter did not receive the exact scenario.'
        }
        return @(
            'OBSERVER_LEAK_PROBE|READY|pid=77|mode=lifecycle|configuration=Release|scenarios=malformed',
            'OBSERVER_LEAK_PROBE|SNAPSHOT|baseline|pid=77|completed_operations=1',
            'OBSERVER_LEAK_PROBE|DONE|pid=77|completed_operations=4'
        )
    }
    $leakEvidencePath = Join-Path $leafRoot 'leak-preflight.json'
    $leakEvidence = Invoke-DynamicLeakPreflightLeaf `
        -ProbePath $fakeProbe `
        -BinaryDirectory $leafRoot `
        -Mode lifecycle `
        -Scenario malformed `
        -EvidencePath $leakEvidencePath `
        -NativeCapture $probeCapture
    Assert-Equal -Actual $leakEvidence.processId -Expected 77 -Description 'Leak preflight evidence process ID'
    if (-not (Test-Path -LiteralPath $leakEvidencePath -PathType Leaf)) {
        throw 'Leak preflight did not write its isolated evidence file.'
    }
}
finally {
    if (Test-Path -LiteralPath $leafRoot) {
        Remove-Item -LiteralPath $leafRoot -Recurse -Force
    }
}

$scenarioNames = @(Get-DynamicLeakScenarioName)
Assert-Equal -Actual ($scenarioNames -join ',') -Expected 'small-success,malformed,cancellation,read-failure,write-failure,large-metadata,sparse-metadata' -Description 'Leak scenarios'
$probeArguments = Get-DynamicLeakProbeArgumentList `
    -Mode lifecycle `
    -Scenario sparse-metadata `
    -Warmup 2 `
    -Iterations 3 `
    -Windows 4
Assert-Equal -Actual ($probeArguments -join ' ') -Expected '--mode lifecycle --scenario sparse-metadata --warmup 2 --iterations 3 --windows 4' -Description 'Leak probe arguments'

$ready = 'OBSERVER_LEAK_PROBE|READY|pid=42|mode=operations|configuration=Release|scenarios=read-failure'
$readyEvidence = Assert-DynamicLeakReadyMarker -Line $ready -Mode operations -Scenario read-failure
Assert-Equal -Actual $readyEvidence.ProcessId -Expected 42 -Description 'Leak READY process ID'
Assert-Throw -Description 'READY marker for another scenario' -Action {
    Assert-DynamicLeakReadyMarker -Line $ready -Mode operations -Scenario malformed
}

$probeText = Get-Content -Raw -LiteralPath $probeSource
foreach ($requiredProbeContract in @('--scenario', 'settings.scenario', 'selected_scenario')) {
    if (-not $probeText.Contains($requiredProbeContract, [System.StringComparison]::Ordinal)) {
        throw "Leak probe source is missing exact scenario selection contract: $requiredProbeContract"
    }
}

Write-Output '[OK] Dynamic graph leaf contracts passed.'

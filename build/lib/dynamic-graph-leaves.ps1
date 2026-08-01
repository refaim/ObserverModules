#requires -Version 7.4

[CmdletBinding()]
param(
    [Parameter()][ValidateSet('', 'fuzz', 'leak', 'audit', 'package')][string] $Leaf = '',
    [Parameter()][string] $Phase,
    [Parameter()][ValidateSet('x86', 'x64', 'arm64')][string] $Architecture = 'x64',
    [Parameter()][ValidateSet('pickle', 'renpy', 'rpgmaker', 'zanzarah')][string] $TargetName = 'pickle',
    [Parameter()][ValidateSet('operations', 'lifecycle')][string] $Mode = 'operations',
    [Parameter()][ValidateSet(
        'small-success',
        'malformed',
        'cancellation',
        'read-failure',
        'write-failure',
        'large-metadata',
        'sparse-metadata'
    )][string] $Scenario = 'small-success',
    [Parameter()][ValidateRange(1, 1000000)][int] $Warmup = 8,
    [Parameter()][ValidateRange(1, 1000000)][int] $Iterations = 100,
    [Parameter()][ValidateRange(3, 10)][int] $Windows = 3,
    [Parameter()][ValidateRange(1, 86400)][int] $Seconds = 60,
    [Parameter()][string] $Label,
    [Parameter()][string] $RunId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$null = $Warmup, $Iterations, $Windows, $Label

$script:DynamicFuzzTargetSpecs = @(
    [pscustomobject]@{ Name = 'pickle'; MaxLength = 262144 },
    [pscustomobject]@{ Name = 'renpy'; MaxLength = 1048576 },
    [pscustomobject]@{ Name = 'rpgmaker'; MaxLength = 1048576 },
    [pscustomobject]@{ Name = 'zanzarah'; MaxLength = 1048576 }
)
$script:DynamicLeakScenarioNames = @(
    'small-success',
    'malformed',
    'cancellation',
    'read-failure',
    'write-failure',
    'large-metadata',
    'sparse-metadata'
)
$script:DynamicRepositoryRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent

function Assert-DynamicRunId {
    param([Parameter(Mandatory)][string] $RunId)

    if ($RunId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') {
        throw "Dynamic run ID must be a safe 1-128 character path component: '$RunId'."
    }
}

function Get-DynamicFuzzTargetSpec {
    param([Parameter()][string] $TargetName)

    if (-not $TargetName) {
        return $script:DynamicFuzzTargetSpecs
    }
    $result = @($script:DynamicFuzzTargetSpecs | Where-Object Name -CEQ $TargetName)
    if ($result.Count -ne 1) {
        throw "Unknown dynamic fuzz target: $TargetName"
    }
    return $result[0]
}

function Get-DynamicFuzzArgumentList {
    param(
        [Parameter(Mandatory)][ValidateSet('seed-replay', 'timed')][string] $Phase,
        [Parameter(Mandatory)][string] $TargetName,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string[]] $InputPath,
        [Parameter(Mandatory)][string] $ArtifactDirectory,
        [Parameter()] $Seconds
    )

    $spec = Get-DynamicFuzzTargetSpec -TargetName $TargetName
    if ($InputPath.Count -eq 0 -or @($InputPath | Where-Object { [string]::IsNullOrWhiteSpace($_) }).Count -ne 0) {
        throw 'Dynamic fuzz input paths must be non-empty.'
    }
    if ([string]::IsNullOrWhiteSpace($ArtifactDirectory)) {
        throw 'Dynamic fuzz artifact directory must be non-empty.'
    }

    $artifactPrefix = $ArtifactDirectory.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $arguments = [System.Collections.Generic.List[string]]::new()
    foreach ($inputPathItem in $InputPath) {
        $arguments.Add($inputPathItem)
    }
    $arguments.Add("-max_len=$($spec.MaxLength)")
    $arguments.Add('-rss_limit_mb=1024')
    $arguments.Add('-timeout=10')
    $arguments.Add('-print_final_stats=1')
    $arguments.Add("-artifact_prefix=$artifactPrefix")
    if ($Phase -eq 'timed') {
        if ($null -eq $Seconds -or $Seconds -is [bool] -or $Seconds -isnot [int] -or $Seconds -lt 1 -or $Seconds -gt 86400) {
            throw 'Timed fuzzing requires Seconds from 1 to 86400.'
        }
        if ($InputPath.Count -ne 1) {
            throw 'Timed fuzzing requires exactly one writable corpus directory.'
        }
        $arguments.Add("-max_total_time=$Seconds")
        $arguments.Add('-use_value_profile=1')
    }
    return ,$arguments.ToArray()
}

function Initialize-DynamicFuzzCorpus {
    param(
        [Parameter(Mandatory)][string] $SeedDirectory,
        [Parameter(Mandatory)][string] $CorpusDirectory
    )

    $resolvedSeedDirectory = [System.IO.Path]::GetFullPath($SeedDirectory)
    $resolvedCorpusDirectory = [System.IO.Path]::GetFullPath($CorpusDirectory)
    if (-not (Test-Path -LiteralPath $resolvedSeedDirectory -PathType Container)) {
        throw "Fuzzer seed directory was not found: $resolvedSeedDirectory"
    }
    if ($resolvedCorpusDirectory.Equals($resolvedSeedDirectory, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Fuzzer seed and corpus directories must be different.'
    }

    $plans = [System.Collections.Generic.List[object]]::new()
    $destinations = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($seed in @(Get-ChildItem -LiteralPath $resolvedSeedDirectory -File | Sort-Object Name)) {
        if (($seed.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Fuzzer seed must not be a reparse point: $($seed.FullName)"
        }
        if ($seed.Length -gt 16MB) {
            throw "Fuzzer seed exceeds the 16 MiB preparation bound: $($seed.FullName)"
        }

        $destinationName = if ($seed.Extension -CEQ '.hex') { $seed.BaseName } else { $seed.Name }
        if (-not $destinations.Add($destinationName)) {
            throw "Fuzzer seeds map to a duplicate corpus name: $destinationName"
        }
        $bytes = if ($seed.Extension -CEQ '.hex') {
            $hex = (Get-Content -Raw -LiteralPath $seed.FullName) -replace '\s', ''
            if ($hex.Length -eq 0 -or $hex.Length % 2 -ne 0 -or $hex -notmatch '^[0-9A-Fa-f]+$') {
                throw "Invalid hexadecimal fuzzer seed: $($seed.FullName)"
            }
            [Convert]::FromHexString($hex)
        }
        else {
            [System.IO.File]::ReadAllBytes($seed.FullName)
        }
        $plans.Add([pscustomobject]@{ Name = $destinationName; Bytes = $bytes })
    }
    if ($plans.Count -eq 0) {
        throw "No checked-in fuzzer seeds were found in: $resolvedSeedDirectory"
    }

    New-Item -ItemType Directory -Force -Path $resolvedCorpusDirectory | Out-Null
    foreach ($plan in $plans) {
        [System.IO.File]::WriteAllBytes((Join-Path $resolvedCorpusDirectory $plan.Name), $plan.Bytes)
    }
    return @($plans | ForEach-Object { Join-Path $resolvedCorpusDirectory $_.Name })
}

function Write-DynamicLeafJson {
    param(
        [Parameter(Mandatory)] $Value,
        [Parameter(Mandatory)][string] $Path
    )

    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $parent = [System.IO.Path]::GetDirectoryName($resolvedPath)
    if ([string]::IsNullOrEmpty($parent)) {
        throw "Dynamic leaf evidence path has no parent: $resolvedPath"
    }
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporaryPath = "$resolvedPath.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $json = $Value | ConvertTo-Json -Depth 8
        [System.IO.File]::WriteAllText($temporaryPath, $json, [System.Text.UTF8Encoding]::new($false))
        [System.IO.File]::Move($temporaryPath, $resolvedPath, $true)
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

function Invoke-DynamicNativeProcess {
    param(
        [Parameter(Mandatory)][string] $FilePath,
        [Parameter(Mandatory)][string[]] $Arguments,
        [Parameter(Mandatory)][string] $WorkingDirectory,
        [Parameter(Mandatory)][string] $LogPath
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.Environment['ASAN_OPTIONS'] = 'halt_on_error=1:alloc_dealloc_mismatch=1'
    foreach ($argument in $Arguments) {
        $startInfo.ArgumentList.Add($argument)
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "Failed to start dynamic native leaf: $FilePath"
        }
        $standardOutput = $process.StandardOutput.ReadToEndAsync()
        $standardError = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $combinedOutput = $standardOutput.GetAwaiter().GetResult() + $standardError.GetAwaiter().GetResult()
        $logParent = [System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($LogPath))
        New-Item -ItemType Directory -Force -Path $logParent | Out-Null
        [System.IO.File]::WriteAllText($LogPath, $combinedOutput, [System.Text.UTF8Encoding]::new($false))
        return $process.ExitCode
    }
    finally {
        $process.Dispose()
    }
}

function Invoke-DynamicFuzzLeaf {
    param(
        [Parameter(Mandatory)][ValidateSet('seed-replay', 'timed')][string] $Phase,
        [Parameter(Mandatory)][string] $TargetName,
        [Parameter(Mandatory)][string] $FuzzerPath,
        [Parameter(Mandatory)][string] $SeedDirectory,
        [Parameter(Mandatory)][string] $CorpusDirectory,
        [Parameter(Mandatory)][string] $RunDirectory,
        [Parameter(Mandatory)][string] $WorkingDirectory,
        [Parameter(Mandatory)][ValidateRange(1, 86400)][int] $Seconds,
        [Parameter()][scriptblock] $NativeInvoker = {
            param($FilePath, $Arguments, $WorkingDirectory, $LogPath)
            Invoke-DynamicNativeProcess `
                -FilePath $FilePath `
                -Arguments $Arguments `
                -WorkingDirectory $WorkingDirectory `
                -LogPath $LogPath
        }
    )

    $resolvedFuzzer = [System.IO.Path]::GetFullPath($FuzzerPath)
    $resolvedWorkingDirectory = [System.IO.Path]::GetFullPath($WorkingDirectory)
    $resolvedRunDirectory = [System.IO.Path]::GetFullPath($RunDirectory)
    if (-not (Test-Path -LiteralPath $resolvedFuzzer -PathType Leaf)) {
        throw "Fuzzer executable was not found: $resolvedFuzzer"
    }
    if (-not (Test-Path -LiteralPath $resolvedWorkingDirectory -PathType Container)) {
        throw "Fuzzer working directory was not found: $resolvedWorkingDirectory"
    }
    if (Test-Path -LiteralPath $resolvedRunDirectory) {
        throw "Dynamic fuzz run directory already exists: $resolvedRunDirectory"
    }
    New-Item -ItemType Directory -Path $resolvedRunDirectory | Out-Null
    $artifactDirectory = Join-Path $resolvedRunDirectory 'artifacts'
    New-Item -ItemType Directory -Path $artifactDirectory | Out-Null

    $inputs = @(
        if ($Phase -eq 'seed-replay') {
            $replayDirectory = Join-Path $resolvedRunDirectory 'inputs'
            Initialize-DynamicFuzzCorpus -SeedDirectory $SeedDirectory -CorpusDirectory $replayDirectory |
                Sort-Object
        }
        else {
            [void](Initialize-DynamicFuzzCorpus -SeedDirectory $SeedDirectory -CorpusDirectory $CorpusDirectory)
            [System.IO.Path]::GetFullPath($CorpusDirectory)
        }
    )
    $argumentParameters = @{
        Phase = $Phase
        TargetName = $TargetName
        InputPath = $inputs
        ArtifactDirectory = $artifactDirectory
    }
    if ($Phase -eq 'timed') {
        $argumentParameters.Seconds = $Seconds
    }
    $arguments = Get-DynamicFuzzArgumentList @argumentParameters
    $logPath = Join-Path $resolvedRunDirectory 'native.log'
    $exitCode = & $NativeInvoker $resolvedFuzzer $arguments $resolvedWorkingDirectory $logPath
    if ($exitCode -is [bool] -or $exitCode -isnot [int] -or $exitCode -ne 0) {
        throw "Dynamic fuzz leaf failed for $TargetName $Phase with exit code '$exitCode'. Log: $logPath"
    }

    $evidence = [ordered]@{
        phase = $Phase
        target = $TargetName
        inputCount = $inputs.Count
        log = $logPath
        passed = $true
    }
    Write-DynamicLeafJson -Value $evidence -Path (Join-Path $resolvedRunDirectory 'result.json')
    return [pscustomobject]$evidence
}

function Get-DynamicLeakScenarioName {
    return $script:DynamicLeakScenarioNames
}

function Assert-DynamicLeakScenario {
    param([Parameter(Mandatory)][string] $Scenario)

    if ($Scenario -cnotin $script:DynamicLeakScenarioNames) {
        throw "Unknown leak scenario: $Scenario"
    }
}

function Get-DynamicLeakProbeArgumentList {
    param(
        [Parameter(Mandatory)][ValidateSet('operations', 'lifecycle')][string] $Mode,
        [Parameter(Mandatory)][string] $Scenario,
        [Parameter(Mandatory)][ValidateRange(1, 1000000)][int] $Warmup,
        [Parameter(Mandatory)][ValidateRange(1, 1000000)][int] $Iterations,
        [Parameter(Mandatory)][ValidateRange(3, 10)][int] $Windows,
        [Parameter()][switch] $Automatic
    )

    Assert-DynamicLeakScenario -Scenario $Scenario
    $arguments = [System.Collections.Generic.List[string]]::new()
    if ($Automatic) {
        $arguments.Add('--automatic')
    }
    foreach ($argument in @(
            '--mode', $Mode,
            '--scenario', $Scenario,
            '--warmup', [string]$Warmup,
            '--iterations', [string]$Iterations,
            '--windows', [string]$Windows
        )) {
        $arguments.Add($argument)
    }
    return ,$arguments.ToArray()
}

function Assert-DynamicLeakReadyMarker {
    param(
        [Parameter(Mandatory)][string] $Line,
        [Parameter(Mandatory)][ValidateSet('operations', 'lifecycle')][string] $Mode,
        [Parameter(Mandatory)][string] $Scenario
    )

    Assert-DynamicLeakScenario -Scenario $Scenario
    $pattern = '^OBSERVER_LEAK_PROBE\|READY\|pid=([1-9][0-9]*)\|mode=' +
        [regex]::Escape($Mode) + '\|configuration=Release\|scenarios=' + [regex]::Escape($Scenario) + '$'
    if ($Line -cnotmatch $pattern) {
        throw "Leak probe READY marker does not match mode '$Mode' and scenario '$Scenario'."
    }
    return [pscustomobject]@{
        ProcessId = [int]$Matches[1]
        Mode = $Mode
        Scenario = $Scenario
    }
}

function Invoke-DynamicLeakPreflightLeaf {
    param(
        [Parameter(Mandatory)][string] $ProbePath,
        [Parameter(Mandatory)][string] $BinaryDirectory,
        [Parameter(Mandatory)][ValidateSet('operations', 'lifecycle')][string] $Mode,
        [Parameter(Mandatory)][string] $Scenario,
        [Parameter(Mandatory)][string] $EvidencePath,
        [Parameter()][scriptblock] $NativeCapture = {
            param($FilePath, $Arguments, $WorkingDirectory)
            $null = $WorkingDirectory
            $output = & $FilePath @Arguments 2>&1
            if ($LASTEXITCODE -ne 0) {
                throw "Leak preflight failed with exit code $LASTEXITCODE."
            }
            return @($output | ForEach-Object ToString)
        }
    )

    $resolvedProbe = [System.IO.Path]::GetFullPath($ProbePath)
    $resolvedBinaryDirectory = [System.IO.Path]::GetFullPath($BinaryDirectory)
    if (-not (Test-Path -LiteralPath $resolvedProbe -PathType Leaf)) {
        throw "Leak probe was not found: $resolvedProbe"
    }
    if (-not (Test-Path -LiteralPath $resolvedBinaryDirectory -PathType Container)) {
        throw "Leak probe binary directory was not found: $resolvedBinaryDirectory"
    }
    $arguments = Get-DynamicLeakProbeArgumentList `
        -Mode $Mode `
        -Scenario $Scenario `
        -Warmup 1 `
        -Iterations 1 `
        -Windows 3 `
        -Automatic
    $output = @(& $NativeCapture $resolvedProbe $arguments $resolvedBinaryDirectory)
    $errors = @($output | Where-Object { $_.StartsWith('OBSERVER_LEAK_PROBE|ERROR|', [StringComparison]::Ordinal) })
    if ($errors.Count -ne 0) {
        throw "Leak preflight reported an error: $($errors -join '; ')"
    }
    $readyLines = @($output | Where-Object { $_.StartsWith('OBSERVER_LEAK_PROBE|READY|', [StringComparison]::Ordinal) })
    if ($readyLines.Count -ne 1) {
        throw "Leak preflight expected one READY marker, received $($readyLines.Count)."
    }
    $ready = Assert-DynamicLeakReadyMarker -Line $readyLines[0] -Mode $Mode -Scenario $Scenario
    $donePattern = '^OBSERVER_LEAK_PROBE\|DONE\|pid=' + $ready.ProcessId + '\|completed_operations=([1-9][0-9]*)$'
    $doneLines = @($output | Where-Object { $_ -cmatch $donePattern })
    if ($doneLines.Count -ne 1) {
        throw "Leak preflight expected one matching DONE marker, received $($doneLines.Count)."
    }

    $evidence = [ordered]@{
        mode = $Mode
        scenario = $Scenario
        processId = $ready.ProcessId
        passed = $true
    }
    Write-DynamicLeafJson -Value $evidence -Path $EvidencePath
    return [pscustomobject]$evidence
}

if ($Leaf -eq 'fuzz') {
    if ($Architecture -ne 'x64') {
        throw 'Dynamic libFuzzer leaves are intentionally x64-only.'
    }
    if ($Phase -notin @('seed-replay', 'timed')) {
        throw "Unknown dynamic fuzz phase: '$Phase'."
    }
    Assert-DynamicRunId -RunId $RunId
    $binaryDirectory = Join-Path $script:DynamicRepositoryRoot '.artifacts\bin\x64\Fuzz'
    $targetRoot = Join-Path $script:DynamicRepositoryRoot ".artifacts\fuzz\x64\$TargetName"
    $runDirectory = Join-Path $script:DynamicRepositoryRoot ".artifacts\fuzz-runs\$RunId\x64\$TargetName\$Phase"
    Invoke-DynamicFuzzLeaf `
        -Phase $Phase `
        -TargetName $TargetName `
        -FuzzerPath (Join-Path $binaryDirectory "fuzz-$TargetName.exe") `
        -SeedDirectory (Join-Path $script:DynamicRepositoryRoot "src\fuzz\corpus\$TargetName") `
        -CorpusDirectory (Join-Path $targetRoot 'corpus') `
        -RunDirectory $runDirectory `
        -WorkingDirectory $binaryDirectory `
        -Seconds $Seconds | Out-Null
}
elseif ($Leaf -eq 'leak' -and $Phase -eq 'preflight') {
    if ($Architecture -ne 'x64') {
        throw 'Dynamic UMDH leak leaves are intentionally x64-only.'
    }
    $binaryDirectory = Join-Path $script:DynamicRepositoryRoot '.artifacts\bin\x64\Release'
    $evidencePath = Join-Path $script:DynamicRepositoryRoot ".artifacts\reports\leaks\x64\preflight\$Mode\$Scenario.json"
    Invoke-DynamicLeakPreflightLeaf `
        -ProbePath (Join-Path $binaryDirectory 'leak-probe.exe') `
        -BinaryDirectory $binaryDirectory `
        -Mode $Mode `
        -Scenario $Scenario `
        -EvidencePath $evidencePath | Out-Null
}
elseif ($Leaf) {
    throw "Dynamic leaf '$Leaf' phase '$Phase' is not wired to native execution yet."
}

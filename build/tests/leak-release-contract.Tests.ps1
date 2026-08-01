#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$namespace = [System.Xml.XmlNamespaceManager]::new([System.Xml.NameTable]::new())
$namespace.AddNamespace('msb', 'http://schemas.microsoft.com/developer/msbuild/2003')

function Read-MSBuildProject {
    param([Parameter(Mandatory)][string] $Path)

    [xml] $project = Get-Content -Raw -LiteralPath $Path
    return $project
}

function Assert-ContainsExactly {
    param(
        [Parameter(Mandatory)][string[]] $Actual,
        [Parameter(Mandatory)][string[]] $Expected,
        [Parameter(Mandatory)][string] $Description
    )

    $difference = @(Compare-Object -ReferenceObject $Expected -DifferenceObject $Actual)
    if ($difference.Count -ne 0) {
        throw "$Description differs from the required Release contract."
    }
}

$probeProject = Read-MSBuildProject -Path (Join-Path $repositoryRoot 'build\projects\leak-probe.vcxproj')
$runtimeLibraries = @(
    $probeProject.SelectNodes('//msb:RuntimeLibrary', $namespace) |
        ForEach-Object InnerText |
        Sort-Object -Unique
)
Assert-ContainsExactly -Actual $runtimeLibraries -Expected @('MultiThreaded') -Description 'Leak probe runtime library'

$dependencies = @(
    $probeProject.SelectNodes('//msb:AdditionalDependencies', $namespace) |
        ForEach-Object InnerText
)
if ($dependencies -notcontains 'zs.lib;%(AdditionalDependencies)') {
    throw 'Leak probe must link the Release zlib library.'
}
if ($dependencies -match 'zsd\.lib') {
    throw 'Leak probe must not link a Debug zlib library.'
}

$validationError = $probeProject.SelectSingleNode(
    '//msb:Target[@Name="ValidateLeakProbeConfiguration"]/msb:Error',
    $namespace
)
if ($null -eq $validationError -or $validationError.Condition -notmatch 'Release\|x64') {
    throw 'Leak probe validation must accept only Release|x64.'
}

$aggregateProject = Read-MSBuildProject -Path (Join-Path $repositoryRoot 'build\ObserverModules.proj')
$aggregateValidation = $aggregateProject.SelectSingleNode(
    '//msb:Target[@Name="BuildLeakProbe"]/msb:Error',
    $namespace
)
if ($null -eq $aggregateValidation -or $aggregateValidation.Condition -notmatch 'Release\|x64') {
    throw 'BuildLeakProbe must accept only Release|x64.'
}

$orchestrationFiles = @(
    Get-Item -LiteralPath (Join-Path $repositoryRoot 'build\build.ps1')
    Get-ChildItem -LiteralPath (Join-Path $repositoryRoot 'build\lib') -File -Filter '*.ps1' | Sort-Object Name
)
foreach ($requiredPattern in @(
        "Invoke-MSBuild -Target 'BuildLeakProbe' -Architecture 'x64' -Configuration 'Release'",
        "Get-BinaryDirectory -Architecture 'x64' -Configuration 'Release'",
        "Assert-ReleaseBinary -Architecture 'x64'"
    )) {
    $matchingFiles = @(
        $orchestrationFiles |
            Where-Object {
                (Get-Content -Raw -LiteralPath $_.FullName).Contains($requiredPattern, [StringComparison]::Ordinal)
            }
    )
    if ($matchingFiles.Count -ne 1) {
        throw "Leak orchestration is missing the Release evidence step: $requiredPattern"
    }
}

Write-Host '[OK] Leak probe is constrained to the shipping x64 Release /MT configuration.'

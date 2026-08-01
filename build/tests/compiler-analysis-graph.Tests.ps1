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

$aggregateProject = Read-MSBuildProject -Path (Join-Path $repositoryRoot 'build\ObserverModules.proj')
$analysisProjects = @($aggregateProject.SelectNodes('//msb:AnalysisProject', $namespace))
$expectedManifest = @(
    'renpy.vcxproj|renpy|Debug|Win32;x64;ARM64',
    'rpgmaker.vcxproj|rpgmaker|Debug|Win32;x64;ARM64',
    'zanzarah.vcxproj|zanzarah|Debug|Win32;x64;ARM64',
    'tests.vcxproj|tests|Debug|Win32;x64;ARM64',
    'fuzz-pickle.vcxproj|fuzz-pickle|Debug|Win32;x64;ARM64',
    'fuzz-renpy.vcxproj|fuzz-renpy|Debug|Win32;x64;ARM64',
    'fuzz-rpgmaker.vcxproj|fuzz-rpgmaker|Debug|Win32;x64;ARM64',
    'fuzz-zanzarah.vcxproj|fuzz-zanzarah|Debug|Win32;x64;ARM64',
    'leak-probe.vcxproj|leak-probe|Release|x64'
)
$actualManifest = @(
    $analysisProjects |
        ForEach-Object {
            '{0}|{1}|{2}|{3}' -f `
                [System.IO.Path]::GetFileName($_.Include), `
                $_.AnalysisReportName, `
                $_.AnalysisConfiguration, `
                $_.AnalysisPlatforms
        }
)
Assert-SequenceEqual -Actual $actualManifest -Expected $expectedManifest -Description 'Compiler-analysis project/report manifest'

$reportNames = @($analysisProjects | ForEach-Object AnalysisReportName)
if (@($reportNames | Sort-Object -Unique).Count -ne $reportNames.Count) {
    throw 'Compiler-analysis report names must be globally unique.'
}

$expectedReportsByPlatform = @{
    Win32 = @(
        'fuzz-pickle.sarif',
        'fuzz-renpy.sarif',
        'fuzz-rpgmaker.sarif',
        'fuzz-zanzarah.sarif',
        'renpy.sarif',
        'rpgmaker.sarif',
        'tests.sarif',
        'zanzarah.sarif'
    )
    x64 = @(
        'fuzz-pickle.sarif',
        'fuzz-renpy.sarif',
        'fuzz-rpgmaker.sarif',
        'fuzz-zanzarah.sarif',
        'leak-probe.sarif',
        'renpy.sarif',
        'rpgmaker.sarif',
        'tests.sarif',
        'zanzarah.sarif'
    )
    ARM64 = @(
        'fuzz-pickle.sarif',
        'fuzz-renpy.sarif',
        'fuzz-rpgmaker.sarif',
        'fuzz-zanzarah.sarif',
        'renpy.sarif',
        'rpgmaker.sarif',
        'tests.sarif',
        'zanzarah.sarif'
    )
}
foreach ($platform in @('Win32', 'x64', 'ARM64')) {
    $actualReports = @(
        $analysisProjects |
            Where-Object { $platform -in @($_.AnalysisPlatforms -split ';') } |
            ForEach-Object { "$($_.AnalysisReportName).sarif" } |
            Sort-Object
    )
    Assert-SequenceEqual `
        -Actual $actualReports `
        -Expected $expectedReportsByPlatform[$platform] `
        -Description "$platform compiler-analysis report manifest"
}

$analysisTarget = $aggregateProject.SelectSingleNode('//msb:Target[@Name="RunCompilerAnalysis"]', $namespace)
if ($null -eq $analysisTarget) {
    throw 'The aggregate project must expose a RunCompilerAnalysis target.'
}
$activeProject = $analysisTarget.SelectSingleNode('msb:ItemGroup/msb:ActiveAnalysisProject', $namespace)
if (
    $null -eq $activeProject -or
    $activeProject.Include -ne '@(AnalysisProject)' -or
    $activeProject.Condition -notmatch 'AnalysisPlatforms' -or
    $activeProject.Condition -notmatch '\$\(Platform\)'
) {
    throw 'RunCompilerAnalysis must filter the explicit manifest by MSBuild platform.'
}
$analysisBuild = $analysisTarget.SelectSingleNode('msb:MSBuild', $namespace)
$expectedProperties = '$(ProjectProperties);Configuration=%(ActiveAnalysisProject.AnalysisConfiguration);ObserverAnalysisReportName=%(ActiveAnalysisProject.AnalysisReportName);ObserverCompileAnalysis=true;ForceRebuild=true'
if (
    $null -eq $analysisBuild -or
    $analysisBuild.Projects -ne '@(ActiveAnalysisProject)' -or
    $analysisBuild.Targets -ne 'ClCompile' -or
    $analysisBuild.BuildInParallel -ne 'true' -or
    $analysisBuild.GetAttribute('ContinueOnError') -ne 'ErrorAndContinue' -or
    $analysisBuild.Properties -ne $expectedProperties
) {
    throw 'RunCompilerAnalysis must analyze every project, retain failures, and avoid linking.'
}

$expectedPlatformMonikers = @(
    "'`$(Platform)' == 'Win32'|x86",
    "'`$(Platform)' == 'x64'|x64",
    "'`$(Platform)' == 'ARM64'|arm64"
)
$actualPlatformMonikers = @(
    $aggregateProject.SelectNodes('//msb:AnalysisPlatformMoniker', $namespace) |
        ForEach-Object { "$($_.Condition)|$($_.InnerText)" }
)
Assert-SequenceEqual `
    -Actual $actualPlatformMonikers `
    -Expected $expectedPlatformMonikers `
    -Description 'Compiler-analysis report platform mapping'
$expectedReportProperties = @(
    'AnalysisExpectedReportRoot|$([System.IO.Path]::GetFullPath(''$(MSBuildThisFileDirectory)..\.artifacts\reports\msvc\''))',
    'AnalysisRequestedReportRoot|$([System.IO.Path]::GetFullPath(''$(ObserverAnalysisReportDirectory)\''))',
    'AnalysisReportArchDirectory|$([System.IO.Path]::GetFullPath(''$(AnalysisRequestedReportRoot)$(AnalysisPlatformMoniker)\''))'
)
$actualReportProperties = @(
    foreach ($propertyName in @('AnalysisExpectedReportRoot', 'AnalysisRequestedReportRoot', 'AnalysisReportArchDirectory')) {
        $property = $aggregateProject.SelectSingleNode("//msb:$propertyName", $namespace)
        if ($null -eq $property) {
            "${propertyName}|"
        } else {
            "${propertyName}|$($property.InnerText)"
        }
    }
)
Assert-SequenceEqual `
    -Actual $actualReportProperties `
    -Expected $expectedReportProperties `
    -Description 'Compiler-analysis report path contract'
$pathValidationError = $analysisTarget.SelectSingleNode('msb:Error[contains(@Condition, "AnalysisExpectedReportRoot")]', $namespace)
$reparseValidationError = $analysisTarget.SelectSingleNode('msb:Error[contains(@Condition, "ReparsePoint")]', $namespace)
$validatedPaths = @(
    $analysisTarget.SelectNodes('msb:ItemGroup/msb:AnalysisReportPathToValidate[@Include]', $namespace) |
        ForEach-Object Include
)
Assert-SequenceEqual `
    -Actual $validatedPaths `
    -Expected @(
        '$(MSBuildThisFileDirectory)..\.artifacts',
        '$(MSBuildThisFileDirectory)..\.artifacts\reports',
        '$(AnalysisExpectedReportRoot)',
        '$(AnalysisReportArchDirectory)'
    ) `
    -Description 'Compiler-analysis report paths validated before cleanup'
if (
    $null -eq $pathValidationError -or
    $null -eq $reparseValidationError
) {
    throw 'RunCompilerAnalysis must reject an unexpected report root and reparse-point cleanup paths.'
}
$pathAttributesUpdate = $analysisTarget.SelectSingleNode(
    'msb:ItemGroup/msb:AnalysisReportPathToValidate[@Update="@(AnalysisReportPathToValidate)"]/msb:PathAttributes',
    $namespace
)
if (
    $null -eq $pathAttributesUpdate -or
    $pathAttributesUpdate.InnerText -ne "$([char]36)([System.IO.File]::GetAttributes('%(AnalysisReportPathToValidate.FullPath)'))"
) {
    throw 'Reparse validation must populate path attributes only after every path item has a full identity.'
}
$staleReports = $analysisTarget.SelectSingleNode('msb:ItemGroup/msb:ExistingAnalysisReport', $namespace)
$deleteReports = $analysisTarget.SelectSingleNode('msb:Delete', $namespace)
if (
    $null -eq $staleReports -or
    $staleReports.Include -ne '$(AnalysisReportArchDirectory)*.sarif' -or
    $null -eq $deleteReports -or
    $deleteReports.Files -ne '@(ExistingAnalysisReport)'
) {
    throw 'RunCompilerAnalysis must remove stale architecture SARIF before producing the exact manifest.'
}

$rebuildTarget = $aggregateProject.SelectSingleNode('//msb:Target[@Name="Rebuild"]', $namespace)
$analysisDispatch = $rebuildTarget.SelectSingleNode('msb:CallTarget[@Targets="RunCompilerAnalysis"]', $namespace)
if ($null -eq $analysisDispatch -or $analysisDispatch.Condition -ne "'`$(ObserverRunCodeAnalysis)' == 'true'") {
    throw 'Aggregate Rebuild must dispatch the compiler-analysis graph only when explicitly requested.'
}
$binaryRebuild = $rebuildTarget.SelectSingleNode('msb:MSBuild', $namespace)
if ($null -eq $binaryRebuild -or $binaryRebuild.Condition -ne "'`$(ObserverRunCodeAnalysis)' != 'true'") {
    throw 'Ordinary aggregate Rebuild must retain the module/test binary graph.'
}

$projectProperties = Read-MSBuildProject -Path (Join-Path $repositoryRoot 'build\ObserverProject.props')
$reportNameDefault = $projectProperties.SelectSingleNode('//msb:ObserverAnalysisReportName', $namespace)
if (
    $null -eq $reportNameDefault -or
    $reportNameDefault.InnerText -ne '$(ProjectName)' -or
    $reportNameDefault.Condition -ne "'`$(ObserverAnalysisReportName)' == ''"
) {
    throw 'Analysis reports must default to the unique MSBuild project name.'
}
$prefastLog = $projectProperties.SelectSingleNode('//msb:ClCompile/msb:PREfastLog', $namespace)
if (
    $null -eq $prefastLog -or
    $prefastLog.InnerText -ne '$(ObserverAnalysisReportDirectory)\$(PlatformMoniker)\$(ObserverAnalysisReportName).sarif'
) {
    throw 'PREfastLog must use the explicit per-project report name.'
}

$fuzzProperties = Read-MSBuildProject -Path (Join-Path $repositoryRoot 'build\ObserverFuzz.props')
$fuzzValidation = $fuzzProperties.SelectSingleNode('//msb:Target[@Name="ValidateFuzzConfiguration"]/msb:Error', $namespace)
$expectedFuzzCondition = "'`$(ObserverCompileAnalysis)' != 'true' And '`$(Configuration)|`$(Platform)' != 'Fuzz|x64'"
if ($null -eq $fuzzValidation -or $fuzzValidation.Condition -ne $expectedFuzzCondition) {
    throw 'Fuzz validation may be bypassed only by the compile-only analysis graph.'
}

$leakProject = Read-MSBuildProject -Path (Join-Path $repositoryRoot 'build\projects\leak-probe.vcxproj')
$leakValidation = $leakProject.SelectSingleNode(
    '//msb:Target[@Name="ValidateLeakProbeConfiguration"]/msb:Error',
    $namespace
)
if ($null -eq $leakValidation -or $leakValidation.Condition -ne "'`$(Configuration)|`$(Platform)' != 'Release|x64'") {
    throw 'Compile analysis must not weaken the leak probe Release|x64 runtime contract.'
}

Write-Host '[OK] MSVC compile-analysis graph and SARIF manifest cover every first-party target.'

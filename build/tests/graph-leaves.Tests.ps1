#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
. (Join-Path $repositoryRoot 'build\lib\analysis-reporting.ps1')
. (Join-Path $repositoryRoot 'build\lib\graph-leaves.ps1')

function Assert-Equal {
    param(
        [Parameter(Mandatory)][AllowNull()] $Actual,
        [Parameter(Mandatory)][AllowNull()] $Expected,
        [Parameter(Mandatory)][string] $Description
    )

    if (-not [object]::Equals($Actual, $Expected)) {
        throw "${Description}: actual='$Actual', expected='$Expected'."
    }
}

function Assert-ScriptFailure {
    param(
        [Parameter(Mandatory)][scriptblock] $Action,
        [Parameter(Mandatory)][string] $Pattern,
        [Parameter(Mandatory)][string] $Description
    )

    try {
        & $Action
    } catch {
        if ($_.Exception.Message -notmatch $Pattern) {
            throw "${Description}: unexpected error '$($_.Exception.Message)'."
        }
        return
    }
    throw "${Description}: expected an error matching '$Pattern'."
}

$buildRequest = Get-GraphBuildProjectRequest `
    -RepositoryRoot $repositoryRoot `
    -Architecture 'x64' `
    -Configuration 'Debug' `
    -Project 'renpy'
Assert-Equal -Actual $buildRequest.Target -Expected 'Build' -Description 'Native project target'
Assert-Equal -Actual $buildRequest.Platform -Expected 'x64' -Description 'Native project platform'
Assert-Equal `
    -Actual $buildRequest.ProjectPath `
    -Expected (Join-Path $repositoryRoot 'build\projects\renpy.vcxproj') `
    -Description 'Allowlisted native project path'

Assert-ScriptFailure `
    -Action { Get-GraphBuildProjectRequest -RepositoryRoot $repositoryRoot -Architecture x64 -Configuration Debug -Project '..\renpy' } `
    -Pattern 'not allowlisted' `
    -Description 'Project traversal rejection'

$selectedSource = 'src/core/io/bounded_stream.cpp'
$unitSlug = Get-GraphTranslationUnitSlug -Source $selectedSource
Assert-Equal -Actual $unitSlug -Expected 'core-io-bounded-stream-bc188848' -Description 'Stable TU slug'

$tidyRequest = Get-GraphAnalysisRequest `
    -RepositoryRoot $repositoryRoot `
    -Architecture 'x64' `
    -Backend 'clang-tidy' `
    -Project 'renpy' `
    -SelectedFile $selectedSource `
    -Unit $unitSlug
Assert-Equal -Actual $tidyRequest.Target -Expected 'ClCompile' -Description 'Selected-file target'
Assert-Equal -Actual $tidyRequest.Configuration -Expected 'Debug' -Description 'Selected-file configuration'
Assert-Equal `
    -Actual $tidyRequest.Properties.SelectedFiles `
    -Expected (Join-Path $repositoryRoot 'src\core\io\bounded_stream.cpp') `
    -Description 'SelectedFiles exact MSBuild seam'
Assert-Equal -Actual $tidyRequest.Properties.SelectedFilesBuildPCH -Expected 'false' -Description 'Selected-file PCH isolation'
Assert-Equal -Actual $tidyRequest.Properties.SelectedFilesBuildModules -Expected 'false' -Description 'Selected-file module isolation'
Assert-Equal -Actual $tidyRequest.Properties.EnableMicrosoftCodeAnalysis -Expected 'false' -Description 'Tidy excludes PREfast'
Assert-Equal -Actual $tidyRequest.Properties.ObserverEnableClangTidy -Expected 'true' -Description 'Tidy enabled'
Assert-Equal `
    -Actual $tidyRequest.Properties.IntDir `
    -Expected (Join-Path $repositoryRoot ".artifacts\analysis\clang-tidy\x64\renpy\$unitSlug\obj\") `
    -Description 'Tidy isolated IntDir'

Assert-ScriptFailure `
    -Action {
        Get-GraphAnalysisRequest `
            -RepositoryRoot $repositoryRoot `
            -Architecture x64 `
            -Backend clang-tidy `
            -Project renpy `
            -SelectedFile src/tests/main.cpp `
            -Unit tests-main
    } `
    -Pattern 'not a ClCompile Include item' `
    -Description 'Cross-project selected file rejection'

$msvcSelectedSource = 'src/modules/renpy/pickle.cpp'
$msvcUnitSlug = Get-GraphTranslationUnitSlug -Source $msvcSelectedSource
$msvcRequest = Get-GraphAnalysisRequest `
    -RepositoryRoot $repositoryRoot `
    -Architecture 'x64' `
    -Backend 'msvc' `
    -Project 'renpy' `
    -SelectedFile $msvcSelectedSource `
    -Unit $msvcUnitSlug
Assert-Equal -Actual $msvcRequest.Configuration -Expected 'Debug' -Description 'MSVC unit analysis configuration'
Assert-Equal -Actual $msvcRequest.Properties.EnableMicrosoftCodeAnalysis -Expected 'true' -Description 'PREfast enabled'
Assert-Equal -Actual $msvcRequest.Properties.ObserverEnableClangTidy -Expected 'false' -Description 'PREfast excludes tidy'
Assert-Equal `
    -Actual $msvcRequest.Properties.SelectedFiles `
    -Expected (Join-Path $repositoryRoot 'src\modules\renpy\pickle.cpp') `
    -Description 'PREfast SelectedFiles exact seam'
Assert-Equal `
    -Actual $msvcRequest.Properties.IntDir `
    -Expected (Join-Path $repositoryRoot ".artifacts\analysis\msvc\x64\renpy\$msvcUnitSlug\obj\") `
    -Description 'PREfast isolated IntDir'
Assert-Equal `
    -Actual $msvcRequest.Properties.ObserverAnalysisReportPath `
    -Expected (Join-Path $repositoryRoot ".artifacts\analysis\msvc\x64\renpy\$msvcUnitSlug\renpy.sarif") `
    -Description 'PREfast isolated raw report'

$leakSelectedSource = 'src/tests/leaks/probe.cpp'
$leakUnitSlug = Get-GraphTranslationUnitSlug -Source $leakSelectedSource
$leakMsvcRequest = Get-GraphAnalysisRequest `
    -RepositoryRoot $repositoryRoot `
    -Architecture x64 `
    -Backend msvc `
    -Project leak-probe `
    -SelectedFile $leakSelectedSource `
    -Unit $leakUnitSlug
Assert-Equal -Actual $leakMsvcRequest.Configuration -Expected 'Release' -Description 'Leak unit analysis configuration'

Assert-ScriptFailure `
    -Action { Get-GraphAnalysisRequest -RepositoryRoot $repositoryRoot -Architecture x64 -Backend msvc -Project renpy } `
    -Pattern 'requires SelectedFile and Unit' `
    -Description 'Project-wide PREfast rejection'

[xml] $projectProperties = Get-Content -Raw -LiteralPath (Join-Path $repositoryRoot 'build\ObserverProject.props')
$namespace = [System.Xml.XmlNamespaceManager]::new($projectProperties.NameTable)
$namespace.AddNamespace('msb', 'http://schemas.microsoft.com/developer/msbuild/2003')
$isolatedPrefastLog = $projectProperties.SelectSingleNode(
    '//msb:ClCompile/msb:PREfastLog[contains(@Condition, "ObserverAnalysisReportPath")]',
    $namespace
)
if ($null -eq $isolatedPrefastLog -or $isolatedPrefastLog.InnerText -ne '$(ObserverAnalysisReportPath)') {
    throw 'ObserverProject.props must honor the isolated per-analysis-node PREfast path.'
}

$testRoot = Join-Path $repositoryRoot ".artifacts\contract-tests\graph-leaves-$([guid]::NewGuid().ToString('N'))"
try {
    $artifactJunction = Join-Path $testRoot 'artifact-junction'
    New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
    New-Item -ItemType Junction -Path $artifactJunction -Target (Join-Path $repositoryRoot 'src') | Out-Null
    try {
        Assert-ScriptFailure `
            -Action { Resolve-GraphArtifactPath -RepositoryRoot $repositoryRoot -Path $artifactJunction } `
            -Pattern 'reparse point' `
            -Description 'Artifact leaf reparse-point rejection'
    } finally {
        $junctionItem = Get-Item -Force -LiteralPath $artifactJunction
        if (($junctionItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) {
            throw "Refusing to remove a non-reparse test path: $artifactJunction"
        }
        Remove-Item -Force -LiteralPath $artifactJunction
    }

    $sarifRepositoryRoot = Join-Path $testRoot 'repository'
    $projectRoot = Join-Path $sarifRepositoryRoot 'build\projects'
    New-Item -ItemType Directory -Force -Path $projectRoot | Out-Null
    @'
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup>
    <ClCompile Include="$(RepositoryRoot)src\core\io\bounded_stream.cpp" />
    <ClCompile Include="$(RepositoryRoot)src\modules\renpy\pickle.cpp" />
  </ItemGroup>
</Project>
'@ | Set-Content -LiteralPath (Join-Path $projectRoot 'renpy.vcxproj') -Encoding utf8
    @'
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup>
    <ClCompile Include="$(RepositoryRoot)src\modules\rpgmaker\rpgmaker.cpp" />
  </ItemGroup>
</Project>
'@ | Set-Content -LiteralPath (Join-Path $projectRoot 'rpgmaker.vcxproj') -Encoding utf8

    $msvcUnitRoot = Join-Path $sarifRepositoryRoot '.artifacts\reports\msvc\x64\units'
    New-Item -ItemType Directory -Force -Path $msvcUnitRoot | Out-Null
    $rpgmakerUnitSlug = Get-GraphTranslationUnitSlug -Source 'src/modules/rpgmaker/rpgmaker.cpp'
    Assert-ScriptFailure `
        -Action {
            Assert-GraphProjectUnit `
                -RepositoryRoot $sarifRepositoryRoot `
                -Project renpy `
                -Unit $unitSlug.ToUpperInvariant()
        } `
        -Pattern 'not a ClCompile unit' `
        -Description 'Translation-unit slug case rejection'
    $convertedMsvcReports = [System.Collections.Generic.List[string]]::new()
    foreach ($msvcCase in @(
            [pscustomobject]@{ Project = 'renpy'; Unit = $msvcUnitSlug },
            [pscustomobject]@{ Project = 'renpy'; Unit = 'core-io-bounded-stream-bc188848' }
        )) {
        $rawMsvc = Join-Path $sarifRepositoryRoot ".artifacts\analysis\msvc\x64\$($msvcCase.Project)\$($msvcCase.Unit)\$($msvcCase.Project).sarif"
        New-Item -ItemType Directory -Force -Path (Split-Path $rawMsvc -Parent) | Out-Null
        [ordered]@{
            version = '2.1.0'
            runs = @(
                [ordered]@{
                    tool = [ordered]@{ driver = [ordered]@{ name = 'PREfast' } }
                    results = @()
                }
            )
        } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $rawMsvc -Encoding utf8
        $convertedMsvc = Join-Path $msvcUnitRoot "$($msvcCase.Project)-$($msvcCase.Unit).sarif"
        Convert-GraphMsvcSarif `
            -RepositoryRoot $sarifRepositoryRoot `
            -InputPath $rawMsvc `
            -OutputPath $convertedMsvc `
            -Architecture x64 `
            -Project $msvcCase.Project `
            -Unit $msvcCase.Unit
        $convertedMsvcReports.Add($convertedMsvc)
    }
    $convertedMsvcMerged = Join-Path $sarifRepositoryRoot '.artifacts\reports\msvc\x64\msvc-analyze.sarif'
    Merge-GraphSarif `
        -RepositoryRoot $sarifRepositoryRoot `
        -Architecture x64 `
        -Backend msvc `
        -InputPaths $convertedMsvcReports.ToArray() `
        -OutputPath $convertedMsvcMerged
    $convertedMsvcResult = Get-Content -Raw -LiteralPath $convertedMsvcMerged | ConvertFrom-Json
    Assert-Equal -Actual @($convertedMsvcResult.runs).Count -Expected 2 -Description 'MSVC unit SARIF identities are mergeable'

    $tidyUnitRoot = Join-Path $sarifRepositoryRoot '.artifacts\reports\clang-tidy\x64\units'
    New-Item -ItemType Directory -Force -Path $tidyUnitRoot | Out-Null
    $tidyReports = [System.Collections.Generic.List[string]]::new()
    foreach ($tidyCase in @(
            [pscustomobject]@{ Project = 'renpy'; Unit = 'core-io-bounded-stream-bc188848' },
            [pscustomobject]@{ Project = 'rpgmaker'; Unit = $rpgmakerUnitSlug }
        )) {
        $tidyObjectRoot = Join-Path $sarifRepositoryRoot ".artifacts\analysis\clang-tidy\x64\$($tidyCase.Project)\$($tidyCase.Unit)\obj"
        New-Item -ItemType Directory -Force -Path $tidyObjectRoot | Out-Null
        New-Item -ItemType File -Force -Path (Join-Path $tidyObjectRoot "$($tidyCase.Project).ClangTidy.log") | Out-Null
        $tidyReport = Join-Path $tidyUnitRoot "$($tidyCase.Project)-$($tidyCase.Unit).sarif"
        Convert-GraphClangTidyUnitSarif `
            -RepositoryRoot $sarifRepositoryRoot `
            -ObjectRoot $tidyObjectRoot `
            -OutputPath $tidyReport `
            -Architecture x64 `
            -Project $tidyCase.Project `
            -Unit $tidyCase.Unit
        $tidyReports.Add($tidyReport)
    }
    $tidyMergedPath = Join-Path $sarifRepositoryRoot '.artifacts\reports\clang-tidy\x64\clang-tidy.sarif'
    Merge-GraphSarif `
        -RepositoryRoot $sarifRepositoryRoot `
        -Architecture x64 `
        -Backend clang-tidy `
        -InputPaths $tidyReports.ToArray() `
        -OutputPath $tidyMergedPath
    $tidyMerged = Get-Content -Raw -LiteralPath $tidyMergedPath | ConvertFrom-Json
    Assert-Equal -Actual @($tidyMerged.runs).Count -Expected 2 -Description 'Tidy unit SARIF identities are mergeable'
    if ($tidyMerged.runs[0].automationDetails.id -eq $tidyMerged.runs[1].automationDetails.id) {
        throw 'Tidy unit SARIF identities must be unique.'
    }

    $foreignUnit = $rpgmakerUnitSlug
    $foreignRaw = Join-Path $sarifRepositoryRoot ".artifacts\analysis\msvc\x64\renpy\$foreignUnit\renpy.sarif"
    New-Item -ItemType Directory -Force -Path (Split-Path $foreignRaw -Parent) | Out-Null
    Copy-Item -LiteralPath $convertedMsvcReports[0] -Destination $foreignRaw
    Assert-ScriptFailure `
        -Action {
            Convert-GraphMsvcSarif `
                -RepositoryRoot $sarifRepositoryRoot `
                -InputPath $foreignRaw `
                -OutputPath (Join-Path $msvcUnitRoot "renpy-$foreignUnit.sarif") `
                -Architecture x64 `
                -Project renpy `
                -Unit $foreignUnit
        } `
        -Pattern 'not a ClCompile unit' `
        -Description 'Regex-shaped foreign unit rejection'

    $wrongVersionRaw = Join-Path $sarifRepositoryRoot ".artifacts\analysis\msvc\x64\renpy\$msvcUnitSlug\renpy.sarif"
    New-Item -ItemType Directory -Force -Path (Split-Path $wrongVersionRaw -Parent) | Out-Null
    [ordered]@{ version = '2.0.0'; runs = @([ordered]@{}) } |
        ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath $wrongVersionRaw -Encoding utf8
    Assert-ScriptFailure `
        -Action {
            Convert-GraphMsvcSarif `
                -RepositoryRoot $sarifRepositoryRoot `
                -InputPath $wrongVersionRaw `
                -OutputPath (Join-Path $msvcUnitRoot "renpy-$msvcUnitSlug.sarif") `
                -Architecture x64 `
                -Project renpy `
                -Unit $msvcUnitSlug
        } `
        -Pattern 'version.*2\.1\.0' `
        -Description 'Normalized SARIF version rejection'

    $emptyRunsRaw = Join-Path $sarifRepositoryRoot ".artifacts\analysis\msvc\x64\renpy\$unitSlug\renpy.sarif"
    New-Item -ItemType Directory -Force -Path (Split-Path $emptyRunsRaw -Parent) | Out-Null
    [ordered]@{ version = '2.1.0'; runs = @() } |
        ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath $emptyRunsRaw -Encoding utf8
    Assert-ScriptFailure `
        -Action {
            Convert-GraphMsvcSarif `
                -RepositoryRoot $sarifRepositoryRoot `
                -InputPath $emptyRunsRaw `
                -OutputPath (Join-Path $msvcUnitRoot "renpy-$unitSlug.sarif") `
                -Architecture x64 `
                -Project renpy `
                -Unit $unitSlug
        } `
        -Pattern 'runs.*non-empty list of objects' `
        -Description 'Normalized SARIF empty runs rejection'

    [ordered]@{ version = '2.1.0'; runs = @('not-an-object') } |
        ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath $emptyRunsRaw -Encoding utf8
    Assert-ScriptFailure `
        -Action {
            Convert-GraphMsvcSarif `
                -RepositoryRoot $sarifRepositoryRoot `
                -InputPath $emptyRunsRaw `
                -OutputPath (Join-Path $msvcUnitRoot "renpy-$unitSlug.sarif") `
                -Architecture x64 `
                -Project renpy `
                -Unit $unitSlug
        } `
        -Pattern 'runs.*non-empty list of objects' `
        -Description 'Normalized SARIF non-object run rejection'

    $crossRootRaw = Join-Path $sarifRepositoryRoot ".artifacts\analysis\msvc\x86\renpy\$unitSlug\renpy.sarif"
    New-Item -ItemType Directory -Force -Path (Split-Path $crossRootRaw -Parent) | Out-Null
    Copy-Item -LiteralPath $convertedMsvcReports[0] -Destination $crossRootRaw
    Assert-ScriptFailure `
        -Action {
            Convert-GraphMsvcSarif `
                -RepositoryRoot $sarifRepositoryRoot `
                -InputPath $crossRootRaw `
                -OutputPath (Join-Path $msvcUnitRoot "renpy-$unitSlug.sarif") `
                -Architecture x64 `
                -Project renpy `
                -Unit $unitSlug
        } `
        -Pattern 'expected input path' `
        -Description 'MSVC cross-architecture input rejection'

    $wrongTidyOutput = Join-Path $sarifRepositoryRoot ".artifacts\reports\clang-tidy\x64\units\rpgmaker-$unitSlug.sarif"
    $validTidyObjectRoot = Join-Path $sarifRepositoryRoot ".artifacts\analysis\clang-tidy\x64\renpy\$unitSlug\obj"
    New-Item -ItemType Directory -Force -Path $validTidyObjectRoot | Out-Null
    New-Item -ItemType File -Force -Path (Join-Path $validTidyObjectRoot 'renpy.ClangTidy.log') | Out-Null
    Assert-ScriptFailure `
        -Action {
            Convert-GraphClangTidyUnitSarif `
                -RepositoryRoot $sarifRepositoryRoot `
                -ObjectRoot $validTidyObjectRoot `
                -OutputPath $wrongTidyOutput `
                -Architecture x64 `
                -Project renpy `
                -Unit $unitSlug
        } `
        -Pattern 'expected output path' `
        -Description 'Tidy project relabeling rejection'

    $wrongTidyObjectRoot = Join-Path $sarifRepositoryRoot ".artifacts\analysis\clang-tidy\x64\rpgmaker\$unitSlug\obj"
    New-Item -ItemType Directory -Force -Path $wrongTidyObjectRoot | Out-Null
    Assert-ScriptFailure `
        -Action {
            Convert-GraphClangTidyUnitSarif `
                -RepositoryRoot $sarifRepositoryRoot `
                -ObjectRoot $wrongTidyObjectRoot `
                -OutputPath (Join-Path $tidyUnitRoot "renpy-$unitSlug.sarif") `
                -Architecture x64 `
                -Project renpy `
                -Unit $unitSlug
        } `
        -Pattern 'expected object root path' `
        -Description 'Tidy object-root relabeling rejection'

    $emptyMergeInput = Join-Path $msvcUnitRoot "renpy-$unitSlug.sarif"
    [ordered]@{ version = '2.1.0'; runs = @() } |
        ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath $emptyMergeInput -Encoding utf8
    Assert-ScriptFailure `
        -Action {
            Merge-GraphSarif `
                -RepositoryRoot $sarifRepositoryRoot `
                -Architecture x64 `
                -Backend msvc `
                -InputPaths @($emptyMergeInput) `
                -OutputPath $convertedMsvcMerged
        } `
        -Pattern 'runs.*non-empty list of objects' `
        -Description 'Zero-run merge input rejection'

    [ordered]@{
        version = '2.1.0'
        runs = @([ordered]@{ automationDetails = [ordered]@{ id = "msvc-analyze/x64/rpgmaker/$foreignUnit/" } })
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $emptyMergeInput -Encoding utf8
    Assert-ScriptFailure `
        -Action {
            Merge-GraphSarif `
                -RepositoryRoot $sarifRepositoryRoot `
                -Architecture x64 `
                -Backend msvc `
                -InputPaths @($emptyMergeInput) `
                -OutputPath $convertedMsvcMerged
        } `
        -Pattern 'expected unit identities' `
        -Description 'Merge relabeled identity rejection'

    $crossBackendMergeInput = Join-Path $tidyUnitRoot "renpy-$unitSlug.sarif"
    Copy-Item -LiteralPath $emptyMergeInput -Destination $crossBackendMergeInput -Force
    Assert-ScriptFailure `
        -Action {
            Merge-GraphSarif `
                -RepositoryRoot $sarifRepositoryRoot `
                -Architecture x64 `
                -Backend msvc `
                -InputPaths @($crossBackendMergeInput) `
                -OutputPath $convertedMsvcMerged
        } `
        -Pattern 'expected input path' `
        -Description 'Merge cross-backend input rejection'

    Assert-ScriptFailure `
        -Action {
            Merge-GraphSarif `
                -RepositoryRoot $sarifRepositoryRoot `
                -Architecture x64 `
                -Backend msvc `
                -InputPaths @($emptyMergeInput) `
                -OutputPath (Join-Path $sarifRepositoryRoot '.artifacts\reports\msvc\x86\msvc-analyze.sarif')
        } `
        -Pattern 'expected output path' `
        -Description 'Merge cross-architecture output rejection'

    Assert-ScriptFailure `
        -Action {
            Merge-GraphSarif `
                -RepositoryRoot $sarifRepositoryRoot `
                -Architecture x64 `
                -Backend msvc `
                -InputPaths @((Join-Path $msvcUnitRoot 'renpy-missing-12345678.sarif')) `
                -OutputPath $convertedMsvcMerged
        } `
        -Pattern 'was not found' `
        -Description 'Missing unit SARIF rejection'
} finally {
    if (Test-Path -LiteralPath $testRoot) {
        $resolvedTestRoot = [System.IO.Path]::GetFullPath($testRoot)
        $expectedParent = [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot '.artifacts\contract-tests'))
        if (-not $resolvedTestRoot.StartsWith($expectedParent + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove unexpected contract-test path: $resolvedTestRoot"
        }
        Remove-Item -Recurse -Force -LiteralPath $resolvedTestRoot
    }
}

Write-Output '[OK] Native graph leaves isolate selected-file analysis and deterministic SARIF fan-in.'

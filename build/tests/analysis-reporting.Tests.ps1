#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$modulePath = Join-Path $repositoryRoot 'build\lib\analysis-reporting.ps1'
if (-not (Test-Path -LiteralPath $modulePath -PathType Leaf)) {
    throw "Analysis-reporting production module was not found: $modulePath"
}
. $modulePath

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

$testRoot = Join-Path $repositoryRoot ".artifacts\contract-tests\analysis-reporting-$([guid]::NewGuid().ToString('N'))"
$objectRoot = Join-Path $testRoot 'obj\x64'
$reportPath = Join-Path $testRoot 'reports\clang-tidy\x64\clang-tidy.sarif'

try {
    $renpyLogDirectory = Join-Path $objectRoot 'Debug\renpy'
    $testsLogDirectory = Join-Path $objectRoot 'Debug\tests'
    New-Item -ItemType Directory -Force -Path $renpyLogDirectory, $testsLogDirectory | Out-Null

    $apiPath = Join-Path $repositoryRoot 'src\api.h'
    $archivePath = Join-Path $repositoryRoot 'src\archive.cpp'
    $outsidePath = Join-Path (Split-Path $repositoryRoot -Parent) 'dependency\upstream.cpp'
    @(
        "[1/2] Processing file $apiPath."
        "${apiPath}(28,9): error : declaration uses a reserved identifier [bugprone-reserved-identifier,-warnings-as-errors] [$repositoryRoot\build\projects\renpy.vcxproj]"
        "${archivePath}(41,7): warning : prefer a scoped lock [bugprone-lock-mutex] [$repositoryRoot\build\projects\renpy.vcxproj]"
        "${archivePath}(41,7): message : diagnostic note without an independent rule [$repositoryRoot\build\projects\renpy.vcxproj]"
        "${outsidePath}(5,2): warning : dependency warning [bugprone-example] [$repositoryRoot\build\projects\renpy.vcxproj]"
        'Suppressed 123 warnings (123 in non-user code).'
    ) | Set-Content -LiteralPath (Join-Path $renpyLogDirectory 'renpy.ClangTidy.log') -Encoding utf8
    @(
        "${apiPath}(28,9): error : declaration uses a reserved identifier [bugprone-reserved-identifier,-warnings-as-errors] [$repositoryRoot\build\projects\tests.vcxproj]"
    ) | Set-Content -LiteralPath (Join-Path $testsLogDirectory 'tests.ClangTidy.log') -Encoding utf8

    Export-ClangTidySarif `
        -RepositoryRoot $repositoryRoot `
        -ObjectRoot $objectRoot `
        -OutputPath $reportPath `
        -Architecture 'x64'

    if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
        throw "Clang-tidy SARIF was not created: $reportPath"
    }
    $sarif = Get-Content -Raw -LiteralPath $reportPath | ConvertFrom-Json
    Assert-Equal -Actual $sarif.version -Expected '2.1.0' -Description 'SARIF version'
    Assert-Equal -Actual @($sarif.runs).Count -Expected 1 -Description 'SARIF run count'

    $run = @($sarif.runs)[0]
    Assert-Equal -Actual $run.tool.driver.name -Expected 'clang-tidy' -Description 'SARIF driver name'
    Assert-Equal -Actual $run.automationDetails.id -Expected 'clang-tidy/x64/' -Description 'Stable run identity'
    Assert-Equal -Actual @($run.results).Count -Expected 2 -Description 'Deduplicated first-party result count'

    $results = @($run.results | Sort-Object ruleId)
    Assert-Equal -Actual $results[0].ruleId -Expected 'bugprone-lock-mutex' -Description 'Warning rule ID'
    Assert-Equal -Actual $results[0].level -Expected 'warning' -Description 'Warning SARIF level'
    Assert-Equal -Actual $results[0].locations[0].physicalLocation.artifactLocation.uri -Expected 'src/archive.cpp' -Description 'Normalized warning URI'
    Assert-Equal -Actual $results[1].ruleId -Expected 'bugprone-reserved-identifier' -Description 'Error rule ID'
    Assert-Equal -Actual $results[1].level -Expected 'error' -Description 'Error SARIF level'
    Assert-Equal -Actual ([int] $results[1].locations[0].physicalLocation.region.startLine) -Expected 28 -Description 'Error line'
    Assert-Equal -Actual ([int] $results[1].locations[0].physicalLocation.region.startColumn) -Expected 9 -Description 'Error column'
    Assert-Equal -Actual @($run.tool.driver.rules).Count -Expected 2 -Description 'Unique rule metadata count'

    $emptyObjectRoot = Join-Path $testRoot 'obj\arm64'
    $emptyReportPath = Join-Path $testRoot 'reports\clang-tidy\arm64\clang-tidy.sarif'
    New-Item -ItemType Directory -Force -Path $emptyObjectRoot | Out-Null
    Export-ClangTidySarif `
        -RepositoryRoot $repositoryRoot `
        -ObjectRoot $emptyObjectRoot `
        -OutputPath $emptyReportPath `
        -Architecture 'arm64'
    $emptySarif = Get-Content -Raw -LiteralPath $emptyReportPath | ConvertFrom-Json
    Assert-Equal -Actual @($emptySarif.runs[0].results).Count -Expected 0 -Description 'Empty result count'
    Assert-Equal -Actual $emptySarif.runs[0].automationDetails.id -Expected 'clang-tidy/arm64/' -Description 'Empty run identity'

    $msvcReportDirectory = Join-Path $testRoot 'reports\msvc\x64'
    New-Item -ItemType Directory -Force -Path $msvcReportDirectory | Out-Null
    foreach ($reportName in @('renpy', 'tests')) {
        [ordered]@{
            version = '2.1.0'
            runs = @(
                [ordered]@{
                    tool = [ordered]@{ driver = [ordered]@{ name = 'Microsoft C/C++ Code Analysis' } }
                    results = @([ordered]@{ ruleId = 'C6001' })
                }
            )
        } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $msvcReportDirectory "$reportName.sarif") -Encoding utf8
    }

    Set-MSVCAnalysisSarifIdentity -ReportDirectory $msvcReportDirectory -Architecture 'x64'
    foreach ($reportName in @('renpy', 'tests')) {
        $report = Get-Content -Raw -LiteralPath (Join-Path $msvcReportDirectory "$reportName.sarif") | ConvertFrom-Json
        Assert-Equal `
            -Actual $report.runs[0].automationDetails.id `
            -Expected "msvc-analyze/x64/$reportName/" `
            -Description "$reportName MSVC run identity"
        Assert-Equal -Actual @($report.runs[0].results).Count -Expected 1 -Description "$reportName result preservation"
    }
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

Write-Host '[OK] Clang-tidy logs convert to deterministic first-party SARIF.'

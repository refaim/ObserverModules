#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$workflowPath = Join-Path $repositoryRoot '.github\workflows\main.yml'
$buildScriptPath = Join-Path $repositoryRoot 'build\build.ps1'
$workflow = Get-Content -Raw -LiteralPath $workflowPath
$buildScript = Get-Content -Raw -LiteralPath $buildScriptPath

function Assert-ContainsText {
    param(
        [Parameter(Mandatory)][string] $Text,
        [Parameter(Mandatory)][string] $Expected,
        [Parameter(Mandatory)][string] $Description
    )

    if (-not $Text.Contains($Expected, [System.StringComparison]::Ordinal)) {
        throw "$Description is missing '$Expected'."
    }
}

Assert-ContainsText `
    -Text $buildScript `
    -Expected ". (Join-Path `$script:BuildRoot 'lib\analysis-reporting.ps1')" `
    -Description 'Build entry-point analysis-reporting import'

$analysisFunctionMatch = [regex]::Match(
    $buildScript,
    '(?s)function Invoke-CodeAnalysis \{(?<body>.*?)\r?\n\}\r?\n\r?\nfunction Add-ASanRuntimeToPath'
)
if (-not $analysisFunctionMatch.Success) {
    throw 'Invoke-CodeAnalysis could not be isolated for reporting-contract validation.'
}
$analysisFunction = $analysisFunctionMatch.Groups['body'].Value
foreach ($requiredFragment in @(
    'try {',
    '} finally {',
    'Set-MSVCAnalysisSarifIdentity',
    'Export-ClangTidySarif',
    "reports\clang-tidy",
    'clang-tidy.sarif'
)) {
    Assert-ContainsText -Text $analysisFunction -Expected $requiredFragment -Description 'Compiler-analysis reporting contract'
}

$expectedUploads = @(
    @{
        Name = 'Cppcheck x86'
        File = '.artifacts/reports/cppcheck/cppcheck-x86.sarif'
        Category = 'cppcheck/x86'
    },
    @{
        Name = 'Cppcheck x64'
        File = '.artifacts/reports/cppcheck/cppcheck-x64.sarif'
        Category = 'cppcheck/x64'
    },
    @{
        Name = 'Cppcheck ARM64'
        File = '.artifacts/reports/cppcheck/cppcheck-arm64.sarif'
        Category = 'cppcheck/arm64'
    },
    @{
        Name = 'MSVC'
        File = '.artifacts/reports/msvc/${{ matrix.arch }}'
        Category = 'msvc-analyze/${{ matrix.arch }}'
    },
    @{
        Name = 'clang-tidy'
        File = '.artifacts/reports/clang-tidy/${{ matrix.arch }}/clang-tidy.sarif'
        Category = 'clang-tidy/${{ matrix.arch }}'
    },
    @{
        Name = 'BinSkim'
        File = '.artifacts/audit/binskim-${{ matrix.arch }}.sarif'
        Category = 'binskim/${{ matrix.arch }}'
    }
)
foreach ($upload in $expectedUploads) {
    Assert-ContainsText -Text $workflow -Expected "sarif_file: $($upload.File)" -Description "$($upload.Name) SARIF upload"
    Assert-ContainsText -Text $workflow -Expected "category: $($upload.Category)" -Description "$($upload.Name) category"
}

Assert-ContainsText -Text $workflow -Expected '- name: Upload clang-tidy SARIF' -Description 'clang-tidy upload step'
Assert-ContainsText -Text $workflow -Expected '.artifacts/reports/clang-tidy/${{ matrix.arch }}' -Description 'Archived clang-tidy evidence'

foreach ($codeQlFragment in @(
    '- name: Analyze and upload CodeQL SARIF',
    'category: codeql/c-cpp',
    'output: .artifacts/reports/codeql/raw',
    'post-processed-sarif-path: .artifacts/reports/codeql/uploaded',
    '- name: Archive CodeQL SARIF',
    'name: codeql-sarif',
    'path: .artifacts/reports/codeql'
)) {
    Assert-ContainsText -Text $workflow -Expected $codeQlFragment -Description 'CodeQL retained-report contract'
}

$workflowSteps = [regex]::Matches(
    $workflow,
    '(?ms)^      - name: (?<name>[^\r\n]+)\r?\n(?<body>.*?)(?=^      - |^  \S|\z)'
)
$sarifUploadSteps = @(
    $workflowSteps |
        Where-Object { $_.Groups['body'].Value.Contains('uses: github/codeql-action/upload-sarif@v4') }
)
if ($sarifUploadSteps.Count -eq 0) {
    throw 'No third-party SARIF upload steps were found.'
}
foreach ($uploadStep in $sarifUploadSteps) {
    $uploadBody = $uploadStep.Groups['body'].Value
    Assert-ContainsText `
        -Text $uploadBody `
        -Expected 'if: always() && hashFiles(' `
        -Description "$($uploadStep.Groups['name'].Value) report-existence guard"
    Assert-ContainsText `
        -Text $uploadBody `
        -Expected "github.event.pull_request.head.repo.full_name == github.repository" `
        -Description "$($uploadStep.Groups['name'].Value) fork-permission guard"
}

Write-Host '[OK] CI retains and uniquely categorizes Cppcheck, MSVC, clang-tidy, CodeQL, and BinSkim SARIF.'

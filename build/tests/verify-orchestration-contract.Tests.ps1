#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-PowerShellAst {
    param([Parameter(Mandatory)][string] $Path)

    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref] $tokens, [ref] $errors)
    if ($errors.Count -ne 0) {
        $messages = @($errors | ForEach-Object Message)
        throw "PowerShell parse failed for '$Path': $($messages -join '; ')"
    }
    return $ast
}

$repositoryRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$entrypointPath = Join-Path $repositoryRoot 'build\build.ps1'
$orchestrationFiles = @(
    Get-Item -LiteralPath $entrypointPath
    Get-ChildItem -LiteralPath (Join-Path $repositoryRoot 'build\lib') -File -Filter '*.ps1' | Sort-Object Name
)
$verifyFunctions = @(
    foreach ($file in $orchestrationFiles) {
        $ast = Read-PowerShellAst -Path $file.FullName
        foreach ($definition in $ast.FindAll(
                {
                    param($node)
                    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                        $node.Name -eq 'Invoke-Verify'
                },
                $false
            )) {
            [pscustomobject]@{ File = $file.FullName; Definition = $definition }
        }
    }
)
if ($verifyFunctions.Count -ne 1) {
    throw 'The build orchestration sources must define exactly one Invoke-Verify orchestrator.'
}

$verifyText = $verifyFunctions[0].Definition.Extent.Text
foreach ($requiredFragment in @(
        'Get-CurrentVerifyHostArchitecture',
        'Get-VerifyRoutingPlan',
        '$plan.RequestedArchitectures',
        '$plan.Builds',
        '$plan.TestRuns',
        '$plan.SpecialistGates',
        '$plan.PackageContentArchitectures',
        '$plan.Deferred',
        'Invoke-Restore',
        'Invoke-Lint',
        'Invoke-Build',
        'Invoke-TestExecutable',
        'Invoke-CodeAnalysis',
        'Invoke-Coverage',
        'Invoke-ASan',
        'Invoke-Ubsan',
        'Invoke-LeakTest',
        'Invoke-Fuzz',
        "-TargetName 'all'",
        'Invoke-Package'
    )) {
    if (-not $verifyText.Contains($requiredFragment, [StringComparison]::Ordinal)) {
        throw "Invoke-Verify is missing the required plan-driven step: $requiredFragment"
    }
}

if ($verifyText -match "@\(\s*'(x86|x64|arm64)'" -or
    $verifyText -match "@\(\s*'(Debug|Release)'") {
    throw 'Invoke-Verify must consume the routing plan instead of declaring an architecture/configuration matrix.'
}
if ($verifyText.Contains('$PSScriptRoot', [StringComparison]::Ordinal)) {
    throw 'Invoke-Verify must use the caller-provided build context instead of its physical source location.'
}

$entrypointText = Get-Content -Raw -LiteralPath $entrypointPath
$verifyDispatch = [regex]::Match(
    $entrypointText,
    "(?s)'verify'\s*\{(?<body>.*?)\r?\n\s*\}\r?\n\s*'clean'"
)
if (-not $verifyDispatch.Success) {
    throw 'The verify command dispatch block is missing.'
}
$dispatchBody = $verifyDispatch.Groups['body'].Value
if (-not $dispatchBody.Contains('Invoke-Verify', [StringComparison]::Ordinal)) {
    throw 'The verify command must delegate to Invoke-Verify.'
}
foreach ($forbiddenCall in @('Invoke-Lint', 'Invoke-Test ', 'Invoke-Audit')) {
    if ($dispatchBody.Contains($forbiddenCall, [StringComparison]::Ordinal)) {
        throw "The verify command duplicates orchestration outside the routing-plan consumer: $forbiddenCall"
    }
}

Write-Host '[OK] Verify orchestration is complete and consumes the routing plan without a duplicate matrix.'

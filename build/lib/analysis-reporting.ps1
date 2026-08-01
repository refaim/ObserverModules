#requires -Version 7.4

Set-StrictMode -Version Latest

function Set-MSVCAnalysisSarifIdentity {
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string] $ReportDirectory,
        [Parameter(Mandatory)][ValidateSet('x86', 'x64', 'arm64')][string] $Architecture
    )

    if (-not (Test-Path -LiteralPath $ReportDirectory -PathType Container)) {
        return
    }

    $reportFiles = @(Get-ChildItem -LiteralPath $ReportDirectory -File -Filter '*.sarif' | Sort-Object Name)
    foreach ($reportFile in $reportFiles) {
        $sarif = Get-Content -Raw -LiteralPath $reportFile.FullName | ConvertFrom-Json -AsHashtable
        if (-not $sarif.Contains('runs') -or $sarif['runs'] -isnot [System.Collections.IList]) {
            throw "MSVC analysis report has no SARIF runs array: $($reportFile.FullName)"
        }

        $reportName = [System.IO.Path]::GetFileNameWithoutExtension($reportFile.Name)
        $runs = @($sarif['runs'])
        for ($runIndex = 0; $runIndex -lt $runs.Count; ++$runIndex) {
            $run = $runs[$runIndex]
            if ($run -isnot [System.Collections.IDictionary]) {
                throw "MSVC analysis report contains a non-object run: $($reportFile.FullName)"
            }

            if (-not $run.Contains('automationDetails') -or $run['automationDetails'] -isnot [System.Collections.IDictionary]) {
                $run['automationDetails'] = [ordered]@{}
            }
            $identitySuffix = if ($runs.Count -eq 1) { '' } else { "run-$($runIndex + 1)/" }
            $run['automationDetails']['id'] = "msvc-analyze/$Architecture/$reportName/$identitySuffix"
        }

        if ($PSCmdlet.ShouldProcess($reportFile.FullName, 'Assign stable MSVC SARIF run identities')) {
            $sarif | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $reportFile.FullName -Encoding utf8
        }
    }
}

function Export-ClangTidySarif {
    param(
        [Parameter(Mandatory)][string] $RepositoryRoot,
        [Parameter(Mandatory)][string] $ObjectRoot,
        [Parameter(Mandatory)][string] $OutputPath,
        [Parameter(Mandatory)][ValidateSet('x86', 'x64', 'arm64')][string] $Architecture
    )

    $resolvedRepositoryRoot = [System.IO.Path]::GetFullPath($RepositoryRoot).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $repositoryPrefix = $resolvedRepositoryRoot + [System.IO.Path]::DirectorySeparatorChar
    $diagnosticPattern = '^(?<path>.+)\((?<line>\d+),(?<column>\d+)\):\s+(?<severity>warning|error)\s*:\s+(?<body>.+?)\s*$'
    $projectSuffixPattern = '^(?<diagnostic>.*)\s+\[[^\]]+\.vcxproj\]$'
    $ruleSuffixPattern = '^(?<message>.*)\s+\[(?<checks>[^\]]+)\]$'
    $deduplicationKeys = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    $rules = [System.Collections.Generic.Dictionary[string, object]]::new(
        [System.StringComparer]::Ordinal
    )
    $results = [System.Collections.Generic.List[object]]::new()

    $logFiles = @()
    if (Test-Path -LiteralPath $ObjectRoot -PathType Container) {
        $logFiles = @(
            Get-ChildItem -LiteralPath $ObjectRoot -Recurse -File -Filter '*.ClangTidy.log' |
                Sort-Object FullName
        )
    }

    foreach ($logFile in $logFiles) {
        foreach ($line in Get-Content -LiteralPath $logFile.FullName) {
            $diagnosticMatch = [regex]::Match($line, $diagnosticPattern)
            if (-not $diagnosticMatch.Success) {
                continue
            }

            $body = $diagnosticMatch.Groups['body'].Value
            $projectMatch = [regex]::Match($body, $projectSuffixPattern)
            if ($projectMatch.Success) {
                $body = $projectMatch.Groups['diagnostic'].Value
            }
            $ruleMatch = [regex]::Match($body, $ruleSuffixPattern)
            if (-not $ruleMatch.Success) {
                continue
            }

            $ruleId = @(
                $ruleMatch.Groups['checks'].Value -split ',' |
                    ForEach-Object { $_.Trim() } |
                    Where-Object { $_ -and -not $_.StartsWith('-', [System.StringComparison]::Ordinal) }
            ) | Select-Object -First 1
            if (-not $ruleId) {
                continue
            }

            try {
                $resolvedDiagnosticPath = [System.IO.Path]::GetFullPath($diagnosticMatch.Groups['path'].Value)
            } catch {
                continue
            }
            if (-not $resolvedDiagnosticPath.StartsWith($repositoryPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                continue
            }

            $relativePath = [System.IO.Path]::GetRelativePath($resolvedRepositoryRoot, $resolvedDiagnosticPath).
                Replace([System.IO.Path]::DirectorySeparatorChar, [char]'/')
            $lineNumber = [int] $diagnosticMatch.Groups['line'].Value
            $columnNumber = [int] $diagnosticMatch.Groups['column'].Value
            $level = $diagnosticMatch.Groups['severity'].Value
            $message = $ruleMatch.Groups['message'].Value.Trim()
            $deduplicationKey = "$relativePath`n$lineNumber`n$columnNumber`n$level`n$ruleId`n$message"
            if (-not $deduplicationKeys.Add($deduplicationKey)) {
                continue
            }

            if (-not $rules.ContainsKey($ruleId)) {
                $rules.Add($ruleId, [ordered]@{
                        id = $ruleId
                        name = $ruleId
                        shortDescription = [ordered]@{ text = "clang-tidy check $ruleId" }
                    })
            }
            $results.Add([ordered]@{
                    ruleId = $ruleId
                    level = $level
                    message = [ordered]@{ text = $message }
                    locations = @(
                        [ordered]@{
                            physicalLocation = [ordered]@{
                                artifactLocation = [ordered]@{ uri = $relativePath }
                                region = [ordered]@{
                                    startLine = $lineNumber
                                    startColumn = $columnNumber
                                }
                            }
                        }
                    )
                })
        }
    }

    $sortedResults = @(
        $results |
            Sort-Object `
                @{ Expression = { $_.locations[0].physicalLocation.artifactLocation.uri } }, `
                @{ Expression = { $_.locations[0].physicalLocation.region.startLine } }, `
                @{ Expression = { $_.locations[0].physicalLocation.region.startColumn } }, `
                ruleId, `
                @{ Expression = { $_.message.text } }
    )
    $sortedRules = @($rules.Values | Sort-Object id)
    $sarif = [ordered]@{
        version = '2.1.0'
        '$schema' = 'https://json.schemastore.org/sarif-2.1.0.json'
        runs = @(
            [ordered]@{
                automationDetails = [ordered]@{ id = "clang-tidy/$Architecture/" }
                tool = [ordered]@{
                    driver = [ordered]@{
                        name = 'clang-tidy'
                        informationUri = 'https://clang.llvm.org/extra/clang-tidy/'
                        rules = $sortedRules
                    }
                }
                results = $sortedResults
            }
        )
    }

    $outputDirectory = Split-Path $OutputPath -Parent
    New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
    $sarif | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $OutputPath -Encoding utf8
}

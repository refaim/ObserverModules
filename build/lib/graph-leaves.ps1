#requires -Version 7.4

Set-StrictMode -Version Latest

$script:GraphProjectNames = @(
    'renpy',
    'rpgmaker',
    'zanzarah',
    'tests',
    'fuzz-pickle',
    'fuzz-renpy',
    'fuzz-rpgmaker',
    'fuzz-zanzarah',
    'leak-probe'
)

function Get-GraphMSBuildPlatform {
    param([Parameter(Mandatory)][ValidateSet('x86', 'x64', 'arm64')][string] $Architecture)

    switch ($Architecture) {
        'x86' { return 'Win32' }
        'x64' { return 'x64' }
        'arm64' { return 'ARM64' }
    }
}

function Resolve-GraphProjectPath {
    param(
        [Parameter(Mandatory)][string] $RepositoryRoot,
        [Parameter(Mandatory)][string] $Project
    )

    if ($Project -notin $script:GraphProjectNames) {
        throw "Native graph project '$Project' is not allowlisted."
    }
    $resolvedRepositoryRoot = [System.IO.Path]::GetFullPath($RepositoryRoot)
    $projectPath = [System.IO.Path]::GetFullPath((Join-Path $resolvedRepositoryRoot "build\projects\$Project.vcxproj"))
    $expectedProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $resolvedRepositoryRoot 'build\projects'))
    if (-not $projectPath.StartsWith($expectedProjectRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Native graph project escaped the project root: $projectPath"
    }
    if (-not (Test-Path -LiteralPath $projectPath -PathType Leaf)) {
        throw "Native graph project was not found: $projectPath"
    }
    return $projectPath
}

function Get-GraphProjectTranslationUnit {
    param(
        [Parameter(Mandatory)][string] $RepositoryRoot,
        [Parameter(Mandatory)][string] $Project
    )

    $projectPath = Resolve-GraphProjectPath -RepositoryRoot $RepositoryRoot -Project $Project
    [xml] $projectXml = Get-Content -Raw -LiteralPath $projectPath
    $namespace = [System.Xml.XmlNamespaceManager]::new($projectXml.NameTable)
    $namespace.AddNamespace('msb', 'http://schemas.microsoft.com/developer/msbuild/2003')
    $prefix = '$(RepositoryRoot)'
    return @(
        $projectXml.SelectNodes('//msb:ClCompile[@Include]', $namespace) |
            ForEach-Object {
                $include = $_.Include.ToString()
                if (-not $include.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
                    throw "Native graph ClCompile path must begin with '$prefix': $include"
                }
                $relativePath = $include.Substring($prefix.Length).Replace('\', '/')
                if (
                    [System.IO.Path]::IsPathFullyQualified($relativePath) -or
                    @($relativePath -split '/') -contains '..' -or
                    -not $relativePath.EndsWith('.cpp', [System.StringComparison]::OrdinalIgnoreCase)
                ) {
                    throw "Unsafe native graph ClCompile path: $relativePath"
                }
                $relativePath
            }
    )
}

function Get-GraphTranslationUnitSlug {
    param([Parameter(Mandatory)][string] $Source)

    $normalizedSource = $Source.Replace('\', '/')
    if (
        [System.IO.Path]::IsPathFullyQualified($normalizedSource) -or
        @($normalizedSource -split '/') -contains '..' -or
        -not $normalizedSource.EndsWith('.cpp', [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Unsafe translation-unit path: $Source"
    }
    $readable = $normalizedSource
    if ($readable.StartsWith('src/', [System.StringComparison]::Ordinal)) {
        $readable = $readable.Substring(4)
    }
    $readable = $readable.Substring(0, $readable.Length - 4).ToLowerInvariant()
    $readable = [regex]::Replace($readable, '[^a-z0-9]+', '-').Trim('-')
    $digestBytes = [System.Security.Cryptography.SHA256]::HashData(
        [System.Text.Encoding]::UTF8.GetBytes($normalizedSource)
    )
    $digest = [System.Convert]::ToHexString($digestBytes).ToLowerInvariant().Substring(0, 8)
    return "$readable-$digest"
}

function Assert-GraphProjectUnit {
    param(
        [Parameter(Mandatory)][string] $RepositoryRoot,
        [Parameter(Mandatory)][string] $Project,
        [Parameter(Mandatory)][string] $Unit
    )

    $expectedUnits = @(
        Get-GraphProjectTranslationUnit -RepositoryRoot $RepositoryRoot -Project $Project |
            ForEach-Object { Get-GraphTranslationUnitSlug -Source $_ }
    )
    if ($Unit -cnotin $expectedUnits) {
        throw "Translation-unit identity '$Unit' is not a ClCompile unit in $Project.vcxproj."
    }
}

function Assert-GraphProjectConfiguration {
    param(
        [Parameter(Mandatory)][string] $Project,
        [Parameter(Mandatory)][string] $Configuration
    )

    if ($Project -eq 'leak-probe' -and $Configuration -ne 'Release') {
        throw 'The leak-probe graph leaf supports only Release|x64.'
    }
    if ($Project.StartsWith('fuzz-', [System.StringComparison]::Ordinal) -and $Configuration -ne 'Fuzz') {
        throw "Fuzz graph project '$Project' requires the Fuzz configuration."
    }
    if (
        -not $Project.StartsWith('fuzz-', [System.StringComparison]::Ordinal) -and
        $Project -ne 'leak-probe' -and
        $Configuration -eq 'Fuzz'
    ) {
        throw "Non-fuzz graph project '$Project' cannot use the Fuzz configuration."
    }
}

function Get-GraphBuildProjectRequest {
    param(
        [Parameter(Mandatory)][string] $RepositoryRoot,
        [Parameter(Mandatory)][ValidateSet('x86', 'x64', 'arm64')][string] $Architecture,
        [Parameter(Mandatory)][ValidateSet('Debug', 'Release', 'Coverage', 'ASan', 'UBSan', 'Fuzz')][string] $Configuration,
        [Parameter(Mandatory)][string] $Project
    )

    $projectPath = Resolve-GraphProjectPath -RepositoryRoot $RepositoryRoot -Project $Project
    Assert-GraphProjectConfiguration -Project $Project -Configuration $Configuration
    if (($Configuration -in @('ASan', 'Fuzz')) -and $Architecture -eq 'arm64') {
        throw "$Configuration is not supported for ARM64."
    }
    if (($Configuration -eq 'UBSan' -or $Project -eq 'leak-probe') -and $Architecture -ne 'x64') {
        throw "$Configuration/$Project is supported only for x64."
    }

    return [pscustomobject]@{
        ProjectPath = $projectPath
        Target = 'Build'
        Architecture = $Architecture
        Platform = Get-GraphMSBuildPlatform -Architecture $Architecture
        Configuration = $Configuration
        Properties = [ordered]@{}
    }
}

function Get-GraphAnalysisRequest {
    param(
        [Parameter(Mandatory)][string] $RepositoryRoot,
        [Parameter(Mandatory)][ValidateSet('x86', 'x64', 'arm64')][string] $Architecture,
        [Parameter(Mandatory)][ValidateSet('msvc', 'clang-tidy')][string] $Backend,
        [Parameter(Mandatory)][string] $Project,
        [Parameter()][string] $SelectedFile,
        [Parameter()][string] $Unit
    )

    $projectPath = Resolve-GraphProjectPath -RepositoryRoot $RepositoryRoot -Project $Project
    $configuration = if ($Project -eq 'leak-probe') { 'Release' } else { 'Debug' }
    if ($Project -eq 'leak-probe' -and $Architecture -ne 'x64') {
        throw 'leak-probe analysis is supported only for x64.'
    }
    $resolvedRepositoryRoot = [System.IO.Path]::GetFullPath($RepositoryRoot)
    $properties = [ordered]@{
        ObserverRunCodeAnalysis = 'true'
        RunCodeAnalysis = 'true'
        ObserverCompileAnalysis = 'true'
        ForceRebuild = 'true'
    }

    if (-not $SelectedFile -or -not $Unit) {
        throw "$Backend selected-file analysis requires SelectedFile and Unit."
    }
    $normalizedSource = $SelectedFile.Replace('\', '/')
    $translationUnits = @(Get-GraphProjectTranslationUnit -RepositoryRoot $resolvedRepositoryRoot -Project $Project)
    if ($normalizedSource -notin $translationUnits) {
        throw "'$SelectedFile' is not a ClCompile Include item in $Project.vcxproj."
    }
    $expectedUnit = Get-GraphTranslationUnitSlug -Source $normalizedSource
    if ($Unit -cne $expectedUnit) {
        throw "Translation-unit key '$Unit' does not match '$expectedUnit'."
    }
    $selectedPath = [System.IO.Path]::GetFullPath((Join-Path $resolvedRepositoryRoot $normalizedSource.Replace('/', '\')))
    $properties.SelectedFiles = $selectedPath
    $properties.SelectedFilesBuildPCH = 'false'
    $properties.SelectedFilesBuildModules = 'false'

    if ($Backend -eq 'msvc') {
        $scratchRoot = Join-Path $resolvedRepositoryRoot ".artifacts\analysis\msvc\$Architecture\$Project\$Unit"
        $properties.EnableMicrosoftCodeAnalysis = 'true'
        $properties.ObserverEnableClangTidy = 'false'
        $properties.IntDir = Join-Path $scratchRoot 'obj\'
        $properties.ObserverAnalysisReportName = $Project
        $properties.ObserverAnalysisReportPath = Join-Path $scratchRoot "$Project.sarif"
    } else {
        $scratchRoot = Join-Path $resolvedRepositoryRoot ".artifacts\analysis\clang-tidy\$Architecture\$Project\$Unit"
        $properties.EnableMicrosoftCodeAnalysis = 'false'
        $properties.ObserverEnableClangTidy = 'true'
        $properties.IntDir = Join-Path $scratchRoot 'obj\'
        $properties.ClangTidyLogFile = "$Project.ClangTidy.log"
    }

    return [pscustomobject]@{
        ProjectPath = $projectPath
        Target = 'ClCompile'
        Architecture = $Architecture
        Platform = Get-GraphMSBuildPlatform -Architecture $Architecture
        Configuration = $configuration
        Properties = $properties
    }
}

function Resolve-GraphArtifactPath {
    param(
        [Parameter(Mandatory)][string] $RepositoryRoot,
        [Parameter(Mandatory)][string] $Path
    )

    $resolvedRepositoryRoot = [System.IO.Path]::GetFullPath($RepositoryRoot)
    $resolvedPath = if ([System.IO.Path]::IsPathFullyQualified($Path)) {
        [System.IO.Path]::GetFullPath($Path)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path $resolvedRepositoryRoot $Path))
    }
    $artifactRoot = [System.IO.Path]::GetFullPath((Join-Path $resolvedRepositoryRoot '.artifacts'))
    if (-not $resolvedPath.StartsWith($artifactRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Graph artifact path must stay below '$artifactRoot': $resolvedPath"
    }

    $current = $resolvedPath
    while ($current.StartsWith($artifactRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $current) {
            $attributes = [System.IO.File]::GetAttributes($current)
            if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Graph artifact path crosses a reparse point: $current"
            }
        }
        if ($current.Equals($artifactRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $current = Split-Path $current -Parent
    }
    return $resolvedPath
}

function Assert-GraphArtifactPath {
    param(
        [Parameter(Mandatory)][string] $RepositoryRoot,
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $ExpectedRelativePath,
        [Parameter(Mandatory)][ValidateSet('input', 'output', 'object root')][string] $Role
    )

    $resolvedPath = Resolve-GraphArtifactPath -RepositoryRoot $RepositoryRoot -Path $Path
    $expectedPath = Resolve-GraphArtifactPath -RepositoryRoot $RepositoryRoot -Path $ExpectedRelativePath
    if (-not $resolvedPath.Equals($expectedPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Graph SARIF expected $Role path '$expectedPath', received '$resolvedPath'."
    }
    return $resolvedPath
}

function Read-GraphSarifDocument {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Description
    )

    $sarif = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json -AsHashtable
    if (
        $sarif -isnot [System.Collections.IDictionary] -or
        -not $sarif.Contains('version') -or
        $sarif['version'] -isnot [string] -or
        $sarif['version'] -cne '2.1.0'
    ) {
        throw "$Description must use SARIF version exactly 2.1.0: $Path"
    }
    if (
        -not $sarif.Contains('runs') -or
        $sarif['runs'] -isnot [System.Collections.IList] -or
        $sarif['runs'].Count -eq 0 -or
        @($sarif['runs'] | Where-Object { $_ -isnot [System.Collections.IDictionary] }).Count -ne 0
    ) {
        throw "$Description runs must be a non-empty list of objects: $Path"
    }
    return $sarif
}

function Convert-GraphMsvcSarif {
    param(
        [Parameter(Mandatory)][string] $RepositoryRoot,
        [Parameter(Mandatory)][string] $InputPath,
        [Parameter(Mandatory)][string] $OutputPath,
        [Parameter(Mandatory)][ValidateSet('x86', 'x64', 'arm64')][string] $Architecture,
        [Parameter(Mandatory)][string] $Project,
        [Parameter(Mandatory)][string] $Unit
    )

    if ($Project -notin $script:GraphProjectNames) {
        throw "Native graph project '$Project' is not allowlisted."
    }
    Assert-GraphProjectUnit -RepositoryRoot $RepositoryRoot -Project $Project -Unit $Unit
    $resolvedInput = Assert-GraphArtifactPath `
        -RepositoryRoot $RepositoryRoot `
        -Path $InputPath `
        -ExpectedRelativePath ".artifacts\analysis\msvc\$Architecture\$Project\$Unit\$Project.sarif" `
        -Role input
    $resolvedOutput = Assert-GraphArtifactPath `
        -RepositoryRoot $RepositoryRoot `
        -Path $OutputPath `
        -ExpectedRelativePath ".artifacts\reports\msvc\$Architecture\units\$Project-$Unit.sarif" `
        -Role output
    if (-not (Test-Path -LiteralPath $resolvedInput -PathType Leaf)) {
        throw "MSVC unit SARIF was not found: $resolvedInput"
    }
    $sarif = Read-GraphSarifDocument -Path $resolvedInput -Description 'MSVC unit SARIF'
    $runs = @($sarif['runs'])
    for ($index = 0; $index -lt $runs.Count; ++$index) {
        $run = $runs[$index]
        if (
            -not $run.ContainsKey('automationDetails') -or
            $run['automationDetails'] -isnot [System.Collections.IDictionary]
        ) {
            $run['automationDetails'] = [ordered]@{}
        }
        $suffix = if ($runs.Count -eq 1) { '' } else { "run-$($index + 1)/" }
        $run['automationDetails']['id'] = "msvc-analyze/$Architecture/$Project/$Unit/$suffix"
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $resolvedOutput -Parent) | Out-Null
    $sarif | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $resolvedOutput -Encoding utf8
}

function Convert-GraphClangTidyUnitSarif {
    param(
        [Parameter(Mandatory)][string] $RepositoryRoot,
        [Parameter(Mandatory)][string] $ObjectRoot,
        [Parameter(Mandatory)][string] $OutputPath,
        [Parameter(Mandatory)][ValidateSet('x86', 'x64', 'arm64')][string] $Architecture,
        [Parameter(Mandatory)][string] $Project,
        [Parameter(Mandatory)][string] $Unit
    )

    if ($Project -notin $script:GraphProjectNames) {
        throw "Native graph project '$Project' is not allowlisted."
    }
    Assert-GraphProjectUnit -RepositoryRoot $RepositoryRoot -Project $Project -Unit $Unit
    $resolvedObjectRoot = Assert-GraphArtifactPath `
        -RepositoryRoot $RepositoryRoot `
        -Path $ObjectRoot `
        -ExpectedRelativePath ".artifacts\analysis\clang-tidy\$Architecture\$Project\$Unit\obj" `
        -Role 'object root'
    $resolvedOutput = Assert-GraphArtifactPath `
        -RepositoryRoot $RepositoryRoot `
        -Path $OutputPath `
        -ExpectedRelativePath ".artifacts\reports\clang-tidy\$Architecture\units\$Project-$Unit.sarif" `
        -Role output
    Export-ClangTidySarif `
        -RepositoryRoot $RepositoryRoot `
        -ObjectRoot $resolvedObjectRoot `
        -OutputPath $resolvedOutput `
        -Architecture $Architecture

    $sarif = Read-GraphSarifDocument -Path $resolvedOutput -Description 'Clang-tidy unit SARIF'
    $runs = @($sarif['runs'])
    for ($index = 0; $index -lt $runs.Count; ++$index) {
        $run = $runs[$index]
        if (
            -not $run.ContainsKey('automationDetails') -or
            $run['automationDetails'] -isnot [System.Collections.IDictionary]
        ) {
            $run['automationDetails'] = [ordered]@{}
        }
        $suffix = if ($runs.Count -eq 1) { '' } else { "run-$($index + 1)/" }
        $run['automationDetails']['id'] = "clang-tidy/$Architecture/$Project/$Unit/$suffix"
    }
    $sarif | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $resolvedOutput -Encoding utf8
}

function Merge-GraphSarif {
    param(
        [Parameter(Mandatory)][string] $RepositoryRoot,
        [Parameter(Mandatory)][ValidateSet('x86', 'x64', 'arm64')][string] $Architecture,
        [Parameter(Mandatory)][ValidateSet('msvc', 'clang-tidy')][string] $Backend,
        [Parameter(Mandatory)][string[]] $InputPaths,
        [Parameter(Mandatory)][string] $OutputPath
    )

    if ($InputPaths.Count -eq 0) {
        throw 'SARIF merge requires at least one input.'
    }
    $outputName = if ($Backend -eq 'msvc') { 'msvc-analyze.sarif' } else { 'clang-tidy.sarif' }
    $resolvedOutput = Assert-GraphArtifactPath `
        -RepositoryRoot $RepositoryRoot `
        -Path $OutputPath `
        -ExpectedRelativePath ".artifacts\reports\$Backend\$Architecture\$outputName" `
        -Role output
    $expectedInputRoot = Resolve-GraphArtifactPath `
        -RepositoryRoot $RepositoryRoot `
        -Path ".artifacts\reports\$Backend\$Architecture\units"
    $runs = [System.Collections.Generic.List[object]]::new()
    foreach ($inputPath in $InputPaths) {
        $resolvedInput = Resolve-GraphArtifactPath -RepositoryRoot $RepositoryRoot -Path $inputPath
        $resolvedInputParent = Split-Path $resolvedInput -Parent
        if (-not $resolvedInputParent.Equals($expectedInputRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Graph SARIF expected input path below '$expectedInputRoot', received '$resolvedInput'."
        }
        if (-not (Test-Path -LiteralPath $resolvedInput -PathType Leaf)) {
            throw "SARIF merge input was not found: $resolvedInput"
        }
        $inputName = [System.IO.Path]::GetFileNameWithoutExtension($resolvedInput)
        $project = @(
            $script:GraphProjectNames |
                Sort-Object Length -Descending |
                Where-Object { $inputName.StartsWith("$_-", [System.StringComparison]::Ordinal) }
        ) | Select-Object -First 1
        if (-not $project) {
            throw "SARIF merge input does not identify an allowlisted project: $resolvedInput"
        }
        $unit = $inputName.Substring($project.Length + 1)
        Assert-GraphProjectUnit -RepositoryRoot $RepositoryRoot -Project $project -Unit $unit
        $expectedInput = Assert-GraphArtifactPath `
            -RepositoryRoot $RepositoryRoot `
            -Path $resolvedInput `
            -ExpectedRelativePath ".artifacts\reports\$Backend\$Architecture\units\$project-$unit.sarif" `
            -Role input

        $sarif = Read-GraphSarifDocument -Path $expectedInput -Description 'SARIF merge input'
        $inputRuns = @($sarif['runs'])
        $identityPrefix = if ($Backend -eq 'msvc') { 'msvc-analyze' } else { 'clang-tidy' }
        for ($index = 0; $index -lt $inputRuns.Count; ++$index) {
            $run = $inputRuns[$index]
            $suffix = if ($inputRuns.Count -eq 1) { '' } else { "run-$($index + 1)/" }
            $expectedIdentity = "$identityPrefix/$Architecture/$project/$unit/$suffix"
            $automationDetails = $run['automationDetails']
            if (
                $automationDetails -isnot [System.Collections.IDictionary] -or
                -not $automationDetails.Contains('id') -or
                $automationDetails['id'] -isnot [string] -or
                $automationDetails['id'] -cne $expectedIdentity
            ) {
                throw "SARIF merge input does not contain the exact expected unit identities: $resolvedInput"
            }
            $runs.Add($run)
        }
    }
    $sortedRuns = @($runs | Sort-Object { $_['automationDetails']['id'] })
    $identities = @($sortedRuns | ForEach-Object { $_['automationDetails']['id'] })
    if (@($identities | Sort-Object -Unique).Count -ne $identities.Count) {
        throw 'SARIF merge found duplicate automationDetails.id values.'
    }
    $merged = [ordered]@{
        version = '2.1.0'
        '$schema' = 'https://json.schemastore.org/sarif-2.1.0.json'
        runs = $sortedRuns
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $resolvedOutput -Parent) | Out-Null
    $merged | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $resolvedOutput -Encoding utf8
}

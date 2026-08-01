#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$artifactsRoot = Join-Path $repositoryRoot '.artifacts'
$manifestModule = Join-Path $repositoryRoot 'build\lib\package-manifest.ps1'
$commonModule = Join-Path $repositoryRoot 'build\lib\common.ps1'
$smokeModule = Join-Path $repositoryRoot 'build\lib\package-smoke.ps1'
$buildEntrypoint = Join-Path $repositoryRoot 'build\build.ps1'

if (-not (Test-Path -LiteralPath $smokeModule -PathType Leaf)) {
    throw "Package smoke module is missing: $smokeModule"
}

. $manifestModule
$script:RepositoryRoot = $repositoryRoot
. $commonModule
. $smokeModule

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

$temporaryRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $artifactsRoot "package-smoke-contract-$([guid]::NewGuid().ToString('N'))")
)
$artifactsPrefix = [System.IO.Path]::GetFullPath($artifactsRoot) + [System.IO.Path]::DirectorySeparatorChar
if (-not $temporaryRoot.StartsWith($artifactsPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to create package smoke test data outside .artifacts: $temporaryRoot"
}

try {
    $extractedRoot = Join-Path $temporaryRoot 'renpy'
    foreach ($relativeName in $script:ObserverModulePackageEntries.renpy) {
        $path = Join-Path $extractedRoot ($relativeName.Replace('/', '\'))
        $parent = Split-Path $path -Parent
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        [System.IO.File]::WriteAllText($path, $relativeName)
    }

    [void](Assert-ObserverExtractedModulePackage -DirectoryPath $extractedRoot -ModuleName 'renpy')

    $extraPath = Join-Path $extractedRoot 'unexpected.txt'
    [System.IO.File]::WriteAllText($extraPath, 'unexpected')
    Assert-Throw -Description 'extra extracted package file' -Action {
        Assert-ObserverExtractedModulePackage -DirectoryPath $extractedRoot -ModuleName 'renpy'
    }
    Remove-Item -LiteralPath $extraPath

    $symbolsRoot = Join-Path $temporaryRoot 'symbols'
    New-Item -ItemType Directory -Force -Path $symbolsRoot | Out-Null
    foreach ($relativeName in $script:ObserverSymbolsPackageEntries) {
        [System.IO.File]::WriteAllText((Join-Path $symbolsRoot $relativeName), $relativeName)
    }
    [void](Assert-ObserverExtractedSymbolsPackage -DirectoryPath $symbolsRoot)

    $modulePath = Join-Path $extractedRoot 'renpy.so'
    $probePath = Join-Path $temporaryRoot 'package-smoke-probe.cmd'
    [System.IO.File]::WriteAllLines($probePath, @(
            '@echo off',
            ('if not "%OBSERVER_PACKAGE_MODULE%"=="{0}" exit /b 21' -f $modulePath),
            'if not "%OBSERVER_PACKAGE_FORMAT%"=="renpy" exit /b 22',
            'exit /b 0'
        ))

    $previousModule = 'module-sentinel'
    $previousFormat = 'format-sentinel'
    [Environment]::SetEnvironmentVariable('OBSERVER_PACKAGE_MODULE', $previousModule, 'Process')
    [Environment]::SetEnvironmentVariable('OBSERVER_PACKAGE_FORMAT', $previousFormat, 'Process')

    Invoke-ObserverPackageRuntimeSmoke `
        -TestExecutablePath $probePath `
        -ModulePath $modulePath `
        -ModuleName 'renpy' `
        -WorkingDirectory $temporaryRoot

    if ([Environment]::GetEnvironmentVariable('OBSERVER_PACKAGE_MODULE', 'Process') -ne $previousModule) {
        throw 'Package smoke did not restore OBSERVER_PACKAGE_MODULE.'
    }
    if ([Environment]::GetEnvironmentVariable('OBSERVER_PACKAGE_FORMAT', 'Process') -ne $previousFormat) {
        throw 'Package smoke did not restore OBSERVER_PACKAGE_FORMAT.'
    }
}
finally {
    [Environment]::SetEnvironmentVariable('OBSERVER_PACKAGE_MODULE', $null, 'Process')
    [Environment]::SetEnvironmentVariable('OBSERVER_PACKAGE_FORMAT', $null, 'Process')
    if (Test-Path -LiteralPath $temporaryRoot) {
        $item = Get-Item -LiteralPath $temporaryRoot -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to remove reparse-point test directory: $temporaryRoot"
        }
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}

$orchestrationFiles = @(
    Get-Item -LiteralPath $buildEntrypoint
    Get-ChildItem -LiteralPath (Join-Path $repositoryRoot 'build\lib') -File -Filter '*.ps1' | Sort-Object Name
)
$packageDefinitions = @(
    foreach ($file in $orchestrationFiles) {
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            $file.FullName,
            [ref] $tokens,
            [ref] $errors
        )
        if ($errors.Count -ne 0) {
            throw "PowerShell parse failed for '$($file.FullName)'."
        }
        foreach ($definition in $ast.FindAll(
                {
                    param($node)
                    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                        $node.Name -eq 'Invoke-Package'
                },
                $false
            )) {
            [pscustomobject]@{ File = $file.FullName; Definition = $definition }
        }
    }
)
if ($packageDefinitions.Count -ne 1) {
    throw "Package orchestration must define Invoke-Package exactly once; found $($packageDefinitions.Count)."
}
$packageSource = $packageDefinitions[0].Definition.Extent.Text
foreach ($requiredCall in @(
        'Expand-ObserverModulePackageForSmoke',
        'Expand-ObserverSymbolsPackageForSmoke',
        'Invoke-ObserverPackageRuntimeSmoke'
    )) {
    if (-not $packageSource.Contains($requiredCall, [System.StringComparison]::Ordinal)) {
        throw "Package orchestration does not invoke $requiredCall."
    }
}
if ($packageSource.Contains('$PSScriptRoot', [System.StringComparison]::Ordinal)) {
    throw 'Invoke-Package must use the caller-provided build context instead of its physical source location.'
}

Write-Host 'Package smoke contracts passed.'

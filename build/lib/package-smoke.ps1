#requires -Version 7.4

Set-StrictMode -Version Latest

function Get-ObserverExtractedPackageManifest {
    param([Parameter(Mandatory)][string] $DirectoryPath)

    $root = [System.IO.Path]::GetFullPath($DirectoryPath)
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "Extracted package directory does not exist: $root"
    }

    $files = [System.Collections.Generic.List[string]]::new()
    $directories = [System.Collections.Generic.List[string]]::new()
    foreach ($item in Get-ChildItem -LiteralPath $root -Force -Recurse) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Extracted package contains a reparse point: $($item.FullName)"
        }

        $relativeName = [System.IO.Path]::GetRelativePath($root, $item.FullName).Replace('\', '/')
        Assert-SafeObserverZipEntryName -Name $relativeName
        if ($item.PSIsContainer) {
            $directories.Add($relativeName)
        }
        else {
            $files.Add($relativeName)
        }
    }

    return [pscustomobject]@{
        DirectoryPath = $root
        Files = $files.ToArray()
        Directories = $directories.ToArray()
    }
}

function Assert-ObserverExtractedPackageEntrySet {
    param(
        [Parameter(Mandatory)][string] $DirectoryPath,
        [Parameter(Mandatory)][string[]] $ExpectedFileName,
        [Parameter(Mandatory)][string] $PackageDescription
    )

    $manifest = Get-ObserverExtractedPackageManifest -DirectoryPath $DirectoryPath
    $expectedDirectories = Get-ObserverExpectedDirectoryName -FileName $ExpectedFileName
    $actualFiles = [System.Collections.Generic.HashSet[string]]::new(
        [string[]]$manifest.Files,
        [System.StringComparer]::Ordinal
    )
    $actualDirectories = [System.Collections.Generic.HashSet[string]]::new(
        [string[]]$manifest.Directories,
        [System.StringComparer]::Ordinal
    )

    $missingFiles = @($ExpectedFileName | Where-Object { -not $actualFiles.Contains($_) } | Sort-Object)
    $extraFiles = @($manifest.Files | Where-Object { $_ -notin $ExpectedFileName } | Sort-Object)
    $missingDirectories = @($expectedDirectories | Where-Object { -not $actualDirectories.Contains($_) } | Sort-Object)
    $extraDirectories = @($manifest.Directories | Where-Object { -not $expectedDirectories.Contains($_) } | Sort-Object)
    if (
        $missingFiles.Count -ne 0 -or
        $extraFiles.Count -ne 0 -or
        $missingDirectories.Count -ne 0 -or
        $extraDirectories.Count -ne 0
    ) {
        $details = @(
            if ($missingFiles.Count -ne 0) { "missing files: $($missingFiles -join ', ')" }
            if ($extraFiles.Count -ne 0) { "extra files: $($extraFiles -join ', ')" }
            if ($missingDirectories.Count -ne 0) { "missing directories: $($missingDirectories -join ', ')" }
            if ($extraDirectories.Count -ne 0) { "extra directories: $($extraDirectories -join ', ')" }
        ) -join '; '
        throw "$PackageDescription extracted manifest does not match the exact allowlist ($details)."
    }

    return $manifest
}

function Assert-ObserverExtractedModulePackage {
    param(
        [Parameter(Mandatory)][string] $DirectoryPath,
        [Parameter(Mandatory)][ValidateSet('renpy', 'rpgmaker', 'zanzarah')][string] $ModuleName
    )

    return Assert-ObserverExtractedPackageEntrySet `
        -DirectoryPath $DirectoryPath `
        -ExpectedFileName $script:ObserverModulePackageEntries[$ModuleName] `
        -PackageDescription "$ModuleName module package"
}

function Assert-ObserverExtractedSymbolsPackage {
    param([Parameter(Mandatory)][string] $DirectoryPath)

    return Assert-ObserverExtractedPackageEntrySet `
        -DirectoryPath $DirectoryPath `
        -ExpectedFileName $script:ObserverSymbolsPackageEntries `
        -PackageDescription 'Combined symbols package'
}

function Expand-ObserverModulePackageForSmoke {
    param(
        [Parameter(Mandatory)][string] $ArchivePath,
        [Parameter(Mandatory)][string] $DestinationPath,
        [Parameter(Mandatory)][ValidateSet('renpy', 'rpgmaker', 'zanzarah')][string] $ModuleName
    )

    $destination = [System.IO.Path]::GetFullPath($DestinationPath)
    if (Test-Path -LiteralPath $destination) {
        throw "Package smoke destination already exists: $destination"
    }

    [void](Assert-ObserverModulePackageManifest -ArchivePath $ArchivePath -ModuleName $ModuleName)
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $destination
    [void](Assert-ObserverExtractedModulePackage -DirectoryPath $destination -ModuleName $ModuleName)
    return $destination
}

function Expand-ObserverSymbolsPackageForSmoke {
    param(
        [Parameter(Mandatory)][string] $ArchivePath,
        [Parameter(Mandatory)][string] $DestinationPath
    )

    $destination = [System.IO.Path]::GetFullPath($DestinationPath)
    if (Test-Path -LiteralPath $destination) {
        throw "Package smoke destination already exists: $destination"
    }

    [void](Assert-ObserverSymbolsPackageManifest -ArchivePath $ArchivePath)
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $destination
    [void](Assert-ObserverExtractedSymbolsPackage -DirectoryPath $destination)
    return $destination
}

function Invoke-ObserverPackageRuntimeSmoke {
    param(
        [Parameter(Mandatory)][string] $TestExecutablePath,
        [Parameter(Mandatory)][string] $ModulePath,
        [Parameter(Mandatory)][ValidateSet('renpy', 'rpgmaker', 'zanzarah')][string] $ModuleName,
        [Parameter(Mandatory)][string] $WorkingDirectory,
        [Parameter()][string] $ReportPath
    )

    if (-not [System.IO.Path]::IsPathFullyQualified($ModulePath)) {
        throw "Package module path must be absolute: $ModulePath"
    }
    $resolvedModulePath = [System.IO.Path]::GetFullPath($ModulePath)
    if (-not (Test-Path -LiteralPath $resolvedModulePath -PathType Leaf)) {
        throw "Extracted package module does not exist: $resolvedModulePath"
    }
    $resolvedTestExecutable = [System.IO.Path]::GetFullPath($TestExecutablePath)
    if (-not (Test-Path -LiteralPath $resolvedTestExecutable -PathType Leaf)) {
        throw "Package smoke test executable does not exist: $resolvedTestExecutable"
    }

    $arguments = [System.Collections.Generic.List[string]]::new()
    foreach ($argument in @('[package-smoke]', '--reporter', 'compact', '--rng-seed', '1')) {
        $arguments.Add($argument)
    }
    if ($ReportPath) {
        $arguments.Add('--reporter')
        $arguments.Add("JUnit::out=$([System.IO.Path]::GetFullPath($ReportPath))")
    }

    $previousModule = [Environment]::GetEnvironmentVariable('OBSERVER_PACKAGE_MODULE', 'Process')
    $previousFormat = [Environment]::GetEnvironmentVariable('OBSERVER_PACKAGE_FORMAT', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('OBSERVER_PACKAGE_MODULE', $resolvedModulePath, 'Process')
        [Environment]::SetEnvironmentVariable('OBSERVER_PACKAGE_FORMAT', $ModuleName, 'Process')
        Invoke-Native `
            -FilePath $resolvedTestExecutable `
            -Arguments $arguments.ToArray() `
            -WorkingDirectory $WorkingDirectory
    }
    finally {
        [Environment]::SetEnvironmentVariable('OBSERVER_PACKAGE_MODULE', $previousModule, 'Process')
        [Environment]::SetEnvironmentVariable('OBSERVER_PACKAGE_FORMAT', $previousFormat, 'Process')
    }
}

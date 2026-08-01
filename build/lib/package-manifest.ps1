#requires -Version 7.4

Set-StrictMode -Version Latest

$script:ObserverModulePackageEntries = @{
    renpy = @(
        'renpy.so'
        'observer_user.ini'
        'docs/license.txt'
        'docs/thirdparty/Observer.txt'
        'docs/thirdparty/rpatool.txt'
        'docs/thirdparty/serde-pickle.txt'
        'docs/thirdparty/zlib.txt'
    )
    rpgmaker = @(
        'rpgmaker.so'
        'observer_user.ini'
        'docs/license.txt'
        'docs/thirdparty/Observer.txt'
        'docs/thirdparty/rgssad.txt'
    )
    zanzarah = @(
        'zanzarah.so'
        'observer_user.ini'
        'docs/license.txt'
        'docs/thirdparty/Observer.txt'
        'docs/thirdparty/zanzapak.txt'
    )
}
$script:ObserverSymbolsPackageEntries = @('renpy.pdb', 'rpgmaker.pdb', 'zanzarah.pdb')

function Assert-SafeObserverZipEntryName {
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Name)

    $isUnsafe = [string]::IsNullOrEmpty($Name) -or
        $Name.Length -gt 4096 -or
        $Name[0] -eq '/' -or
        $Name[0] -eq '\' -or
        $Name.Contains('\', [System.StringComparison]::Ordinal) -or
        $Name -match '^[A-Za-z]:' -or
        -not $Name.IsNormalized([System.Text.NormalizationForm]::FormC)
    if ($isUnsafe) {
        throw "Package contains an unsafe ZIP entry name: '$Name'."
    }

    foreach ($character in $Name.ToCharArray()) {
        if ([char]::IsControl($character)) {
            throw "Package contains an unsafe ZIP entry name: '$Name'."
        }
    }

    $canonicalName = $Name
    if ($canonicalName.EndsWith('/', [System.StringComparison]::Ordinal)) {
        $canonicalName = $canonicalName.Substring(0, $canonicalName.Length - 1)
    }
    if ([string]::IsNullOrEmpty($canonicalName)) {
        throw "Package contains an unsafe ZIP entry name: '$Name'."
    }

    $segments = $canonicalName.Split([char[]]@('/'), [System.StringSplitOptions]::None)
    foreach ($segment in $segments) {
        $hasUnsafeCharacter = $segment.IndexOfAny([char[]]@('<', '>', ':', '"', '|', '?', '*')) -ge 0
        $baseName = $segment.Split('.', 2)[0]
        $isUnsafeSegment = [string]::IsNullOrEmpty($segment) -or
            $segment -eq '.' -or
            $segment -eq '..' -or
            $segment.Length -gt 255 -or
            $segment.EndsWith('.', [System.StringComparison]::Ordinal) -or
            $segment.EndsWith(' ', [System.StringComparison]::Ordinal) -or
            $hasUnsafeCharacter -or
            $baseName -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$'
        if ($isUnsafeSegment) {
            throw "Package contains an unsafe ZIP entry name: '$Name'."
        }
    }
}

function Get-ObserverZipManifest {
    param([Parameter(Mandatory)][string] $ArchivePath)

    $resolvedArchivePath = [System.IO.Path]::GetFullPath($ArchivePath)
    if (-not (Test-Path -LiteralPath $resolvedArchivePath -PathType Leaf)) {
        throw "Package archive does not exist: $resolvedArchivePath"
    }

    $fileNames = [System.Collections.Generic.List[string]]::new()
    $directoryNames = [System.Collections.Generic.List[string]]::new()
    $entryNames = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $fileStream = [System.IO.File]::Open(
        $resolvedArchivePath,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    try {
        $archive = [System.IO.Compression.ZipArchive]::new(
            $fileStream,
            [System.IO.Compression.ZipArchiveMode]::Read,
            $true
        )
        try {
            foreach ($entry in $archive.Entries) {
                $entryName = $entry.FullName
                Assert-SafeObserverZipEntryName -Name $entryName

                $isDirectory = $entryName.EndsWith('/', [System.StringComparison]::Ordinal)
                $collisionKey = if ($isDirectory) {
                    $entryName.Substring(0, $entryName.Length - 1)
                }
                else {
                    $entryName
                }
                if (-not $entryNames.Add($collisionKey)) {
                    throw "Package contains a duplicate or case-colliding ZIP entry: '$entryName'."
                }

                if ($isDirectory) {
                    if ($entry.Length -ne 0) {
                        throw "Package contains an unsafe ZIP entry with directory data: '$entryName'."
                    }
                    $directoryNames.Add($collisionKey)
                }
                else {
                    $fileNames.Add($entryName)
                }
            }
        }
        finally {
            $archive.Dispose()
        }
    }
    finally {
        $fileStream.Dispose()
    }

    return [pscustomobject]@{
        ArchivePath = $resolvedArchivePath
        Files = $fileNames.ToArray()
        Directories = $directoryNames.ToArray()
    }
}

function Get-ObserverExpectedDirectoryName {
    param([Parameter(Mandatory)][string[]] $FileName)

    $directories = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
    foreach ($name in $FileName) {
        $separatorIndex = $name.LastIndexOf('/', [System.StringComparison]::Ordinal)
        while ($separatorIndex -gt 0) {
            $null = $directories.Add($name.Substring(0, $separatorIndex))
            $separatorIndex = $name.LastIndexOf('/', $separatorIndex - 1)
        }
    }
    return ,$directories
}

function Assert-ObserverPackageEntrySet {
    param(
        [Parameter(Mandatory)][string] $ArchivePath,
        [Parameter(Mandatory)][string[]] $ExpectedFileName,
        [Parameter(Mandatory)][string] $PackageDescription
    )

    $manifest = Get-ObserverZipManifest -ArchivePath $ArchivePath
    $expectedFiles = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
    foreach ($name in $ExpectedFileName) {
        $null = $expectedFiles.Add($name)
    }
    $actualFiles = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
    foreach ($name in @($manifest.Files)) {
        $null = $actualFiles.Add($name)
    }
    $expectedDirectories = Get-ObserverExpectedDirectoryName -FileName $ExpectedFileName

    $missingFiles = @($ExpectedFileName | Where-Object { -not $actualFiles.Contains($_) } | Sort-Object)
    $extraFiles = @($manifest.Files | Where-Object { -not $expectedFiles.Contains($_) } | Sort-Object)
    $extraDirectories = @(
        $manifest.Directories |
            Where-Object { -not $expectedDirectories.Contains($_) } |
            Sort-Object
    )
    if ($missingFiles.Count -ne 0 -or $extraFiles.Count -ne 0 -or $extraDirectories.Count -ne 0) {
        $details = @(
            if ($missingFiles.Count -ne 0) { "missing: $($missingFiles -join ', ')" }
            if ($extraFiles.Count -ne 0) { "extra files: $($extraFiles -join ', ')" }
            if ($extraDirectories.Count -ne 0) { "extra directories: $($extraDirectories -join ', ')" }
        ) -join '; '
        throw "$PackageDescription manifest does not match the exact allowlist ($details)."
    }

    return $manifest
}

function Assert-ObserverModulePackageManifest {
    param(
        [Parameter(Mandatory)][string] $ArchivePath,
        [Parameter(Mandatory)][ValidateSet('renpy', 'rpgmaker', 'zanzarah')][string] $ModuleName
    )

    return Assert-ObserverPackageEntrySet `
        -ArchivePath $ArchivePath `
        -ExpectedFileName $script:ObserverModulePackageEntries[$ModuleName] `
        -PackageDescription "$ModuleName module package"
}

function Assert-ObserverSymbolsPackageManifest {
    param([Parameter(Mandatory)][string] $ArchivePath)

    return Assert-ObserverPackageEntrySet `
        -ArchivePath $ArchivePath `
        -ExpectedFileName $script:ObserverSymbolsPackageEntries `
        -PackageDescription 'Combined symbols package'
}

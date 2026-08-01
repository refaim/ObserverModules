#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$validatorPath = Join-Path $repositoryRoot 'build\lib\package-manifest.ps1'
if (-not (Test-Path -LiteralPath $validatorPath -PathType Leaf)) {
    throw "Package manifest validator is missing: $validatorPath"
}
. $validatorPath

$moduleEntries = @{
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
$symbolsEntries = @('renpy.pdb', 'rpgmaker.pdb', 'zanzarah.pdb')
$assertionCount = 0

function Write-TestZip {
    param(
        [Parameter(Mandatory)][string] $Directory,
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]] $Entries
    )

    $archivePath = Join-Path $Directory $Name
    $fileStream = [System.IO.File]::Open(
        $archivePath,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
    try {
        $archive = [System.IO.Compression.ZipArchive]::new(
            $fileStream,
            [System.IO.Compression.ZipArchiveMode]::Create,
            $true
        )
        try {
            foreach ($entryName in $Entries) {
                $entry = $archive.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::NoCompression)
                if ($entryName.EndsWith('/', [System.StringComparison]::Ordinal)) {
                    continue
                }

                $entryStream = $entry.Open()
                try {
                    $content = [System.Text.Encoding]::UTF8.GetBytes("fixture:$entryName")
                    $entryStream.Write($content, 0, $content.Length)
                }
                finally {
                    $entryStream.Dispose()
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

    return $archivePath
}

function Assert-Success {
    param(
        [Parameter(Mandatory)][string] $Description,
        [Parameter(Mandatory)][scriptblock] $Action
    )

    try {
        & $Action | Out-Null
    }
    catch {
        throw "$Description should succeed, but failed: $($_.Exception.Message)"
    }
    $script:assertionCount++
}

function Assert-Rejected {
    param(
        [Parameter(Mandatory)][string] $Description,
        [Parameter(Mandatory)][string] $MessageFragment,
        [Parameter(Mandatory)][scriptblock] $Action
    )

    $caught = $false
    try {
        & $Action | Out-Null
    }
    catch {
        $caught = $true
        if (-not $_.Exception.Message.Contains($MessageFragment, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "$Description failed for the wrong reason: $($_.Exception.Message)"
        }
    }
    if (-not $caught) {
        throw "$Description should have been rejected."
    }
    $script:assertionCount++
}

$artifactsRoot = [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot '.artifacts'))
New-Item -ItemType Directory -Force -Path $artifactsRoot | Out-Null
$temporaryRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $artifactsRoot "package-manifest-tests-$([guid]::NewGuid().ToString('N'))")
)
$requiredPrefix = $artifactsRoot.TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
) + [System.IO.Path]::DirectorySeparatorChar
if (-not $temporaryRoot.StartsWith($requiredPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to create package test data outside .artifacts: $temporaryRoot"
}
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null

try {
    foreach ($moduleName in @('renpy', 'rpgmaker', 'zanzarah')) {
        $archivePath = Write-TestZip -Directory $temporaryRoot -Name "$moduleName-valid.zip" -Entries $moduleEntries[$moduleName]
        Assert-Success "$moduleName exact manifest" {
            Assert-ObserverModulePackageManifest -ArchivePath $archivePath -ModuleName $moduleName
        }
    }

    $archivePath = Write-TestZip -Directory $temporaryRoot -Name 'renpy-with-directory-entries.zip' -Entries @(
        'docs/'
        'docs/thirdparty/'
        $moduleEntries.renpy
    )
    Assert-Success 'Expected directory records' {
        Assert-ObserverModulePackageManifest -ArchivePath $archivePath -ModuleName 'renpy'
    }

    $archivePath = Write-TestZip -Directory $temporaryRoot -Name 'symbols-valid.zip' -Entries $symbolsEntries
    Assert-Success 'Combined symbols exact manifest' {
        Assert-ObserverSymbolsPackageManifest -ArchivePath $archivePath
    }

    $archivePath = Write-TestZip -Directory $temporaryRoot -Name 'renpy-missing.zip' -Entries @(
        $moduleEntries.renpy | Where-Object { $_ -ne 'renpy.so' }
    )
    Assert-Rejected 'Missing module entry' 'does not match' {
        Assert-ObserverModulePackageManifest -ArchivePath $archivePath -ModuleName 'renpy'
    }

    $archivePath = Write-TestZip -Directory $temporaryRoot -Name 'renpy-extra.zip' -Entries @(
        $moduleEntries.renpy
        'unexpected.txt'
    )
    Assert-Rejected 'Extra module entry' 'does not match' {
        Assert-ObserverModulePackageManifest -ArchivePath $archivePath -ModuleName 'renpy'
    }

    $archivePath = Write-TestZip -Directory $temporaryRoot -Name 'renpy-extra-directory.zip' -Entries @(
        $moduleEntries.renpy
        'unexpected/'
    )
    Assert-Rejected 'Extra directory entry' 'does not match' {
        Assert-ObserverModulePackageManifest -ArchivePath $archivePath -ModuleName 'renpy'
    }

    $archivePath = Write-TestZip -Directory $temporaryRoot -Name 'symbols-missing.zip' -Entries @(
        $symbolsEntries | Where-Object { $_ -ne 'zanzarah.pdb' }
    )
    Assert-Rejected 'Missing symbol entry' 'does not match' {
        Assert-ObserverSymbolsPackageManifest -ArchivePath $archivePath
    }

    $archivePath = Write-TestZip -Directory $temporaryRoot -Name 'symbols-extra.zip' -Entries @(
        $symbolsEntries
        'unexpected.pdb'
    )
    Assert-Rejected 'Extra symbol entry' 'does not match' {
        Assert-ObserverSymbolsPackageManifest -ArchivePath $archivePath
    }

    $archivePath = Write-TestZip -Directory $temporaryRoot -Name 'symbols-extra-directory.zip' -Entries @(
        $symbolsEntries
        'symbols/'
    )
    Assert-Rejected 'Extra symbols directory entry' 'does not match' {
        Assert-ObserverSymbolsPackageManifest -ArchivePath $archivePath
    }

    $archivePath = Write-TestZip -Directory $temporaryRoot -Name 'symbols-empty.zip' -Entries @()
    Assert-Rejected 'Empty symbols archive' 'does not match' {
        Assert-ObserverSymbolsPackageManifest -ArchivePath $archivePath
    }

    $archivePath = Write-TestZip -Directory $temporaryRoot -Name 'renpy-duplicate.zip' -Entries @(
        $moduleEntries.renpy
        'renpy.so'
    )
    Assert-Rejected 'Duplicate ZIP entry' 'duplicate or case-colliding' {
        Assert-ObserverModulePackageManifest -ArchivePath $archivePath -ModuleName 'renpy'
    }

    $archivePath = Write-TestZip -Directory $temporaryRoot -Name 'renpy-case-collision.zip' -Entries @(
        $moduleEntries.renpy
        'RenPy.so'
    )
    Assert-Rejected 'Case-colliding ZIP entry' 'duplicate or case-colliding' {
        Assert-ObserverModulePackageManifest -ArchivePath $archivePath -ModuleName 'renpy'
    }

    $unsafeNames = @(
        '/absolute.txt'
        'C:/absolute.txt'
        '../traversal.txt'
        'docs/../traversal.txt'
        'docs\backslash.txt'
        'docs//empty-segment.txt'
        'docs/./relative.txt'
        'docs/thirdparty/NUL.txt'
        'docs/thirdparty/bad:name.txt'
        'docs/thirdparty/trailing-dot.'
    )
    foreach ($unsafeName in $unsafeNames) {
        $archivePath = Write-TestZip -Directory $temporaryRoot -Name "unsafe-$([guid]::NewGuid().ToString('N')).zip" -Entries @(
            $moduleEntries.renpy
            $unsafeName
        )
        Assert-Rejected "Unsafe ZIP entry '$unsafeName'" 'unsafe ZIP entry' {
            Assert-ObserverModulePackageManifest -ArchivePath $archivePath -ModuleName 'renpy'
        }
    }

    $archivePath = Write-TestZip -Directory $temporaryRoot -Name 'rpgmaker-wrong-thirdparty.zip' -Entries @(
        $moduleEntries.rpgmaker | Where-Object { $_ -ne 'docs/thirdparty/rgssad.txt' }
        'docs/thirdparty/zanzapak.txt'
    )
    Assert-Rejected 'Third-party document from another module' 'does not match' {
        Assert-ObserverModulePackageManifest -ArchivePath $archivePath -ModuleName 'rpgmaker'
    }
}
finally {
    $cleanupTarget = [System.IO.Path]::GetFullPath($temporaryRoot)
    if (-not $cleanupTarget.Equals($temporaryRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not $cleanupTarget.StartsWith($requiredPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing unsafe package test cleanup target: $cleanupTarget"
    }
    if (Test-Path -LiteralPath $cleanupTarget) {
        $cleanupItem = Get-Item -Force -LiteralPath $cleanupTarget
        if (-not $cleanupItem.PSIsContainer -or
            ($cleanupItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing unsafe package test cleanup directory: $cleanupTarget"
        }

        $cleanupFiles = @(Get-ChildItem -Force -LiteralPath $cleanupTarget)
        foreach ($cleanupFile in $cleanupFiles) {
            $expectedParent = [System.IO.Path]::GetDirectoryName($cleanupFile.FullName)
            $isUnsafeFile = $cleanupFile.PSIsContainer -or
                ($cleanupFile.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
                -not $cleanupFile.Extension.Equals('.zip', [System.StringComparison]::OrdinalIgnoreCase) -or
                -not $expectedParent.Equals($cleanupTarget, [System.StringComparison]::OrdinalIgnoreCase)
            if ($isUnsafeFile) {
                throw "Refusing unexpected package test cleanup entry: $($cleanupFile.FullName)"
            }
        }
        foreach ($cleanupFile in $cleanupFiles) {
            Remove-Item -Force -LiteralPath $cleanupFile.FullName
        }
        Remove-Item -Force -LiteralPath $cleanupTarget
    }
}

Write-Host "[OK] Package manifest contract passed $assertionCount assertions."

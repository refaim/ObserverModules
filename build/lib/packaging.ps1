#requires -Version 7.4

Set-StrictMode -Version Latest

function Invoke-Package {
    param([Parameter(Mandatory)][string[]] $Architectures)

    Invoke-Restore -Architectures $Architectures
    Invoke-Build -Architectures $Architectures -Configuration 'Release'
    Invoke-Audit -Architectures $Architectures

    $date = Get-Date -Format 'yyyy-MM-dd'
    $packagesDirectory = Join-Path $script:ArtifactsRoot 'packages'
    $moduleLicenseFiles = @{
        renpy = @('Observer.txt', 'rpatool.txt', 'serde-pickle.txt', 'zlib.txt')
        rpgmaker = @('Observer.txt', 'rgssad.txt')
        zanzarah = @('Observer.txt', 'zanzapak.txt')
    }
    $packageSmokeRunRoot = Join-Path $script:ArtifactsRoot "package-smoke\$([guid]::NewGuid().ToString('N'))"
    $packageSmokeReportDirectory = Join-Path $script:ArtifactsRoot 'reports\package-smoke'
    $packageEvidence = [System.Collections.Generic.List[object]]::new()
    New-Item -ItemType Directory -Force -Path $packageSmokeReportDirectory | Out-Null
    if (Test-Path -LiteralPath $packagesDirectory) {
        Remove-Item -Recurse -Force -LiteralPath $packagesDirectory
    }
    New-Item -ItemType Directory -Force -Path $packagesDirectory | Out-Null

    foreach ($architecture in $Architectures) {
        $binaryDirectory = Get-BinaryDirectory -Architecture $architecture -Configuration 'Release'
        $architectureSymbols = Join-Path $script:ArtifactsRoot "package\symbols\$architecture"
        if (Test-Path -LiteralPath $architectureSymbols) {
            Remove-Item -Recurse -Force -LiteralPath $architectureSymbols
        }
        New-Item -ItemType Directory -Force -Path $architectureSymbols | Out-Null
        foreach ($moduleName in $script:ModuleNames) {
            $stage = Join-Path $script:ArtifactsRoot "package\$architecture\$moduleName"
            if (Test-Path -LiteralPath $stage) {
                Remove-Item -Recurse -Force -LiteralPath $stage
            }
            $docsDirectory = Join-Path $stage 'docs'
            $thirdPartyDirectory = Join-Path $docsDirectory 'thirdparty'
            New-Item -ItemType Directory -Force -Path $thirdPartyDirectory | Out-Null
            Copy-Item -LiteralPath (Join-Path $binaryDirectory "$moduleName.so") -Destination $stage
            Copy-Item -LiteralPath (Join-Path $script:RepositoryRoot "src\modules\$moduleName\observer_user.ini") -Destination $stage
            Copy-Item -LiteralPath (Join-Path $script:RepositoryRoot 'LICENSE.txt') -Destination (Join-Path $docsDirectory 'license.txt')
            foreach ($licenseFile in $moduleLicenseFiles[$moduleName]) {
                $licensePath = Join-Path $script:RepositoryRoot "licenses\$licenseFile"
                if (-not (Test-Path -LiteralPath $licensePath -PathType Leaf)) {
                    throw "Required third-party license is missing for ${moduleName}: $licensePath"
                }
                Copy-Item -LiteralPath $licensePath -Destination $thirdPartyDirectory
            }

            $moduleArchive = Join-Path $packagesDirectory "$moduleName-$date-$architecture-dll.zip"
            if (Test-Path -LiteralPath $moduleArchive) {
                Remove-Item -Force -LiteralPath $moduleArchive
            }
            Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $moduleArchive -CompressionLevel Optimal
            [void](Assert-ObserverModulePackageManifest -ArchivePath $moduleArchive -ModuleName $moduleName)
            $extractedPackage = Expand-ObserverModulePackageForSmoke `
                -ArchivePath $moduleArchive `
                -DestinationPath (Join-Path $packageSmokeRunRoot "$architecture\$moduleName") `
                -ModuleName $moduleName

            $runtimeResult = 'deferred'
            if (Test-CanRunArchitecture -Architecture $architecture) {
                Write-Step "Running package smoke for $moduleName $architecture"
                $smokeReport = Join-Path $packageSmokeReportDirectory "$moduleName-$architecture-release.xml"
                Invoke-ObserverPackageRuntimeSmoke `
                    -TestExecutablePath (Join-Path $binaryDirectory 'tests.exe') `
                    -ModulePath (Join-Path $extractedPackage "$moduleName.so") `
                    -ModuleName $moduleName `
                    -WorkingDirectory $extractedPackage `
                    -ReportPath $smokeReport
                $runtimeResult = 'passed'
            }
            else {
                Write-Host "Deferred package runtime smoke for $moduleName ${architecture}: the current host cannot load $architecture binaries."
            }

            $packageEvidence.Add([pscustomobject]@{
                    Kind = 'module'
                    Architecture = $architecture
                    Module = $moduleName
                    Archive = [System.IO.Path]::GetFullPath($moduleArchive)
                    SHA256 = (Get-FileHash -LiteralPath $moduleArchive -Algorithm SHA256).Hash
                    ExtractedDirectory = $extractedPackage
                    RuntimeSmoke = $runtimeResult
                })
            Copy-Item -LiteralPath (Join-Path $binaryDirectory "$moduleName.pdb") -Destination $architectureSymbols
            Write-Host "Created and validated $moduleArchive"
        }

        $symbolsArchive = Join-Path $packagesDirectory "observer-modules-$date-$architecture-pdb.zip"
        if (Test-Path -LiteralPath $symbolsArchive) {
            Remove-Item -Force -LiteralPath $symbolsArchive
        }
        Compress-Archive -Path (Join-Path $architectureSymbols '*') -DestinationPath $symbolsArchive -CompressionLevel Optimal
        [void](Assert-ObserverSymbolsPackageManifest -ArchivePath $symbolsArchive)
        $extractedSymbols = Expand-ObserverSymbolsPackageForSmoke `
            -ArchivePath $symbolsArchive `
            -DestinationPath (Join-Path $packageSmokeRunRoot "$architecture\symbols")
        $packageEvidence.Add([pscustomobject]@{
                Kind = 'symbols'
                Architecture = $architecture
                Module = $null
                Archive = [System.IO.Path]::GetFullPath($symbolsArchive)
                SHA256 = (Get-FileHash -LiteralPath $symbolsArchive -Algorithm SHA256).Hash
                ExtractedDirectory = $extractedSymbols
                RuntimeSmoke = 'not-applicable'
            })
        Write-Host "Created and validated $symbolsArchive"
    }

    $evidencePath = Join-Path $packagesDirectory 'package-smoke-evidence.json'
    $packageEvidence | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $evidencePath -Encoding utf8NoBOM
    Write-Host "Package smoke evidence: $evidencePath"
}

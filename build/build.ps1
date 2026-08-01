#requires -Version 7.4

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet(
        'help',
        'doctor',
        'restore',
        'build',
        'test',
        'source-checks',
        'compiler-analysis',
        'test-coverage',
        'test-asan',
        'test-ubsan',
        'test-leaks',
        'fuzz',
        'audit-binaries',
        'package',
        'verify',
        'clean'
    )]
    [string] $Command = 'help',

    [string[]] $Arch = @('x64'),

    [ValidateSet('Debug', 'Release')]
    [string] $Config = 'Debug',

    [string] $Corpus,

    [ValidateSet('default', 'asan', 'all')]
    [string] $RestoreFlavor = 'default',

    [switch] $SkipDependencyRestore,

    [ValidateRange(1, 86400)]
    [int] $FuzzSeconds = 60,

    [ValidateSet('all', 'pickle', 'renpy', 'rpgmaker', 'zanzarah')]
    [string] $FuzzTarget = 'all',

    [ValidateRange(1, 1000000)]
    [int] $LeakWarmup = 8,

    [ValidateRange(1, 1000000)]
    [int] $LeakIterations = 100,

    [ValidateRange(3, 10)]
    [int] $LeakWindows = 3,

    [ValidateRange(0, 1073741824)]
    [int64] $LeakToleranceBytes = 0,

    [ValidateRange(0, 100)]
    [double] $CoverageThreshold = 100
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:BuildRoot = $PSScriptRoot
$script:RepositoryRoot = Split-Path $script:BuildRoot -Parent
$script:AggregateProject = Join-Path $script:BuildRoot 'ObserverModules.proj'
$script:ArtifactsRoot = Join-Path $script:RepositoryRoot '.artifacts'
$script:KnownArchitectures = @('x86', 'x64', 'arm64')
$script:ModuleNames = @('renpy', 'rpgmaker', 'zanzarah')

. (Join-Path $script:BuildRoot 'lib\package-manifest.ps1')
. (Join-Path $script:BuildRoot 'lib\analysis-reporting.ps1')
. (Join-Path $script:BuildRoot 'lib\common.ps1')
. (Join-Path $script:BuildRoot 'lib\package-smoke.ps1')
. (Join-Path $script:BuildRoot 'lib\verify-routing.ps1')
. (Join-Path $script:BuildRoot 'lib\packaging.ps1')
. (Join-Path $script:BuildRoot 'lib\verify.ps1')

function Resolve-VisualStudio {
    $programFilesX86 = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
    $vswhere = Join-Path $programFilesX86 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path -LiteralPath $vswhere)) {
        throw 'vswhere.exe was not found. Install Visual Studio 2022 Build Tools with the Desktop development with C++ workload.'
    }

    $installationPath = @(
        & $vswhere -latest -products '*' -requires Microsoft.Component.MSBuild -property installationPath
    ) | Select-Object -First 1
    if (-not $installationPath) {
        throw 'Visual Studio Build Tools with MSBuild were not found.'
    }

    $msbuild = Join-Path $installationPath 'MSBuild\Current\Bin\amd64\MSBuild.exe'
    if (-not (Test-Path -LiteralPath $msbuild)) {
        $msbuild = Join-Path $installationPath 'MSBuild\Current\Bin\MSBuild.exe'
    }
    if (-not (Test-Path -LiteralPath $msbuild)) {
        throw "MSBuild.exe was not found under '$installationPath'."
    }

    $toolsetDirectory = Get-ChildItem -Directory -LiteralPath (Join-Path $installationPath 'VC\Tools\MSVC') |
        Sort-Object { [version]$_.Name } -Descending |
        Select-Object -First 1
    if (-not $toolsetDirectory) {
        throw "No MSVC toolset was found under '$installationPath'."
    }

    $dumpbin = Join-Path $toolsetDirectory.FullName 'bin\Hostx64\x64\dumpbin.exe'
    if (-not (Test-Path -LiteralPath $dumpbin)) {
        throw "dumpbin.exe was not found under '$($toolsetDirectory.FullName)'."
    }

    $developerCommand = Join-Path $installationPath 'Common7\Tools\VsDevCmd.bat'
    if (-not (Test-Path -LiteralPath $developerCommand)) {
        throw "VsDevCmd.bat was not found under '$installationPath'."
    }

    return [pscustomobject]@{
        InstallationPath = $installationPath.ToString()
        MSBuild = $msbuild
        ToolsetDirectory = $toolsetDirectory.FullName
        ToolsetVersion = $toolsetDirectory.Name
        Dumpbin = $dumpbin
        DeveloperCommand = $developerCommand
        Vswhere = $vswhere
    }
}

function Initialize-MSVCEnvironment {
    param(
        [Parameter(Mandatory)] $VisualStudio,
        [Parameter(Mandatory)][string] $Architecture
    )

    $developerArchitecture = switch ($Architecture) {
        'x86' { 'x86' }
        'x64' { 'amd64' }
        'arm64' { 'arm64' }
        default { throw "Unsupported architecture '$Architecture'." }
    }
    $commandProcessor = [Environment]::GetEnvironmentVariable('ComSpec', 'Process')
    if (-not $commandProcessor) {
        $commandProcessor = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::System)) 'cmd.exe'
    }
    $commandLine = 'call "{0}" -no_logo -arch={1} -host_arch=amd64 >nul && set' -f $VisualStudio.DeveloperCommand, $developerArchitecture
    $environmentLines = @(& $commandProcessor /d /s /c $commandLine)
    if ($LASTEXITCODE -ne 0) {
        throw "VsDevCmd failed for architecture '$Architecture'."
    }
    foreach ($line in $environmentLines) {
        if ($line -match '^([^=]+)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
        }
    }

    # Some process launchers inject both PATH and Path. .NET Framework build tasks reject that environment block.
    $cleanPath = [Environment]::GetEnvironmentVariable('PATH', 'Process')
    [Environment]::SetEnvironmentVariable('PATH', $null, 'Process')
    [Environment]::SetEnvironmentVariable('Path', $null, 'Process')
    [Environment]::SetEnvironmentVariable('PATH', $cleanPath, 'Process')

    # VsDevCmd includes optional ATL/MFC directories even when that workload is absent.
    # Roslyn-backed MSBuild tasks warn about every nonexistent LIB entry, so retain only real paths.
    $cleanLibraryPath = @(
        [Environment]::GetEnvironmentVariable('LIB', 'Process') -split ';' |
            Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) }
    ) -join ';'
    [Environment]::SetEnvironmentVariable('LIB', $null, 'Process')
    [Environment]::SetEnvironmentVariable('Lib', $null, 'Process')
    [Environment]::SetEnvironmentVariable('LIB', $cleanLibraryPath, 'Process')
}

function Resolve-Vcpkg {
    $candidates = [System.Collections.Generic.List[string]]::new()

    $environmentRoot = [Environment]::GetEnvironmentVariable('VCPKG_ROOT', 'Process')
    if ($environmentRoot) {
        $candidates.Add($environmentRoot)
    }

    $vcpkgCommand = Get-Command vcpkg.exe -ErrorAction SilentlyContinue
    if ($vcpkgCommand) {
        $commandDirectory = Split-Path $vcpkgCommand.Source -Parent
        $candidates.Add($commandDirectory)
        $candidates.Add((Join-Path (Split-Path $commandDirectory -Parent) 'apps\vcpkg\current'))
    }

    $scoopCommand = Get-Command scoop -ErrorAction SilentlyContinue
    if ($scoopCommand) {
        try {
            $scoopPrefix = @(& $scoopCommand.Source prefix vcpkg 2>$null) | Select-Object -First 1
            if ($LASTEXITCODE -eq 0 -and $scoopPrefix) {
                $candidates.Add($scoopPrefix.ToString())
            }
        } catch {
            Write-Verbose "Scoop prefix lookup failed; direct vcpkg candidates remain available: $($_.Exception.Message)"
        }
    }

    foreach ($candidate in $candidates) {
        if (-not $candidate) {
            continue
        }
        $root = [System.IO.Path]::GetFullPath($candidate)
        $executable = Join-Path $root 'vcpkg.exe'
        $props = Join-Path $root 'scripts\buildsystems\msbuild\vcpkg.props'
        if ((Test-Path -LiteralPath $executable) -and (Test-Path -LiteralPath $props)) {
            return [pscustomobject]@{ Root = $root; Executable = $executable }
        }
    }

    throw 'A complete vcpkg installation was not found. Set VCPKG_ROOT or install vcpkg with Scoop.'
}

function Resolve-Llvm {
    $candidateDirectories = [System.Collections.Generic.List[string]]::new()
    $tidyCommand = Get-Command clang-tidy.exe -ErrorAction SilentlyContinue
    if ($tidyCommand) {
        $candidateDirectories.Add((Split-Path $tidyCommand.Source -Parent))
    }

    try {
        $visualStudio = Resolve-VisualStudio
        $candidateDirectories.Add((Join-Path $visualStudio.InstallationPath 'VC\Tools\Llvm\x64\bin'))
        $candidateDirectories.Add((Join-Path $visualStudio.InstallationPath 'VC\Tools\Llvm\bin'))
    }
    catch {
        Write-Verbose "Visual Studio LLVM lookup failed; a standalone LLVM installation may still be available: $($_.Exception.Message)"
    }

    foreach ($binDirectory in $candidateDirectories | Select-Object -Unique) {
        $tools = @{
            Clang = Join-Path $binDirectory 'clang-cl.exe'
            Cov = Join-Path $binDirectory 'llvm-cov.exe'
            Format = Join-Path $binDirectory 'clang-format.exe'
            Profdata = Join-Path $binDirectory 'llvm-profdata.exe'
            Tidy = Join-Path $binDirectory 'clang-tidy.exe'
        }
        if (@($tools.Values | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }).Count -eq 0) {
            return [pscustomobject]@{
                Root = Split-Path $binDirectory -Parent
                Bin = $binDirectory
                Clang = $tools.Clang
                Cov = $tools.Cov
                Format = $tools.Format
                Profdata = $tools.Profdata
                Tidy = $tools.Tidy
            }
        }
    }

    throw 'A complete LLVM toolset (clang-cl, clang-format, clang-tidy, llvm-cov, llvm-profdata) was not found.'
}

function Resolve-Cppcheck {
    $command = Get-Command cppcheck.exe -ErrorAction SilentlyContinue
    if (-not $command) {
        throw 'cppcheck.exe was not found. Install Cppcheck and ensure it is available on PATH.'
    }
    return $command.Source
}

function Resolve-BinSkim {
    $command = Get-Command BinSkim.exe -ErrorAction SilentlyContinue
    if (-not $command) {
        throw 'BinSkim.exe was not found. Install Microsoft.CodeAnalysis.BinSkim and ensure it is available on PATH.'
    }
    return $command.Source
}

function Resolve-WindowsDebuggingTool {
    $programFilesX86 = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
    $candidates = @(
        Join-Path $programFilesX86 'Windows Kits\10\Debuggers\x64'
        Join-Path $programFilesX86 'Windows Kits\11\Debuggers\x64'
    )

    foreach ($directory in $candidates) {
        $umdh = Join-Path $directory 'umdh.exe'
        if (Test-Path -LiteralPath $umdh -PathType Leaf) {
            return [pscustomobject]@{
                Directory = $directory
                Umdh = $umdh
            }
        }
    }

    throw 'UMDH and gflags.exe were not found. Enable Debugging Tools for Windows in the installed Windows SDK.'
}

function Invoke-MSBuild {
    param(
        [Parameter(Mandatory)][string] $Target,
        [Parameter(Mandatory)][string] $Architecture,
        [Parameter(Mandatory)][string] $Configuration,
        [Parameter()][hashtable] $Properties = @{}
    )

    $visualStudio = Resolve-VisualStudio
    Initialize-MSVCEnvironment -VisualStudio $visualStudio -Architecture $Architecture
    $vcpkg = Resolve-Vcpkg
    $platform = Get-MSBuildPlatform $Architecture
    $arguments = [System.Collections.Generic.List[string]]::new()
    $arguments.Add($script:AggregateProject)
    $arguments.Add('/nologo')
    $arguments.Add('/m')
    $arguments.Add('/nr:false')
    $msbuildVerbosity = if ([string]::IsNullOrWhiteSpace($env:OBSERVER_MSBUILD_VERBOSITY)) {
        'minimal'
    }
    else {
        $env:OBSERVER_MSBUILD_VERBOSITY
    }
    $arguments.Add("/verbosity:$msbuildVerbosity")
    $arguments.Add("/t:$Target")
    $arguments.Add("/p:Configuration=$Configuration")
    $arguments.Add("/p:Platform=$platform")
    $arguments.Add("/p:VcpkgRoot=$($vcpkg.Root)")
    foreach ($entry in $Properties.GetEnumerator()) {
        $arguments.Add("/p:$($entry.Key)=$($entry.Value)")
    }

    $previousMSBuild = [Environment]::GetEnvironmentVariable('OBSERVER_MSBUILD_EXE', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('OBSERVER_MSBUILD_EXE', $visualStudio.MSBuild, 'Process')
        Invoke-Native -FilePath (Join-Path $script:BuildRoot 'run-msbuild.cmd') -Arguments $arguments.ToArray()
    } finally {
        [Environment]::SetEnvironmentVariable('OBSERVER_MSBUILD_EXE', $previousMSBuild, 'Process')
    }
}

function Invoke-Restore {
    param(
        [Parameter(Mandatory)][string[]] $Architectures,
        [Parameter()][ValidateSet('default', 'asan', 'all')][string] $Flavor = 'default'
    )

    if ($SkipDependencyRestore) {
        Write-Verbose 'Skipping dependency restore because -SkipDependencyRestore was explicitly requested.'
        return
    }

    $vcpkg = Resolve-Vcpkg
    $overlayTriplets = Join-Path $script:BuildRoot 'vcpkg\triplets'
    $requestedFlavors = if ($Flavor -eq 'all') { @('default', 'asan') } else { @($Flavor) }

    foreach ($requestedFlavor in $requestedFlavors) {
        foreach ($architecture in $Architectures) {
            if ($requestedFlavor -eq 'asan' -and $architecture -eq 'arm64') {
                Write-Verbose 'Skipping the unsupported ARM64 ASan dependency flavor.'
                continue
            }

            $triplet = Get-VcpkgTriplet -Architecture $architecture -Flavor $requestedFlavor
            $installRootSuffix = if ($requestedFlavor -eq 'asan') { "$architecture-asan" } else { $architecture }
            $installRoot = Join-Path $script:ArtifactsRoot "vcpkg_installed\$installRootSuffix"
            New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
            Write-Step "Restoring vcpkg dependencies for $architecture ($triplet)"
            Invoke-Native -FilePath $vcpkg.Executable -Arguments @(
                'install',
                "--triplet=$triplet",
                "--overlay-triplets=$overlayTriplets",
                "--x-manifest-root=$script:RepositoryRoot",
                "--x-install-root=$installRoot"
            )
        }
    }
}

function Invoke-Build {
    param(
        [Parameter(Mandatory)][string[]] $Architectures,
        [Parameter(Mandatory)][string] $Configuration,
        [Parameter()][string] $Target = 'Build',
        [Parameter()][hashtable] $Properties = @{}
    )

    foreach ($architecture in $Architectures) {
        Write-Step "Building $Target for $architecture $Configuration"
        Invoke-MSBuild -Target $Target -Architecture $architecture -Configuration $Configuration -Properties $Properties
    }
}

function Test-CanRunArchitecture {
    param([Parameter(Mandatory)][string] $Architecture)

    return Test-VerifyArchitectureRunnable `
        -HostArchitecture (Get-CurrentVerifyHostArchitecture) `
        -TargetArchitecture $Architecture
}

function Get-TestReportPath {
    param(
        [Parameter(Mandatory)][string] $Architecture,
        [Parameter(Mandatory)][string] $Configuration,
        [Parameter()][string] $Suite = 'tests'
    )

    $reportDirectory = Join-Path $script:ArtifactsRoot 'reports\tests'
    New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
    return Join-Path $reportDirectory "$Suite-$Architecture-$($Configuration.ToLowerInvariant()).xml"
}

function Invoke-TestExecutable {
    param(
        [Parameter(Mandatory)][string] $Architecture,
        [Parameter(Mandatory)][string] $Configuration,
        [Parameter()][string] $CorpusPath
    )

    if (-not (Test-CanRunArchitecture $Architecture)) {
        throw "The current host cannot execute $Architecture test binaries. Use a native $Architecture CI runner."
    }

    $binaryDirectory = Get-BinaryDirectory -Architecture $Architecture -Configuration $Configuration
    $testExecutable = Join-Path $binaryDirectory 'tests.exe'
    if (-not (Test-Path -LiteralPath $testExecutable)) {
        throw "Test executable was not found: $testExecutable"
    }

    $previousCorpus = [Environment]::GetEnvironmentVariable('OBSERVER_TEST_CORPUS', 'Process')
    try {
        if ($CorpusPath) {
            $resolvedCorpus = Resolve-UserPath -Path $CorpusPath
            if (-not (Test-Path -LiteralPath $resolvedCorpus -PathType Container)) {
                throw "Corpus directory does not exist: $resolvedCorpus"
            }
            [Environment]::SetEnvironmentVariable('OBSERVER_TEST_CORPUS', $resolvedCorpus, 'Process')
        }

        Write-Step "Running tests for $Architecture $Configuration"
        $testReportPath = Get-TestReportPath -Architecture $Architecture -Configuration $Configuration
        Invoke-Native -FilePath $testExecutable -Arguments @(
            '--reporter', 'compact',
            '--reporter', "JUnit::out=$testReportPath",
            '--durations', 'yes',
            '--order', 'lex'
        ) -WorkingDirectory $binaryDirectory
        Write-Host "Report: $testReportPath"
        if ($CorpusPath) {
            Write-Step "Running external compatibility corpus for $Architecture $Configuration"
            $compatibilityReportPath = Get-TestReportPath -Architecture $Architecture -Configuration $Configuration -Suite 'compatibility'
            Invoke-Native -FilePath $testExecutable -Arguments @(
                '[compatibility]',
                '--reporter', 'compact',
                '--reporter', "JUnit::out=$compatibilityReportPath",
                '--durations', 'yes',
                '--order', 'lex'
            ) -WorkingDirectory $binaryDirectory
            Write-Host "Report: $compatibilityReportPath"
        }
    } finally {
        [Environment]::SetEnvironmentVariable('OBSERVER_TEST_CORPUS', $previousCorpus, 'Process')
    }
}

function Invoke-Test {
    param(
        [Parameter(Mandatory)][string[]] $Architectures,
        [Parameter(Mandatory)][string] $Configuration,
        [Parameter()][string] $CorpusPath
    )

    Invoke-Build -Architectures $Architectures -Configuration $Configuration
    foreach ($architecture in $Architectures) {
        Invoke-TestExecutable -Architecture $architecture -Configuration $Configuration -CorpusPath $CorpusPath
    }
}

function Invoke-FormatCheck {
    $llvm = Resolve-Llvm
    $files = @(
        Get-ChildItem -Recurse -File -LiteralPath (Join-Path $script:RepositoryRoot 'src') -Include '*.cpp', '*.h', '*.hpp' |
            Select-Object -ExpandProperty FullName
    )
    Write-Step 'Checking C++ formatting'
    Invoke-Native -FilePath $llvm.Format -Arguments (@('--dry-run', '--Werror') + $files)
}

function Invoke-Cppcheck {
    param([Parameter(Mandatory)][string[]] $Architectures)

    $cppcheck = Resolve-Cppcheck
    $sourceDirectory = Join-Path $script:RepositoryRoot 'src'
    $reportDirectory = Join-Path $script:ArtifactsRoot 'reports\cppcheck'
    New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null

    $failures = [System.Collections.Generic.List[string]]::new()
    foreach ($architecture in $Architectures) {
        $platform = if ($architecture -eq 'x86') { 'win32W' } else { 'win64' }
        $architectureDefine = switch ($architecture) {
            'x86' { '_M_IX86=600' }
            'x64' { '_M_X64=100' }
            'arm64' { '_M_ARM64=1' }
        }

        $triplet = Get-VcpkgTriplet -Architecture $architecture
        $vcpkgIncludeDirectory = Join-Path $script:ArtifactsRoot "vcpkg_installed\$architecture\$triplet\include"
        if (-not (Test-Path -LiteralPath $vcpkgIncludeDirectory -PathType Container)) {
            throw "Cppcheck dependency headers were not restored for ${architecture}: $vcpkgIncludeDirectory"
        }
        $buildDirectory = Join-Path $script:ArtifactsRoot "cppcheck\$architecture"
        $reportPath = Join-Path $reportDirectory "cppcheck-$architecture.sarif"
        New-Item -ItemType Directory -Force -Path $buildDirectory | Out-Null

        Write-Step "Cppcheck: all C++ sources for $architecture"
        try {
            Invoke-Native -FilePath $cppcheck -Arguments @(
                $sourceDirectory,
                '--std=c++23',
                "--platform=$platform",
                '-DWIN32=1',
                '-D_WIN32=1',
                '-DUNICODE=1',
                '-D_UNICODE=1',
                "-D$architectureDefine",
                "-I$sourceDirectory",
                "-I$vcpkgIncludeDirectory",
                '--enable=warning,style,performance,portability',
                '--check-level=exhaustive',
                '--inconclusive',
                '--inline-suppr',
                '--error-exitcode=1',
                '--suppress=missingIncludeSystem',
                '--suppress=uninitMemberVarNoCtor:src/api.h',
                # Dependency diagnostics belong to their upstream projects; keep every enabled rule active for first-party sources.
                '--suppress=*:.artifacts/vcpkg_installed/*',
                # Each plugin currently supplies one non-polymorphic extractor implementation; this changes in the parser-core refactor.
                '--suppress=functionStatic',
                "--relative-paths=$script:RepositoryRoot",
                '--output-format=sarif',
                "--output-file=$reportPath",
                "--cppcheck-build-dir=$buildDirectory"
            )
        } catch {
            $failures.Add("${architecture}: $($_.Exception.Message)")
        } finally {
            if (Test-Path -LiteralPath $reportPath) {
                Write-Host "Report: $reportPath"
            }
        }
    }

    if ($failures.Count -gt 0) {
        throw "Cppcheck failed:`n$($failures -join "`n")"
    }
}

function Invoke-PowerShellAnalysis {
    $module = Get-Module -ListAvailable PSScriptAnalyzer |
        Sort-Object Version -Descending |
        Select-Object -First 1
    if (-not $module) {
        throw 'PSScriptAnalyzer was not found. Install-Module PSScriptAnalyzer -Scope CurrentUser.'
    }

    Import-Module $module.Path
    $settings = Join-Path $script:BuildRoot 'PSScriptAnalyzerSettings.psd1'
    $diagnostics = @(
        Invoke-ScriptAnalyzer -Path (Join-Path $script:RepositoryRoot 'build.ps1') -Settings $settings
        Invoke-ScriptAnalyzer -Path $script:BuildRoot -Recurse -Settings $settings
    )

    $rules = @(
        $diagnostics |
            Group-Object RuleName |
            ForEach-Object {
                [ordered]@{
                    id = $_.Name
                    name = $_.Name
                    shortDescription = [ordered]@{ text = $_.Group[0].Message }
                }
            }
    )
    $results = @(
        foreach ($diagnostic in $diagnostics) {
            $level = switch ($diagnostic.Severity.ToString()) {
                'Error' { 'error' }
                'Warning' { 'warning' }
                default { 'note' }
            }
            $relativePath = [System.IO.Path]::GetRelativePath($script:RepositoryRoot, $diagnostic.ScriptPath).Replace('\', '/')
            [ordered]@{
                ruleId = $diagnostic.RuleName
                level = $level
                message = [ordered]@{ text = $diagnostic.Message }
                locations = @(
                    [ordered]@{
                        physicalLocation = [ordered]@{
                            artifactLocation = [ordered]@{ uri = $relativePath }
                            region = [ordered]@{
                                startLine = [int] $diagnostic.Line
                                startColumn = [int] $diagnostic.Column
                            }
                        }
                    }
                )
            }
        }
    )
    $sarif = [ordered]@{
        version = '2.1.0'
        '$schema' = 'https://json.schemastore.org/sarif-2.1.0.json'
        runs = @(
            [ordered]@{
                tool = [ordered]@{
                    driver = [ordered]@{
                        name = 'PSScriptAnalyzer'
                        version = $module.Version.ToString()
                        informationUri = 'https://github.com/PowerShell/PSScriptAnalyzer'
                        rules = $rules
                    }
                }
                results = $results
            }
        )
    }
    $reportDirectory = Join-Path $script:ArtifactsRoot 'reports\psscriptanalyzer'
    $reportPath = Join-Path $reportDirectory 'psscriptanalyzer.sarif'
    New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
    $sarif | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $reportPath -Encoding utf8
    Write-Host "Report: $reportPath"

    if ($diagnostics.Count -gt 0) {
        Write-Host ($diagnostics | Format-Table -AutoSize | Out-String)
        throw "PSScriptAnalyzer reported $($diagnostics.Count) diagnostic(s)."
    }
}

function Invoke-BuildContractTest {
    $testDirectory = Join-Path $script:BuildRoot 'tests'
    $tests = @(Get-ChildItem -LiteralPath $testDirectory -File -Filter '*.Tests.ps1' | Sort-Object Name)
    if ($tests.Count -eq 0) {
        throw "No build contract tests were found in '$testDirectory'."
    }

    Write-Step "Running $($tests.Count) build contract test(s)"
    foreach ($test in $tests) {
        & $test.FullName
    }
}

function Invoke-Lint {
    param([Parameter(Mandatory)][string[]] $Architectures)

    $requestedArchitectures = $Architectures
    $failures = [System.Collections.Generic.List[string]]::new()
    foreach ($check in @(
        @{ Name = 'clang-format'; Action = { Invoke-FormatCheck } },
        @{ Name = 'Cppcheck'; Action = { Invoke-Cppcheck -Architectures $requestedArchitectures } },
        @{ Name = 'build contracts'; Action = { Invoke-BuildContractTest } },
        @{ Name = 'PSScriptAnalyzer'; Action = {
            Write-Step 'Checking PowerShell sources'
            Invoke-PowerShellAnalysis
        } }
    )) {
        try {
            & $check.Action
        } catch {
            $failures.Add("$($check.Name): $($_.Exception.Message)")
        }
    }

    if ($failures.Count -gt 0) {
        throw "Source checks failed:`n$($failures -join "`n")"
    }
}

function Invoke-CodeAnalysis {
    param([Parameter(Mandatory)][string[]] $Architectures)

    $llvm = Resolve-Llvm
    $reportDirectory = Join-Path $script:ArtifactsRoot 'reports\msvc'
    foreach ($architecture in $Architectures) {
        New-Item -ItemType Directory -Force -Path (Join-Path $reportDirectory $architecture) | Out-Null
    }
    try {
        Invoke-Build -Architectures $Architectures -Configuration 'Debug' -Target 'Rebuild' -Properties @{
            ObserverRunCodeAnalysis = 'true'
            ObserverAnalysisReportDirectory = $reportDirectory
            ObserverEnableClangTidy = 'true'
            LLVMInstallDir = $llvm.Root
        }
    } finally {
        foreach ($architecture in $Architectures) {
            Set-MSVCAnalysisSarifIdentity `
                -ReportDirectory (Join-Path $reportDirectory $architecture) `
                -Architecture $architecture

            $clangTidyReportPath = Join-Path `
                $script:ArtifactsRoot `
                "reports\clang-tidy\$architecture\clang-tidy.sarif"
            Export-ClangTidySarif `
                -RepositoryRoot $script:RepositoryRoot `
                -ObjectRoot (Join-Path $script:ArtifactsRoot "obj\$architecture") `
                -OutputPath $clangTidyReportPath `
                -Architecture $architecture
            Write-Host "clang-tidy SARIF report: $clangTidyReportPath"
        }
    }
    Write-Host "MSVC SARIF reports: $reportDirectory"
}

function Add-ASanRuntimeToPath {
    param([Parameter(Mandatory)][string] $Architecture)

    $visualStudio = Resolve-VisualStudio
    $targetDirectory = if ($Architecture -eq 'x86') { 'x86' } else { 'x64' }
    $runtimeDirectory = Join-Path $visualStudio.ToolsetDirectory "bin\Hostx64\$targetDirectory"
    $runtime = Get-ChildItem -File -LiteralPath $runtimeDirectory -Filter 'clang_rt.asan_dynamic-*.dll' | Select-Object -First 1
    if (-not $runtime) {
        throw "The MSVC AddressSanitizer runtime was not found in '$runtimeDirectory'."
    }
    [Environment]::SetEnvironmentVariable('PATH', "$runtimeDirectory;$env:PATH", 'Process')
}

function Invoke-ASan {
    param([Parameter(Mandatory)][string[]] $Architectures)

    foreach ($architecture in $Architectures) {
        if ($architecture -eq 'arm64') {
            throw 'MSVC AddressSanitizer does not support ARM64. Use x86 or x64.'
        }
    }

    Invoke-Restore -Architectures $Architectures -Flavor 'asan'
    Invoke-Build -Architectures $Architectures -Configuration 'ASan'
    $previousOptions = [Environment]::GetEnvironmentVariable('ASAN_OPTIONS', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('ASAN_OPTIONS', 'halt_on_error=1:alloc_dealloc_mismatch=1', 'Process')
        foreach ($architecture in $Architectures) {
            Add-ASanRuntimeToPath -Architecture $architecture
            Invoke-TestExecutable -Architecture $architecture -Configuration 'ASan'
        }
    } finally {
        [Environment]::SetEnvironmentVariable('ASAN_OPTIONS', $previousOptions, 'Process')
    }
}

function Invoke-Ubsan {
    param([Parameter(Mandatory)][string[]] $Architectures)

    foreach ($architecture in $Architectures) {
        if ($architecture -ne 'x64') {
            throw 'The clang-cl UBSan configuration is intentionally x64-only. Release and MSVC test builds still cover x86, x64, and ARM64.'
        }

        $llvm = Resolve-Llvm
        $llvmRuntimeDirectory = Get-ChildItem -Directory -Path (Join-Path $llvm.Root 'lib\clang\*\lib\windows') |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if (-not $llvmRuntimeDirectory) {
            throw "LLVM sanitizer runtimes were not found under '$($llvm.Root)'."
        }

        Invoke-Restore -Architectures @($architecture)
        Write-Step "Building tests with clang-cl UBSan for $architecture"
        Invoke-MSBuild -Target 'Build' -Architecture $architecture -Configuration 'UBSan' -Properties @{
            LLVMInstallDir = $llvm.Root
            LLVMRuntimeDir = $llvmRuntimeDirectory.FullName
        }

        $previousOptions = [Environment]::GetEnvironmentVariable('UBSAN_OPTIONS', 'Process')
        try {
            [Environment]::SetEnvironmentVariable('UBSAN_OPTIONS', 'halt_on_error=1:print_stacktrace=1', 'Process')
            Invoke-TestExecutable -Architecture $architecture -Configuration 'UBSan'
        } finally {
            [Environment]::SetEnvironmentVariable('UBSAN_OPTIONS', $previousOptions, 'Process')
        }
    }
}

function Read-LeakProbeMarker {
    param(
        [Parameter(Mandatory)][System.Diagnostics.Process] $Process,
        [Parameter(Mandatory)][string] $Marker,
        [Parameter()][string] $Label
    )

    while ($true) {
        try {
            $line = $Process.StandardOutput.ReadLineAsync().WaitAsync([TimeSpan]::FromSeconds(60)).GetAwaiter().GetResult()
        } catch [TimeoutException] {
            throw "Leak probe produced no $Marker $Label marker within 60 seconds."
        }
        if ($null -eq $line) {
            $errorOutput = $Process.StandardError.ReadToEnd()
            throw "Leak probe exited before $Marker $Label. $errorOutput"
        }
        Write-Host $line
        if ($line.StartsWith('OBSERVER_LEAK_PROBE|ERROR|', [StringComparison]::Ordinal)) {
            throw $line
        }

        $expectedPrefix = if ($Label) {
            "OBSERVER_LEAK_PROBE|$Marker|$Label|"
        } else {
            "OBSERVER_LEAK_PROBE|$Marker|"
        }
        if ($line.StartsWith($expectedPrefix, [StringComparison]::Ordinal)) {
            return $line
        }
    }
}

function Invoke-LeakProbePreflight {
    param(
        [Parameter(Mandatory)][ValidateSet('operations', 'lifecycle')][string] $Mode,
        [Parameter(Mandatory)][string] $Probe,
        [Parameter(Mandatory)][string] $BinaryDirectory
    )

    $expectedScenarios = 'small-success,malformed,cancellation,read-failure,write-failure,large-metadata,sparse-metadata'
    $output = Invoke-NativeCapture -FilePath $Probe -Arguments @(
        '--automatic',
        '--mode', $Mode,
        '--warmup', '1',
        '--iterations', '1',
        '--windows', '3'
    ) -WorkingDirectory $BinaryDirectory
    $ready = @($output | Where-Object { $_.StartsWith('OBSERVER_LEAK_PROBE|READY|', [StringComparison]::Ordinal) })
    if ($ready.Count -ne 1 -or
        $ready[0] -notmatch "\|mode=$Mode\|configuration=Release\|scenarios=$([regex]::Escape($expectedScenarios))$") {
        throw "Leak probe $Mode preflight did not report the required Release scenario contract."
    }
    if (@($output | Where-Object { $_.StartsWith('OBSERVER_LEAK_PROBE|DONE|', [StringComparison]::Ordinal) }).Count -ne 1) {
        throw "Leak probe $Mode preflight did not complete."
    }
}

function Assert-UmdhSnapshotUsable {
    param([Parameter(Mandatory)][string] $Path)

    $snapshot = Get-Content -Raw -LiteralPath $Path
    if ($snapshot -match "didn't find any allocations|database is full|stack trace database.*full") {
        throw "UMDH snapshot reports unusable allocation-stack data: $Path"
    }
    if ($snapshot -notmatch 'BackTrace') {
        throw "UMDH snapshot contains no allocation backtraces: $Path"
    }
}

function Compare-UmdhSnapshot {
    param(
        [Parameter(Mandatory)][string] $Umdh,
        [Parameter(Mandatory)][string] $Before,
        [Parameter(Mandatory)][string] $After,
        [Parameter(Mandatory)][string] $Output,
        [Parameter(Mandatory)][string] $Label
    )

    [void](Invoke-Native -FilePath $Umdh -Arguments @('-d', $Before, $After, "-f:$Output"))
    $lines = @(Get-Content -LiteralPath $Output)
    $totalLine = $lines | Where-Object { $_ -match '^Total (increase|decrease)\s*==' } | Select-Object -Last 1
    if (-not $totalLine -or $totalLine -notmatch '^Total (increase|decrease)\s*==\s*([0-9]+)') {
        throw "UMDH comparison did not contain a total allocation delta: $Output"
    }
    $direction = $Matches[1]
    $totalIncrease = [int64]::Parse($Matches[2], [Globalization.CultureInfo]::InvariantCulture)
    if ($direction -eq 'decrease') {
        $totalIncrease = -$totalIncrease
    }

    $positiveStacks = @{}
    foreach ($line in $lines) {
        if ($line -match '^\+\s+([0-9]+)\s+\([^)]*\)\s+[0-9]+\s+allocs\s+BackTrace\s*([0-9A-Fa-f]+)') {
            $positiveStacks[$Matches[2]] = [int64]::Parse($Matches[1], [Globalization.CultureInfo]::InvariantCulture)
        }
    }

    return [pscustomobject]@{
        Label = $Label
        TotalIncrease = $totalIncrease
        PositiveStacks = $positiveStacks
        Report = $Output
    }
}

function Invoke-LeakProbeMode {
    param(
        [Parameter(Mandatory)][ValidateSet('operations', 'lifecycle')][string] $Mode,
        [Parameter(Mandatory)][string] $Probe,
        [Parameter(Mandatory)][string] $BinaryDirectory,
        [Parameter(Mandatory)] $DebuggingTools,
        [Parameter(Mandatory)][string] $ReportDirectory,
        [Parameter(Mandatory)][int] $Warmup,
        [Parameter(Mandatory)][int] $Iterations,
        [Parameter(Mandatory)][int] $Windows,
        [Parameter(Mandatory)][int64] $ToleranceBytes
    )

    $modeDirectory = Join-Path $ReportDirectory $Mode
    New-Item -ItemType Directory -Force -Path $modeDirectory | Out-Null
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Probe
    $startInfo.WorkingDirectory = $BinaryDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    foreach ($argument in @('--mode', $Mode, '--warmup', $Warmup, '--iterations', $Iterations, '--windows', $Windows)) {
        $startInfo.ArgumentList.Add([string]$argument)
    }
    $startInfo.Environment['_NT_SYMBOL_PATH'] = $BinaryDirectory
    $startInfo.Environment['OANOCACHE'] = '1'

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $snapshots = [System.Collections.Generic.List[string]]::new()
    $started = $false
    try {
        if (-not $process.Start()) {
            throw "Failed to start leak probe: $Probe"
        }
        $started = $true
        [void](Read-LeakProbeMarker -Process $process -Marker 'READY')

        $labels = @('baseline') + @(1..$Windows | ForEach-Object { "window-$_" })
        foreach ($label in $labels) {
            $marker = Read-LeakProbeMarker -Process $process -Marker 'SNAPSHOT' -Label $label
            if ($marker -notmatch '\|pid=([0-9]+)\|') {
                throw "Leak probe snapshot marker has no PID: $marker"
            }
            $reportedPid = [int]$Matches[1]
            if ($reportedPid -ne $process.Id) {
                throw "Leak probe reported PID $reportedPid, but the started process is $($process.Id)."
            }

            $snapshot = Join-Path $modeDirectory "$label.txt"
            if ($label -eq 'baseline') {
                & $DebuggingTools.Umdh "-p:$($process.Id)" "-f:$snapshot"
                $snapshotExitCode = $LASTEXITCODE
                $snapshotText = if (Test-Path -LiteralPath $snapshot) {
                    Get-Content -Raw -LiteralPath $snapshot
                } else {
                    ''
                }
                if ($snapshotExitCode -notin @(0, 1) -or
                    ($snapshotExitCode -eq 1 -and $snapshotText -notmatch 'enabled allocation stack collection')) {
                    throw "UMDH could not prime allocation stack collection for PID $($process.Id) (exit $snapshotExitCode)."
                }
            } else {
                Invoke-Native -FilePath $DebuggingTools.Umdh -Arguments @("-p:$($process.Id)", "-f:$snapshot")
                Assert-UmdhSnapshotUsable -Path $snapshot
            }
            $snapshots.Add($snapshot)
            $process.StandardInput.WriteLine("continue|$label")
            $process.StandardInput.Flush()
        }

        [void](Read-LeakProbeMarker -Process $process -Marker 'DONE')
        if (-not $process.WaitForExit(120000)) {
            throw "Leak probe did not exit after its final snapshot ($Mode)."
        }
        $errorOutput = $process.StandardError.ReadToEnd()
        if ($process.ExitCode -ne 0) {
            throw "Leak probe failed with exit code $($process.ExitCode): $errorOutput"
        }
        if ($errorOutput) {
            Write-Host $errorOutput
        }
    } finally {
        if ($started -and -not $process.HasExited) {
            $process.Kill($true)
            $process.WaitForExit()
        }
        $process.Dispose()
    }

    # The first UMDH attachment enables per-process allocation stack collection without requiring an elevated,
    # persistent GFlags registry setting. It is deliberately a priming snapshot. window-1 becomes the measured
    # baseline after another complete workload window has run with stack collection active.
    $analysisSnapshots = @($snapshots | Select-Object -Skip 1)
    $comparisons = [System.Collections.Generic.List[object]]::new()
    for ($index = 1; $index -lt $analysisSnapshots.Count; ++$index) {
        $label = "window-$index"
        $comparisonPath = Join-Path $modeDirectory "growth-$label.txt"
        $comparisons.Add((Compare-UmdhSnapshot -Umdh $DebuggingTools.Umdh -Before $analysisSnapshots[$index - 1] -After $analysisSnapshots[$index] -Output $comparisonPath -Label $label))
    }
    $overallPath = Join-Path $modeDirectory 'growth-overall.txt'
    $overall = Compare-UmdhSnapshot -Umdh $DebuggingTools.Umdh -Before $analysisSnapshots[0] -After $analysisSnapshots[$analysisSnapshots.Count - 1] -Output $overallPath -Label 'overall'

    $last = $comparisons[$comparisons.Count - 1]
    $previous = $comparisons[$comparisons.Count - 2]
    $repeatedGrowingStacks = @(
        $last.PositiveStacks.Keys | Where-Object {
            $previous.PositiveStacks.ContainsKey($_) -and
            $last.PositiveStacks[$_] -gt $ToleranceBytes -and
            $previous.PositiveStacks[$_] -gt $ToleranceBytes
        }
    )
    $sustainedTotalGrowth = $last.TotalIncrease -gt $ToleranceBytes -and
        $previous.TotalIncrease -gt $ToleranceBytes -and $overall.TotalIncrease -gt (2 * $ToleranceBytes)

    $summary = [ordered]@{
        mode = $Mode
        warmupRounds = $Warmup
        iterationsPerWindow = $Iterations
        windows = $Windows
        initialSnapshot = 'UMDH stack-collection priming only'
        measuredBaseline = 'window-1'
        toleranceBytes = $ToleranceBytes
        totalGrowthByWindow = @($comparisons | ForEach-Object { $_.TotalIncrease })
        overallGrowthBytes = $overall.TotalIncrease
        repeatedGrowingStacks = $repeatedGrowingStacks
        passed = -not $sustainedTotalGrowth -and $repeatedGrowingStacks.Count -eq 0
    }
    $summaryPath = Join-Path $modeDirectory 'summary.json'
    $summary | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $summaryPath -Encoding utf8
    if (-not $summary.passed) {
        throw "UMDH found sustained heap growth in $Mode mode. Summary: $summaryPath"
    }
    Write-Host "[OK] UMDH $Mode mode: no sustained growth. Summary: $summaryPath"
}

function Invoke-LeakTest {
    param(
        [Parameter(Mandatory)][string[]] $Architectures,
        [Parameter(Mandatory)][int] $Warmup,
        [Parameter(Mandatory)][int] $Iterations,
        [Parameter(Mandatory)][int] $Windows,
        [Parameter(Mandatory)][int64] $ToleranceBytes
    )

    if ($Architectures.Count -ne 1 -or $Architectures[0] -ne 'x64') {
        throw 'UMDH leak testing is intentionally x64-only. Use -Arch x64.'
    }

    $debuggingTools = Resolve-WindowsDebuggingTool
    Invoke-Restore -Architectures @('x64')
    Write-Step 'Building the shipping x64 Release /MT leak probe and module DLLs'
    Invoke-MSBuild -Target 'BuildLeakProbe' -Architecture 'x64' -Configuration 'Release'
    $binaryDirectory = Get-BinaryDirectory -Architecture 'x64' -Configuration 'Release'
    $probe = Join-Path $binaryDirectory 'leak-probe.exe'
    if (-not (Test-Path -LiteralPath $probe -PathType Leaf)) {
        throw "Leak probe was not found: $probe"
    }

    $reportDirectory = Join-Path $script:ArtifactsRoot 'reports\leaks\x64'
    if (Test-Path -LiteralPath $reportDirectory) {
        Remove-Item -Recurse -Force -LiteralPath $reportDirectory
    }
    New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null

    $moduleEvidence = @(
        foreach ($moduleName in $script:ModuleNames) {
            Assert-ReleaseBinary -Architecture 'x64' -ModuleName $moduleName
            $modulePath = Join-Path $binaryDirectory "$moduleName.so"
            [ordered]@{
                module = $moduleName
                path = $modulePath
                sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $modulePath).Hash
            }
        }
    )
    $binaryEvidence = [ordered]@{
        architecture = 'x64'
        configuration = 'Release'
        runtimeLibrary = 'MT_StaticRelease'
        probe = [ordered]@{
            path = $probe
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $probe).Hash
        }
        modules = $moduleEvidence
    }
    $binaryEvidence | ConvertTo-Json -Depth 6 |
        Set-Content -LiteralPath (Join-Path $reportDirectory 'release-binaries.json') -Encoding utf8

    foreach ($mode in @('operations', 'lifecycle')) {
        Write-Step "Release leak-probe preflight: $mode"
        Invoke-LeakProbePreflight -Mode $mode -Probe $probe -BinaryDirectory $binaryDirectory
    }

    $previousSymbolPath = [Environment]::GetEnvironmentVariable('_NT_SYMBOL_PATH', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('_NT_SYMBOL_PATH', $binaryDirectory, 'Process')
        foreach ($mode in @('operations', 'lifecycle')) {
            Write-Step "UMDH leak test: $mode"
            Invoke-LeakProbeMode -Mode $mode -Probe $probe -BinaryDirectory $binaryDirectory -DebuggingTools $debuggingTools -ReportDirectory $reportDirectory -Warmup $Warmup -Iterations $Iterations -Windows $Windows -ToleranceBytes $ToleranceBytes
        }
    } finally {
        [Environment]::SetEnvironmentVariable('_NT_SYMBOL_PATH', $previousSymbolPath, 'Process')
    }
}

function Initialize-FuzzCorpus {
    param(
        [Parameter(Mandatory)][string] $SeedDirectory,
        [Parameter(Mandatory)][string] $CorpusDirectory
    )

    if (-not (Test-Path -LiteralPath $SeedDirectory -PathType Container)) {
        throw "Fuzzer seed directory was not found: $SeedDirectory"
    }
    New-Item -ItemType Directory -Force -Path $CorpusDirectory | Out-Null

    foreach ($seed in Get-ChildItem -LiteralPath $SeedDirectory -File) {
        if ($seed.Extension -eq '.hex') {
            $hex = (Get-Content -LiteralPath $seed.FullName -Raw) -replace '\s', ''
            if ($hex.Length -eq 0 -or $hex.Length % 2 -ne 0 -or $hex -notmatch '^[0-9A-Fa-f]+$') {
                throw "Invalid hexadecimal fuzzer seed: $($seed.FullName)"
            }
            $destination = Join-Path $CorpusDirectory $seed.BaseName
            [System.IO.File]::WriteAllBytes($destination, [Convert]::FromHexString($hex))
        } else {
            Copy-Item -LiteralPath $seed.FullName -Destination $CorpusDirectory -Force
        }
    }
}

function Invoke-Fuzz {
    param(
        [Parameter(Mandatory)][string[]] $Architectures,
        [Parameter(Mandatory)][int] $Seconds,
        [Parameter(Mandatory)][string] $TargetName
    )

    foreach ($architecture in $Architectures) {
        if ($architecture -ne 'x64') {
            throw 'The LLVM libFuzzer configuration is intentionally x64-only. Release and MSVC test builds still cover x86, x64, and ARM64.'
        }

        $llvm = Resolve-Llvm
        $llvmRuntimeDirectory = Get-ChildItem -Directory -Path (Join-Path $llvm.Root 'lib\clang\*\lib\windows') |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if (-not $llvmRuntimeDirectory) {
            throw "LLVM sanitizer runtimes were not found under '$($llvm.Root)'."
        }

        Invoke-Restore -Architectures @($architecture) -Flavor 'asan'
        Write-Step "Building all parser fuzzers for $architecture"
        Invoke-MSBuild -Target 'BuildFuzz' -Architecture $architecture -Configuration 'Fuzz' -Properties @{
            LLVMInstallDir = $llvm.Root
            LLVMRuntimeDir = $llvmRuntimeDirectory.FullName
        }

        [Environment]::SetEnvironmentVariable('PATH', "$($llvmRuntimeDirectory.FullName);$env:PATH", 'Process')

        $targets = @(
            [pscustomobject]@{ Name = 'pickle'; MaxLength = 262144 },
            [pscustomobject]@{ Name = 'renpy'; MaxLength = 1048576 },
            [pscustomobject]@{ Name = 'rpgmaker'; MaxLength = 1048576 },
            [pscustomobject]@{ Name = 'zanzarah'; MaxLength = 1048576 }
        )
        if ($TargetName -ne 'all') {
            $targets = @($targets | Where-Object Name -eq $TargetName)
        }
        $binaryDirectory = Get-BinaryDirectory -Architecture $architecture -Configuration 'Fuzz'
        $previousOptions = [Environment]::GetEnvironmentVariable('ASAN_OPTIONS', 'Process')
        try {
            [Environment]::SetEnvironmentVariable('ASAN_OPTIONS', 'halt_on_error=1:alloc_dealloc_mismatch=1', 'Process')
            foreach ($target in $targets) {
                $fuzzer = Join-Path $binaryDirectory "fuzz-$($target.Name).exe"
                if (-not (Test-Path -LiteralPath $fuzzer -PathType Leaf)) {
                    throw "Fuzzer executable was not found: $fuzzer"
                }

                $seedCorpusDirectory = Join-Path $script:RepositoryRoot "src\fuzz\corpus\$($target.Name)"
                $fuzzDirectory = Join-Path $script:ArtifactsRoot "fuzz\$architecture\$($target.Name)"
                $corpusDirectory = Join-Path $fuzzDirectory 'corpus'
                $artifactDirectory = Join-Path $fuzzDirectory 'artifacts'
                if (Test-Path -LiteralPath $artifactDirectory) {
                    Remove-Item -Recurse -Force -LiteralPath $artifactDirectory
                }
                New-Item -ItemType Directory -Force -Path $artifactDirectory | Out-Null

                $seedReplayDirectory = Join-Path $fuzzDirectory "seed-replay\$([Guid]::NewGuid().ToString('N'))"
                Initialize-FuzzCorpus -SeedDirectory $seedCorpusDirectory -CorpusDirectory $seedReplayDirectory
                $seedInputs = @(Get-ChildItem -LiteralPath $seedReplayDirectory -File | Sort-Object Name)
                if ($seedInputs.Count -eq 0) {
                    throw "No checked-in fuzzer seeds were found for $($target.Name)."
                }

                Write-Step "Replaying $($seedInputs.Count) checked-in $($target.Name) seed(s)"
                Invoke-Native -FilePath $fuzzer -Arguments (@($seedInputs.FullName) + @(
                        "-max_len=$($target.MaxLength)",
                        '-rss_limit_mb=1024',
                        '-timeout=10',
                        '-print_final_stats=1',
                        "-artifact_prefix=$artifactDirectory\"
                    )) -WorkingDirectory $binaryDirectory

                Initialize-FuzzCorpus -SeedDirectory $seedCorpusDirectory -CorpusDirectory $corpusDirectory

                Write-Step "Fuzzing $($target.Name) for $Seconds second(s)"
                Invoke-Native -FilePath $fuzzer -Arguments @(
                    $corpusDirectory,
                    "-max_total_time=$Seconds",
                    "-max_len=$($target.MaxLength)",
                    '-rss_limit_mb=1024',
                    '-timeout=10',
                    '-use_value_profile=1',
                    '-print_final_stats=1',
                    "-artifact_prefix=$artifactDirectory\"
                ) -WorkingDirectory $binaryDirectory
            }
        } finally {
            [Environment]::SetEnvironmentVariable('ASAN_OPTIONS', $previousOptions, 'Process')
        }
    }
}

function Invoke-Coverage {
    param(
        [Parameter(Mandatory)][string[]] $Architectures,
        [Parameter()][string] $CorpusPath,
        [Parameter(Mandatory)][double] $Threshold
    )

    $llvm = Resolve-Llvm
    $ignoredSources = '([\\/]src[\\/](tests|fuzz)[\\/])|([\\/]vcpkg_installed[\\/])|([\\/]Microsoft Visual Studio[\\/])|([\\/]Windows Kits[\\/])'

    foreach ($architecture in $Architectures) {
        if (-not (Test-CanRunArchitecture $architecture)) {
            throw "The current host cannot execute $architecture coverage binaries."
        }

        Invoke-Build -Architectures @($architecture) -Configuration 'Coverage' -Target 'Rebuild' -Properties @{
            LLVMInstallDir = $llvm.Root
        }
        $binaryDirectory = Get-BinaryDirectory -Architecture $architecture -Configuration 'Coverage'
        $coverageDirectory = Join-Path $script:ArtifactsRoot "coverage\$architecture"
        $runDirectory = Join-Path $coverageDirectory ([Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $runDirectory | Out-Null
        $rawProfilePattern = Join-Path $runDirectory 'observer-%m-%p.profraw'
        $profilePath = Join-Path $coverageDirectory 'coverage.profdata'
        $jsonReportPath = Join-Path $coverageDirectory 'coverage.json'
        $lcovReportPath = Join-Path $coverageDirectory 'coverage.lcov'
        $testExecutable = Join-Path $binaryDirectory 'tests.exe'
        $coverageObjects = @($script:ModuleNames | ForEach-Object { Join-Path $binaryDirectory "$_.so" })

        $previousCorpus = [Environment]::GetEnvironmentVariable('OBSERVER_TEST_CORPUS', 'Process')
        $previousProfile = [Environment]::GetEnvironmentVariable('LLVM_PROFILE_FILE', 'Process')
        try {
            if ($CorpusPath) {
                [Environment]::SetEnvironmentVariable('OBSERVER_TEST_CORPUS', (Resolve-UserPath -Path $CorpusPath), 'Process')
            }
            [Environment]::SetEnvironmentVariable('LLVM_PROFILE_FILE', $rawProfilePattern, 'Process')
            Write-Step "Collecting LLVM source coverage for $architecture"
            $testReportPath = Get-TestReportPath -Architecture $architecture -Configuration 'Coverage'
            Invoke-Native -FilePath $testExecutable -Arguments @(
                '--reporter', 'compact',
                '--reporter', "JUnit::out=$testReportPath",
                '--durations', 'yes',
                '--order', 'lex'
            ) -WorkingDirectory $binaryDirectory
            Write-Host "Test report: $testReportPath"
        } finally {
            [Environment]::SetEnvironmentVariable('OBSERVER_TEST_CORPUS', $previousCorpus, 'Process')
            [Environment]::SetEnvironmentVariable('LLVM_PROFILE_FILE', $previousProfile, 'Process')
        }

        $rawProfiles = @(Get-ChildItem -File -LiteralPath $runDirectory -Filter '*.profraw')
        if ($rawProfiles.Count -eq 0) {
            throw "The instrumented test run produced no LLVM raw profiles in '$runDirectory'."
        }

        $mergeArguments = @('merge', '-sparse') + @($rawProfiles.FullName) + @('-o', $profilePath)
        Invoke-Native -FilePath $llvm.Profdata -Arguments $mergeArguments

        $objectArguments = @($coverageObjects | ForEach-Object { @('--object', $_) })
        $commonArguments = @(
            $testExecutable,
            '--instr-profile', $profilePath,
            '--ignore-filename-regex', $ignoredSources
        ) + $objectArguments

        $summaryJson = Invoke-NativeCapture -FilePath $llvm.Cov -Arguments (@('export') + $commonArguments + @('--summary-only'))
        $summaryText = @($summaryJson | Where-Object { $_ -notmatch '^warning:' }) -join "`n"
        Set-Content -LiteralPath $jsonReportPath -Value $summaryText -Encoding utf8
        $report = $summaryText | ConvertFrom-Json
        $totals = $report.data[0].totals
        if ($totals.branches.count -eq 0) {
            throw "LLVM coverage report contains no source branches: $jsonReportPath"
        }

        Invoke-Native -FilePath $llvm.Cov -Arguments (@('report') + $commonArguments + @('--show-branch-summary'))
        $lcov = Invoke-NativeCapture -FilePath $llvm.Cov -Arguments (@('export') + $commonArguments + @('--format=lcov'))
        $lcovText = @($lcov | Where-Object { $_ -notmatch '^warning:' }) -join "`n"
        Set-Content -LiteralPath $lcovReportPath -Value $lcovText -Encoding utf8

        $reportedMetrics = @(
            [pscustomobject]@{ Name = 'branches'; Value = $totals.branches },
            [pscustomobject]@{ Name = 'functions'; Value = $totals.functions },
            [pscustomobject]@{ Name = 'lines'; Value = $totals.lines },
            [pscustomobject]@{ Name = 'regions'; Value = $totals.regions }
        )
        $requiredMetrics = @($reportedMetrics | Where-Object { $_.Name -in @('branches', 'lines') })
        $failedMetrics = @($requiredMetrics | Where-Object { $_.Value.percent -lt $Threshold })

        Write-Host "Reports: $jsonReportPath, $lcovReportPath"
        if ($failedMetrics.Count -gt 0) {
            $failures = $failedMetrics | ForEach-Object { "{0}={1:N2}%" -f $_.Name, $_.Value.percent }
            throw "Coverage is below the required $Threshold%: $($failures -join ', ')."
        }
    }
}

function Assert-ReleaseBinary {
    param(
        [Parameter(Mandatory)][string] $Architecture,
        [Parameter(Mandatory)][string] $ModuleName
    )

    $visualStudio = Resolve-VisualStudio
    $binary = Join-Path (Get-BinaryDirectory -Architecture $Architecture -Configuration 'Release') "$ModuleName.so"
    if (-not (Test-Path -LiteralPath $binary)) {
        throw "Release module was not found: $binary"
    }

    $headers = Invoke-NativeCapture -FilePath $visualStudio.Dumpbin -Arguments @('/headers', $binary)
    $expectedMachine = switch ($Architecture) {
        'x86' { '14C machine \(x86\)' }
        'x64' { '8664 machine \(x64\)' }
        'arm64' { 'AA64 machine \(ARM64\)' }
    }
    if (($headers -join "`n") -notmatch $expectedMachine) {
        throw "$ModuleName has the wrong PE machine type for $Architecture."
    }

    $dependencyOutput = Invoke-NativeCapture -FilePath $visualStudio.Dumpbin -Arguments @('/dependents', $binary)
    $dependencies = @(
        $dependencyOutput |
            ForEach-Object {
                if ($_ -match '^\s+([A-Za-z0-9._-]+\.dll)\s*$') { $Matches[1] }
            } |
            Select-Object -Unique
    )
    $allowedDependencies = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    @('KERNEL32.dll', 'USER32.dll', 'ADVAPI32.dll', 'SHELL32.dll', 'OLE32.dll', 'OLEAUT32.dll', 'SHLWAPI.dll', 'BCRYPT.dll', 'NTDLL.dll') |
        ForEach-Object { [void]$allowedDependencies.Add($_) }
    $forbiddenRuntimePattern = '^(VCRUNTIME|MSVCP|UCRTBASE|api-ms-win-crt-|ext-ms-win-crt-|zlib|zstd|xxhash|clang_rt\.).*\.dll$'
    $unexpectedDependencies = @(
        $dependencies | Where-Object {
            $_ -match $forbiddenRuntimePattern -or
            (-not $allowedDependencies.Contains($_) -and $_ -notmatch '^(api|ext)-ms-win-.*\.dll$')
        }
    )
    if ($unexpectedDependencies.Count -gt 0) {
        throw "$ModuleName has non-system DLL dependencies: $($unexpectedDependencies -join ', ')"
    }

    $exportsOutput = Invoke-NativeCapture -FilePath $visualStudio.Dumpbin -Arguments @('/exports', $binary)
    $exports = @(
        $exportsOutput | ForEach-Object {
            if ($_ -match '^\s+\d+\s+[0-9A-F]+\s+[0-9A-F]+\s+(\S+)') { $Matches[1] }
        }
    )
    $expectedExports = @('LoadSubModule', 'UnloadSubModule')
    $exportDifference = @(
        Compare-Object -ReferenceObject $expectedExports -DifferenceObject $exports |
            ForEach-Object { "$($_.SideIndicator)$($_.InputObject)" }
    )
    if ($exportDifference.Count -gt 0) {
        throw "$ModuleName has an unexpected export surface: $($exportDifference -join ', ')"
    }

    Write-Host "[OK] $ModuleName.so: $Architecture, static runtime/dependencies, expected Observer exports"
}

function Invoke-BinSkimAudit {
    param([Parameter(Mandatory)][string] $Architecture)

    $binSkim = Resolve-BinSkim
    $binaryDirectory = Get-BinaryDirectory -Architecture $Architecture -Configuration 'Release'
    $reportDirectory = Join-Path $script:ArtifactsRoot 'audit'
    $reportPath = Join-Path $reportDirectory "binskim-$Architecture.sarif"
    New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null

    $targets = @($script:ModuleNames | ForEach-Object { Join-Path $binaryDirectory "$_.so" })
    $arguments = @(
        'analyze'
    ) + $targets + @(
        '--level', 'Error;Warning',
        '--kind', 'Fail',
        '--local-symbol-directories', $binaryDirectory,
        '--output', $reportPath,
        '--log', 'ForceOverwrite',
        '--quiet',
        '--disable-telemetry'
    )
    Invoke-Native -FilePath $binSkim -Arguments $arguments

    $report = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json -Depth 100
    $ruleLevels = @{}
    foreach ($run in $report.runs) {
        foreach ($rule in @($run.tool.driver.rules)) {
            $defaultConfiguration = $rule.PSObject.Properties['defaultConfiguration']
            $configuredLevel = if ($defaultConfiguration) {
                $defaultConfiguration.Value.PSObject.Properties['level']
            } else {
                $null
            }
            $ruleLevels[$rule.id] = if ($configuredLevel) { [string]$configuredLevel.Value } else { 'warning' }
        }
    }

    $results = @($report.runs | ForEach-Object { @($_.results) })
    $approvedWarnings = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    [void]$approvedWarnings.Add('BA2027') # SourceLink is tracked explicitly in docs/autonomous-work-log.md.
    $unexpectedFindings = [System.Collections.Generic.List[string]]::new()

    foreach ($result in $results) {
        $resultLevelProperty = $result.PSObject.Properties['level']
        $effectiveLevel = if ($resultLevelProperty) { [string]$resultLevelProperty.Value } else { $ruleLevels[$result.ruleId] }
        if ($effectiveLevel -notin @('error', 'warning')) {
            continue
        }

        $artifactUri = [string]$result.locations[0].physicalLocation.artifactLocation.uri
        $binaryName = [System.IO.Path]::GetFileName(([uri]$artifactUri).LocalPath)
        $findingKey = "${binaryName}:$($result.ruleId)"
        if ($effectiveLevel -eq 'error' -or -not $approvedWarnings.Contains([string]$result.ruleId)) {
            $unexpectedFindings.Add("${effectiveLevel}:$findingKey")
        }
    }

    if ($unexpectedFindings.Count -gt 0) {
        throw "BinSkim found unapproved findings: $($unexpectedFindings -join ', '). Report: $reportPath"
    }
    Write-Host "[OK] BinSkim: $($results.Count) finding(s), no unapproved errors. Report: $reportPath"
}

function Invoke-Audit {
    param(
        [Parameter(Mandatory)][string[]] $Architectures,
        [switch] $BuildFirst
    )

    if ($BuildFirst) {
        Invoke-Build -Architectures $Architectures -Configuration 'Release'
    }
    foreach ($architecture in $Architectures) {
        Write-Step "Auditing Release binaries for $architecture"
        foreach ($moduleName in $script:ModuleNames) {
            Assert-ReleaseBinary -Architecture $architecture -ModuleName $moduleName
        }
        Invoke-BinSkimAudit -Architecture $architecture
    }
}

function Invoke-Doctor {
    $checks = [System.Collections.Generic.List[object]]::new()
    $checks.Add([pscustomobject]@{ Tool = 'PowerShell'; Status = 'OK'; Detail = $PSVersionTable.PSVersion.ToString() })

    foreach ($probe in @(
        @{ Name = 'Visual Studio/MSVC'; Action = { $value = Resolve-VisualStudio; "$($value.InstallationPath), MSVC $($value.ToolsetVersion)" } },
        @{ Name = 'vcpkg'; Action = { (Resolve-Vcpkg).Root } },
        @{ Name = 'LLVM'; Action = { (Resolve-Llvm).Root } },
        @{ Name = 'Cppcheck'; Action = { Resolve-Cppcheck } },
        @{ Name = 'BinSkim'; Action = { Resolve-BinSkim } },
        @{ Name = 'UMDH'; Action = { (Resolve-WindowsDebuggingTool).Umdh } },
        @{ Name = 'PSScriptAnalyzer'; Action = {
            $module = Get-Module -ListAvailable PSScriptAnalyzer | Sort-Object Version -Descending | Select-Object -First 1
            if (-not $module) { throw 'PSScriptAnalyzer module was not found.' }
            $module.Version.ToString()
        } }
    )) {
        try {
            $detail = & $probe.Action
            if (-not $detail) { throw 'not found' }
            $checks.Add([pscustomobject]@{ Tool = $probe.Name; Status = 'OK'; Detail = $detail })
        } catch {
            $checks.Add([pscustomobject]@{ Tool = $probe.Name; Status = 'MISSING'; Detail = $_.Exception.Message })
        }
    }

    Write-Host ($checks | Format-Table -AutoSize -Wrap | Out-String)
    if ($checks.Status -contains 'MISSING') {
        Write-Host 'Core build commands can still work when only analysis/coverage tools are missing.' -ForegroundColor Yellow
    }
}

function Invoke-Clean {
    $resolvedArtifacts = [System.IO.Path]::GetFullPath($script:ArtifactsRoot)
    $resolvedRepository = [System.IO.Path]::GetFullPath($script:RepositoryRoot)
    if (-not $resolvedArtifacts.StartsWith($resolvedRepository, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean path outside the repository: $resolvedArtifacts"
    }
    if (Test-Path -LiteralPath $resolvedArtifacts) {
        Remove-Item -Recurse -Force -LiteralPath $resolvedArtifacts
        Write-Host "Removed $resolvedArtifacts"
    }
}

function Show-Help {
    Write-Host @'
ObserverModules build entry point

  doctor                         inspect the complete toolchain
  restore  -Arch <list|all>      restore pinned static vcpkg dependencies
  build    -Arch <list|all>      build modules and tests
  test     -Arch <list>          build and run deterministic/corpus tests
  source-checks -Arch <list|all> clang-format, Cppcheck, PSScriptAnalyzer
  compiler-analysis -Arch <list> MSVC /analyze plus clang-tidy
  test-coverage -Arch <list>     run tests and enforce llvm-cov source coverage
  test-asan -Arch <x86|x64>      build dependencies/code and run tests with MSVC ASan
  test-ubsan -Arch x64           build and run tests with clang-cl UBSan
  test-leaks -Arch x64           run UMDH operation and DLL-lifecycle leak checks
  fuzz     -Arch x64             build and run one or all libFuzzer targets
  audit-binaries -Arch <list|all> build and inspect Release PE files with dumpbin
  package  -Arch <list|all>      module ZIPs plus one combined PDB ZIP
  verify   -Arch <list|all>      complete host-capable gate with explicit native deferrals
  clean                           remove .artifacts

Options:
  -Config Debug|Release
  -Corpus <path>
  -RestoreFlavor default|asan|all  restore only; "all" prepares serial DAG dependency flavors
  -SkipDependencyRestore          DAG leaf only; dependencies must already be restored
  -FuzzSeconds <seconds>
  -FuzzTarget <name|all>         pickle, renpy, rpgmaker, zanzarah, or all (default)
  -LeakWarmup <rounds>
  -LeakIterations <rounds per window>
  -LeakWindows <3..10>
  -LeakToleranceBytes <bytes>    defaults to zero; use only for a reviewed stack-specific exception
  -CoverageThreshold <0..100>    defaults to 100 for source lines and branches
'@
}

$architectures = Get-RequestedArchitecture -Requested $Arch
switch ($Command) {
    'help' { Show-Help }
    'doctor' { Invoke-Doctor }
    'restore' {
        if ($SkipDependencyRestore) {
            throw '-SkipDependencyRestore cannot be used with the restore command.'
        }
        Invoke-Restore -Architectures $architectures -Flavor $RestoreFlavor
    }
    'build' {
        Invoke-Restore -Architectures $architectures
        Invoke-Build -Architectures $architectures -Configuration $Config
    }
    'test' {
        Invoke-Restore -Architectures $architectures
        Invoke-Test -Architectures $architectures -Configuration $Config -CorpusPath $Corpus
    }
    'source-checks' {
        Invoke-Restore -Architectures $architectures
        Invoke-Lint -Architectures $architectures
    }
    'compiler-analysis' {
        Invoke-Restore -Architectures $architectures
        Invoke-CodeAnalysis -Architectures $architectures
    }
    'test-coverage' {
        Invoke-Restore -Architectures $architectures
        Invoke-Coverage -Architectures $architectures -CorpusPath $Corpus -Threshold $CoverageThreshold
    }
    'test-asan' { Invoke-ASan -Architectures $architectures }
    'test-ubsan' { Invoke-Ubsan -Architectures $architectures }
    'test-leaks' {
        Invoke-LeakTest -Architectures $architectures -Warmup $LeakWarmup -Iterations $LeakIterations -Windows $LeakWindows -ToleranceBytes $LeakToleranceBytes
    }
    'fuzz' { Invoke-Fuzz -Architectures $architectures -Seconds $FuzzSeconds -TargetName $FuzzTarget }
    'audit-binaries' {
        Invoke-Restore -Architectures $architectures
        Invoke-Audit -Architectures $architectures -BuildFirst
    }
    'package' { Invoke-Package -Architectures $architectures }
    'verify' {
        Invoke-Verify `
            -Architectures $architectures `
            -CorpusPath $Corpus `
            -RequiredCoverageThreshold $CoverageThreshold `
            -RequiredFuzzSeconds $FuzzSeconds `
            -RequiredLeakWarmup $LeakWarmup `
            -RequiredLeakIterations $LeakIterations `
            -RequiredLeakWindows $LeakWindows `
            -RequiredLeakToleranceBytes $LeakToleranceBytes
    }
    'clean' { Invoke-Clean }
}

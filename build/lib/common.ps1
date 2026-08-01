#requires -Version 7.4

Set-StrictMode -Version Latest

function Write-Step {
    param([Parameter(Mandatory)][string] $Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Invoke-Native {
    param(
        [Parameter(Mandatory)][string] $FilePath,
        [Parameter()][string[]] $Arguments = @(),
        [Parameter()][string] $WorkingDirectory = $script:RepositoryRoot
    )

    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
        }
    } finally {
        Pop-Location
    }
}

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory)][string] $FilePath,
        [Parameter()][string[]] $Arguments = @(),
        [Parameter()][string] $WorkingDirectory = $script:RepositoryRoot
    )

    Push-Location $WorkingDirectory
    try {
        $output = @(& $FilePath @Arguments 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')`n$($output -join "`n")"
        }
        return @($output | ForEach-Object { $_.ToString() })
    } finally {
        Pop-Location
    }
}

function Get-RequestedArchitecture {
    param([Parameter(Mandatory)][string[]] $Requested)

    $requested = @(
        $Requested |
            ForEach-Object { $_ -split ',' } |
            ForEach-Object { $_.Trim().ToLowerInvariant() } |
            Where-Object { $_ }
    )

    if ($requested -contains 'all') {
        return $script:KnownArchitectures
    }

    foreach ($item in $requested) {
        if ($item -notin $script:KnownArchitectures) {
            throw "Unknown architecture '$item'. Expected x86, x64, arm64, or all."
        }
    }

    return @($requested | Select-Object -Unique)
}

function Get-MSBuildPlatform {
    param([Parameter(Mandatory)][string] $Architecture)

    switch ($Architecture) {
        'x86' { return 'Win32' }
        'x64' { return 'x64' }
        'arm64' { return 'ARM64' }
        default { throw "Unsupported architecture '$Architecture'." }
    }
}

function Get-VcpkgTriplet {
    param(
        [Parameter(Mandatory)][string] $Architecture,
        [Parameter()][ValidateSet('default', 'asan')][string] $Flavor = 'default'
    )

    $suffix = if ($Flavor -eq 'asan') { '-asan' } else { '' }
    return "observer-$Architecture-windows-static$suffix"
}

function Get-BinaryDirectory {
    param(
        [Parameter(Mandatory)][string] $Architecture,
        [Parameter(Mandatory)][string] $Configuration
    )
    return Join-Path $script:ArtifactsRoot "bin\$Architecture\$Configuration"
}

function Resolve-UserPath {
    param([Parameter(Mandatory)][string] $Path)

    if ([System.IO.Path]::IsPathFullyQualified($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $script:RepositoryRoot $Path))
}

#requires -Version 7.4

[CmdletBinding(PositionalBinding = $false)]
param(
    # PowerShell parses an unquoted `x86,x64` in argument mode as an array literal. Collect the
    # command line as written so that splatting cannot expand one written value into several driver
    # arguments, which is how every documented multi-value example used to fail.
    [Parameter(ValueFromRemainingArguments = $true)]
    [AllowEmptyCollection()]
    [object[]] $DriverArguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$forwarded = [string[]]@(
    foreach ($argument in $DriverArguments) {
        if ($argument -is [string]) {
            $argument
        }
        elseif ($argument -is [System.Collections.IEnumerable]) {
            # Rejoin any array literal, so every multi-value option keeps its documented comma form.
            [string]::Join(',', @($argument | ForEach-Object { [string]$_ }))
        }
        else {
            [string]$argument
        }
    }
)

$buildProject = Join-Path $PSScriptRoot 'build'
$driverScript = Join-Path $buildProject 'main.py'
$env:UV_CACHE_DIR = Join-Path $buildProject '.uv-cache'

if ($env:UNPACKRAT_BUILD_PRINT_ARGV -eq '1') {
    # Contract-test seam: report the driver argv instead of building anything. The distinctive
    # failure code keeps an accidentally leaked variable from passing as a successful build.
    ConvertTo-Json -InputObject (@($driverScript) + $forwarded) -Compress
    exit 97
}

& uv run --project $buildProject --frozen --no-sync python $driverScript @forwarded
exit $LASTEXITCODE

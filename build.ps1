#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$buildProject = Join-Path $PSScriptRoot 'tools\build'
$env:UV_CACHE_DIR = Join-Path $buildProject '.uv-cache'
& uv run --project $buildProject --frozen --no-sync python (Join-Path $buildProject 'main.py') @args
exit $LASTEXITCODE

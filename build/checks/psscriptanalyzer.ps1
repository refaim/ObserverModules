#requires -Version 7.4

[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Mandatory)][string] $Module,
    [Parameter(Mandatory)][string] $Settings,
    [Parameter(Mandatory)][string] $Output,
    [Parameter(ValueFromRemainingArguments = $true)][AllowEmptyCollection()][string[]] $Source = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module -Name $Module

# One branchless pass: the analyzer accepts a pipeline of paths, so an empty list simply yields
# no records.  The raw records are serialised with the severity kept as its name, and the caller
# maps RuleName/Severity/Message/Line/Column/ScriptPath into SARIF.
$records = @($Source | Invoke-ScriptAnalyzer -Settings $Settings)
ConvertTo-Json -InputObject @($records) -Depth 4 -EnumsAsStrings |
    Set-Content -LiteralPath $Output -Encoding utf8

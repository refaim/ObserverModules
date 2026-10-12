#requires -Version 7.4

[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Mandatory)][string] $Settings,
    [Parameter(ValueFromRemainingArguments = $true)][AllowEmptyCollection()][string[]] $Source = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module -Name PSScriptAnalyzer

# One branchless pass over every source. The settings file restricts the analyzer to the
# Warning and Error severities, so any returned record is a real finding and the exit code is
# the gate; informational messages are never requested and never fail the run.
$records = @($Source | Invoke-ScriptAnalyzer -Settings $Settings)
if ($records.Count -gt 0) {
    $records | Format-List -Property RuleName, Severity, ScriptName, Line, Message | Out-String | Write-Error
    throw "PSScriptAnalyzer reported $($records.Count) finding(s)."
}

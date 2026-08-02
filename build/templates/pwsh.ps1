{% extends "script.json" %}

{% block script_exec %}
[{{ pwsh | json }},"-NoLogo","-NoProfile","-NonInteractive","-Command","$ErrorActionPreference = 'Stop'; & ([ScriptBlock]::Create([Console]::In.ReadToEnd()))"]
{% endblock %}

{% block script_body %}
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$outDir = $env:OBSERVER_OUT_DIR
$buildDir = $env:OBSERVER_BUILD_DIR
if ([string]::IsNullOrWhiteSpace($outDir) -or [string]::IsNullOrWhiteSpace($buildDir)) {
    throw 'OBSERVER_OUT_DIR and OBSERVER_BUILD_DIR are required'
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory)][string] $FilePath,
        [Parameter()][string[]] $ArgumentList = @()
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath"
    }
}

{% block pwsh_body required %}{% endblock %}
{% endblock %}

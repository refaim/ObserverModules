{% extends "pwsh.ps1" %}
{% block pwsh_body %}
$status = {{ status | ps_quote }}
if (-not (Test-Path -LiteralPath $status -PathType Leaf)) { throw 'Fuzzer status was not found' }
$exitCode = 0
if (-not [int]::TryParse((Get-Content -LiteralPath $status -Raw), [ref]$exitCode)) {
    throw 'Fuzzer status is not an integer'
}
if ($exitCode -ne 0) { throw "Fuzzer exited with code $exitCode" }
{% endblock %}

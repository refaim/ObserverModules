{% extends "pwsh.ps1" %}
{% block pwsh_body %}
$profiles = @(
{% for directory in profile_directories %}    Get-ChildItem -LiteralPath {{ directory | ps_quote }} -File -Filter '*.profraw'
{% endfor %}) | Sort-Object FullName | ForEach-Object FullName
if ($profiles.Count -eq 0) {
    throw 'Coverage shards produced no LLVM raw profiles'
}
$arguments = @('merge', '-sparse') + $profiles + @('-o', (Join-Path $outDir 'coverage.profdata'))
Invoke-Checked {{ llvm_profdata | ps_quote }} $arguments
{% endblock %}

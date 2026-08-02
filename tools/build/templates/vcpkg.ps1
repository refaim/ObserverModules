{% extends "pwsh.ps1" %}
{% block pwsh_body %}
Invoke-Checked {{ vcpkg | ps_quote }} @(
    'install'
    "--x-install-root=$outDir"
    '--triplet'
    {{ triplet | ps_quote }}
    {{ ('--x-manifest-root=' ~ repository) | ps_quote }}
    {{ ('--overlay-triplets=' ~ repository ~ '\\build\\vcpkg\\triplets') | ps_quote }}
)
if (-not (Test-Path -LiteralPath (Join-Path $outDir {{ (triplet ~ '\\include') | ps_quote }}) -PathType Container)) {
    throw 'vcpkg restore did not produce the include directory'
}
{% endblock %}

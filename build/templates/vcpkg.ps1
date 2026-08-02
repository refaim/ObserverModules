{% extends "pwsh.ps1" %}{% from "_output.ps1" import require_output %}
{% block pwsh_body %}
Invoke-Checked {{ vcpkg | ps_quote }} @(
    'install'
    "--x-install-root=$outDir"
    "--x-buildtrees-root=$buildDir\b"
    "--x-packages-root=$buildDir\p"
    '--triplet'
    {{ triplet | ps_quote }}
    {{ ('--x-manifest-root=' ~ repository) | ps_quote }}
    {{ ('--overlay-triplets=' ~ repository ~ '\\build\\vcpkg\\triplets') | ps_quote }}
)
{{ require_output(triplet ~ '\\include', 'vcpkg restore did not produce the include directory', 'Container') }}
{% endblock %}

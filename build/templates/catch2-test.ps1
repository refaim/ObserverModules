{% extends "pwsh.ps1" %}{% from "_output.ps1" import require_output %}
{% block pwsh_body %}
{% for artifact in artifacts %}Copy-Item -LiteralPath {{ artifact.source | ps_quote }} -Destination (Join-Path $outDir {{ artifact.name | ps_quote }})
{% endfor %}
{% block catch2_setup %}{% endblock %}Push-Location $outDir
try {
    Invoke-Checked (Join-Path $outDir 'tests.exe') @(
{% block test_filter %}{% endblock %}
        '--reporter'
        'compact'
        '--reporter'
        'JUnit::out=tests.xml'
        '--durations'
        'yes'
        '--order'
        'lex'
        '--shard-count'
        {{ shard_count | string | ps_quote }}
        '--shard-index'
        {{ shard_index | string | ps_quote }}
    )
} finally {
    Pop-Location
}
{% block catch2_post %}{{ require_output('tests.xml', 'Catch2 did not produce tests.xml') }}
{% endblock %}{% endblock %}

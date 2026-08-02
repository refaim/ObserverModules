{% extends "pwsh.ps1" %}
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
{% block catch2_post %}if (-not (Test-Path -LiteralPath (Join-Path $outDir 'tests.xml') -PathType Leaf)) {
    throw 'Catch2 did not produce tests.xml'
}
{% endblock %}{% endblock %}

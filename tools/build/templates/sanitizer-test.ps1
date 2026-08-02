{% extends "catch2-test.ps1" %}
{% block catch2_setup %}{% if runtime %}Copy-Item -LiteralPath {{ runtime.source | ps_quote }} -Destination (Join-Path $outDir {{ runtime.name | ps_quote }})
{% endif %}$env:{{ options_name }} = {{ options_value | ps_quote }}
{% endblock %}
{% block catch2_post %}if (-not (Test-Path -LiteralPath (Join-Path $outDir 'tests.xml') -PathType Leaf)) {
    throw 'Sanitizer Catch2 shard did not produce tests.xml'
}
{% endblock %}

{% extends "catch2-test.ps1" %}{% from "_output.ps1" import require_output %}
{% block catch2_setup %}{% if runtime %}Copy-Item -LiteralPath {{ runtime.source | ps_quote }} -Destination (Join-Path $outDir {{ runtime.name | ps_quote }})
{% endif %}$env:{{ options_name }} = {{ options_value | ps_quote }}
{% endblock %}
{% block catch2_post %}{{ require_output('tests.xml', 'Sanitizer Catch2 shard did not produce tests.xml') }}
{% endblock %}

{% extends "fuzz-common.ps1" %}
{% block fuzz_invoke %}$inputs = @(Get-ChildItem -LiteralPath $corpus -File | Sort-Object Name)
Invoke-Checked $fuzzer (@($inputs.FullName) + @(
    {{ ('-max_len=' ~ max_length) | ps_quote }}
    '-rss_limit_mb=1024'
    '-timeout=10'
    '-print_final_stats=1'
    "-artifact_prefix=$artifactDir\"
))
{% endblock %}

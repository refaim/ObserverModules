{% extends "fuzz-common.ps1" %}
{% block fuzz_invoke %}$PSNativeCommandUseErrorActionPreference = $false
& $fuzzer @(
    $corpus
    {{ ('-max_total_time=' ~ seconds) | ps_quote }}
    {{ ('-max_len=' ~ max_length) | ps_quote }}
    '-rss_limit_mb=1024'
    '-timeout=10'
    '-use_value_profile=1'
    '-print_final_stats=1'
    "-artifact_prefix=$artifactDir\"
)
$fuzzExitCode = $LASTEXITCODE
{% endblock %}
{% block fuzz_post %}$publishedCorpus = Join-Path $outDir 'corpus'
[void](New-Item -ItemType Directory -Path $publishedCorpus)
Get-ChildItem -LiteralPath $corpus -File | Sort-Object Name | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $publishedCorpus
}
[IO.File]::WriteAllText((Join-Path $outDir 'status.txt'), [string]$fuzzExitCode)
{% endblock %}

{% extends "catch2-test.ps1" %}
{% block catch2_setup %}$env:LLVM_PROFILE_FILE = Join-Path $outDir 'coverage-%m-%p.profraw'
{% endblock %}
{% block catch2_post %}if (-not (Get-ChildItem -LiteralPath $outDir -File -Filter '*.profraw')) {
    throw 'Instrumented Catch2 shard produced no LLVM raw profiles'
}
{% endblock %}

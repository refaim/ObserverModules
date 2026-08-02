{% extends "pwsh.ps1" %}
{% block pwsh_body %}
$arguments = @(
    'export'
    {{ test_executable | ps_quote }}
    '--instr-profile'
    {{ profile | ps_quote }}
    '--ignore-filename-regex'
    {{ ignore_regex | ps_quote }}
{% for object in objects %}    '--object'
    {{ object | ps_quote }}
{% endfor %}{% if summary_only %}    '--summary-only'
{% else %}    '--format=lcov'
{% endif %})
$content = & {{ llvm_cov | ps_quote }} @arguments
if ($LASTEXITCODE -ne 0) {
    throw "llvm-cov failed with exit code $LASTEXITCODE"
}
$text = @($content | Where-Object { $_ -notmatch '^warning:' }) -join "`n"
if ([string]::IsNullOrWhiteSpace($text)) {
    throw 'llvm-cov produced an empty report'
}
[IO.File]::WriteAllText((Join-Path $outDir {{ report_name | ps_quote }}), $text, [Text.UTF8Encoding]::new($false))
{% endblock %}

{% extends "pwsh.ps1" %}
{% block pwsh_body %}
if (-not (Test-Path -LiteralPath {{ include_dir | ps_quote }} -PathType Container)) {
    throw 'Cppcheck dependency headers were not restored'
}
Invoke-Checked {{ cppcheck | ps_quote }} @(
    {{ source_dir | ps_quote }}
    '--std=c++23'
    {{ ('--platform=' ~ platform) | ps_quote }}
    '-DWIN32=1'
    '-D_WIN32=1'
    '-DUNICODE=1'
    '-D_UNICODE=1'
    {{ ('-D' ~ architecture_define) | ps_quote }}
    {{ ('-I' ~ source_dir) | ps_quote }}
    {{ ('-I' ~ include_dir) | ps_quote }}
    '--enable=warning,style,performance,portability'
    '--check-level=exhaustive'
    '--inconclusive'
    '--inline-suppr'
    '--suppress=missingIncludeSystem'
    '--suppress=uninitMemberVarNoCtor:src/api.h'
    '--suppress=*:out/cas/*-restore-vcpkg-*/out/*'
    '--suppress=functionStatic'
    {{ ('--relative-paths=' ~ repository) | ps_quote }}
    '--output-format=sarif'
    "--output-file=$outDir\cppcheck.sarif"
    "--cppcheck-build-dir=$buildDir"
)
$reportPath = Join-Path $outDir 'cppcheck.sarif'
if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
    throw 'Cppcheck did not produce cppcheck.sarif'
}
$report = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
$index = 0
foreach ($run in @($report.runs)) {
    $run | Add-Member -NotePropertyName automationDetails -NotePropertyValue ([ordered]@{ id = {{ automation_id | ps_quote }} + "$index/" }) -Force
    ++$index
}
$report | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $reportPath -Encoding utf8
{% endblock %}

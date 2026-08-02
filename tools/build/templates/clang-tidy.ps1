{% extends "analysis.ps1" %}
{% block int_dir %}
    "/p:IntDir=$outDir\obj\"
{% endblock %}
{% block analyzer_args %}
    '/p:EnableMicrosoftCodeAnalysis=false'
    '/p:ObserverEnableClangTidy=true'
    {{ ('/p:LLVMInstallDir=' ~ llvm_dir) | ps_quote }}
    {{ ('/p:ClangTidyLogFile=' ~ project_name ~ '.ClangTidy.log') | ps_quote }}
{% endblock %}
{% block post_msbuild %}
if (-not (Test-Path -LiteralPath (Join-Path $outDir {{ ('obj\\' ~ project_name ~ '.ClangTidy.log') | ps_quote }}) -PathType Leaf)) {
    throw {{ ('clang-tidy did not produce ' ~ project_name ~ '.ClangTidy.log') | ps_quote }}
}
{% endblock %}

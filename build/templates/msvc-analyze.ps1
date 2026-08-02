{% extends "analysis.ps1" %}
{% block analyzer_args %}
    '/p:EnableMicrosoftCodeAnalysis=true'
    '/p:ObserverEnableClangTidy=false'
    {{ ('/p:ObserverAnalysisReportName=' ~ project_name) | ps_quote }}
    "/p:ObserverAnalysisReportPath=$outDir\{{ project_name }}.sarif"
{% endblock %}
{% block post_msbuild %}
if (-not (Test-Path -LiteralPath (Join-Path $outDir {{ (project_name ~ '.sarif') | ps_quote }}) -PathType Leaf)) {
    throw {{ ('MSVC analysis did not produce ' ~ project_name ~ '.sarif') | ps_quote }}
}
{% endblock %}

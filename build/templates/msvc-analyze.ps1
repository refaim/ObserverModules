{% extends "analysis.ps1" %}{% from "_output.ps1" import require_output %}
{% block analyzer_args %}
    '/p:EnableMicrosoftCodeAnalysis=true'
    '/p:ObserverEnableClangTidy=false'
    {{ ('/p:ObserverAnalysisReportName=' ~ project_name) | ps_quote }}
    "/p:ObserverAnalysisReportPath=$outDir\{{ project_name }}.sarif"
{% endblock %}
{% block post_msbuild %}
{{ require_output(project_name ~ '.sarif', 'MSVC analysis did not produce ' ~ project_name ~ '.sarif') }}
{% endblock %}

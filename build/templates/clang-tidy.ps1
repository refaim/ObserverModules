{% extends "analysis.ps1" %}{% from "_output.ps1" import require_output %}
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
{{ require_output('obj\\' ~ project_name ~ '.ClangTidy.log', 'clang-tidy did not produce ' ~ project_name ~ '.ClangTidy.log') }}
{% endblock %}

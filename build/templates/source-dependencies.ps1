{% extends "selected-compile.ps1" %}{% from "_output.ps1" import require_output %}
{% block compile_args %}
    "/p:ObserverSourceDependenciesPath=$outDir\dependencies.json"
{% endblock %}
{% block post_msbuild %}
{{ require_output('dependencies.json', 'MSVC did not produce dependencies.json') }}
{% endblock %}

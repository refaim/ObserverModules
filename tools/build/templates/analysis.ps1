{% extends "selected-compile.ps1" %}
{% block compile_args %}
    '/p:ObserverRunCodeAnalysis=true'
    '/p:RunCodeAnalysis=true'
{% block analyzer_args required %}{% endblock %}
{% endblock %}

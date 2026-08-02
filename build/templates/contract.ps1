{% extends "pwsh.ps1" %}
{% block pwsh_body %}
& {{ test | ps_quote }}
{% endblock %}

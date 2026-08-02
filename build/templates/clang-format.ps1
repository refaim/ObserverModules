{% extends "pwsh.ps1" %}
{% block pwsh_body %}
Invoke-Checked {{ clang_format | ps_quote }} @(
    '--dry-run'
    '--Werror'
    {{ source | ps_quote }}
)
{% endblock %}

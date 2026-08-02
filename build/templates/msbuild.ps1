{% extends "pwsh.ps1" %}
{% block pwsh_body %}
{% block pwsh_setup %}{% endblock %}Invoke-Checked {{ msbuild | ps_quote }} @(
    {{ project | ps_quote }}
    '/nologo'
    '/m:1'
    '/nr:false'
    {{ ('/t:' ~ target) | ps_quote }}
    '/p:BuildProjectReferences=false'
    {{ ('/p:Configuration=' ~ configuration) | ps_quote }}
    {{ ('/p:Platform=' ~ platform) | ps_quote }}
    "/p:OutDir=$outDir\"
{% block int_dir %}    "/p:IntDir=$buildDir\"{{ '\n' }}{% endblock %}
{% block msbuild_args %}{% for argument in msbuild_args %}
    {{ argument | ps_quote }}
{% endfor %}{% endblock %}
){{ '\n' }}{% block post_msbuild %}{% endblock %}
{% endblock %}

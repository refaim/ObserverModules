{% extends "msbuild.ps1" %}
{% block pwsh_setup %}
{% if vcpkg_installed %}$vcpkgInstalledDir = {{ vcpkg_installed | ps_quote }}
{% else %}$vcpkgInstalledDir = Join-Path $buildDir 'vcpkg'
[void](New-Item -ItemType Directory -Force -Path $vcpkgInstalledDir)
{% endif %}{% endblock %}
{% block msbuild_args %}
    '/p:ObserverCompileAnalysis=true'
    '/p:ForceRebuild=true'
    {{ ('/p:SelectedFiles=' ~ source) | ps_quote }}
    '/p:SelectedFilesBuildPCH=false'
    '/p:SelectedFilesBuildModules=false'
    {{ ('/p:VcpkgRoot=' ~ vcpkg_root) | ps_quote }}
    "/p:VcpkgInstalledDir=$vcpkgInstalledDir\"
{% block compile_args required %}{% endblock %}
{% endblock %}

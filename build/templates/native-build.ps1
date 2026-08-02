{% extends "msbuild.ps1" %}
{% block msbuild_args %}
    {{ ('/p:VcpkgRoot=' ~ vcpkg_root) | ps_quote }}
    '/p:VcpkgManifestInstall=false'
{% if vcpkg_installed %}
    {{ ('/p:VcpkgInstalledDir=' ~ vcpkg_installed ~ '\\') | ps_quote }}
{% else %}
    "/p:VcpkgInstalledDir=$buildDir\vcpkg\"
{% endif %}
{% endblock %}

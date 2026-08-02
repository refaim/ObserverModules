{% extends "msbuild.ps1" %}
{% block msbuild_args %}
    {{ ('/p:VcpkgRoot=' ~ vcpkg_root) | ps_quote }}
    '/p:VcpkgManifestInstall=false'
    {{ ('/p:VcpkgInstalledDir=' ~ vcpkg_installed ~ '\\') | ps_quote }}
{% if llvm_dir %}    {{ ('/p:LLVMInstallDir=' ~ llvm_dir) | ps_quote }}
{% endif %}{% if llvm_runtime %}    {{ ('/p:LLVMRuntimeDir=' ~ llvm_runtime) | ps_quote }}
{% endif %}{% endblock %}

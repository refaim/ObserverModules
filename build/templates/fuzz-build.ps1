{% extends "msbuild.ps1" %}{% from "_output.ps1" import require_output %}
{% block msbuild_args %}
    {{ ('/p:VcpkgRoot=' ~ vcpkg_root) | ps_quote }}
    {{ ('/p:VcpkgInstalledDir=' ~ vcpkg_installed ~ '\\') | ps_quote }}
    {{ ('/p:LLVMInstallDir=' ~ llvm_dir) | ps_quote }}
    {{ ('/p:LLVMRuntimeDir=' ~ llvm_runtime) | ps_quote }}
{% endblock %}
{% block post_msbuild %}{{ require_output(executable_name, 'MSBuild did not produce the fuzzer executable') }}
{% endblock %}

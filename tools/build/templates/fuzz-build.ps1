{% extends "msbuild.ps1" %}
{% block msbuild_args %}
    {{ ('/p:VcpkgRoot=' ~ vcpkg_root) | ps_quote }}
    {{ ('/p:VcpkgInstalledDir=' ~ vcpkg_installed ~ '\\') | ps_quote }}
    {{ ('/p:LLVMInstallDir=' ~ llvm_dir) | ps_quote }}
    {{ ('/p:LLVMRuntimeDir=' ~ llvm_runtime) | ps_quote }}
{% endblock %}
{% block post_msbuild %}if (-not (Test-Path -LiteralPath (Join-Path $outDir {{ executable_name | ps_quote }}) -PathType Leaf)) {
    throw 'MSBuild did not produce the fuzzer executable'
}
{% endblock %}

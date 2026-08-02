{% extends "selected-compile.ps1" %}
{% block compile_args %}
    "/p:ObserverClangCommandPath=$outDir\compile-command.json"
    {{ ('/p:LLVMInstallDir=' ~ llvm_dir) | ps_quote }}
{% endblock %}
{% block post_msbuild %}
if (-not (Test-Path -LiteralPath (Join-Path $outDir 'compile-command.json') -PathType Leaf)) {
    throw 'clang-cl did not produce compile-command.json'
}
{% endblock %}

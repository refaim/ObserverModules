{% extends "selected-compile.ps1" %}{% from "_output.ps1" import require_output %}
{% block compile_args %}
    "/p:ObserverClangCommandPath=$outDir\compile-command.json"
    {{ ('/p:LLVMInstallDir=' ~ llvm_dir) | ps_quote }}
{% endblock %}
{% block post_msbuild %}
{{ require_output('compile-command.json', 'clang-cl did not produce compile-command.json') }}
{% endblock %}

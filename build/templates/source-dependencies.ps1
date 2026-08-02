{% extends "selected-compile.ps1" %}
{% block compile_args %}
    "/p:ObserverSourceDependenciesPath=$outDir\dependencies.json"
{% endblock %}
{% block post_msbuild %}
if (-not (Test-Path -LiteralPath (Join-Path $outDir 'dependencies.json') -PathType Leaf)) {
    throw 'MSVC did not produce dependencies.json'
}
{% endblock %}

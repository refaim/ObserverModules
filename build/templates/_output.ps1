{% macro require_output(relative_path, message, path_type='Leaf') -%}
if (-not (Test-Path -LiteralPath (Join-Path $outDir {{ relative_path | ps_quote }}) -PathType {{ path_type }})) {
    throw {{ message | ps_quote }}
}
{%- endmacro %}

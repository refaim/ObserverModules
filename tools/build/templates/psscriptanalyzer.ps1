{% extends "pwsh.ps1" %}
{% block pwsh_body %}
Import-Module {{ psscriptanalyzer | ps_quote }}
$diagnostics = @(Invoke-ScriptAnalyzer -Path {{ source | ps_quote }} -Settings {{ settings | ps_quote }})
$results = @($diagnostics | ForEach-Object {
    $level = switch ($_.Severity.ToString()) { 'Error' { 'error' } 'Warning' { 'warning' } default { 'note' } }
    [ordered]@{
        ruleId = $_.RuleName
        level = $level
        message = [ordered]@{ text = $_.Message }
        locations = @([ordered]@{ physicalLocation = [ordered]@{
            artifactLocation = [ordered]@{ uri = [IO.Path]::GetRelativePath({{ repository | ps_quote }}, $_.ScriptPath).Replace('\', '/') }
            region = [ordered]@{ startLine = [int]$_.Line; startColumn = [int]$_.Column }
        }})
    }
})
$sarif = [ordered]@{
    version = '2.1.0'
    '$schema' = 'https://json.schemastore.org/sarif-2.1.0.json'
    runs = @([ordered]@{
        automationDetails = [ordered]@{ id = {{ automation_id | ps_quote }} }
        tool = [ordered]@{ driver = [ordered]@{ name = 'PSScriptAnalyzer' } }
        results = $results
    })
}
$sarif | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath "$outDir\psscriptanalyzer.sarif" -Encoding utf8
{% endblock %}

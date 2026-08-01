#requires -Version 7.4

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$projectPropertiesPath = Join-Path $repositoryRoot 'build\ObserverProject.props'
[xml]$projectProperties = Get-Content -LiteralPath $projectPropertiesPath -Raw
$namespace = [System.Xml.XmlNamespaceManager]::new($projectProperties.NameTable)
$namespace.AddNamespace('msb', 'http://schemas.microsoft.com/developer/msbuild/2003')

$cetCompat = $projectProperties.SelectSingleNode(
    '/msb:Project/msb:ItemDefinitionGroup/msb:Link/msb:CETCompat',
    $namespace
)
if ($null -eq $cetCompat -or $cetCompat.InnerText -ne 'true') {
    throw 'Release CET compatibility must remain enabled for supported targets.'
}
$expectedCondition = "'`$(Configuration)' == 'Release' And '`$(Platform)' != 'ARM64'"
if ($cetCompat.Condition -ne $expectedCondition) {
    throw "CET compatibility must exclude only ARM64; actual condition: '$($cetCompat.Condition)'."
}

$linkControlFlowGuard = $projectProperties.SelectSingleNode(
    '/msb:Project/msb:ItemDefinitionGroup/msb:Link/msb:ControlFlowGuard',
    $namespace
)
if (
    $null -eq $linkControlFlowGuard -or
    $linkControlFlowGuard.InnerText -ne 'Guard' -or
    $linkControlFlowGuard.Condition -ne "'`$(Configuration)' == 'Release'"
) {
    throw 'Control Flow Guard must remain enabled for every Release architecture, including ARM64.'
}

Write-Host '[OK] Release CET is scoped to supported targets without weakening CFG.'

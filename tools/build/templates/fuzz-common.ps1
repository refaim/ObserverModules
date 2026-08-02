{% extends "pwsh.ps1" %}
{% block pwsh_body %}
$fuzzer = {{ fuzzer | ps_quote }}
$seedDir = {{ seed_dir | ps_quote }}
$corpus = Join-Path $buildDir 'corpus'
$artifactDir = Join-Path $outDir 'artifacts'
if (-not (Test-Path -LiteralPath $fuzzer -PathType Leaf)) { throw 'Fuzzer executable was not found' }
if (-not (Test-Path -LiteralPath $seedDir -PathType Container)) { throw 'Fuzzer seed directory was not found' }
[void](New-Item -ItemType Directory -Path $corpus)
[void](New-Item -ItemType Directory -Path $artifactDir)
$seeds = @(Get-ChildItem -LiteralPath $seedDir -File | Sort-Object Name)
if ($seeds.Count -eq 0) { throw 'No checked-in fuzzer seeds were found' }
foreach ($seed in $seeds) {
    if ($seed.Extension -eq '.hex') {
        $hex = (Get-Content -LiteralPath $seed.FullName -Raw) -replace '\s', ''
        if ($hex.Length -eq 0 -or $hex.Length % 2 -ne 0 -or $hex -notmatch '^[0-9A-Fa-f]+$') {
            throw "Invalid hexadecimal fuzzer seed: $($seed.FullName)"
        }
        [IO.File]::WriteAllBytes((Join-Path $corpus $seed.BaseName), [Convert]::FromHexString($hex))
    } else {
        Copy-Item -LiteralPath $seed.FullName -Destination $corpus
    }
}
{% if prior_corpus is defined and prior_corpus %}$priorCorpus = {{ prior_corpus | ps_quote }}
if (-not (Test-Path -LiteralPath $priorCorpus -PathType Container)) { throw 'Prior fuzzer corpus was not found' }
Get-ChildItem -LiteralPath $priorCorpus -File | Sort-Object Name | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $corpus -Force
}
{% endif %}Push-Location (Split-Path -Parent $fuzzer)
try {
{% block fuzz_invoke required %}{% endblock %}
} finally {
    Pop-Location
}
{% block fuzz_post %}{% endblock %}
{% endblock %}

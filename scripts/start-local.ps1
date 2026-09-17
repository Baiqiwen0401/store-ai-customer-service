<#
Starts the local service with settings from .env.local without committing or
printing secrets. Intended for local WeCom callback testing only.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot '.env.local'

if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing local configuration file: $envFile"
}

Get-Content -LiteralPath $envFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith('#')) { return }
    $separator = $line.IndexOf('=')
    if ($separator -lt 1) { throw "Invalid .env.local line: $line" }
    $name = $line.Substring(0, $separator).Trim()
    $value = $line.Substring($separator + 1)
    Set-Item -Path "Env:$name" -Value $value
}

Set-Location -LiteralPath $projectRoot
$python = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python runtime not found: $python"
}
& $python app.py

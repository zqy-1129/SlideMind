$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root ".env.cn.example"
$target = Join-Path $root ".env"

Copy-Item -LiteralPath $source -Destination $target -Force
Write-Host "Copied .env.cn.example to .env"
Write-Host "Run: docker compose pull"
Write-Host "Then: docker compose up --build"


#Requires -Version 7.0
<#
统一门禁：格式检查 -> lint -> 类型检查 -> 测试。任一失败即退出。
用法： .\scripts\check.ps1
#>
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

function Invoke-Step {
    param([string]$Name, [string[]]$UvArgs)
    Write-Host "==> $Name" -ForegroundColor Cyan
    & "$PSScriptRoot\uv.ps1" run @UvArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $Name (exit $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

Invoke-Step 'ruff format' @('ruff', 'format', '--check', '.')
Invoke-Step 'ruff lint' @('ruff', 'check', '--output-format=concise', '.')
Invoke-Step 'ty types' @('ty', 'check', 'src', 'tests')
Invoke-Step 'pytest' @('pytest')

Write-Host 'ALL GREEN' -ForegroundColor Green

#Requires -Version 7.0
<#
项目级 uv 包装器：把 uv 的下载缓存与托管解释器留在 Strata 根目录下，不落 C 盘。
用法： .\scripts\uv.ps1 add httpx ;  .\scripts\uv.ps1 run pytest
#>
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot          # ...\Strata\Workspace
$strataRoot = Split-Path -Parent $projectRoot            # ...\Strata

$env:UV_CACHE_DIR = Join-Path $strataRoot 'uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $strataRoot 'uv-python'
New-Item -ItemType Directory -Force -Path $env:UV_CACHE_DIR, $env:UV_PYTHON_INSTALL_DIR | Out-Null

uv @args
exit $LASTEXITCODE

#requires -Version 5.1
<#
.SYNOPSIS
    一行安装 flclash-auto(Windows 按需代理启动器)。
.DESCRIPTION
    下载仓库快照 → 解压到 %LOCALAPPDATA%\flclash-auto → 把目录加入当前用户 PATH,
    之后任意终端可直接用 `px`(px.cmd)或 `python flclash_proxy.py`。
    不需要管理员权限,只改当前用户的环境变量。
.PARAMETER Dir
    安装目录,默认 %LOCALAPPDATA%\flclash-auto。
.PARAMETER NoPath
    不修改 PATH(只想解压看看时用)。
.PARAMETER Force
    安装目录已存在时覆盖。
.PARAMETER Ref
    下载的 git ref,默认 main(可指定 tag,如 v1.0.0)。
.EXAMPLE
    irm https://raw.githubusercontent.com/fool415/flclash-auto/main/install.ps1 | iex
.EXAMPLE
    .\install.ps1 -Ref v1.0.0 -Dir D:\tools\flclash-auto -Force
#>
[CmdletBinding()]
param(
    [string]$Dir = (Join-Path $env:LOCALAPPDATA "flclash-auto"),
    [switch]$NoPath,
    [switch]$Force,
    [string]$Ref = "main"
)

$ErrorActionPreference = "Stop"
$Repo = "fool415/flclash-auto"

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  OK  $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  !!  $msg" -ForegroundColor Yellow }
function Write-Bad($msg)  { Write-Host "  XX  $msg" -ForegroundColor Red }

# TLS 1.2:旧版 PowerShell 默认可能还是 TLS 1.0,codeload 会拒
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

# ---- 0. 前置检查 ----
Write-Step "检查 Python"
$py = $null
foreach ($cand in @("python", "py")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source; break }
}
if (-not $py) {
    Write-Bad "没找到 python / py。请先安装 Python 3(https://www.python.org/downloads/windows/),勾选 Add to PATH。"
    throw "安装中止(见上方提示)"
}
Write-Ok "Python: $py"

if ((Test-Path $Dir) -and -not $Force) {
    Write-Bad "目录已存在:$Dir(要覆盖请加 -Force)"
    throw "安装中止(见上方提示)"
}

# ---- 1. 下载 ----
Write-Step "下载 flclash-auto ($Ref)"
$zip = Join-Path $env:TEMP ("flclash-auto-" + [guid]::NewGuid().ToString("N") + ".zip")
$url = "https://codeload.github.com/$Repo/zip/refs/heads/$Ref"
if ($Ref -match '^v') {
    $url = "https://codeload.github.com/$Repo/zip/refs/tags/$Ref"
}
try {
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
} catch {
    Write-Bad "下载失败:$($_.Exception.Message)"
    Write-Warn2 "如果你在国内且 GitHub 直连不通,先开代理再跑本脚本,或手动从 Release 页下载:https://github.com/$Repo/releases"
    throw "安装中止(见上方提示)"
}
$size = (Get-Item $zip).Length
Write-Ok ("已下载 {0:N0} KB" -f ($size / 1KB))

# ---- 2. 解压 ----
Write-Step "解压到 $Dir"
$tmp = Join-Path $env:TEMP ("flclash-auto-" + [guid]::NewGuid().ToString("N"))
try {
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $inner = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
    if (-not $inner) { throw "压缩包里没有目录" }
    if (Test-Path $Dir) { Remove-Item -Recurse -Force $Dir }
    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    # 只搬运行需要的文件,避免把 .git 之类带进来(快照本来也没有)
    Copy-Item -Path (Join-Path $inner.FullName '*') -Destination $Dir -Recurse -Force
} catch {
    Write-Bad "解压失败:$($_.Exception.Message)"
    throw "安装中止(见上方提示)"
} finally {
    Remove-Item -Force $zip -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
Write-Ok "已安装到 $Dir"

# ---- 3. PATH ----
$px = Join-Path $Dir "px.cmd"
if (Test-Path $px) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($userPath) { $parts = $userPath -split ';' | Where-Object { $_ } }
    if ($NoPath) {
        Write-Warn2 "按 -NoPath 跳过修改 PATH"
    } elseif ($parts -contains $Dir) {
        Write-Ok "PATH 里已有该目录"
    } else {
        $newPath = (@($parts) + $Dir) -join ';'
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Ok "已加入用户 PATH(新开的终端生效)"
    }
} else {
    Write-Warn2 "没找到 px.cmd,安装可能不完整"
}

# ---- 4. 自检 ----
Write-Step "自检"
$status = & $py (Join-Path $Dir "flclash_proxy.py") --help 2>&1 | Select-Object -First 1
if ($status) { Write-Ok "脚本可执行:$($status.ToString().Trim())" } else { Write-Warn2 "脚本自检没输出,建议手动跑一次 --help" }

$clash = Get-ChildItem "C:\Program Files\FlClash\FlClash.exe", "$env:LOCALAPPDATA\Programs\FlClash\FlClash.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $clash) {
    Write-Warn2 "没探测到 FlClash。本工具只做调度,请先装 FlClash:https://github.com/chen08209/FlClash/releases"
}

Write-Host ""
Write-Host "装好了。新开一个终端后试试:" -ForegroundColor Cyan
Write-Host "  px --status          # 看状态" -ForegroundColor White
Write-Host "  px --test            # 测速所有节点" -ForegroundColor White
Write-Host "  px git pull          # 需要代理的命令(自动启停)" -ForegroundColor White
Write-Host "  px --buy             # 还没有节点?打开订阅购买入口" -ForegroundColor White
Write-Host ""
Write-Host "卸载:删掉 $Dir,再从用户 PATH 里移除该目录即可。" -ForegroundColor DarkGray

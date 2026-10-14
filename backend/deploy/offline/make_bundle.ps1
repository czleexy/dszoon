#
.SYNOPSIS
  在 Windows 打包机上生成 dszoon 内网离线安装包。

.DESCRIPTION
  与 deploy/offline/make_bundle.sh 等价（复制同一份 install.sh，逻辑不分叉）。

  ★ wheel 与**平台 + Python 小版本**绑定，先想清楚目标机是什么：
    * 目标机也是 Windows、且 Python 小版本相同  → -TargetOs native（默认）
    * 目标是 Linux x86_64                      → -TargetOs linux 跨平台下载轮子。
      这条路径要求所有依赖都有 manylinux 轮子（本项目当前满足），但**未在
      真机验证过**；更稳的做法是走 README §4 的 Docker 镜像包，或者在一台
      Linux 机器（或 Linux 容器）里直接跑 make_bundle.sh。

.EXAMPLE
  pwsh -File deploy/offline/make_bundle.ps1

.EXAMPLE
  # 为 Linux x86_64 + Python 3.12 的内网机打包
  pwsh -File deploy/offline/make_bundle.ps1 -TargetOs linux -TargetPython 3.12

.EXAMPLE
  # 只下一版 Python 依赖、不重新构建前端（前端没改时省几分钟）
  pwsh -File deploy/offline/make_bundle.ps1 -SkipWeb
# 
[CmdletBinding()]
param(
  [ValidateSet("native", "linux")]
  [string]$TargetOs = "native",
  # 仅 -TargetOs linux 用：目标机的 Python 小版本（如 3.12 / 3.13）
  [string]$TargetPython = "3.12",
  # 跳过前端构建（web/dist 已经是新的时才用）
  [switch]$SkipWeb
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Stamp = Get-Date -Format "yyyyMMdd"
$Bundle = Join-Path $Root "deploy\offline\dist\dszoon-offline-$Stamp"

function Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Note([string]$Text) { Write-Host "    $Text" -ForegroundColor DarkGray }
function Fail([string]$Text) { Write-Host "[FAIL] $Text" -ForegroundColor Red; exit 1 }

# 优先用仓库里的 .venv：避免 pip 把依赖下到全局 Python 或下成别人的版本
$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
  $python = "python"
  Note "没找到 .venv\Scripts\python.exe，改用 PATH 里的 python"
}

Step "工作目录：$Root"
if (Test-Path $Bundle) { Remove-Item -Recurse -Force $Bundle }
foreach ($dir in @("wheels", "web-dist", "deploy")) {
  New-Item -ItemType Directory -Force -Path (Join-Path $Bundle $dir) | Out-Null
}
$wheels = Join-Path $Bundle "wheels"

Step "1/5 下载 Python 依赖（含 web extras）"
if ($TargetOs -eq "linux") {
  # 首选在 Linux 容器里下载：pip 跑在 Linux 上，平台天然正确，
  # 不需要用 --platform/--abi 去猜 manylinux 标签（那套标签很容易猜错，
  # 而且 abi3 的轮子还得额外列 --abi abi3）。
  $docker = Get-Command docker -ErrorAction SilentlyContinue
  if ($docker) {
    Note "在 Linux 容器（python:$TargetPython-slim）里下载，平台天然正确"
    $srcMount = "$Root`:/src:ro"
    $outMount = "$wheels`:/out"
    & docker run --rm -v $srcMount -v $outMount -w /src "python:$TargetPython-slim" `
      sh -c "pip download -d /out '.[web]' && pip download -d /out setuptools wheel"
    if ($LASTEXITCODE -ne 0) { Fail "容器内下载依赖失败（看上面 pip/docker 输出）" }
  } else {
    # 没有 Docker，只能跨平台下载：显式给出目标平台标签，并且只取 wheel
    # （sdist 在 Windows 上编译不出 Linux 的扩展模块）。
    $abi = "cp" + ($TargetPython -replace '\.', '')
    $common = @(
      "-m", "pip", "download", "-d", $wheels, "--only-binary=:all:",
      "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_17_x86_64",
      "--implementation", "cp", "--python-version", $TargetPython,
      "--abi", $abi, "--abi", "abi3", "--abi", "none"
    )
    Write-Host "    ! 没装 Docker，退回跨平台标签下载（未真机验证过的路径）" -ForegroundColor Yellow
    & $python @common ".[web]"
    if ($LASTEXITCODE -ne 0) {
      Fail "跨平台下载失败。最稳的做法：装 Docker 后重跑，或直接走 README §4 的 Docker 镜像包"
    }
    & $python @common "setuptools" "wheel"
    if ($LASTEXITCODE -ne 0) { Fail "下载构建期依赖（setuptools/wheel）失败" }
  }
} else {
  $common = @("-m", "pip", "download", "-d", $wheels)
  Note "目标平台：与本机相同（wheel 只能装到同平台同 Python 小版本的目标机）"
  & $python @common ".[web]"
  if ($LASTEXITCODE -ne 0) { Fail "下载运行期依赖失败" }
  # 构建期依赖：Python 3.12 起 venv 不再自带 setuptools，缺了它离线安装会直接失败
  & $python @common "setuptools" "wheel"
  if ($LASTEXITCODE -ne 0) { Fail "下载构建期依赖（setuptools/wheel）失败" }
}

Step "2/5 构建前端产物"
$web = Join-Path $Root "web"
if ($SkipWeb) {
  if (-not (Test-Path (Join-Path $web "dist"))) { Fail "指定了 -SkipWeb 但 web\dist 不存在" }
  Note "按 -SkipWeb 跳过构建，直接使用现有 web\dist"
} else {
  Push-Location $web
  try {
    if (-not (Test-Path (Join-Path $web "node_modules"))) {
      npm install --no-audit --no-fund
      if ($LASTEXITCODE -ne 0) { Fail "npm install 失败" }
    }
    npm run build
    if ($LASTEXITCODE -ne 0) { Fail "npm run build 失败：上面的 vite 报错就是原因" }
  } finally { Pop-Location }
}
Copy-Item -Recurse -Force (Join-Path $web "dist\*") (Join-Path $Bundle "web-dist")

Step "3/5 复制源码与测试夹具"
Copy-Item -Recurse -Force (Join-Path $Root "app") $Bundle
Copy-Item -Recurse -Force (Join-Path $Root "scripts") $Bundle
# ★ tests/ 必须带上：scripts/selfcheck.py 会 import tests.support.fake_device
#   并读 tests/fixtures 下的样本回显。漏了它，内网现场的"装完自检"会直接崩。
Copy-Item -Recurse -Force (Join-Path $Root "tests") $Bundle
Copy-Item -Force (Join-Path $Root "pyproject.toml") $Bundle
# alembic.ini + migrations/：现场也能把 schema 交给 Alembic 管理
Copy-Item -Force (Join-Path $Root "alembic.ini") $Bundle
Copy-Item -Recurse -Force (Join-Path $Root "migrations") $Bundle

Step "4/5 复制部署文件"
$deploySrc = Join-Path $Root "deploy"
# nginx.conf 也要带：compose 的 with-nginx profile 挂着它
foreach ($file in @("Dockerfile", "docker-compose.yml", ".env.example", "nginx.conf")) {
  Copy-Item -Force (Join-Path $deploySrc $file) (Join-Path $Bundle "deploy")
}
Copy-Item -Recurse -Force (Join-Path $deploySrc "systemd") (Join-Path $Bundle "deploy")
Copy-Item -Force (Join-Path $deploySrc "offline\README.md") (Join-Path $Bundle "deploy")
# install.sh 与 bash 打包器复制同一份
Copy-Item -Force (Join-Path $deploySrc "offline\install.sh") (Join-Path $Bundle "install.sh")

Step "5/5 记录打包环境并打包"
# 全部是 ASCII，所以用 ascii 编码写出，避免 PowerShell 5.1 的 UTF8 带 BOM
# 让目标机的 install.sh 读第一行时多出几个怪字符。
$packedPython = (& $python -c "import platform; print(platform.python_version())").Trim()
$info = @(
  "packed_at=$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))",
  "os=$($env:OS)",
  "target=$TargetOs",
  "arch=$($env:PROCESSOR_ARCHITECTURE)",
  "python=$packedPython"
)
$info | Set-Content -Encoding ascii (Join-Path $Bundle "PACKAGING.txt")
$info | ForEach-Object { Note $_ }

$dist = Split-Path $Bundle -Parent
if (Get-Command tar.exe -ErrorAction SilentlyContinue) {
  Push-Location $dist
  try { & tar.exe -czf "dszoon-offline-$Stamp.tar.gz" "dszoon-offline-$Stamp" } finally { Pop-Location }
  if ($LASTEXITCODE -ne 0) { Fail "tar 打包失败" }
  $artifact = Join-Path $dist "dszoon-offline-$Stamp.tar.gz"
  $hint = "tar -xzf"
} else {
  Note "没找到 tar.exe（Win10 1803+ 自带），改用 zip；Linux 目标机用 unzip 解"
  $artifact = Join-Path $dist "dszoon-offline-$Stamp.zip"
  Compress-Archive -Path $Bundle -DestinationPath $artifact -Force
  $hint = "unzip"
}

Write-Host ""
Write-Host "完成：$artifact" -ForegroundColor Green
Note "拷到内网目标机后："
Note "    $hint $(Split-Path $artifact -Leaf)"
Note "    cd dszoon-offline-$Stamp && bash install.sh"

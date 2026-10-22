# =============================================================================
# 注意：本文件必须保存为 **UTF-8 with BOM**。
#   Windows PowerShell 5.1 在没有 BOM 时会把 UTF-8 中文按系统 ANSI 代码页解码，
#   结果是中文注释变成乱码并直接抛 "Unexpected token" 解析错误 —— 脚本根本跑不起来。
#   如果你用编辑器改动本文件，请确认保存时保留了 BOM（VS Code 右下角选 UTF-8 with BOM）。
# =============================================================================
# =============================================================================
# scripts/mvn.ps1 —— 项目专用 Maven 启动器
#
# 为什么不用官方 mvn.cmd：
#   本项目路径包含中文字符（...\项目目录\...），mvn.cmd 是批处理脚本，
#   参数会经过 cmd.exe 的代码页转换，导致 -Dmaven.repo.local 等属性被丢弃，
#   Maven 会回退到默认的 %USERPROFILE%\.m2 仓库。
#   这里直接调用 Maven 的 classworlds launcher，参数由 PowerShell 以 Unicode
#   传给 java.exe，中文路径完全安全。
#
# 用法：
#   pwsh -File scripts/mvn.ps1 -f backend/pom.xml test
#   pwsh -File scripts/mvn.ps1 -f backend/pom.xml -DskipTests package
# =============================================================================
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$MvnArgs
)

$ErrorActionPreference = 'Stop'

$proj = Split-Path -Parent $PSScriptRoot
$mvnHome = Join-Path $proj '.tools\apache-maven-3.9.9'

if (-not (Test-Path $mvnHome)) {
    throw "未找到本地 Maven：$mvnHome`n请先执行 scripts/bootstrap.ps1 或手动解压 apache-maven-3.9.9 到 .tools/ 目录。"
}

$cwJar = Get-ChildItem (Join-Path $mvnHome 'boot') -Filter 'plexus-classworlds-*.jar' |
    Select-Object -First 1
if (-not $cwJar) { throw "未找到 plexus-classworlds jar，Maven 安装可能不完整：$mvnHome" }

$java = (Get-Command java -ErrorAction Stop).Source
$repo = Join-Path $proj '.tools\m2repo'
$settings = Join-Path $proj '.tools\settings.xml'
$conf = Join-Path $mvnHome 'bin\m2.conf'

if (-not (Test-Path $repo)) { New-Item -ItemType Directory -Force -Path $repo | Out-Null }

Write-Host "[mvn.ps1] java   = $java"
Write-Host "[mvn.ps1] repo   = $repo"
Write-Host "[mvn.ps1] args   = $($MvnArgs -join ' ')"

$commonArgs = @(
    "-Dmaven.home=$mvnHome"
    "-Dclassworlds.conf=$conf"
    "-Dmaven.multiModuleProjectDirectory=$proj"
    "-Dmaven.repo.local=$repo"
    '-Dfile.encoding=UTF-8'
    '-classpath'
    $cwJar.FullName
    'org.codehaus.plexus.classworlds.launcher.Launcher'
)

$fullArgs = @()
if (Test-Path $settings) { $fullArgs += @('-s', $settings) }
$fullArgs += @('-B', '-ntp')
$fullArgs += $MvnArgs

& $java @commonArgs @fullArgs
exit $LASTEXITCODE

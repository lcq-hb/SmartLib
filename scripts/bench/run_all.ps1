# =============================================================================
# scripts/bench/run_all.ps1 —— SmartLib 一键基准测试
#
# 流程（每一步都会在控制台打印进度，失败的步骤不会中断整体流程，只记进汇总表）：
#   1) 等后端健康（GET /actuator/health 直到 status=UP）
#   2) 依次用 PUT /api/admin/stock-strategy 切换 6 种库存策略（S1~S6），
#      每种策略跑一次 scripts/bench/load_borrow.py
#   3) 跑检索基准（Q1 like / Q3 inverted-index 标签各一次）
#   4) 跑召回率评估（scripts/bench/recall_dataset.json）
#   5) GET /api/admin/reconcile 记录对账结果
#   6) 汇总成 Markdown 表格写入 scripts/bench/results/summary.md
#
# 兼容性：PowerShell 5.1（Windows 自带）。刻意不用 ?? / ?. / 三元运算符 / -Parallel。
#
# 用法（注意：直接 & 脚本会被执行策略拦截，必须显式 Bypass）：
#   powershell -ExecutionPolicy Bypass -File scripts/bench/run_all.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/bench/run_all.ps1 `
#       -BaseUrl http://localhost:8081 -BookId 5 -Threads 200 -Requests 400
#
# 【重要前提】策略切换接口要求后端配置 smartlib.admin.enabled=true，
#   否则 /api/admin/** 不存在（404）。本地 H2 起后端时加上：
#     --smartlib.admin.enabled=true
# =============================================================================
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://localhost:8080",
    [int]$BookId = 5,
    [int]$Threads = 50,
    [int]$Requests = 300,
    [int]$RecallRequests = 42,
    [string]$ReaderIds = "1-30",
    [string]$OutDir = "",
    [int]$HealthTimeoutSeconds = 180,
    [switch]$SkipSearch,
    [switch]$SkipRecall,
    [switch]$KeepGoingOnStrategyError
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

# ---------------------------- 路径推导（不硬编码盘符） ------------------------
$benchDir = $PSScriptRoot
$repoRoot = Split-Path -Parent (Split-Path -Parent $benchDir)
if ([string]::IsNullOrWhiteSpace($OutDir)) {
    $OutDir = Join-Path $benchDir "results"
}
if (-not (Test-Path -LiteralPath $OutDir)) {
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
}
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path

$loadBorrow = Join-Path $benchDir "load_borrow.py"
$searchBench = Join-Path $benchDir "search_bench.py"
$recallData = Join-Path $benchDir "recall_dataset.json"

if (-not (Test-Path -LiteralPath $loadBorrow)) { throw "找不到压测脚本：$loadBorrow" }
if (-not (Test-Path -LiteralPath $searchBench)) { throw "找不到检索压测脚本：$searchBench" }
if (-not (Test-Path -LiteralPath $recallData)) { throw "找不到召回数据集：$recallData" }

# ---------------------------- Python 解释器 ----------------------------------
function Resolve-Python {
    foreach ($name in @("python", "python3", "py")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $cmd) { return $cmd.Source }
    }
    throw "找不到 python 解释器。请安装 Python 3.8+ 并确保在 PATH 中。"
}
$python = Resolve-Python

# ---------------------------- HTTP 工具 --------------------------------------
# 用 .NET HttpWebRequest，不用 Invoke-RestMethod / WebClient，原因都是实测踩出来的：
#  * Invoke-RestMethod：PS 5.1 在 4xx/5xx 上抛 WebException，默认拿不到响应体，
#    而我们的业务失败（409/403）恰恰带 JSON 响应体，必须读出来。
#  * WebClient：在 PS 5.1 上 DownloadData()（GET）会直接失败返回 Ok=false/status=0，
#    但 UploadData()（PUT/POST）却正常 —— 行为不一致，不可靠。
#  * HttpWebRequest：GET/PUT/POST/DELETE 全部走同一条路径，能拿到状态码 + 响应体 +
#    自定义头 + 超时，行为一致（本机 5.1 实测 GET/PUT/POST 均正常）。
#
# 说明：ReadWriteTimeout / Timeout 都用秒转毫秒，避免脚本卡死。
function Invoke-SmartLib {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$Body = $null,
        [hashtable]$Headers = $null,
        [int]$TimeoutSeconds = 30
    )
    $url = $BaseUrl.TrimEnd("/") + $Path
    $request = [System.Net.HttpWebRequest]::Create($url)
    $request.Method = $Method
    $request.Accept = "application/json"
    $request.UserAgent = "smartlib-bench/1.0"
    $request.Timeout = $TimeoutSeconds * 1000
    $request.ReadWriteTimeout = $TimeoutSeconds * 1000
    # 压测/编排场景下不需要连接复用，关掉可以避免服务端 keepalive 干扰状态判断
    $request.KeepAlive = $false
    if ($null -ne $Headers) {
        foreach ($key in $Headers.Keys) { $request.Headers.Add($key, [string]$Headers[$key]) }
    }

    # 注意：GET 等谓词不允许带请求体，只有真的传了 Body 才写 content。
    # （无条件 GetRequestStream() 会抛 ProtocolViolationException：
    #   "无法发送具有此谓词类型的内容正文"。）
    if ($null -ne $Body) {
        $request.ContentType = "application/json; charset=utf-8"
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Body)
        $request.ContentLength = $bytes.Length
        $requestStream = $request.GetRequestStream()
        $requestStream.Write($bytes, 0, $bytes.Length)
        $requestStream.Close()
    }

    $response = $null
    $statusCode = 0
    try {
        $response = $request.GetResponse()
        $statusCode = [int]$response.StatusCode
    }
    catch [System.Net.WebException] {
        # 4xx/5xx 会走这里，但响应体依然可以读出来
        $response = $_.Exception.Response
        if ($null -eq $response) {
            return @{ Ok = $false; StatusCode = 0; Text = $_.Exception.Message; Json = $null }
        }
        $statusCode = [int]$response.StatusCode
    }

    $text = ""
    try {
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream(), [System.Text.Encoding]::UTF8)
        $text = $reader.ReadToEnd()
        $reader.Close()
    }
    catch {
        $text = ""
    }
    finally {
        if ($null -ne $response) { $response.Close() }
    }

    $json = $null
    if (-not [string]::IsNullOrWhiteSpace($text)) {
        try { $json = ConvertFrom-Json -InputObject $text } catch { $json = $null }
    }
    return @{
        Ok = ($statusCode -ge 200 -and $statusCode -lt 300)
        StatusCode = $statusCode
        Text = $text
        Json = $json
    }
}

function Get-HealthSnapshot {
    <#
      返回哈希表：StatusAll / DbStatus / DownComponents / Ok
      为什么要看组件级状态，而不是只看整体 status：
        本地 H2 profile 下没有 Redis 时，/actuator/health 返回 HTTP 503 +
        "status":"DOWN"（redis 组件 DOWN），但 db 是 UP、应用完全可用
        （§6.1 的降级设计：Redis 不可用时自动降级为 DB 条件更新）。
        如果只等整体 UP，本地压测会永远等不到，脚本直接卡死/全部跳过。
      因此判据改成：HTTP 有响应 且 db=UP 即视为"可压测"，并把非 UP 组件打出来。
    #>
    $result = Invoke-SmartLib -Method "GET" -Path "/actuator/health"
    $snapshot = @{
        Ok = $false
        HttpStatus = $result.StatusCode
        StatusAll = "UNKNOWN"
        DbStatus = "UNKNOWN"
        DownComponents = @()
        Raw = $result.Text
    }
    if ($null -eq $result.Json) {
        return $snapshot
    }
    if ($null -ne $result.Json.status) { $snapshot.StatusAll = [string]$result.Json.status }
    if ($null -ne $result.Json.components) {
        foreach ($property in $result.Json.components.PSObject.Properties) {
            $componentStatus = [string]$property.Value.status
            if ($property.Name -eq "db") { $snapshot.DbStatus = $componentStatus }
            if ($componentStatus -ne "UP") {
                $snapshot.DownComponents += ("{0}={1}" -f $property.Name, $componentStatus)
            }
        }
    }
    # 有响应 + 数据库可用 => 可以压测（Redis 等组件 DOWN 由降级兜底）
    $snapshot.Ok = ($result.StatusCode -ne 0 -and $snapshot.DbStatus -eq "UP")
    return $snapshot
}

function Wait-BackendHealthy {
    param([int]$TimeoutSeconds)
    Write-Host "[run_all] 等待后端就绪：$BaseUrl/actuator/health（最多 $TimeoutSeconds 秒）"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $attempt = 0
    while ((Get-Date) -lt $deadline) {
        $attempt++
        $snapshot = Get-HealthSnapshot
        if ($snapshot.Ok) {
            Write-Host ("[run_all] 后端已就绪（第 {0} 次探测）：overall={1}, db={2}" -f `
                $attempt, $snapshot.StatusAll, $snapshot.DbStatus)
            if ($snapshot.DownComponents.Count -gt 0) {
                Write-Warning ("[run_all]   注意：以下组件非 UP -> {0}（本地无 Redis 时属预期，后端会走降级）" -f `
                    ($snapshot.DownComponents -join ", "))
            }
            return $true
        }
        Write-Host ("[run_all]   第 {0} 次：overall={1} db={2} http={3}，2 秒后重试" -f `
            $attempt, $snapshot.StatusAll, $snapshot.DbStatus, $snapshot.HttpStatus)
        Start-Sleep -Seconds 2
    }
    Write-Warning "[run_all] 后端在 $TimeoutSeconds 秒内没有就绪，后续步骤可能全部失败。"
    return $false
}

# ---------------------------- 结果收集 --------------------------------------
# 每一行 = 一个方案/引擎的一次实测；字段名与 summary.md 表头一一对应
$rows = New-Object System.Collections.ArrayList
$notes = New-Object System.Collections.ArrayList

function Read-JsonFile {
    # 【必须按字节读 + 显式 UTF-8 解码】
    # PS 5.1 的 Get-Content -Encoding UTF8 会把无 BOM 的 UTF-8 当 ANSI 读，
    # 中文（查询词/书名/备注）直接变成乱码，$obj.Note 之类的派生字段也可能因此错乱。
    # 我们自己的 Python 脚本写出来的是无 BOM 的 UTF-8，所以这里用 ReadAllBytes。
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $bytes = [System.IO.File]::ReadAllBytes($Path)
    }
    catch {
        return $null
    }
    if ($bytes.Length -eq 0) { return $null }
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    # 去掉可能的 UTF-8 BOM 字符，否则 ConvertFrom-Json 会报"无效的 JSON 基元"
    if ($text.Length -gt 0 -and [int][char]$text[0] -eq 65279) { $text = $text.Substring(1) }
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    try {
        return (ConvertFrom-Json -InputObject $text)
    }
    catch {
        Write-Warning "[run_all] 解析 JSON 失败：$Path -> $($_.Exception.Message)"
        return $null
    }
}

# 安全读属性：Set-StrictMode 2.0 下访问不存在的属性会直接抛异常，
# 而结果文件的结构会随脚本演进（例如早期版本没有 switchOk 字段），
# 所以统一用这个函数取值，避免"字段不存在"把整个汇总流程打断。
function Get-Prop {
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    if ($null -eq $property.Value) { return $Default }
    return $property.Value
}

# 按 "result.latencyMs.p50" 这样的路径取值；任意一段缺失就返回 $Default。
# 用途：直接写 $report.result.latencyMs.p50 在 StrictMode 下会因为
#       "属性不存在"抛 PropertyNotFoundException（本机实测踩过），
#       而 JSON 结构在不同后端/不同脚本版本间可能有差异。
function Get-PropPath {
    param($Object, [string]$Path, $Default = $null)
    $current = $Object
    foreach ($segment in $Path.Split(".")) {
        $current = Get-Prop -Object $current -Name $segment
        if ($null -eq $current) { return $Default }
    }
    return $current
}

function Format-Num {
    param($Value, [string]$Format = "0.##")
    if ($null -eq $Value) { return "-" }
    return ([double]$Value).ToString($Format, [System.Globalization.CultureInfo]::InvariantCulture)
}

# 百分比格式化：InvariantCulture 的 "P2" 会多出一个空格（"6.94 %"），
# 在 Markdown 表格里很难看，所以手工乘 100 再拼 "%"。
function Format-Percent {
    param($Value, [int]$Digits = 2)
    if ($null -eq $Value) { return "-" }
    $number = ([double]$Value) * 100.0
    return ($number.ToString("F" + $Digits, [System.Globalization.CultureInfo]::InvariantCulture) + "%")
}

# ---------------------------- 1. 健康检查 ------------------------------------
Write-Host ""
Write-Host "[run_all] ==================== SmartLib 一键基准测试 ====================" 
Write-Host "[run_all] 仓库根目录 : $repoRoot"
Write-Host "[run_all] 结果目录   : $OutDir"
Write-Host "[run_all] Python     : $python"
Write-Host "[run_all] 目标后端   : $BaseUrl"
Write-Host ""

$healthy = Wait-BackendHealthy -TimeoutSeconds $HealthTimeoutSeconds

# ---------------------------- 2. 准备策略列表 --------------------------------
# 与后端 application.yml 的 smartlib.stock.strategy 合法取值完全一致（顺序对应 S1~S6）
$allStrategies = @(
    "local-lock",        # S1
    "optimistic-db",     # S2（默认）
    "pessimistic-db",    # S3
    "version-retry",     # S4
    "redis-lua",         # S5
    "mq-serial"          # S6
)
$strategyPlan = @()
$available = $null
$adminEnabled = $true

# 用 GET /api/admin/search-engine 探测管理端点是否开启：
# AdminController 整个类由 @ConditionalOnProperty(smartlib.admin.enabled=true) 控制，
# 关闭时这些路径全都不存在 -> 404。
$probe = Invoke-SmartLib -Method "GET" -Path "/api/admin/search-engine"
if ($probe.StatusCode -eq 404 -or $probe.StatusCode -eq 405) {
    $adminEnabled = $false
    Write-Warning "[run_all] /api/admin/** 返回 $($probe.StatusCode)：后端未开启管理端点。"
    Write-Warning "[run_all]   本地启动请加：--smartlib.admin.enabled=true"
    Add-Content -LiteralPath (Join-Path $OutDir "notes.txt") -Encoding UTF8 `
        -Value "管理端点不可用（HTTP $($probe.StatusCode)）：无法运行期切换策略/引擎，本次只跑当前配置。"
}
elseif (-not $probe.Ok) {
    $adminEnabled = $false
    Write-Warning "[run_all] 无法访问 /api/admin/search-engine（HTTP $($probe.StatusCode)）：$($probe.Text)"
}
elseif ($null -ne (Get-Prop -Object $probe.Json -Name "data")) {
    $probeData = Get-Prop -Object $probe.Json -Name "data"
    Write-Host ("[run_all] 管理端点可用：当前引擎={0}，可选={1}" -f `
        (Get-Prop -Object $probeData -Name "current"), `
        ((@(Get-Prop -Object $probeData -Name "available")) -join ", "))
    $readiness = Get-Prop -Object $probeData -Name "readiness"
    if ($null -ne $readiness) {
        $readyList = @()
        foreach ($property in $readiness.PSObject.Properties) {
            $readyList += ("{0}={1}" -f $property.Name, $property.Value)
        }
        Write-Host ("[run_all]   引擎就绪状态：{0}" -f ($readyList -join ", "))
    }
}

if ($adminEnabled) {
    # 探测可用策略列表：PUT 一次是幂等的（重切到同一策略无副作用），
    # 比"猜一个必然非法的值去触发 400"更安全。
    $switchProbe = Invoke-SmartLib -Method "PUT" -Path "/api/admin/stock-strategy?name=optimistic-db"
    $switchProbeData = Get-Prop -Object $switchProbe.Json -Name "data"
    if ($switchProbe.Ok -and $null -ne $switchProbeData) {
        $availableRaw = Get-Prop -Object $switchProbeData -Name "available"
        if ($null -ne $availableRaw) {
            $available = @($availableRaw)
        }
        Write-Host "[run_all] 库存策略：当前=$(Get-Prop -Object $switchProbeData -Name 'current')，可选=$($available -join ', ')"
    }
    else {
        Write-Warning "[run_all] 切换策略探测失败（HTTP $($switchProbe.StatusCode)）：$($switchProbe.Text)"
        Add-Content -LiteralPath (Join-Path $OutDir "notes.txt") -Encoding UTF8 `
            -Value "切换策略探测失败（HTTP $($switchProbe.StatusCode)）：$($switchProbe.Text)"
    }
}

foreach ($strategy in $allStrategies) {
    if ($null -ne $available -and $available.Count -gt 0) {
        if ($available -contains $strategy) { $strategyPlan += $strategy }
        else { Write-Host "[run_all] 跳过 $strategy：后端未注册该策略" }
    }
    else {
        $strategyPlan += $strategy
    }
}
if ($strategyPlan.Count -eq 0) { $strategyPlan = @("optimistic-db") }

# ---------------------------- 3. 六种库存策略压测 ----------------------------
$strategyIndex = 0
foreach ($strategy in $strategyPlan) {
    $strategyIndex++
    Write-Host ""
    Write-Host "[run_all] ---------- [$strategyIndex/$($strategyPlan.Count)] 库存策略 $strategy ----------"

    $switchNote = "未切换"
    if ($adminEnabled) {
        $switch = Invoke-SmartLib -Method "PUT" -Path ("/api/admin/stock-strategy?name=" + $strategy)
        if ($switch.Ok) {
            $current = Get-PropPath -Object $switch.Json -Path "data.current" -Default $strategy
            $switchNote = "切换成功（current=$current）"
            Write-Host "[run_all]   已切换到 $current"
            # 切换是立即生效的内存状态变更，但给 1 秒让第一次请求的懒初始化（如 MQ 队列）就绪
            Start-Sleep -Seconds 1
        }
        else {
            $switchNote = "切换失败 HTTP $($switch.StatusCode) $($switch.Text)"
            Write-Warning "[run_all]   $switchNote"
            if (-not $KeepGoingOnStrategyError) {
                $rows.Add([pscustomobject]@{
                    Kind = "stock"; Name = $strategy; Switch = $switchNote
                    Threads = $Threads; Qps = $null; P50 = $null; P90 = $null; P99 = $null
                    ErrorRate = $null; Success = $null; Oversold = "-"; Recall = "-"
                    ResultFile = "-"; Note = "策略切换失败，本次未压测"
                }) | Out-Null
                continue
            }
        }
    }
    else {
        $switchNote = "管理端点不可用，沿用配置默认值"
    }

    $outFile = Join-Path $OutDir ("borrow_{0}.json" -f $strategy)
    if (Test-Path -LiteralPath $outFile) { Remove-Item -LiteralPath $outFile -Force }

    # 变量名不要用 $args：那是 PowerShell 的自动变量，会被覆盖掉
    $pyArgs = @(
        $loadBorrow,
        "--url", $BaseUrl,
        "--book-id", $BookId,
        "--threads", $Threads,
        "--requests", $Requests,
        "--reader-ids", $ReaderIds,
        "--label", $strategy,
        "--out", $outFile
    )
    Write-Host "[run_all]   $python $($pyArgs -join ' ')"
    $exitCode = 0
    & $python @pyArgs 2>&1 | ForEach-Object { Write-Host "    $_" }
    $exitCode = $LASTEXITCODE

    $report = Read-JsonFile -Path $outFile
    if ($null -eq $report) {
        $rows.Add([pscustomobject]@{
            Kind = "stock"; Name = $strategy; Switch = $switchNote
            Threads = $Threads; Qps = $null; P50 = $null; P90 = $null; P99 = $null
            ErrorRate = $null; Success = $null; Oversold = "-"; Recall = "-"
            ResultFile = "-"; Note = "压测脚本未产出结果文件（exit=$exitCode）"
        }) | Out-Null
        Add-Content -LiteralPath (Join-Path $OutDir "notes.txt") -Encoding UTF8 `
            -Value "策略 $strategy：压测脚本未产出结果文件，exit=$exitCode"
        continue
    }

    $oversoldText = "未知"
    $oversellCheck = Get-Prop -Object $report -Name "oversellCheck"
    if ($null -ne $oversellCheck) {
        $oversoldFlag = Get-Prop -Object $oversellCheck -Name "oversold"
        if ($oversoldFlag -eq $true) {
            $oversoldText = "超卖 $(Get-Prop -Object $oversellCheck -Name 'oversoldUnits' -Default '?') 册"
        }
        elseif ($oversoldFlag -eq $false) {
            $oversoldText = "0"
        }
    }
    $noteParts = @()
    if ($exitCode -eq 3) { $noteParts += "脚本判定发生超卖" }
    if ($exitCode -eq 4) { $noteParts += "全部请求失败" }
    $messageSamples = Get-PropPath -Object $report -Path "result.messageSamples"
    if ($null -ne $messageSamples -and @($messageSamples).Count -gt 0) {
        $noteParts += "消息样本: " + ((@($messageSamples) | Select-Object -First 2) -join " / ")
    }

    $rows.Add([pscustomobject]@{
        Kind = "stock"
        Name = $strategy
        Switch = $switchNote
        Threads = $Threads
        Qps = Get-PropPath -Object $report -Path "result.qps"
        P50 = Get-PropPath -Object $report -Path "result.latencyMs.p50"
        P90 = Get-PropPath -Object $report -Path "result.latencyMs.p90"
        P99 = Get-PropPath -Object $report -Path "result.latencyMs.p99"
        ErrorRate = Get-PropPath -Object $report -Path "result.errorRate"
        Success = Get-PropPath -Object $report -Path "result.successCount"
        Oversold = $oversoldText
        Recall = "-"
        ResultFile = (Split-Path -Leaf $outFile)
        Note = ($noteParts -join "；")
    }) | Out-Null
}

# ---------------------------- 4. 检索基准（Q1 / Q3） -------------------------
# 后端 AdminController 提供 PUT /api/admin/search-engine?name=xxx 运行期切换引擎，
# 所以这里可以真的切过去再压测（需要 smartlib.admin.enabled=true）。
# 切换后必须重建倒排索引：批量导入书目（gen_books.py）之后索引是空的，
# 不重建的话 Q3 会返回空结果，召回率假性为 0 —— 那是数据没准备好，不是引擎差。
$engineResults = @{}
if (-not $SkipSearch) {
    Write-Host ""
    Write-Host "[run_all] ---------- 检索基准（Q1 like / Q3 inverted-index） ----------"
    if (-not $adminEnabled) {
        Write-Warning "[run_all] 管理端点不可用：无法切换引擎，下面的结果只能标注为"
        Write-Warning "[run_all]   '当前配置引擎' 的实测值，不能当作 Q1/Q3 对比数据。"
    }

    foreach ($engine in @("like", "inverted-index")) {
        $outFile = Join-Path $OutDir ("search_{0}.json" -f $engine)
        if (Test-Path -LiteralPath $outFile) { Remove-Item -LiteralPath $outFile -Force }

        $pyArgs = @(
            $searchBench,
            "--url", $BaseUrl,
            "--engine", $engine,
            "--threads", $Threads,
            "--requests", $Requests,
            "--label", $engine,
            "--out", $outFile
        )
        if ($adminEnabled) {
            $pyArgs += "--switch-engine"
            if ($engine -eq "inverted-index") { $pyArgs += "--reindex" }
        }
        Write-Host "[run_all]   $python $($pyArgs -join ' ')"
        $exitCode = 0
        & $python @pyArgs 2>&1 | ForEach-Object { Write-Host "    $_" }
        $exitCode = $LASTEXITCODE

        $report = Read-JsonFile -Path $outFile
        if ($null -eq $report) {
            $rows.Add([pscustomobject]@{
                Kind = "search"; Name = $engine; Switch = "-"
                Threads = $Threads; Qps = $null; P50 = $null; P90 = $null; P99 = $null
                ErrorRate = $null; Success = $null; Oversold = "-"; Recall = "-"
                ResultFile = "-"; Note = "检索压测未产出结果文件（exit=$exitCode）"
            }) | Out-Null
            continue
        }

        # 服务端"实际"跑的引擎名（来自 /api/admin/search-engine），
        # 与 --engine 标签不一致时必须在表里体现出来
        $observedEngine = "-"
        if ($null -ne $report.engineSwitch -and $null -ne $report.engineSwitch.current) {
            $observedEngine = [string]$report.engineSwitch.current
        }
        elseif ($null -ne $report.engineObservedAfter) {
            $observedEngine = [string]$report.engineObservedAfter
        }
        $switchText = "请求=$engine，实际=$observedEngine"
        if ($null -ne $report.engineSwitch -and $report.engineSwitch.switchOk -eq $false) {
            $switchText = "切换失败（标签=$engine，实际=$observedEngine）"
        }
        $noteParts = @("空结果 $($report.result.emptyResultCount) 条")
        if ($null -ne $report.engineSwitch -and $null -ne $report.engineSwitch.reindexMillis) {
            $noteParts += ("索引重建 {0} ms" -f $report.engineSwitch.reindexMillis)
        }

        $engineResults[$engine] = $report
        $rows.Add([pscustomobject]@{
            Kind = "search"
            Name = $engine
            Switch = $switchText
            Threads = $Threads
            Qps = $report.result.qps
            P50 = $report.result.latencyMs.p50
            P90 = $report.result.latencyMs.p90
            P99 = $report.result.latencyMs.p99
            ErrorRate = $report.result.errorRate
            Success = $report.result.successCount
            Oversold = "-"
            Recall = "-"
            ResultFile = (Split-Path -Leaf $outFile)
            Note = ($noteParts -join "；")
        }) | Out-Null
    }
}

# ---------------------------- 5. 召回率评估 ----------------------------------
# 召回率要对【两个引擎各跑一次】才有对比意义（like 引擎靠 SQL 模糊匹配，
# 倒排索引靠分词召回，两者的召回率差异正是 §6.2 的叙事重点）。
$recallPercent = "-"
if (-not $SkipRecall) {
    Write-Host ""
    Write-Host "[run_all] ---------- 召回率评估（recall_dataset.json） ----------"
    $recallEngines = @("like", "inverted-index")
    if (-not $adminEnabled) { $recallEngines = @("current-config") }

    foreach ($recallEngine in $recallEngines) {
        $outFile = Join-Path $OutDir ("recall_{0}.json" -f $recallEngine)
        if (Test-Path -LiteralPath $outFile) { Remove-Item -LiteralPath $outFile -Force }

        $pyArgs = @(
            $searchBench,
            "--url", $BaseUrl,
            "--engine", $recallEngine,
            "--threads", "1",
            "--requests", "$RecallRequests",
            "--queries-file", $recallData,
            "--recall-check", $recallData,
            "--label", ("recall-" + $recallEngine),
            "--out", $outFile
        )
        if ($adminEnabled -and $recallEngine -ne "current-config") {
            $pyArgs += "--switch-engine"
            if ($recallEngine -eq "inverted-index") { $pyArgs += "--reindex" }
        }
        Write-Host "[run_all]   $python $($pyArgs -join ' ')"
        $exitCode = 0
        & $python @pyArgs 2>&1 | ForEach-Object { Write-Host "    $_" }
        $exitCode = $LASTEXITCODE

        $report = Read-JsonFile -Path $outFile
        if ($null -ne $report -and $null -ne $report.recall) {
            $value = Format-Num -Value $report.recall.averageRecallPercent -Format "0.00"
            if ($recallEngine -eq "inverted-index") { $recallPercent = $value }
            $observedEngine = "-"
            if ($null -ne $report.engineSwitch -and $null -ne $report.engineSwitch.current) {
                $observedEngine = [string]$report.engineSwitch.current
            }
            $rows.Add([pscustomobject]@{
                Kind = "recall"
                Name = $recallEngine
                Switch = "实际=$observedEngine"
                Threads = 1
                Qps = $null
                P50 = $null
                P90 = $null
                P99 = $null
                ErrorRate = $null
                Success = $report.recall.evaluatedQueries
                Oversold = "-"
                Recall = $value
                ResultFile = (Split-Path -Leaf $outFile)
                Note = ("Top1 命中率 {0}%；召回口径：命中期望 id 的比例" -f `
                    [math]::Round([double]$report.recall.hitAt1Rate * 100, 2))
            }) | Out-Null
        }
        else {
            $rows.Add([pscustomobject]@{
                Kind = "recall"; Name = $recallEngine; Switch = "-"
                Threads = 1; Qps = $null; P50 = $null; P90 = $null; P99 = $null
                ErrorRate = $null; Success = $null; Oversold = "-"; Recall = "-"
                ResultFile = "-"; Note = "召回评估未产出结果（exit=$exitCode）"
            }) | Out-Null
        }
    }
}

# ---------------------------- 6. 对账 ----------------------------------------
# 响应结构（AdminController#reconcile）：
#   {"code":0,"msg":"ok","data":{"violations":[...],"violationCount":0,
#                                "consistent":true, "redisLuaDegradedCount":..}}
# 注意：data 是对象，不是裸数组。判据看 data.violations 是否为空。
Write-Host ""
Write-Host "[run_all] ---------- 库存对账 /api/admin/reconcile ----------"
$reconcileText = "无法获取"
$reconcileVerdict = "未知"
$redisDegraded = $null
$reconcileResult = Invoke-SmartLib -Method "GET" -Path "/api/admin/reconcile"
if ($reconcileResult.Ok) {
    $reconcileText = $reconcileResult.Text.Trim()
    Write-Host "[run_all]   响应：$reconcileText"
    if ($null -ne $reconcileResult.Json -and $null -ne $reconcileResult.Json.data) {
        $reconcileData = $reconcileResult.Json.data
        $violations = @($reconcileData.violations)
        if ($null -ne $reconcileData.redisLuaDegradedCount) {
            $redisDegraded = [int]$reconcileData.redisLuaDegradedCount
        }
        if ($null -ne $reconcileData.consistent) {
            if ([bool]$reconcileData.consistent -and $violations.Count -eq 0) {
                $reconcileVerdict = "PASS（violations 为空，库存与流水守恒、无负库存）"
            }
            else {
                $reconcileVerdict = ("FAIL（violations={0}，书目 id: {1}）" -f `
                    $violations.Count, ($violations -join ", "))
            }
        }
        else {
            $reconcileVerdict = ("violations={0}" -f $violations.Count)
        }
    }
    if ($null -ne $redisDegraded) {
        Write-Host "[run_all]   redis-lua 降级次数：$redisDegraded（>0 表示 S5 的数字其实是 S2 的）"
    }
    Write-Host "[run_all]   判据：$reconcileVerdict"
}
else {
    Write-Host "[run_all]   失败 HTTP $($reconcileResult.StatusCode)：$($reconcileResult.Text)"
    $reconcileVerdict = "无法获取（HTTP $($reconcileResult.StatusCode)）"
}
$reconcileFile = Join-Path $OutDir "reconcile.json"
if ($reconcileResult.Ok) {
    Set-Content -LiteralPath $reconcileFile -Value $reconcileResult.Text -Encoding UTF8
}

# ---------------------------- 7. 汇总 Markdown -------------------------------
# 用【单引号 here-string】整段拼模板：
#   * 单引号 here-string 不做变量插值也不解析转义，所以反引号可以直接写（不用写成 ``），
#     这是本文件唯一能既可读又正确的办法（双引号字符串里反引号是转义符）。
#   * 需要插值的地方显式写 + 变量 +，避免整段变成难读的格式化调用。
$summaryPath = Join-Path $OutDir "summary.md"

$healthSnapshot = Get-HealthSnapshot
$healthNow = "overall=$($healthSnapshot.StatusAll), db=$($healthSnapshot.DbStatus), http=$($healthSnapshot.HttpStatus)"
if ($healthSnapshot.DownComponents.Count -gt 0) {
    $healthNow += ", 非 UP 组件: " + ($healthSnapshot.DownComponents -join ", ")
}
# 压测期间后端可能被重启或崩掉，这里重新探一次是为了让 summary.md 记录结束时的事实
if (-not $healthSnapshot.Ok) {
    Write-Warning "[run_all] 收尾健康检查异常：$healthNow（数字请谨慎引用）"
}
$reconcileSnippet = $reconcileText

$mdHeader = @'
# SmartLib 基准测试汇总

> 本文件由 `scripts/bench/run_all.ps1` 自动生成，请勿手工编辑。
> 所有数字都是**本机实测**，没有目标值/承诺值；不同机器、不同数据量不可直接横向比较。
'@

$mdFooter = @'
## 对账结果（GET /api/admin/reconcile）

```text
@@RECONCILE@@
```

判据：响应 data.consistent == true 且 data.violations 为空数组，表示库存与流水守恒、
且没有负库存；非空表示存在绕过 StockService 的写路径（P0 级问题）。

## 原始数据文件

@@FILES@@

## 口径与限制（必须和数字一起看）

1. **客户端即瓶颈**：`load_borrow.py` / `search_bench.py` 用纯标准库 http.client，
   每个请求新建 TCP 连接（没有连接池复用）。因此 QPS 是**下界**，不是服务端极限。
   要测服务端上限请用 wrk/JMeter，并对比两者差异。
2. **检索引擎可以运行期切换**：本脚本用 `PUT /api/admin/search-engine?name=xxx` 真的切换，
   切到 inverted-index 后还会 `POST /api/admin/search/reindex` 重建倒排索引
   （批量导入书目后不重建，索引是空的，召回率会假性为 0）。
   表里"请求=xxx，实际=yyy"两列必须一致才算有效对比；不一致时以"实际"为准。
   该接口需要后端配置 `smartlib.admin.enabled=true`。
3. **召回率口径**：期望集是人工标注的"至少应被召回"的小集合（见 recall_dataset.json），
   不是完整相关文档集合，属可用性召回，不能当学术意义的 recall 用。
4. **策略切换前提**：`/api/admin/stock-strategy` 同样需要 `smartlib.admin.enabled=true`；
   未开启时本轮只会跑当前配置的策略（失败原因记在 notes.txt）。
5. **S5/S6 的真实性**：redis-lua 依赖 Redis 可用，Redis 不可用时后端会**自动降级**为
   optimistic-db（DegradeFallback）。降级的次数可以从 /api/admin/reconcile 的
   redisLuaDegradedCount 看出来 —— 如果它不为 0，S5 那一行的数字实际上是 S2 的数字，
   必须在文档里说明，不能当作"Lua 原子预扣"的性能。
   mq-serial 在本地是"单线程 Dispatcher + 有界队列"的模拟实现，不是真实 RocketMQ。
6. **借阅压测的数据依赖**：同一读者对同一本书只能持 1 册（§2.2-5），
   所以并发抢同一本书必须用读者池（--reader-ids），否则测到的是"重复借阅拦截率"。
   另外每轮压测都会真实扣减库存，多轮跑下来书会被借空，
   那时"错误率上升"是库存耗尽导致的正常业务失败，不是策略问题 ——
   对比多轮数据前请先确认库存量，或每轮之间重置数据。
7. **没有跑起来的方案不要下结论**：表里出现 "-" 表示该项本轮未测得，不等价于"性能为 0"。
'@

# 头部 + 元信息（逐行显式插值，不用 -replace：replace 容易误伤正文里的同名字符串）
$nl = [Environment]::NewLine
$metaLines = New-Object System.Collections.ArrayList
$metaLines.Add("") | Out-Null
$metaLines.Add("- 生成时间：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')") | Out-Null
$metaLines.Add("- 目标后端：$BaseUrl") | Out-Null
$metaLines.Add("- 压测书目：book.id = $BookId") | Out-Null
$metaLines.Add(("- 并发配置：{0} 线程 / 请求数上限 {1}（实际请求数 = 线程数 x floor(请求数/线程数)）" -f $Threads, $Requests)) | Out-Null
$metaLines.Add("- 读者池：$ReaderIds（并发抢同一本书必须给多个读者，否则会被业务规则拦截，见 load_borrow.py 注释）") | Out-Null
$metaLines.Add("- Python：$python") | Out-Null
$metaLines.Add("- 后端健康检查：$healthNow") | Out-Null
$metaLines.Add("- 管理端点：$(if ($adminEnabled) { '可用（可运行期切换策略/引擎）' } else { '不可用（只跑当前配置，见 notes.txt）' })") | Out-Null
$metaLines.Add("- 对账结论：$reconcileVerdict") | Out-Null
if ($null -ne $redisDegraded) {
    $metaLines.Add("- redis-lua 降级次数：$redisDegraded（>0 时 S5 的实测值等同于 S2，需在文档说明）") | Out-Null
}
$metaLines.Add("- 数据规模：使用当前数据库里的真实数据量，未做人为裁剪") | Out-Null
$metaLines.Add("") | Out-Null
$metaLines.Add("## 结果明细") | Out-Null
$metaLines.Add("") | Out-Null
$metaLines.Add("| 类别 | 方案/引擎 | 并发 | QPS | P50(ms) | P90(ms) | P99(ms) | 错误率 | 成功数 | 超卖数 | 召回率(%) | 结果文件 | 备注 |") | Out-Null
$metaLines.Add("|---|---|---|---|---|---|---|---|---|---|---|---|---|") | Out-Null

# 逐行拼装表格
$tableLines = New-Object System.Collections.ArrayList
foreach ($row in $rows) {
    $errorRateText = "-"
    if ($null -ne $row.ErrorRate) {
        $errorRateText = Format-Percent -Value $row.ErrorRate -Digits 2
    }
    $successText = "-"
    if ($null -ne $row.Success) { $successText = [string]$row.Success }
    $tableLines.Add(("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9} | {10} | {11} | {12} |" -f `
        $row.Kind, $row.Name, $row.Threads,
        (Format-Num -Value $row.Qps -Format "0.00"),
        (Format-Num -Value $row.P50 -Format "0.000"),
        (Format-Num -Value $row.P90 -Format "0.000"),
        (Format-Num -Value $row.P99 -Format "0.000"),
        $errorRateText,
        $successText,
        $row.Oversold, $row.Recall, $row.ResultFile, $row.Note)) | Out-Null
}

$fileLines = New-Object System.Collections.ArrayList
foreach ($row in $rows) {
    if ($row.ResultFile -ne "-") {
        $fileLines.Add(("- {0} —— {1} / {2}" -f $row.ResultFile, $row.Kind, $row.Name)) | Out-Null
    }
}
$fileLines.Add("- reconcile.json —— 对账接口原始响应") | Out-Null

$mdFooterFilled = $mdFooter.Replace("@@RECONCILE@@", $reconcileSnippet).Replace("@@FILES@@", ($fileLines -join $nl))

$markdown = $mdHeader + $nl + ($metaLines -join $nl) + $nl + ($tableLines -join $nl) + `
    $nl + $nl + $mdFooterFilled + $nl

Set-Content -LiteralPath $summaryPath -Value $markdown -Encoding UTF8

Write-Host ""
Write-Host "[run_all] ==================== 完成 ===================="
Write-Host "[run_all] 汇总报告 : $summaryPath"
Write-Host "[run_all] 原始 JSON : $OutDir"
if ($rows.Count -eq 0) {
    Write-Warning "[run_all] 一行结果都没有产出，请检查后端是否启动、管理端点是否开启。"
    exit 1
}
exit 0

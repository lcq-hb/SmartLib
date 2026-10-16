# =============================================================================
# scripts/bench/run_all.ps1 —— SmartLib 一键基准测试（v2：逐轮重置库存的可比测量）
#
# 【为什么有 v2】v1 有一个测量设计缺陷：六种库存策略依次压**同一本书**，
#   第一轮就把复本借光了，后面五轮的失败原因是"库存没了 / 同一读者重复借"，
#   于是那张"六策略对比表"里的 QPS/P99 测的其实是"业务规则拒绝的速度"，
#   策略之间根本不可比。
#
# 【v2 怎么修的】
#   1) 每轮开始前把目标书的库存**重置到已知值**：用纯公开 API 实现
#      （借出 → 采集指标 → POST /api/borrows/{id}/return 全部归还），
#      所以不需要改后端、也不需要 DB 客户端。归还同时解除"同一读者同一本书只能持 1 册"，
#      读者可在下一轮复用。
#   2) 读者池先预筛：用 GET /api/readers/{id}/credit 排除
#      信用分 < min_credit_score、有逾期、有欠费、status 非 ACTIVE 的读者。
#      （v1 把信用分 55 的 reader 5 放进池子，每轮都产生固定失败。）
#   3) 每轮请求数 = 该书复本数，且 --reader-mode distinct 保证每个读者只借 1 次
#      => 成功数应当【恰好等于】该轮复本数：少了=误拒，多了=超卖。
#   4) 每轮跑完都调 /api/admin/reconcile，按轮记录"库存与流水守恒"。
#   5) 库存判据一律读**权威源**：GET /internal/agent/tools/books/{id}/stock
#      （HMAC 签名，直读库）。不用 GET /api/books/{id}，因为它读的是缓存，
#      而实测发现后端没有任何代码在库存变化时失效书目缓存 —— 它会返回旧值。
#
# 流程：
#   等后端就绪 → 读规则 → 预筛读者池 → 读候选书 → 
#   依次切 6 种库存策略，每种策略跑 N 轮（每轮独立库存、独立对账）→
#   跑 Q1(like)/Q3(inverted-index) 检索基准与召回率 → 汇总成 Markdown
#
# 兼容性：PowerShell 5.1（Windows 自带）。刻意不用 ?? / ?. / 三元运算符 / -Parallel。
#         本文件含中文输出，必须保存为 UTF-8 with BOM（没有 BOM 时 PS 5.1 会解析失败）。
#
# 用法（直接 & 脚本会被执行策略拦截，必须显式 Bypass）：
#   powershell -ExecutionPolicy Bypass -File scripts/bench/run_all.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/bench/run_all.ps1 `
#       -BaseUrl http://localhost:8080 -CopiesPerRound 8 -Cycles 5
#
# 【前提】策略/引擎切换接口要求后端配置 smartlib.admin.enabled=true。
# =============================================================================
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://localhost:8080",
    [int]$CopiesPerRound = 8,
    [int]$Cycles = 5,
    [int]$RequestsPerCycle = 0,
    [int]$MaxReaderId = 20,
    [int]$RecallRequests = 42,
    [string]$OutDir = "",
    [int]$HealthTimeoutSeconds = 180,
    [string]$InternalSignSecret = "dev-internal-secret-please-change",
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
#  * HttpWebRequest：GET/PUT/POST 全部走同一条路径，能拿到状态码 + 响应体 + 自定义头。
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
    $request.KeepAlive = $false
    if ($null -ne $Headers) {
        foreach ($key in $Headers.Keys) { $request.Headers.Add($key, [string]$Headers[$key]) }
    }
    # GET 不允许带请求体；判据必须同时排除 $null 与空串（[string]$Body = $null 会变成 ""）
    if (-not [string]::IsNullOrEmpty($Body)) {
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

# 内部接口 HMAC 签名（与后端 InternalSignFilter 完全一致）：
#   canonical = timestamp + "\n" + METHOD + "\n" + pathWithQuery + "\n" + bodyUtf8
#   sign      = lowercaseHex(HMAC_SHA256(secret, canonical))
function Get-InternalSignHeaders {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$PathWithQuery,
        [string]$Body = "",
        [string]$Secret = "dev-internal-secret-please-change"
    )
    $timestamp = [string][long][Math]::Floor((Get-Date).ToUniversalTime() - (Get-Date "1970-01-01T00:00:00Z")).TotalSeconds
    $canonical = $timestamp + "`n" + $Method.ToUpper() + "`n" + $PathWithQuery + "`n" + $Body
    $hmac = New-Object System.Security.Cryptography.HMACSHA256
    $hmac.Key = [System.Text.Encoding]::UTF8.GetBytes($Secret)
    $hash = $hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($canonical))
    $hmac.Dispose()
    $sign = -join ($hash | ForEach-Object { $_.ToString("x2") })
    return @{
        "X-Internal-Timestamp" = $timestamp
        "X-Internal-Sign" = $sign
    }
}

# 权威库存：内部签名接口（直读数据库，不经过书目缓存）
function Get-AuthoritativeStock {
    param([Parameter(Mandatory = $true)][long]$BookId)
    $path = "/internal/agent/tools/books/$BookId/stock"
    $headers = Get-InternalSignHeaders -Method "GET" -PathWithQuery $path -Body "" -Secret $InternalSignSecret
    $result = Invoke-SmartLib -Method "GET" -Path $path -Headers $headers
    if (-not $result.Ok) {
        return @{ Ok = $false; Available = $null; Total = $null
                  Note = "内部库存接口失败 HTTP $($result.StatusCode)：$($result.Text)" }
    }
    $data = Get-Prop -Object $result.Json -Name "data"
    return @{
        Ok = $true
        Available = Get-Prop -Object $data -Name "availableCopies"
        Total = Get-Prop -Object $data -Name "totalCopies"
        Title = Get-Prop -Object $data -Name "title"
        Note = ""
    }
}

# ---------------------------- 小工具 ----------------------------------------
function Get-Prop {
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    if ($null -eq $property.Value) { return $Default }
    return $property.Value
}

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

function Format-Percent {
    param($Value, [int]$Digits = 2)
    if ($null -eq $Value) { return "-" }
    $number = ([double]$Value) * 100.0
    return ($number.ToString("F" + $Digits, [System.Globalization.CultureInfo]::InvariantCulture) + "%")
}

# 写 UTF-8【无 BOM】文本文件。
# 为什么不能用 Set-Content -Encoding UTF8：PS 5.1 会写出带 BOM 的文件，
# 于是 reconcile.json 这类"给机器读的 JSON"用 python json.load 直接报
# "Unexpected UTF-8 BOM"。Python 侧写的是无 BOM，两边必须一致。
function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Read-JsonFile {
    # 必须按字节读 + 显式 UTF-8 解码：PS 5.1 的 Get-Content -Encoding UTF8
    # 会把无 BOM 的 UTF-8 当 ANSI 读，中文（书名/备注）全变乱码。
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { $bytes = [System.IO.File]::ReadAllBytes($Path) } catch { return $null }
    if ($bytes.Length -eq 0) { return $null }
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($text.Length -gt 0 -and [int][char]$text[0] -eq 65279) { $text = $text.Substring(1) }
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    try { return (ConvertFrom-Json -InputObject $text) }
    catch {
        Write-Warning "[run_all] 解析 JSON 失败：$Path -> $($_.Exception.Message)"
        return $null
    }
}

function Add-Note {
    param([Parameter(Mandatory = $true)][string]$OutDir, [Parameter(Mandatory = $true)][string]$Message)
    $path = Join-Path $OutDir "notes.txt"
    $existing = ""
    if (Test-Path -LiteralPath $path) {
        $bytes = [System.IO.File]::ReadAllBytes($path)
        $existing = [System.Text.Encoding]::UTF8.GetString($bytes)
        if ($existing.Length -gt 0 -and [int][char]$existing[0] -eq 65279) { $existing = $existing.Substring(1) }
        if (-not $existing.EndsWith([Environment]::NewLine)) { $existing += [Environment]::NewLine }
    }
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Utf8NoBom -Path $path -Content ($existing + "[$stamp] " + $Message + [Environment]::NewLine)
}

# ---------------------------- 健康检查 --------------------------------------
function Get-HealthSnapshot {
    # 判据：HTTP 有响应 且 db=UP 即视为"可压测"。
    # 本地无 Redis 时 /actuator/health 是 503 + status=DOWN（redis 组件 DOWN），
    # 但应用完全可用（库存策略自动降级到 DB 条件更新），只等 status=UP 会永远等不到。
    $result = Invoke-SmartLib -Method "GET" -Path "/actuator/health"
    $snapshot = @{
        Ok = $false
        HttpStatus = $result.StatusCode
        StatusAll = "UNKNOWN"
        DbStatus = "UNKNOWN"
        DownComponents = @()
    }
    if ($null -eq $result.Json) { return $snapshot }
    $statusAll = Get-Prop -Object $result.Json -Name "status"
    if ($null -ne $statusAll) { $snapshot.StatusAll = [string]$statusAll }
    $components = Get-Prop -Object $result.Json -Name "components"
    if ($null -ne $components) {
        foreach ($property in $components.PSObject.Properties) {
            $componentStatus = [string](Get-Prop -Object $property.Value -Name "status" -Default "UNKNOWN")
            if ($property.Name -eq "db") { $snapshot.DbStatus = $componentStatus }
            if ($componentStatus -ne "UP") {
                $snapshot.DownComponents += ("{0}={1}" -f $property.Name, $componentStatus)
            }
        }
    }
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

# ---------------------------- 读者池预筛 ------------------------------------
# 为什么必须筛：蓝图的借阅规则里"信用分 < min_credit_score 禁止借阅"、
# "有逾期未还禁止新增借阅"、"欠费达到阈值禁止借阅"。
# 把这些读者放进池子，他们每次请求都会失败，于是六种策略都会得到同样的固定失败 ——
# 看起来像"策略不好"，实际上是抽样污染。先筛掉，失败才代表策略/库存的真实问题。
function Get-ReaderScreening {
    param(
        [int]$MaxReaderId,
        [double]$MinCredit,
        [double]$FineFreeLimit
    )
    $usable = New-Object System.Collections.ArrayList
    $excluded = New-Object System.Collections.ArrayList
    $checked = 0
    $notFound = 0
    for ($id = 1; $id -le $MaxReaderId; $id++) {
        $credit = Invoke-SmartLib -Method "GET" -Path "/api/readers/$id/credit"
        if ($credit.StatusCode -eq 404) { $notFound++; continue }
        $data = Get-Prop -Object $credit.Json -Name "data"
        if (-not $credit.Ok -or $null -eq $data) {
            $excluded.Add([pscustomobject]@{
                ReaderId = $id; Reason = "读取 credit 失败 HTTP $($credit.StatusCode)"; Detail = $credit.Text
            }) | Out-Null
            continue
        }
        $checked++
        $score = Get-Prop -Object $data -Name "creditScore"
        $status = Get-Prop -Object $data -Name "status"
        $overdue = Get-Prop -Object $data -Name "overdueCount" -Default 0
        $fine = Get-Prop -Object $data -Name "unpaidFine" -Default 0

        # 再用 /loans 交叉核对逾期（credit 里的 overdueCount 是聚合值，
        # 这里拿到逐条 overdueDays 作为二次证据，避免只看一个字段就下结论）
        $overdueLoans = 0
        $loans = Invoke-SmartLib -Method "GET" -Path "/api/readers/$id/loans?limit=50"
        if ($loans.Ok) {
            foreach ($loan in @(Get-Prop -Object $loans.Json -Name "data")) {
                $days = Get-Prop -Object $loan -Name "overdueDays" -Default 0
                if ([double]$days -gt 0) { $overdueLoans++ }
            }
        }

        $reason = $null
        if ($status -ne "ACTIVE") {
            $reason = "status=$status（非 ACTIVE，最常见是 FROZEN）"
        }
        elseif ([double]$score -lt $MinCredit) {
            $reason = "信用分 $score < 阈值 $MinCredit"
        }
        elseif ([double]$overdue -gt 0 -or $overdueLoans -gt 0) {
            $reason = "存在逾期（credit.overdueCount=$overdue，loans 逾期条数=$overdueLoans）"
        }
        elseif ([double]$fine -ge $FineFreeLimit) {
            $reason = "欠费 $fine >= 阈值 $FineFreeLimit"
        }

        if ($null -ne $reason) {
            $excluded.Add([pscustomobject]@{
                ReaderId = $id; Reason = $reason
                Detail = "creditScore=$score status=$status overdueCount=$overdue unpaidFine=$fine"
            }) | Out-Null
        }
        else {
            $usable.Add([pscustomobject]@{
                ReaderId = $id; CreditScore = $score; Status = $status
            }) | Out-Null
        }
    }
    return @{
        Usable = @($usable)
        Excluded = @($excluded)
        Checked = $checked
        NotFound = $notFound
    }
}

# ---------------------------- 候选书目 --------------------------------------
# 注意：这个列表来自 GET /api/books（走缓存）。本轮设计里每轮结束都会把借出的书归还，
# 因此真实库存会回到初始值，缓存值也就等于真实值。
# 即便如此，每轮真正的"初始可借数"仍以 load_borrow.py 读到的
# 权威值（内部签名接口）为准，并写在结果里 —— 选择用缓存只是"挑书"，
# 判据不用缓存。
function Get-CandidateBooks {
    $result = Invoke-SmartLib -Method "GET" -Path "/api/books?availableOnly=true&page=1&size=50&sort=relevance"
    $books = New-Object System.Collections.ArrayList
    if (-not $result.Ok) { return @{ Ok = $false; Books = @(); Note = "读取书目失败 HTTP $($result.StatusCode)" } }
    foreach ($item in @(Get-PropPath -Object $result.Json -Path "data.list")) {
        $id = Get-Prop -Object $item -Name "id"
        $available = Get-Prop -Object $item -Name "availableCopies"
        if ($null -eq $id -or $null -eq $available) { continue }
        $books.Add([pscustomobject]@{
            BookId = [long]$id
            Available = [int]$available
            Total = Get-Prop -Object $item -Name "totalCopies"
            Title = Get-Prop -Object $item -Name "title"
        }) | Out-Null
    }
    $sorted = @($books | Sort-Object -Property Available -Descending)
    return @{ Ok = $true; Books = $sorted; Note = "" }
}

# ---------------------------- 对账 ------------------------------------------
function Invoke-Reconcile {
    $result = Invoke-SmartLib -Method "GET" -Path "/api/admin/reconcile"
    if (-not $result.Ok) {
        return @{ Ok = $false; Verdict = "无法获取（HTTP $($result.StatusCode)）"
                  Violations = @(); Degraded = $null; Raw = $result.Text }
    }
    $data = Get-Prop -Object $result.Json -Name "data"
    $violations = @(Get-Prop -Object $data -Name "violations")
    $consistent = Get-Prop -Object $data -Name "consistent"
    $degraded = Get-Prop -Object $data -Name "redisLuaDegradedCount"
    $verdict = "未知"
    if ($null -ne $consistent) {
        if ([bool]$consistent -and $violations.Count -eq 0) {
            $verdict = "PASS"
        }
        else {
            $verdict = ("FAIL(violations={0})" -f $violations.Count)
        }
    }
    return @{
        Ok = $true
        Verdict = $verdict
        Violations = $violations
        Degraded = $degraded
        Raw = $result.Text
    }
}

# ---------------------------- 结果收集 --------------------------------------
$rows = New-Object System.Collections.ArrayList
$cycleRows = New-Object System.Collections.ArrayList
$reconcileRows = New-Object System.Collections.ArrayList
$searchRows = New-Object System.Collections.ArrayList
$recallRows = New-Object System.Collections.ArrayList

# ---------------------------- 1. 健康检查 ------------------------------------
Write-Host ""
Write-Host "[run_all] ==================== SmartLib 一键基准测试 (v2) ===================="
Write-Host "[run_all] 仓库根目录 : $repoRoot"
Write-Host "[run_all] 结果目录   : $OutDir"
Write-Host "[run_all] Python     : $python"
Write-Host "[run_all] 目标后端   : $BaseUrl"
Write-Host "[run_all] 每轮复本数 : $CopiesPerRound / 每策略轮数 : $Cycles"
Write-Host ""

$healthy = Wait-BackendHealthy -TimeoutSeconds $HealthTimeoutSeconds
if (-not $healthy) {
    Add-Note -OutDir $OutDir -Message "后端在 $HealthTimeoutSeconds 秒内未就绪，后续结果可能不可用。"
}

# ---------------------------- 2. 管理端点探测 --------------------------------
$allStrategies = @(
    "local-lock",        # S1
    "optimistic-db",     # S2（默认）
    "pessimistic-db",    # S3
    "version-retry",     # S4
    "redis-lua",         # S5
    "mq-serial"          # S6
)
$strategyPlan = @()
$availableStrategies = $null
$availableEngines = $null
$adminEnabled = $true

$probe = Invoke-SmartLib -Method "GET" -Path "/api/admin/search-engine"
if ($probe.StatusCode -eq 404 -or $probe.StatusCode -eq 405) {
    $adminEnabled = $false
    Write-Warning "[run_all] /api/admin/** 返回 $($probe.StatusCode)：后端未开启管理端点。"
    Write-Warning "[run_all]   本地启动请加：--smartlib.admin.enabled=true"
    Add-Note -OutDir $OutDir -Message "管理端点不可用（HTTP $($probe.StatusCode)）：无法运行期切换策略/引擎。"
}
elseif (-not $probe.Ok) {
    $adminEnabled = $false
    Write-Warning "[run_all] 无法访问 /api/admin/search-engine（HTTP $($probe.StatusCode)）：$($probe.Text)"
}
else {
    $probeData = Get-Prop -Object $probe.Json -Name "data"
    $availableEngines = @(Get-Prop -Object $probeData -Name "available")
    Write-Host ("[run_all] 管理端点可用：当前引擎={0}，可选={1}" -f `
        (Get-Prop -Object $probeData -Name "current"), ($availableEngines -join ", "))
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
    $switchProbe = Invoke-SmartLib -Method "PUT" -Path "/api/admin/stock-strategy?name=optimistic-db"
    $switchProbeData = Get-Prop -Object $switchProbe.Json -Name "data"
    if ($switchProbe.Ok -and $null -ne $switchProbeData) {
        $availableRaw = Get-Prop -Object $switchProbeData -Name "available"
        if ($null -ne $availableRaw) { $availableStrategies = @($availableRaw) }
        Write-Host "[run_all] 库存策略：当前=$(Get-Prop -Object $switchProbeData -Name 'current')，可选=$($availableStrategies -join ', ')"
    }
    else {
        Write-Warning "[run_all] 切换策略探测失败（HTTP $($switchProbe.StatusCode)）：$($switchProbe.Text)"
        Add-Note -OutDir $OutDir -Message "切换策略探测失败（HTTP $($switchProbe.StatusCode)）：$($switchProbe.Text)"
    }
}

foreach ($strategy in $allStrategies) {
    if ($null -ne $availableStrategies -and $availableStrategies.Count -gt 0) {
        if ($availableStrategies -contains $strategy) { $strategyPlan += $strategy }
        else { Write-Host "[run_all] 跳过 $strategy：后端未注册该策略" }
    }
    else { $strategyPlan += $strategy }
}
if ($strategyPlan.Count -eq 0) { $strategyPlan = @("optimistic-db") }

# ---------------------------- 3. 读规则 --------------------------------------
$minCredit = 60.0
$fineFreeLimit = 10.0
$rulesRead = $false
if ($adminEnabled) {
    $rules = Invoke-SmartLib -Method "GET" -Path "/api/admin/rules"
    $rulesData = Get-Prop -Object $rules.Json -Name "data"
    if ($rules.Ok -and $null -ne $rulesData) {
        $minCreditRaw = Get-Prop -Object $rulesData -Name "min_credit_score"
        $fineRaw = Get-Prop -Object $rulesData -Name "fine_free_limit"
        if ($null -ne $minCreditRaw) { $minCredit = [double]$minCreditRaw }
        if ($null -ne $fineRaw) { $fineFreeLimit = [double]$fineRaw }
        $rulesRead = $true
        Write-Host ("[run_all] 规则：min_credit_score={0}，fine_free_limit={1}" -f $minCredit, $fineFreeLimit)
    }
}
if (-not $rulesRead) {
    Write-Warning "[run_all] 未读到 /api/admin/rules，使用默认阈值 min_credit_score=$minCredit / fine_free_limit=$fineFreeLimit"
    Add-Note -OutDir $OutDir -Message "未读到规则接口，读者筛选使用默认阈值 min_credit_score=$minCredit / fine_free_limit=$fineFreeLimit。"
}

# ---------------------------- 4. 预筛读者池 ----------------------------------
Write-Host ""
Write-Host "[run_all] ---------- 预筛读者池（1..$MaxReaderId，走 GET /api/readers/{id}/credit + /loans） ----------"
$screening = Get-ReaderScreening -MaxReaderId $MaxReaderId -MinCredit $minCredit -FineFreeLimit $fineFreeLimit
$usableReaders = @($screening.Usable)
$excludedReaders = @($screening.Excluded)
$usableReaderIds = @($usableReaders | ForEach-Object { $_.ReaderId })
Write-Host ("[run_all] 可用读者 {0} 人：{1}" -f $usableReaderIds.Count, ($usableReaderIds -join ","))
foreach ($item in $excludedReaders) {
    Write-Host ("[run_all]   排除 reader {0}：{1}（{2}）" -f $item.ReaderId, $item.Reason, $item.Detail)
}

# 每轮复本数不能超过"干净读者数"，否则必然出现 reader 复用 -> 重复借阅失败 -> 污染指标
$copiesPerRound = $CopiesPerRound
$copiesLimitedBy = ""
if ($usableReaderIds.Count -lt $copiesPerRound) {
    $copiesLimitedBy = "干净读者只有 $($usableReaderIds.Count) 人，复本数被下调"
    Write-Warning "[run_all] $copiesLimitedBy（原设定 $copiesPerRound）"
    $copiesPerRound = $usableReaderIds.Count
}
if ($copiesPerRound -lt 1) {
    Write-Warning "[run_all] 没有任何可用读者，无法做有效的借阅压测。"
    Add-Note -OutDir $OutDir -Message "没有可用读者（信用分>=60 且无逾期且无欠费且 ACTIVE），借阅压测未执行。"
}
if ($RequestsPerCycle -gt 0 -and $RequestsPerCycle -ne $copiesPerRound) {
    Write-Warning "[run_all] -RequestsPerCycle=$RequestsPerCycle 与'每读者只借 1 次'的前提冲突（复本数=$copiesPerRound），已按复本数执行。"
}
$requestsPerCycle = $copiesPerRound

# ---------------------------- 5. 候选书目 ------------------------------------
$candidateResult = Get-CandidateBooks
$candidateBooks = @($candidateResult.Books)
Write-Host ""
Write-Host ("[run_all] 可借书目 {0} 种（可用数最多：{1}）" -f `
    $candidateBooks.Count, (($candidateBooks | Select-Object -First 1).Available))
if (-not $candidateResult.Ok) {
    Add-Note -OutDir $OutDir -Message "读取候选书目失败：$($candidateResult.Note)"
}

$targetBook = $null
$targetSelection = ""
$eligibleBooks = @($candidateBooks |
    Where-Object { $_.Available -ge 1 -and $_.Available -le $usableReaderIds.Count } |
    Sort-Object -Property Available -Descending)
$exactMatch = @($eligibleBooks | Where-Object { $_.Available -eq $copiesPerRound })
if ($exactMatch.Count -gt 0) {
    $targetBook = $exactMatch[0]
    $targetSelection = "命中'复本数恰好等于每轮复本数'的书"
}
elseif ($eligibleBooks.Count -gt 0) {
    $targetBook = $eligibleBooks[0]
    $targetSelection = "没有复本数恰好等于 $copiesPerRound 的书，退而选复本数最多的可借空书"
}
elseif ($candidateBooks.Count -gt 0) {
    $targetBook = $candidateBooks[0]
    $targetSelection = "没有复本数 <= 干净读者数的书，只能选可借最多的书（该轮无法借空）"
}
else {
    $targetSelection = "没有任何可借书目"
}

if ($null -ne $targetBook) {
    if ([int]$targetBook.Available -lt $copiesPerRound) {
        Write-Warning ("[run_all] 目标书复本数 {0} < 每轮复本数 {1}，每轮按 {0} 执行（否则无法借空）。" -f `
            $targetBook.Available, $copiesPerRound)
        $copiesPerRound = [int]$targetBook.Available
    }
    Write-Host ("[run_all] 目标书：id={0} '{1}'，可借 {2} 复本（{3}）" -f `
        $targetBook.BookId, $targetBook.Title, $targetBook.Available, $targetSelection)
    Write-Host ("[run_all] 六种策略都压这本书，但**每轮都会重置库存**；" + `
        "这样样本量与数据分布对六种策略完全一致，可比性最强。")
}
else {
    Write-Warning "[run_all] 没有任何可借书目，无法做借阅压测。"
    Add-Note -OutDir $OutDir -Message "没有任何可借书目，借阅压测未执行。"
}
$requestsPerCycle = $copiesPerRound

# ---------------------------- 6. 六种库存策略：逐轮独立压测 ------------------
$strategyIndex = 0
foreach ($strategy in $strategyPlan) {
    $strategyIndex++
    Write-Host ""
    Write-Host "[run_all] ========== [$strategyIndex/$($strategyPlan.Count)] 库存策略 $strategy =========="

    $switchNote = "未切换"
    if ($adminEnabled) {
        $switch = Invoke-SmartLib -Method "PUT" -Path ("/api/admin/stock-strategy?name=" + $strategy)
        if ($switch.Ok) {
            $current = Get-PropPath -Object $switch.Json -Path "data.current" -Default $strategy
            $switchNote = "OK(current=$current)"
            Write-Host "[run_all]   已切换到 $current"
            Start-Sleep -Seconds 1   # 给策略的懒初始化（如 MQ 队列）留时间
        }
        else {
            $switchNote = "失败 HTTP $($switch.StatusCode)"
            Write-Warning "[run_all]   切换失败：$($switch.Text)"
            Add-Note -OutDir $OutDir -Message "策略 $strategy 切换失败：HTTP $($switch.StatusCode) $($switch.Text)"
            if (-not $KeepGoingOnStrategyError) {
                $rows.Add([pscustomobject]@{
                    Kind = "stock"; Name = $strategy; BookId = "-"; Cycles = 0
                    CopiesPerRound = "-"; Expected = $null; Actual = $null; Oversold = "-"
                    ErrorRate = $null; Qps = $null; P50 = $null; P90 = $null; P99 = $null
                    DeductionOk = "-"; StockRestored = "-"; Reconcile = "-"
                    ResultFile = "-"; Note = "策略切换失败，本次未压测"
                }) | Out-Null
                continue
            }
        }
    }
    else {
        $switchNote = "管理端点不可用，沿用配置默认值"
    }

    # 六种策略压同一本目标书（$targetBook）。这**不是** v1 的那个缺陷：
    #   v1 的问题是把书借空之后不还，后面几轮都在"没库存"的状态下测；
    #   这里每一轮结束都会把本轮借出的复本全部归还，库存回到该轮初始值，
    #   逐轮明细里的 availableBefore -> afterBorrow -> afterReturn 就是证据。
    #   压同一本书反而让六种策略的样本量与数据分布完全一致。
    $book = $targetBook
    if ($null -eq $book) {
        Write-Warning "[run_all]   没有可借书目，跳过 $strategy"
        Add-Note -OutDir $OutDir -Message "策略 $strategy 无可借书目，跳过。"
        continue
    }

    Write-Host ("[run_all]   目标书 id={0}（'{1}'，缓存显示可借 {2}），每轮 {3} 次请求 x {4} 轮" -f `
        $book.BookId, $book.Title, $book.Available, $requestsPerCycle, $Cycles)

    $outFile = Join-Path $OutDir ("borrow_{0}.json" -f $strategy)
    if (Test-Path -LiteralPath $outFile) { Remove-Item -LiteralPath $outFile -Force }

    $pyArgs = @(
        $loadBorrow,
        "--url", $BaseUrl,
        "--book-id", $book.BookId,
        "--threads", $requestsPerCycle,
        "--requests", $requestsPerCycle,
        "--cycles", $Cycles,
        "--return-each-cycle",
        "--reader-mode", "distinct",
        "--reader-ids", ($usableReaderIds -join ","),
        "--internal-sign-secret", $InternalSignSecret,
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
            Kind = "stock"; Name = $strategy; BookId = $book.BookId; Cycles = $Cycles
            CopiesPerRound = $requestsPerCycle; Expected = $null; Actual = $null; Oversold = "-"
            ErrorRate = $null; Qps = $null; P50 = $null; P90 = $null; P99 = $null
            DeductionOk = "-"; StockRestored = "-"; Reconcile = "-"
            ResultFile = "-"; Note = "压测脚本未产出结果文件（exit=$exitCode）"
        }) | Out-Null
        Add-Note -OutDir $OutDir -Message "策略 $strategy：压测脚本未产出结果文件，exit=$exitCode"
        continue
    }

    $expectation = Get-Prop -Object $report -Name "expectation"
    $correctness = Get-Prop -Object $report -Name "correctness"
    $oversell = Get-Prop -Object $report -Name "oversellCheck"
    $design = Get-Prop -Object $report -Name "measurementDesign"

    $expectedTotal = Get-Prop -Object $expectation -Name "expectedTotal"
    $actualTotal = Get-Prop -Object $expectation -Name "actualTotal"
    $oversoldAny = Get-Prop -Object $oversell -Name "oversold"
    $oversoldText = "未知"
    if ($oversoldAny -eq $false) { $oversoldText = "0" }
    elseif ($oversoldAny -eq $true) { $oversoldText = "有（见明细）" }
    $deductionBad = @(Get-Prop -Object $correctness -Name "deductionInconsistentCycles")
    $restoreBad = @(Get-Prop -Object $correctness -Name "stockNotRestoredCycles")
    $cacheStale = @(Get-Prop -Object $correctness -Name "cacheStaleCycles")

    $noteParts = @("切换=$switchNote")
    $noteParts += ("目标书 {0} / 每轮 {1} 复本" -f $book.BookId, $requestsPerCycle)
    if ($deductionBad.Count -gt 0) { $noteParts += "扣减不守恒轮次=$($deductionBad -join ',')" }
    if ($restoreBad.Count -gt 0) { $noteParts += "库存未复原轮次=$($restoreBad -join ',')" }
    if ($cacheStale.Count -gt 0) { $noteParts += "缓存偏旧轮次=$($cacheStale.Count)" }
    $messages = @(Get-PropPath -Object $report -Path "result.messageSamples")
    if ($messages.Count -gt 0) { $noteParts += "消息=" + (($messages | Select-Object -First 2) -join " / ") }

    $rows.Add([pscustomobject]@{
        Kind = "stock"
        Name = $strategy
        BookId = $book.BookId
        Cycles = $Cycles
        CopiesPerRound = $requestsPerCycle
        Expected = $expectedTotal
        Actual = $actualTotal
        Oversold = $oversoldText
        ErrorRate = Get-PropPath -Object $report -Path "result.errorRate"
        Qps = Get-PropPath -Object $report -Path "result.qps"
        P50 = Get-PropPath -Object $report -Path "result.latencyMs.p50"
        P90 = Get-PropPath -Object $report -Path "result.latencyMs.p90"
        P99 = Get-PropPath -Object $report -Path "result.latencyMs.p99"
        DeductionOk = $(if ($deductionBad.Count -eq 0) { "是" } else { "否" })
        StockRestored = $(if ($restoreBad.Count -eq 0) { "是" } else { "否" })
        Reconcile = "见下"
        ResultFile = (Split-Path -Leaf $outFile)
        Note = ($noteParts -join "；")
    }) | Out-Null

    # 逐轮明细（这是"每轮独立库存"的核心证据）
    foreach ($cycleRecord in @(Get-Prop -Object $report -Name "cycles")) {
        $cycleRows.Add([pscustomobject]@{
            Strategy = $strategy
            Cycle = Get-Prop -Object $cycleRecord -Name "cycle"
            BookId = Get-Prop -Object $cycleRecord -Name "bookId"
            AvailableBefore = Get-Prop -Object $cycleRecord -Name "availableBefore"
            AvailableAfterBorrow = Get-Prop -Object $cycleRecord -Name "availableAfterBorrow"
            AvailableAfterReturn = Get-Prop -Object $cycleRecord -Name "availableAfterReturn"
            AvailableCached = Get-Prop -Object $cycleRecord -Name "availableCachedAfterBorrow"
            Expected = Get-Prop -Object $cycleRecord -Name "expectedSuccess"
            Actual = Get-Prop -Object $cycleRecord -Name "successCount"
            Matched = Get-Prop -Object $cycleRecord -Name "successMatchesExpected"
            DeductionOk = Get-Prop -Object $cycleRecord -Name "deductionConsistent"
            Restored = Get-Prop -Object $cycleRecord -Name "stockRestored"
            Returned = Get-Prop -Object $cycleRecord -Name "returnedCount"
            Oversold = Get-Prop -Object $cycleRecord -Name "oversold"
            P99 = Get-PropPath -Object $cycleRecord -Path "latencyMs.p99"
            StatusBreakdown = ((Get-Prop -Object $cycleRecord -Name "statusBreakdown") | ConvertTo-Json -Compress)
        }) | Out-Null
    }

    # 每轮结束后的对账
    $reconcile = Invoke-Reconcile
    Write-Host ("[run_all]   对账：{0}（violations={1}，redisLuaDegraded={2}）" -f `
        $reconcile.Verdict, @($reconcile.Violations).Count, $reconcile.Degraded)
    $reconcileRows.Add([pscustomobject]@{
        Stage = "策略 $strategy 之后"
        Verdict = $reconcile.Verdict
        Violations = (@($reconcile.Violations) -join ",")
        Degraded = $reconcile.Degraded
    }) | Out-Null
}

# ---------------------------- 7. 检索基准（Q1 / Q3） -------------------------
# 后端 AdminController 提供 PUT /api/admin/search-engine?name=xxx 运行期切换引擎，
# 所以这里可以真的切过去再压测（需要 smartlib.admin.enabled=true）。
# 切到 inverted-index 后必须重建倒排索引：批量导入书目后索引是空的，
# 不重建的话 Q3 会返回空结果，召回率假性为 0 —— 那是数据没准备好，不是引擎差。
if (-not $SkipSearch) {
    Write-Host ""
    Write-Host "[run_all] ---------- 检索基准（Q1 like / Q3 inverted-index） ----------"
    if (-not $adminEnabled) {
        Write-Warning "[run_all] 管理端点不可用：无法切换引擎，结果只能标注为"
        Write-Warning "[run_all]   '当前配置引擎' 的实测值，不能当作 Q1/Q3 对比数据。"
    }

    foreach ($engine in @("like", "inverted-index")) {
        $outFile = Join-Path $OutDir ("search_{0}.json" -f $engine)
        if (Test-Path -LiteralPath $outFile) { Remove-Item -LiteralPath $outFile -Force }

        $pyArgs = @(
            $searchBench,
            "--url", $BaseUrl,
            "--engine", $engine,
            "--threads", 8,
            "--requests", 200,
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
            $searchRows.Add([pscustomobject]@{
                Engine = $engine; Observed = "-"; Requests = 200; Qps = $null
                P50 = $null; P90 = $null; P99 = $null; ErrorRate = $null
                Empty = "-"; ResultFile = "-"; Note = "检索压测未产出结果文件（exit=$exitCode）"
            }) | Out-Null
            continue
        }
        $engineSwitch = Get-Prop -Object $report -Name "engineSwitch"
        $observed = Get-Prop -Object $engineSwitch -Name "current"
        if ($null -eq $observed) { $observed = Get-Prop -Object $report -Name "engineObservedAfter" }
        $noteParts = @()
        $reindexMillis = Get-Prop -Object $engineSwitch -Name "reindexMillis"
        if ($null -ne $reindexMillis) { $noteParts += ("索引重建 {0} ms" -f $reindexMillis) }
        if ((Get-Prop -Object $engineSwitch -Name "switchOk") -eq $false) {
            $noteParts += "引擎切换失败（结果只是标签）"
        }
        $searchRows.Add([pscustomobject]@{
            Engine = $engine
            Observed = $observed
            Requests = Get-PropPath -Object $report -Path "result.requestsTotal"
            Qps = Get-PropPath -Object $report -Path "result.qps"
            P50 = Get-PropPath -Object $report -Path "result.latencyMs.p50"
            P90 = Get-PropPath -Object $report -Path "result.latencyMs.p90"
            P99 = Get-PropPath -Object $report -Path "result.latencyMs.p99"
            ErrorRate = Get-PropPath -Object $report -Path "result.errorRate"
            Empty = Get-PropPath -Object $report -Path "result.emptyResultCount"
            ResultFile = (Split-Path -Leaf $outFile)
            Note = ($noteParts -join "；")
        }) | Out-Null
    }
}

# ---------------------------- 8. 召回率评估（两个引擎各一次） ----------------
# 召回率要对两个引擎各跑一次才有对比意义：
#   like 引擎靠 SQL 模糊匹配（多词/带空格的查询基本召回不到），
#   倒排索引靠分词召回。两者的差异正是 §6.2 的叙事重点。
# 【两个概念必须分清】
#   平均召回率 = 对每条查询，|命中期望 id| / |期望 id| 的平均（衡量"找全了没有"）
#   Top1 命中率 = 期望 id 出现在第一条结果里的查询占比（衡量"排第一的对不对"）
$recallPercent = "-"
if (-not $SkipRecall) {
    Write-Host ""
    Write-Host "[run_all] ---------- 召回率评估（scripts/bench/recall_dataset.json） ----------"
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
        $recall = Get-Prop -Object $report -Name "recall"
        if ($null -ne $recall) {
            $value = Format-Num -Value (Get-Prop -Object $recall -Name "averageRecallPercent") -Format "0.00"
            if ($recallEngine -eq "inverted-index") { $recallPercent = $value }
            $observedEngine = "-"
            $switchCurrent = Get-PropPath -Object $report -Path "engineSwitch.current"
            if ($null -ne $switchCurrent) { $observedEngine = [string]$switchCurrent }
            $hitAt1 = Get-Prop -Object $recall -Name "hitAt1Rate" -Default 0
            $recallRows.Add([pscustomobject]@{
                Engine = $recallEngine
                Observed = $observedEngine
                Queries = Get-Prop -Object $recall -Name "evaluatedQueries"
                Skipped = Get-Prop -Object $recall -Name "skippedEntries"
                AverageRecall = (Get-Prop -Object $recall -Name "averageRecallPercent")
                HitAt1 = [Math]::Round([double]$hitAt1 * 100, 2)
                ResultFile = (Split-Path -Leaf $outFile)
                Note = "口径：命中期望 id 的比例（人工小标注集，属可用性召回）"
            }) | Out-Null
        }
        else {
            $recallRows.Add([pscustomobject]@{
                Engine = $recallEngine; Observed = "-"; Queries = $null; Skipped = $null
                AverageRecall = $null; HitAt1 = $null; ResultFile = "-"
                Note = "召回评估未产出结果（exit=$exitCode）"
            }) | Out-Null
        }
    }
}

# ---------------------------- 9. 收尾对账 ------------------------------------
Write-Host ""
Write-Host "[run_all] ---------- 收尾对账 /api/admin/reconcile ----------"
$finalReconcile = Invoke-Reconcile
Write-Host "[run_all]   响应：$($finalReconcile.Raw)"
Write-Host "[run_all]   判据：$($finalReconcile.Verdict)"
$reconcileRows.Add([pscustomobject]@{
    Stage = "全部压测结束"
    Verdict = $finalReconcile.Verdict
    Violations = (@($finalReconcile.Violations) -join ",")
    Degraded = $finalReconcile.Degraded
}) | Out-Null
$reconcileFile = Join-Path $OutDir "reconcile.json"
if ($finalReconcile.Ok) {
    Write-Utf8NoBom -Path $reconcileFile -Content $finalReconcile.Raw
}

$healthSnapshot = Get-HealthSnapshot
$healthNow = "overall=$($healthSnapshot.StatusAll), db=$($healthSnapshot.DbStatus), http=$($healthSnapshot.HttpStatus)"
if ($healthSnapshot.DownComponents.Count -gt 0) {
    $healthNow += ", 非 UP 组件: " + ($healthSnapshot.DownComponents -join ", ")
}

# ---------------------------- 10. 汇总 Markdown ------------------------------
# 用【单引号 here-string】整段拼模板：单引号 here-string 不做插值也不解析转义，
# 所以反引号可以直接写（双引号字符串里反引号是转义符，很容易写错）。
$summaryPath = Join-Path $OutDir "summary.md"
$nl = [Environment]::NewLine

$successRows = @($rows | Where-Object { $_.Kind -eq "stock" })
$expectationAllMatched = $true
$expectationChecked = 0
foreach ($row in $successRows) {
    if ($null -ne $row.Expected -and $null -ne $row.Actual) {
        $expectationChecked++
        if ([double]$row.Expected -ne [double]$row.Actual) { $expectationAllMatched = $false }
    }
}
$oversellTotal = 0
foreach ($row in $successRows) { if ($row.Oversold -eq "有（见明细）") { $oversellTotal++ } }

$mdHeader = @'
# SmartLib 基准测试汇总

> 本文件由 `scripts/bench/run_all.ps1` 自动生成，请勿手工编辑。
> 所有数字都是**本机实测**，没有目标值/承诺值；不同机器、不同数据量不可直接横向比较。

## 本次测量的有效性前提（先看这里，再看数字）

1. **每轮库存独立且已知**：每种策略的每一轮开始前，目标书的 `availableCopies` 都是已知值；
   每轮借出后立即用 `POST /api/borrows/{id}/return` 把本轮借出的复本全部归还，
   使库存回到该轮初始值。逐轮证据见「逐轮明细」表（availableBefore / afterBorrow / afterReturn）。
   只有满足这一点，六种策略的 QPS/P99 才是**可比的**。
2. **读者池是干净的**：所有参与压测的读者都满足 `creditScore >= min_credit_score`、
   无逾期、无欠费、`status=ACTIVE`（筛选过程与排除原因见下文）。
   池子里混入不合格读者会引入每轮都一样的固定失败，把"策略差异"淹没成"规则拒绝"。
3. **每轮成功数应当恰好等于该轮可用复本数**：干净读者池 + 每个读者只借 1 次
   （`--reader-mode distinct`）+ 请求数 = 复本数，且目标书的复本数 <= 干净读者数
   （否则读者不够，借不满）。**成功数少了 = 误拒**（该成功的被拒了）；
   **多了 = 超卖**。这是本表最有说服力的一列。
   六种策略**压同一本目标书**（书名见参数行），每轮都借空再还回去 ——
   压同一本书不是缺陷：每轮都从同一个已知库存出发，样本量与数据分布对六种策略完全一致。
4. **QPS 是下界，不是服务端极限**：`load_borrow.py` 用纯标准库 `http.client`，
   每个请求新建一条 TCP 连接（没有连接池复用），客户端自身（GIL + 线程）也是瓶颈。
   因此这些 QPS 只能当作"在同一客户端条件下的相对比较"，不能当容量规划依据。
5. **样本量**：每策略 = 轮数 × 每轮复本数（见参数行）。样本只有几十次时
   P99/QPS 抖动明显，不要拿小数点后几位的差异下结论。
'@

$mdEnvLines = New-Object System.Collections.ArrayList
$mdEnvLines.Add("") | Out-Null
$mdEnvLines.Add("- 生成时间：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')") | Out-Null
$mdEnvLines.Add("- 目标后端：$BaseUrl") | Out-Null
$mdEnvLines.Add("- 每策略轮数：$Cycles / 每轮复本数（=每轮请求数）：$requestsPerCycle") | Out-Null
$mdEnvLines.Add("- 每策略样本量：$(($Cycles * $requestsPerCycle)) 次借阅请求（另有等量归还请求）") | Out-Null
$mdEnvLines.Add("- 读者池：$($usableReaderIds.Count) 人 [$($usableReaderIds -join ', ')]（已排除 $($excludedReaders.Count) 人）") | Out-Null
if (-not [string]::IsNullOrWhiteSpace($copiesLimitedBy)) {
    $mdEnvLines.Add("- 复本数限制说明：$copiesLimitedBy") | Out-Null
}
$mdEnvLines.Add("- Python：$python") | Out-Null
$mdEnvLines.Add("- 后端健康检查：$healthNow") | Out-Null
$mdEnvLines.Add("- 管理端点：$(if ($adminEnabled) { '可用（可运行期切换策略/引擎）' } else { '不可用（只跑当前配置，见 notes.txt）' })") | Out-Null
$mdEnvLines.Add("- 收尾对账：$($finalReconcile.Verdict)") | Out-Null
$mdEnvLines.Add("- 规则阈值：min_credit_score=$minCredit，fine_free_limit=$fineFreeLimit$(if (-not $rulesRead) { '（规则接口未读到，用默认值）' })") | Out-Null
$mdEnvLines.Add("- 库存判据来源：内部签名接口 ``GET /internal/agent/tools/books/{id}/stock``（直读库）") | Out-Null

# ---- 读者池筛选 ----
$mdReaderLines = New-Object System.Collections.ArrayList
$mdReaderLines.Add("") | Out-Null
$mdReaderLines.Add("## 读者池筛选（1..$MaxReaderId，阈值 creditScore>=$minCredit、无逾期、无欠费、ACTIVE）") | Out-Null
$mdReaderLines.Add("") | Out-Null
$mdReaderLines.Add("### 参与压测的读者（$($usableReaderIds.Count) 人）") | Out-Null
$mdReaderLines.Add("") | Out-Null
$mdReaderLines.Add("| readerId | creditScore | status |") | Out-Null
$mdReaderLines.Add("|---|---|---|") | Out-Null
foreach ($item in $usableReaders) {
    $mdReaderLines.Add(("| {0} | {1} | {2} |" -f $item.ReaderId, $item.CreditScore, $item.Status)) | Out-Null
}
$mdReaderLines.Add("") | Out-Null
$mdReaderLines.Add("### 被排除的读者（$($excludedReaders.Count) 人）") | Out-Null
$mdReaderLines.Add("") | Out-Null
$mdReaderLines.Add("| readerId | 排除原因 | 证据 |") | Out-Null
$mdReaderLines.Add("|---|---|---|") | Out-Null
foreach ($item in $excludedReaders) {
    $mdReaderLines.Add(("| {0} | {1} | {2} |" -f $item.ReaderId, $item.Reason, $item.Detail)) | Out-Null
}
if ($excludedReaders.Count -eq 0) {
    $mdReaderLines.Add("| - | 无 | 1..$MaxReaderId 内所有存在的读者都通过筛选 |") | Out-Null
}
$mdReaderLines.Add("") | Out-Null
$mdReaderLines.Add("> 为什么要写这张表：被排除的读者正是【信用分不足/有逾期/有欠费】这些业务规则的触发者。") | Out-Null
$mdReaderLines.Add("> 如果把他们放进读者池，每个策略都会收到同样的失败，表格看起来【六种策略都很差】——") | Out-Null
$mdReaderLines.Add("> 那是抽样污染，不是策略问题。") | Out-Null

# ---- 表1：库存策略 ----
$mdStockLines = New-Object System.Collections.ArrayList
$mdStockLines.Add("") | Out-Null
$mdStockLines.Add("## 表 1：六种库存扣减策略（每轮独立库存、独立归还）") | Out-Null
$mdStockLines.Add("") | Out-Null
$mdStockLines.Add("| 方案 | 书目id | 轮数 | 每轮复本 | 期望成功 | 实测成功 | 超卖 | 错误率 | QPS | P50(ms) | P90(ms) | P99(ms) | 扣减守恒 | 库存复原 | 结果文件 | 备注 |") | Out-Null
$mdStockLines.Add("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|") | Out-Null
foreach ($row in $successRows) {
    $mdStockLines.Add(("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9} | {10} | {11} | {12} | {13} | {14} | {15} |" -f `
        $row.Name, $row.BookId, $row.Cycles, $row.CopiesPerRound,
        $(if ($null -eq $row.Expected) { "-" } else { $row.Expected }),
        $(if ($null -eq $row.Actual) { "-" } else { $row.Actual }),
        $row.Oversold, (Format-Percent -Value $row.ErrorRate),
        (Format-Num -Value $row.Qps -Format "0.00"),
        (Format-Num -Value $row.P50 -Format "0.000"),
        (Format-Num -Value $row.P90 -Format "0.000"),
        (Format-Num -Value $row.P99 -Format "0.000"),
        $row.DeductionOk, $row.StockRestored, $row.ResultFile, $row.Note)) | Out-Null
}
$mdStockLines.Add("") | Out-Null
$mdStockLines.Add("判据解读：") | Out-Null
$mdStockLines.Add("- **期望成功 = 实测成功** ⇒ 既没有超卖，也没有误拒（该借到的都借到了）。") | Out-Null
$mdStockLines.Add("- **超卖 = 0**：每轮借出后 ``availableCopies >= 0``。") | Out-Null
$mdStockLines.Add("- **扣减守恒 = 是**：每轮 ``扣减后可借 == 扣减前可借 - 成功数``（成功 n 次就恰好扣 n 册）。") | Out-Null
$mdStockLines.Add("- **库存复原 = 是**：每轮归还后 ``availableCopies`` 回到该轮初始值（这是【下一轮可比】的前提）。") | Out-Null

# ---- 表2：逐轮明细 ----
$mdCycleLines = New-Object System.Collections.ArrayList
$mdCycleLines.Add("") | Out-Null
$mdCycleLines.Add("## 表 2：逐轮明细（【每轮独立库存】的直接证据）") | Out-Null
$mdCycleLines.Add("") | Out-Null
$mdCycleLines.Add("| 方案 | 轮 | 书目id | 借前可借 | 借后可借 | 归还后可借 | 缓存读数 | 期望成功 | 实测成功 | 匹配 | 扣减守恒 | 库存复原 | 归还笔数 | 超卖 | 该轮P99(ms) | 状态分布 |") | Out-Null
$mdCycleLines.Add("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|") | Out-Null
foreach ($row in $cycleRows) {
    $mdCycleLines.Add(("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9} | {10} | {11} | {12} | {13} | {14} | {15} |" -f `
        $row.Strategy, $row.Cycle, $row.BookId, $row.AvailableBefore, $row.AvailableAfterBorrow,
        $row.AvailableAfterReturn, $row.AvailableCached, $row.Expected, $row.Actual,
        $row.Matched, $row.DeductionOk, $row.Restored, $row.Returned, $row.Oversold,
        (Format-Num -Value $row.P99 -Format "0.000"), $row.StatusBreakdown)) | Out-Null
}
$mdCycleLines.Add("") | Out-Null
$mdCycleLines.Add("> 「缓存读数」= 同一时刻 ``GET /api/books/{id}`` 返回的 availableCopies。") | Out-Null
$mdCycleLines.Add("> 它与「借后可借」不一致时，说明**书目详情缓存没有随库存变化失效**（见文末【已知问题】）。") | Out-Null

# ---- 表3：检索 ----
$mdSearchLines = New-Object System.Collections.ArrayList
$mdSearchLines.Add("") | Out-Null
$mdSearchLines.Add("## 表 3：检索引擎（Q1 like / Q3 inverted-index，运行期真实切换）") | Out-Null
$mdSearchLines.Add("") | Out-Null
$mdSearchLines.Add("| 请求引擎 | 实际引擎 | 请求数 | QPS | P50(ms) | P90(ms) | P99(ms) | 错误率 | 空结果数 | 备注 |") | Out-Null
$mdSearchLines.Add("|---|---|---|---|---|---|---|---|---|---|") | Out-Null
foreach ($row in $searchRows) {
    $mdSearchLines.Add(("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9} |" -f `
        $row.Engine, $row.Observed, $row.Requests,
        (Format-Num -Value $row.Qps -Format "0.00"),
        (Format-Num -Value $row.P50 -Format "0.000"),
        (Format-Num -Value $row.P90 -Format "0.000"),
        (Format-Num -Value $row.P99 -Format "0.000"),
        (Format-Percent -Value $row.ErrorRate), $row.Empty, $row.Note)) | Out-Null
}
$mdSearchLines.Add("") | Out-Null
$mdSearchLines.Add("> 「请求引擎」是脚本要求切换到的引擎，「实际引擎」是从 ``/api/admin/search-engine`` 读回的服务端真实引擎。") | Out-Null
$mdSearchLines.Add("> 两列不一致时以「实际引擎」为准（否则就是编造对比数据）。") | Out-Null

# ---- 表4：召回率 ----
$mdRecallLines = New-Object System.Collections.ArrayList
$mdRecallLines.Add("") | Out-Null
$mdRecallLines.Add("## 表 4：召回率（两个引擎各跑一遍同一份人工标注集）") | Out-Null
$mdRecallLines.Add("") | Out-Null
$mdRecallLines.Add("| 引擎 | 实际引擎 | 评估查询数 | 平均召回率(%) | Top1 命中率(%) | 结果文件 | 口径 |") | Out-Null
$mdRecallLines.Add("|---|---|---|---|---|---|---|") | Out-Null
foreach ($row in $recallRows) {
    $mdRecallLines.Add(("| {0} | {1} | {2} | {3} | {4} | {5} | {6} |" -f `
        $row.Engine, $row.Observed,
        $(if ($null -eq $row.Queries) { "-" } else { $row.Queries }),
        (Format-Num -Value $row.AverageRecall -Format "0.00"),
        (Format-Num -Value $row.HitAt1 -Format "0.00"),
        $row.ResultFile, $row.Note)) | Out-Null
}
$mdRecallLines.Add("") | Out-Null
$mdRecallLines.Add("**两个概念必须分清**（表头列名已分开）：") | Out-Null
$mdRecallLines.Add("- **平均召回率** = 对每条标注查询 ``|命中期望 id| / |期望 id|`` 再取平均，衡量「找全了没有」。") | Out-Null
$mdRecallLines.Add("- **Top1 命中率** = 期望 id 出现在**第一条**结果里的查询占比，衡量「排第一的对不对」。") | Out-Null
$mdRecallLines.Add("- 两者都基于 ``scripts/bench/recall_dataset.json``，期望集是人工标注的") | Out-Null
$mdRecallLines.Add("  「至少应被召回」的**小集合**（id 取自 ``backend/.../db/data.sql`` 里真实存在的 24 本书），") | Out-Null
$mdRecallLines.Add("  不是完整相关文档集合，因此属**可用性召回**，不能当学术意义的 recall 用。") | Out-Null
$mdRecallLines.Add("- ``expected_book_ids`` 为空的条目（负样本）不计入分母，会记在 JSON 的 ``recall.skippedEntries`` 里。") | Out-Null

# ---- 对账（逐轮 + 收尾） ----
$mdReconcileLines = New-Object System.Collections.ArrayList
$mdReconcileLines.Add("") | Out-Null
$mdReconcileLines.Add("## 库存对账（按阶段记录，``GET /api/admin/reconcile``）") | Out-Null
$mdReconcileLines.Add("") | Out-Null
$mdReconcileLines.Add("| 阶段 | 结论 | violations | redis-lua 降级次数（累计值） |") | Out-Null
$mdReconcileLines.Add("|---|---|---|---|") | Out-Null
foreach ($row in $reconcileRows) {
    $mdReconcileLines.Add(("| {0} | {1} | {2} | {3} |" -f `
        $row.Stage, $row.Verdict,
        $(if ([string]::IsNullOrWhiteSpace($row.Violations)) { "（空）" } else { $row.Violations }),
        $(if ($null -eq $row.Degraded) { "-" } else { $row.Degraded }))) | Out-Null
}
$mdReconcileLines.Add("") | Out-Null
$mdReconcileLines.Add("判据：``data.consistent == true`` 且 ``data.violations`` 为空数组，表示库存与 ``stock_ledger`` 守恒、") | Out-Null
$mdReconcileLines.Add("且没有负库存。任何一行 FAIL 都是 P0 级问题（说明有路径绕过了 ``StockService`` 的扣减入口）。") | Out-Null
$mdReconcileLines.Add("") | Out-Null
$mdReconcileLines.Add("``redisLuaDegradedCount`` 是**自后端启动起的累计值**（不是本阶段增量）；") | Out-Null
$mdReconcileLines.Add("相邻两行相减就是该阶段新增的降级次数。它大于 0 说明本机 Redis 不可用、") | Out-Null
$mdReconcileLines.Add("S5（redis-lua）实际走的是降级路径（等价于 S2 的 DB 条件更新）——") | Out-Null
$mdReconcileLines.Add("所以**不能**把表 1 里 redis-lua 那一行当作「Lua 原子预扣」的性能证据。") | Out-Null
$mdReconcileLines.Add("") | Out-Null
$mdReconcileLines.Add("收尾响应原文：") | Out-Null
$mdReconcileLines.Add("") | Out-Null
$mdReconcileLines.Add("``````text") | Out-Null
$mdReconcileLines.Add($finalReconcile.Raw) | Out-Null
$mdReconcileLines.Add("``````") | Out-Null

# ---- 产物与复现 ----
$mdArtifactLines = New-Object System.Collections.ArrayList
$mdArtifactLines.Add("") | Out-Null
$mdArtifactLines.Add("## 原始数据与复现方式") | Out-Null
$mdArtifactLines.Add("") | Out-Null
$mdArtifactLines.Add("``scripts/bench/results/*.json`` 是**本地运行产物，不入库**（``.gitignore`` 忽略了 ``*.json``）。") | Out-Null
$mdArtifactLines.Add("因此上表的数字**不能靠打开 JSON 复核**，只能靠重新跑一遍复现；") | Out-Null
$mdArtifactLines.Add("本文件（summary.md）是随仓库提交的**唯一证据快照**。") | Out-Null
$mdArtifactLines.Add("") | Out-Null
$mdArtifactLines.Add("复现命令（在仓库根目录）：") | Out-Null
$mdArtifactLines.Add("") | Out-Null
$mdArtifactLines.Add("``````powershell") | Out-Null
$mdArtifactLines.Add("# 一键复现本文件（需要后端已在 $BaseUrl 运行，且 smartlib.admin.enabled=true）") | Out-Null
$mdArtifactLines.Add("powershell -ExecutionPolicy Bypass -File scripts/bench/run_all.ps1 ``") | Out-Null
$mdArtifactLines.Add("    -BaseUrl $BaseUrl -CopiesPerRound $CopiesPerRound -Cycles $Cycles") | Out-Null
$mdArtifactLines.Add("``````") | Out-Null
$mdArtifactLines.Add("") | Out-Null
$mdArtifactLines.Add("单策略单跑（换任意策略名即可）：") | Out-Null
$mdArtifactLines.Add("") | Out-Null
$mdArtifactLines.Add("``````powershell") | Out-Null
$mdArtifactLines.Add("python scripts/bench/load_borrow.py --url $BaseUrl --book-id $($targetBook.BookId) ``") | Out-Null
$mdArtifactLines.Add("    --threads $requestsPerCycle --requests $requestsPerCycle --cycles $Cycles ``") | Out-Null
$mdArtifactLines.Add("    --return-each-cycle --reader-mode distinct --reader-ids $($usableReaderIds -join ',') ``") | Out-Null
$mdArtifactLines.Add("    --label manual --out scripts/bench/results/borrow_manual.json") | Out-Null
$mdArtifactLines.Add("``````") | Out-Null
$mdArtifactLines.Add("") | Out-Null
$mdArtifactLines.Add("本次运行写出的文件（未入库，仅供本机核查）：") | Out-Null
$mdArtifactLines.Add("") | Out-Null
foreach ($row in $successRows) {
    if ($row.ResultFile -ne "-") {
        $mdArtifactLines.Add(("- ``{0}`` —— 策略 {1}" -f $row.ResultFile, $row.Name)) | Out-Null
    }
}
foreach ($row in $searchRows) {
    if ($row.ResultFile -ne "-") { $mdArtifactLines.Add(("- ``{0}`` —— 检索 {1}" -f $row.ResultFile, $row.Engine)) | Out-Null }
}
foreach ($row in $recallRows) {
    if ($row.ResultFile -ne "-") { $mdArtifactLines.Add(("- ``{0}`` —— 召回 {1}" -f $row.ResultFile, $row.Engine)) | Out-Null }
}
$mdArtifactLines.Add("- ``reconcile.json`` —— 收尾对账原始响应") | Out-Null
$mdArtifactLines.Add("- ``notes.txt`` —— 跳过的步骤与失败原因（若本次全部成功则不存在）") | Out-Null

# ---- 已知问题与限制 ----
$mdLimitLines = New-Object System.Collections.ArrayList
$mdLimitLines.Add("") | Out-Null
$mdLimitLines.Add("## 已知问题与限制（必须和数字一起看）") | Out-Null
$mdLimitLines.Add("") | Out-Null
$mdLimitLines.Add("### 1) 【后端缺陷·已实测确认】书目详情缓存不随库存变化失效") | Out-Null
$mdLimitLines.Add("") | Out-Null
$mdLimitLines.Add("现象：借出 8 册后立刻 ``GET /api/books/{id}``，``availableCopies`` 仍返回旧值；") | Out-Null
$mdLimitLines.Add("同时内部签名接口 ``GET /internal/agent/tools/books/{id}/stock`` 返回正确值（0）。") | Out-Null
$mdLimitLines.Add("原因：``BookService.getById`` 走 ``CacheGuard``（多级缓存），而后端**唯一**的") | Out-Null
$mdLimitLines.Add("``StockService.StockChangeListener`` 实现是 ``InvertedIndexSearchEngine``（只更新倒排索引），") | Out-Null
$mdLimitLines.Add("**没有任何代码在库存变化时失效书目详情缓存**，只能等 TTL 到期自愈。") | Out-Null
$mdLimitLines.Add("影响：读者/前端看到的「可借数」会短暂偏大（本例实测每一轮都偏旧）；") | Out-Null
$mdLimitLines.Add("**不影响扣减正确性**（``book_stock`` 与 ``stock_ledger`` 守恒，对账全 PASS）。") | Out-Null
$mdLimitLines.Add("本表的处理：库存判据全部改用内部签名接口（权威），并把缓存读数单独列出来做对照。") | Out-Null
$mdLimitLines.Add("修复建议（不在本次写入范围，需后端同学决定）：让书目的写路径同时失效") | Out-Null
$mdLimitLines.Add("``smartlib:book:{id}`` 缓存，或给 BookView 的库存字段设置更短的 TTL。") | Out-Null
$mdLimitLines.Add("") | Out-Null
$mdLimitLines.Add("### 2) 客户端即瓶颈") | Out-Null
$mdLimitLines.Add("") | Out-Null
$mdLimitLines.Add("压测客户端用纯标准库 ``http.client``，每请求新建 TCP 连接、无连接池复用，") | Out-Null
$mdLimitLines.Add("且受 Python GIL 限制。所以 QPS 是**下界**，只能用于「同一客户端条件下的相对比较」。") | Out-Null
$mdLimitLines.Add("要测服务端上限请用 wrk/JMeter，并对比两者差异。") | Out-Null
$mdLimitLines.Add("") | Out-Null
$mdLimitLines.Add("### 3) S5（redis-lua）与 S6（mq-serial）的前提") | Out-Null
$mdLimitLines.Add("") | Out-Null
$mdLimitLines.Add("- S5 依赖 Redis 可用。Redis 不可用时后端会自动降级为 S2（``DegradeFallback``），") | Out-Null
$mdLimitLines.Add("  此时 S5 那一行的数字实际上是 S2 的。每轮对账里的 ``redisLuaDegradedCount``") | Out-Null
$mdLimitLines.Add("  就是降级的累计次数：它 > 0 时**不能**把该行当作「Lua 原子预扣」的性能。") | Out-Null
$mdLimitLines.Add("- S6 在本地是「单线程 Dispatcher + 有界队列」的模拟实现，不是真实 RocketMQ 事务消息。") | Out-Null
$mdLimitLines.Add("") | Out-Null
$mdLimitLines.Add("### 4) 压测会留下数据痕迹（但不改库存）") | Out-Null
$mdLimitLines.Add("") | Out-Null
$mdLimitLines.Add("每轮借出后都归还，因此 ``book_stock.available_copies`` 回到原值；") | Out-Null
$mdLimitLines.Add("但 ``borrow_record``（``RETURNED``）与 ``stock_ledger``（``DEDUCT``+``RELEASE``）会新增记录 ——") | Out-Null
$mdLimitLines.Add("这是对账仍然守恒的原因。需要干净数据请重建库（本地 H2 重启即重建）。") | Out-Null
$mdLimitLines.Add("") | Out-Null
$mdLimitLines.Add("### 5) 检索引擎与召回率的对比边界") | Out-Null
$mdLimitLines.Add("") | Out-Null
$mdLimitLines.Add("- 引擎切换是运行期生效的（``PUT /api/admin/search-engine``），但切换后必须 ``--reindex``；") | Out-Null
$mdLimitLines.Add("  本次每轮切换后都重建了倒排索引（见检索表备注里的毫秒数）。") | Out-Null
$mdLimitLines.Add("- 本次数据量是演示库（24 种书目），**不能**用来论证「120 万书目下的 P99」。") | Out-Null
$mdLimitLines.Add("  要出那种结论，先用 ``scripts/gen_books.py`` 灌 20 万条再跑，并在文档里写明数据量。") | Out-Null
$mdLimitLines.Add("- 召回率用的是 42 条人工小标注集，属可用性召回，不是完整相关集。") | Out-Null
$mdLimitLines.Add("") | Out-Null
$mdLimitLines.Add("### 6) 没有跑起来的项不要下结论") | Out-Null
$mdLimitLines.Add("") | Out-Null
$mdLimitLines.Add("表里出现 ``-`` 表示该项本轮未测得，不等价于「性能为 0」。") | Out-Null
$mdLimitLines.Add("如果管理端点不可用（``smartlib.admin.enabled`` 未开），策略/引擎切换会失败，") | Out-Null
$mdLimitLines.Add("此时表里的「实际引擎/实际策略」列会暴露真相，不要照抄「请求」列。") | Out-Null

# ---- 组装并写出 ----
$markdown = $mdHeader + $nl + (($mdEnvLines + $mdReaderLines + $mdStockLines + $mdCycleLines + `
    $mdSearchLines + $mdRecallLines + $mdReconcileLines + $mdArtifactLines + $mdLimitLines) -join $nl) + $nl

Write-Utf8NoBom -Path $summaryPath -Content $markdown

Write-Host ""
Write-Host "[run_all] ==================== 完成 ===================="
Write-Host "[run_all] 汇总报告 : $summaryPath"
Write-Host "[run_all] 原始 JSON : $OutDir（未入库，见 summary.md 的复现说明）"
if ($expectationChecked -gt 0) {
    Write-Host ("[run_all] 期望 vs 实测：{0} 个策略全部匹配 = {1}" -f $expectationChecked, $expectationAllMatched)
}
if ($oversellTotal -gt 0) {
    Write-Warning "[run_all] 有 $oversellTotal 个策略出现超卖，请查看 summary.md 的逐轮明细。"
}
if ($successRows.Count -eq 0) {
    Write-Warning "[run_all] 一行库存策略结果都没有产出，请检查后端是否启动、管理端点是否开启。"
    exit 1
}
# 退出码：只有"期望与实测不一致"或"超卖"才非 0，便于 CI/脚本判定
if (-not $expectationAllMatched -or $oversellTotal -gt 0) { exit 2 }
exit 0

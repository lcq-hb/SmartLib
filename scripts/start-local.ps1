# =============================================================================
# scripts/start-local.ps1 -- start SmartLib locally with ZERO external dependencies
#
# What it starts
#   1) backend : scripts/mvn.ps1 -f backend/pom.xml spring-boot:run
#                profile = local  -> H2 in-memory DB (MODE=MySQL), schema+seed loaded
#                                    automatically, NO MySQL / Redis needed
#                port    = 8080
#   2) agent   : python -m uvicorn app.main:app --port 8000  (LLM_PROVIDER=mock)
#                -> MockLLM, deterministic, runs with NO API key
#
# Why this file uses the Maven launcher script instead of mvn.cmd
#   The repo path contains Chinese characters. mvn.cmd is a batch file, so its
#   arguments go through the cmd.exe code page and -Dmaven.repo.local gets
#   dropped (Maven then falls back to %USERPROFILE%\.m2). scripts/mvn.ps1 calls the
#   classworlds launcher directly with Unicode-safe arguments.
#
# Execution policy note (IMPORTANT)
#   Running "& .\scripts\start-local.ps1" is blocked by the default execution
#   policy. Always use:
#       powershell -ExecutionPolicy Bypass -File scripts/start-local.ps1
#
# All comments and console output below are ASCII on purpose: PowerShell 5.1
# reads .ps1 files using the ANSI code page unless they have a UTF-8 BOM, and a
# mojibake file can break parsing. Keeping this script ASCII-only makes it
# portable. (run_all.ps1 has a UTF-8 BOM because it must print Chinese labels.)
# =============================================================================
[CmdletBinding()]
param(
    [switch]$SkipAgent,
    [switch]$SkipBackend,
    [int]$BackendPort = 8080,
    [int]$AgentPort = 8000,
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"

# ---------------------------- Paths (derived from $PSScriptRoot) -------------
$scriptsDir = $PSScriptRoot
$repoRoot = Split-Path -Parent $scriptsDir
$backendDir = Join-Path $repoRoot "backend"
$agentDir = Join-Path $repoRoot "agent-service"
$mvnLauncher = Join-Path $scriptsDir "mvn.ps1"
$backendPom = Join-Path $backendDir "pom.xml"
$agentVenvPython = Join-Path $agentDir ".venv\Scripts\python.exe"

Write-Host ""
Write-Host "=============================================================="
Write-Host " SmartLib local start (zero external dependencies)"
Write-Host "=============================================================="
Write-Host " repo root   : $repoRoot"
Write-Host " backend pom : $backendPom"
Write-Host " agent dir   : $agentDir"
Write-Host ""

if (-not $SkipBackend -and -not (Test-Path -LiteralPath $backendPom)) {
    throw "backend/pom.xml not found: $backendPom"
}
if (-not $SkipBackend -and -not (Test-Path -LiteralPath $mvnLauncher)) {
    throw "scripts/mvn.ps1 not found: $mvnLauncher"
}

# ---------------------------- Terminal Launch helpers ------------------------
# -Foreground runs the backend blocking in this window (Ctrl+C stops it);
# default starts each service in its own window so you can watch logs separately.
$started = New-Object System.Collections.ArrayList

# ---------------------------------------------------------------------------
# Maven launcher preflight
#
# scripts/mvn.ps1 contains Chinese comments but has NO UTF-8 BOM. PowerShell 5.1 (shipped with Windows)
# reads .ps1 using the ANSI code page when there is no BOM, so the Chinese comments
# become mojibake and swallow the string quotes -> ParserError ("string is missing the terminator").
# This is a PowerShell 5.1 encoding behaviour, not a Maven problem.
#
# Handling (without modifying someone else's script):
#   1) detect whether mvn.ps1 is "non-ASCII and BOM-less";
#   2) if we are on PS 5.1 and that is the case, print an actionable fix;
#   3) then continue with an equivalent direct classworlds launch (same logic as mvn.ps1:
#      bypass the cmd.exe code page problem of mvn.cmd by running the classworlds launcher with java).
#   PowerShell 7 reads UTF-8 without a BOM fine, so there mvn.ps1 is used as-is.
# ---------------------------------------------------------------------------
function Test-MvnLauncherUsable {
    param([string]$Path)
    $bytes = [System.IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $Path).Path)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191)
    # Do NOT use "foreach + break" here: "break" outside a loop is a PARSE-time
    # error in PowerShell ("Array index expression is missing or not valid") and
    # takes the whole script down. Use an explicit index loop instead.
    $hasNonAscii = $false
    $index = 0
    while ($index -lt $bytes.Length -and -not $hasNonAscii) {
        if ($bytes[$index] -gt 127) { $hasNonAscii = $true }
        $index = $index + 1
    }
    # PowerShell 6+ reads BOM-less UTF-8 correctly, so only 5.1 and older need help
    $isLegacy = ($PSVersionTable.PSVersion.Major -lt 6)
    if ($hasNonAscii -and -not $hasBom -and $isLegacy) {
        return $false
    }
    return $true
}

function Get-MavenInvocation {
    # Returns a hashtable: Mode = 'script' | 'direct'; Display is a human-readable command line.
    # It only decides HOW to launch Maven, it does not launch it (both foreground and
    #
    # background modes need the result, and this file must stay ASCII-only).
    # GOTCHA: never write a PowerShell BLOCK comment here (the one delimited by
    # less-than+hash and hash+greater-than). If those delimiters appear anywhere, the
    # tokenizer opens a real block comment and swallows the rest of the file, producing
    # totally unrelated errors such as "Missing closing '}'". The same applies to "@{"
    # inside comments: use "#" line comments whenever a comment mentions @{, @( or $().
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$MvnLauncher,
        [Parameter(Mandatory = $true)][string[]]$MavenArgs
    )
    if ((Test-Path -LiteralPath $MvnLauncher) -and (Test-MvnLauncherUsable -Path $MvnLauncher)) {
        return @{
            Mode = "script"
            FilePath = "powershell"
            ArgumentList = @("-ExecutionPolicy", "Bypass", "-File", $MvnLauncher) + $MavenArgs
            Display = "powershell -ExecutionPolicy Bypass -File `"$MvnLauncher`" " + ($MavenArgs -join " ")
        }
    }

    Write-Warning "[start-local] scripts/mvn.ps1 cannot be executed directly under this PowerShell."
    Write-Warning "[start-local]   Reason: the file has non-ASCII comments but no UTF-8 BOM, and"
    Write-Warning "[start-local]   PS 5.1 reads it as ANSI -> ParserError (missing terminator)."
    Write-Warning "[start-local]   Two equivalent fixes (neither changes any Maven logic):"
    Write-Warning "[start-local]     A) re-save scripts/mvn.ps1 as UTF-8 with BOM. One-liner:"
    Write-Warning "[start-local]        python -c (see deploy/README.md section 8, limit 12)"
    Write-Warning "[start-local]        in short: read the file as utf-8, write it back as utf-8-sig"
    Write-Warning "[start-local]     B) run this script with PowerShell 7 instead: pwsh -File scripts/start-local.ps1"
    Write-Warning "[start-local]   Falling back to the equivalent direct classworlds launch."

    $mvnHome = Join-Path $RepoRoot ".tools\apache-maven-3.9.9"
    $classworlds = Get-ChildItem -LiteralPath (Join-Path $mvnHome "boot") -Filter "plexus-classworlds-*.jar" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $java = (Get-Command java -ErrorAction SilentlyContinue).Source
    if ($null -eq $classworlds -or $null -eq $java) {
        throw "fallback unavailable: no classworlds jar under $mvnHome\boot or no java on PATH. Fix scripts/mvn.ps1 first (option A or B above)."
    }
    $repo = Join-Path $RepoRoot ".tools\m2repo"
    $common = @(
        "-Dmaven.home=$mvnHome",
        "-Dclassworlds.conf=$(Join-Path $mvnHome 'bin\m2.conf')",
        "-Dmaven.multiModuleProjectDirectory=$RepoRoot",
        "-Dmaven.repo.local=$repo",
        "-Dfile.encoding=UTF-8",
        "-classpath",
        $classworlds.FullName,
        "org.codehaus.plexus.classworlds.launcher.Launcher",
        "-B",
        "-ntp"
    ) + $MavenArgs
    return @{
        Mode = "direct"
        FilePath = $java
        ArgumentList = $common
        Display = "$java $($common -join ' ')"
    }
}

function Start-ServiceWindow {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [hashtable]$Environment = $null
    )
    Write-Host "[start-local] launching $Title ..."
    Write-Host "[start-local]   $FilePath $($ArgumentList -join ' ')"
    $process = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory -PassThru -WindowStyle Normal
    if ($null -ne $Environment) {
        # Environment is applied per-process only when we start via the same
        # shell, so instead we rely on Start-Process inheriting the current
        # process environment; variables set below are exported before launch.
    }
    Write-Host "[start-local]   pid = $($process.Id)"
    return $process
}

function Wait-HttpOk {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSeconds = 240,
        [string]$Name = "service",
        [string]$HealthJsonUrl = ""
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    Write-Host "[start-local] waiting for $Name at $Url (max $TimeoutSeconds s)"
    $attempt = 0
    while ((Get-Date) -lt $deadline) {
        $attempt++
        $status = 0
        $body = ""
        try {
            $request = [System.Net.WebRequest]::Create($Url)
            $request.Timeout = 3000
            $response = $request.GetResponse()
            $status = [int]$response.StatusCode
            $response.Close()
        }
        catch {
            # A 503 is a REAL answer, not "not ready yet". Spring Boot returns 503
            # when a health component is DOWN - e.g. redis on a machine without
            # Redis. The local profile is designed to run without Redis
            # (the stock strategies degrade to DB conditional update), so a 503
            # with db=UP must count as "backend is reachable and usable".
            $webResponse = $_.Exception.Response
            if ($null -ne $webResponse) {
                $status = [int]$webResponse.StatusCode
                try {
                    $stream = $webResponse.GetResponseStream()
                    $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
                    $body = $reader.ReadToEnd()
                    $reader.Close()
                }
                catch {
                    $body = ""
                }
            }
        }

        if ($status -ge 200 -and $status -lt 400) {
            Write-Host "[start-local] $Name is up (attempt $attempt, http $status)"
            return $true
        }
        if ($status -eq 503 -and -not [string]::IsNullOrWhiteSpace($body)) {
            # Parse the component-level health to decide: db UP => usable
            try {
                $json = ConvertFrom-Json -InputObject $body
                $dbStatus = "UNKNOWN"
                $down = @()
                if ($null -ne $json.components) {
                    foreach ($property in $json.components.PSObject.Properties) {
                        if ($property.Name -eq "db") { $dbStatus = [string]$property.Value.status }
                        if ([string]$property.Value.status -ne "UP") {
                            $down += ("{0}={1}" -f $property.Name, [string]$property.Value.status)
                        }
                    }
                }
                if ($dbStatus -eq "UP") {
                    Write-Host "[start-local] $Name is usable (attempt $attempt, http 503, db=UP)"
                    if ($down.Count -gt 0) {
                        Write-Warning "[start-local]   health components not UP: $($down -join ', ')"
                        Write-Warning "[start-local]   expected on a machine without Redis; stock strategies degrade to DB."
                    }
                    return $true
                }
                Write-Host "[start-local] attempt $attempt -> http 503, db=$dbStatus (not usable yet)"
            }
            catch {
                Write-Host "[start-local] attempt $attempt -> http 503 (body not JSON)"
            }
        }
        else {
            Write-Host "[start-local] attempt $attempt -> http $status / no response"
        }
        Start-Sleep -Seconds 3
    }
    Write-Warning "[start-local] $Name did not become ready within $TimeoutSeconds s"
    return $false
}

# ---------------------------- 1. Backend (H2 local profile) ------------------
$backendProcess = $null
if (-not $SkipBackend) {
    Write-Host ""
    Write-Host "[start-local] --- backend (Spring Boot 4.1.1, profile=local, H2 in-memory) ---"
    # spring-boot:run argument order matters: -Dspring-boot.run.* must come BEFORE
    # the plugin goal together with the usual Maven switches.
    # smartlib.admin.enabled=true is what makes the benchmark scripts able to
    # switch stock strategies at runtime (PUT /api/admin/stock-strategy).
    $mvnArgs = @(
        "-f", $backendPom,
        "spring-boot:run",
        "-Dspring-boot.run.profiles=local",
        "-Dspring-boot.run.arguments=--server.port=$BackendPort --smartlib.admin.enabled=true --smartlib.search.engine=inverted-index"
    )
    $invocation = Get-MavenInvocation -RepoRoot $repoRoot -MvnLauncher $mvnLauncher -MavenArgs $mvnArgs
    Write-Host "[start-local]   $($invocation.Display)"

    if ($Foreground) {
        # Foreground: run Maven blocking in this window (Ctrl+C stops the backend)
        if ($invocation.Mode -eq "script") {
            & powershell @($invocation.ArgumentList)
        }
        else {
            & $invocation.FilePath @($invocation.ArgumentList)
        }
        $exitCode = $LASTEXITCODE
        Write-Host "[start-local] backend process exited with code $exitCode"
        exit $exitCode
    }

    if ($invocation.Mode -eq "script") {
        # -NoExit keeps the window open after Maven exits, so you can read the stack trace
        $backendProcess = Start-ServiceWindow -Title "backend" -FilePath $invocation.FilePath `
            -ArgumentList (@("-NoExit") + $invocation.ArgumentList) -WorkingDirectory $repoRoot
    }
    else {
        # fallback: run the classworlds launcher with java directly (== mvn.ps1 internals)
        $backendProcess = Start-ServiceWindow -Title "backend" -FilePath $invocation.FilePath `
            -ArgumentList $invocation.ArgumentList -WorkingDirectory $repoRoot
    }
    $started.Add(@{ Name = "backend"; Pid = $backendProcess.Id }) | Out-Null
}
else {
    Write-Host "[start-local] -SkipBackend: not starting the backend"
}

# ---------------------------- 2. Agent (MockLLM) -----------------------------
$agentProcess = $null
if (-not $SkipAgent) {
    Write-Host ""
    Write-Host "[start-local] --- agent (FastAPI + uvicorn, LLM_PROVIDER=mock) ---"
    $agentEntry = Join-Path $agentDir "app\main.py"
    $requirements = Join-Path $agentDir "requirements.txt"

    if (-not (Test-Path -LiteralPath $agentEntry)) {
        Write-Warning "[start-local] agent-service/app/main.py not found - skipping agent."
        Write-Warning "[start-local]   (agent-service is provided by the project itself;"
        Write-Warning "[start-local]    once app/ and requirements.txt exist, re-run this script.)"
    }
    else {
        if (-not (Test-Path -LiteralPath $agentVenvPython)) {
            Write-Host "[start-local] creating venv at agent-service\.venv ..."
            $basePython = (Get-Command python -ErrorAction SilentlyContinue)
            if ($null -eq $basePython) {
                Write-Warning "[start-local] python not found on PATH - skipping agent."
            }
            else {
                & $basePython.Source -m venv (Join-Path $agentDir ".venv")
                if (Test-Path -LiteralPath $requirements) {
                    Write-Host "[start-local] installing agent dependencies ..."
                    & $agentVenvPython -m pip install --upgrade pip
                    & $agentVenvPython -m pip install -r $requirements
                }
            }
        }
        if (Test-Path -LiteralPath $agentVenvPython) {
            # Environment variables for the agent process. Values chosen so that
            # no API key is required: mock LLM + backend on localhost.
            $agentEnv = @{
                "LLM_PROVIDER" = "mock"
                "SMARTLIB_BACKEND_BASE_URL" = "http://127.0.0.1:$BackendPort"
            }
            foreach ($key in $agentEnv.Keys) {
                Set-Item -Path ("Env:" + $key) -Value $agentEnv[$key]
            }
            $uvicornArgs = @(
                "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1",
                "--port", "$AgentPort",
                "--reload"
            )
            $agentProcess = Start-ServiceWindow -Title "agent" -FilePath $agentVenvPython `
                -ArgumentList $uvicornArgs -WorkingDirectory $agentDir
            $started.Add(@{ Name = "agent"; Pid = $agentProcess.Id }) | Out-Null
        }
    }
}
else {
    Write-Host "[start-local] -SkipAgent: not starting the agent"
}

# ---------------------------- 3. Wait for health -----------------------------
Write-Host ""
if (-not $SkipBackend) {
    $null = Wait-HttpOk -Url ("http://127.0.0.1:{0}/actuator/health" -f $BackendPort) `
        -TimeoutSeconds 240 -Name "backend"
}
if (-not $SkipAgent -and $null -ne $agentProcess) {
    $null = Wait-HttpOk -Url ("http://127.0.0.1:{0}/health" -f $AgentPort) `
        -TimeoutSeconds 120 -Name "agent"
}

# ---------------------------- 4. Access info + verification commands ---------
$backendBase = "http://127.0.0.1:$BackendPort"
$agentBase = "http://127.0.0.1:$AgentPort"

Write-Host ""
Write-Host "=============================================================="
Write-Host " addresses"
Write-Host "=============================================================="
if (-not $SkipBackend) {
    Write-Host " backend health   : $backendBase/actuator/health"
    Write-Host " swagger ui       : $backendBase/swagger-ui.html"
    Write-Host " h2 console       : $backendBase/h2-console  (jdbc:h2:mem:smartlib, user sa, no password)"
    Write-Host " metrics          : $backendBase/actuator/prometheus"
    Write-Host " book search      : $backendBase/api/books?query=Java&page=1&size=20&sort=relevance"
}
if (-not $SkipAgent -and $null -ne $agentProcess) {
    Write-Host " agent health     : $agentBase/health"
    Write-Host " agent docs       : $agentBase/docs"
    Write-Host " agent chat(sync) : POST $agentBase/chat/sync"
}
Write-Host ""
Write-Host "=============================================================="
Write-Host " verification commands (copy & paste)"
Write-Host "=============================================================="
if (-not $SkipBackend) {
    Write-Host "# 1) health"
    Write-Host "Invoke-RestMethod $backendBase/actuator/health"
    Write-Host ""
    Write-Host "# 2) search (Chinese query included)"
    Write-Host "Invoke-RestMethod '$backendBase/api/books?query=Java&page=1&size=5&sort=relevance'"
    Write-Host ""
    Write-Host "# 3) borrow a book (Idempotency-Key is REQUIRED)"
    Write-Host "Invoke-RestMethod -Method Post -Uri $backendBase/api/borrows ``"
    Write-Host "  -Headers @{ 'Idempotency-Key' = [guid]::NewGuid().ToString() } ``"
    Write-Host "  -ContentType 'application/json' -Body '{`"readerId`":1,`"bookId`":1}'"
    Write-Host ""
    Write-Host "# 4) runtime stock-strategy switch (needs smartlib.admin.enabled=true)"
    Write-Host "Invoke-RestMethod -Method Put '$backendBase/api/admin/stock-strategy?name=optimistic-db'"
    Write-Host ""
    Write-Host "# 5) stock/ledger reconciliation (empty array == healthy)"
    Write-Host "Invoke-RestMethod $backendBase/api/admin/reconcile"
}
if (-not $SkipAgent -and $null -ne $agentProcess) {
    Write-Host ""
    Write-Host "# 6) agent chat (sync JSON, mock LLM)"
    Write-Host "Invoke-RestMethod -Method Post -Uri $agentBase/chat/sync ``"
    Write-Host "  -ContentType 'application/json' -Body '{`"message`":`"how many copies of Java book are available?`"}'"
}
Write-Host ""
Write-Host "=============================================================="
Write-Host " stop"
Write-Host "=============================================================="
foreach ($item in $started) {
    Write-Host ("  {0}: Stop-Process -Id {1}" -f $item.Name, $item.Pid)
}
Write-Host "  (each service also runs in its own window: just close it)"
Write-Host ""
Write-Host "[start-local] done."

# Shows model routing working, case by case, with PASS / FAIL for each.
#
#   .\harness\routing-demo.ps1                  run all cases (cases 4, 6 and 7 make two tiny real model calls)
#   .\harness\routing-demo.ps1 -SkipModelCalls  only the free cases: 1, 2, 3 and 5
#
# What "routing" means here: an agent asks the model router for a LOGICAL name ("chat-fast", "chat-default").
# The router checks the caller, checks the name is approved, applies guardrails and the cache, and sends the
# request to the real provider model behind that name. The cases below show each of those steps.
#
# Needs the platform stack in real mode:  .\harness\dev.ps1 up -Mode real
# The key below is the router's dev key for tests (compose.yaml, ROUTER_GATEWAY_KEYS); it is not a provider key.
param(
    [string]$Router = "http://localhost:8788",
    [string]$Key = "dev-key-platform-tests",
    [string]$Jaeger = "http://localhost:16686",
    [switch]$SkipModelCalls
)

# Windows PowerShell 5.1 spends over a second on proxy detection for every localhost call: skip it.
[System.Net.WebRequest]::DefaultWebProxy = $null

$script:pass = 0; $script:fail = 0; $script:skip = 0

function Send-Router([string]$Method, [string]$Path, $Body = $null, [hashtable]$Headers = @{}, [string]$AuthKey = $Key) {
    $h = @{}
    foreach ($name in $Headers.Keys) { $h[$name] = $Headers[$name] }
    $h["Authorization"] = "Bearer $AuthKey"
    $req = @{ Uri = "$Router/v1$Path"; Method = $Method; Headers = $h; UseBasicParsing = $true; TimeoutSec = 120 }
    if ($null -ne $Body) {
        $req.Body = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $Body -Depth 6 -Compress))
        $req.ContentType = "application/json"
    }
    $started = Get-Date
    try {
        $r = Invoke-WebRequest @req
        $status = [int]$r.StatusCode
        $text = [Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray())
        $headersOut = $r.Headers
    }
    catch {
        $resp = $_.Exception.Response
        if (-not $resp) { throw }
        $status = [int]$resp.StatusCode
        # Windows PowerShell 5.1 has already read the error body into ErrorDetails; the stream is empty by now.
        $text = [string]$_.ErrorDetails.Message
        if (-not $text) { $text = (New-Object IO.StreamReader($resp.GetResponseStream(), [Text.Encoding]::UTF8)).ReadToEnd() }
        $headersOut = $resp.Headers
    }
    $json = $null
    try { $json = $text | ConvertFrom-Json } catch { }
    [pscustomobject]@{ Status = $status; Json = $json; Text = $text; Headers = $headersOut; Seconds = ((Get-Date) - $started).TotalSeconds }
}

function Report([string]$Name, [bool]$Ok, [string]$Detail) {
    if ($Ok) { $script:pass++; Write-Host ("  PASS  " + $Name) -ForegroundColor Green } else { $script:fail++; Write-Host ("  FAIL  " + $Name) -ForegroundColor Red }
    if ($Detail) { Write-Host ("        " + $Detail) -ForegroundColor DarkGray }
}
function Skip([string]$Name, [string]$Why) { $script:skip++; Write-Host ("  SKIP  " + $Name) -ForegroundColor Yellow; Write-Host ("        " + $Why) -ForegroundColor DarkGray }
function Chat([string]$Model, [string]$Prompt) { @{ model = $Model; max_tokens = 8; messages = @(@{ role = "user"; content = $Prompt }) } }

# ---- preflight
$mode = "unknown"
try { $mode = (Invoke-RestMethod -Uri "$Router/health" -TimeoutSec 5).mode } catch {
    Write-Host "The model router is not reachable on $Router. Start the stack first:  .\harness\dev.ps1 up -Mode real" -ForegroundColor Red
    exit 1
}
Write-Host "Model router: $Router   mode: $mode" -ForegroundColor Cyan
$live = ($mode -eq "real") -and (-not $SkipModelCalls)
if ($mode -ne "real") { Write-Host "Not in real mode: the cases that need a real provider are skipped (restart with: .\harness\dev.ps1 up -Mode real)." -ForegroundColor Yellow }
Write-Host ""

# ---- 1. approved models
Write-Host "1. Which logical models are approved?"
$r = Send-Router GET "/models"
$ids = @(); if ($r.Json -and $r.Json.data) { $ids = @($r.Json.data | ForEach-Object { $_.id }) }
Report "the router lists its approved names" (($ids -contains "chat-default") -and ($ids -contains "chat-fast")) ("approved: " + ($ids -join ", "))

# ---- 2. an unapproved model
Write-Host "2. What if an agent asks for a model that is not approved?"
$r = Send-Router POST "/chat/completions" (Chat "gpt-4o" "hi")
Report "rejected before any provider is called" (($r.Status -eq 400) -and ($r.Json.error.code -eq "model_not_approved")) ("HTTP $($r.Status): " + $r.Json.error.message)

# ---- 3. a caller the router does not know
Write-Host "3. What if the caller's gateway key is wrong?"
$r = Send-Router POST "/chat/completions" (Chat "chat-fast" "hi") -AuthKey "not-a-real-key"
Report "unknown caller refused" ($r.Status -eq 401) "HTTP $($r.Status)"

# ---- 4. same prompt, two logical names
$traceId = [guid]::NewGuid().ToString("N")
$answered = @{}
Write-Host "4. Same prompt, two logical names: which real models answer?"
if ($live) {
    foreach ($name in @("chat-fast", "chat-default")) {
        $parent = [guid]::NewGuid().ToString("N").Substring(0, 16)
        $r = Send-Router POST "/chat/completions" (Chat $name "Reply with the single word: ok") @{ traceparent = "00-$traceId-$parent-01" }
        $answered[$name] = if ($r.Json -and $r.Json.model) { [string]$r.Json.model } else { "" }
        Write-Host ("        {0,-13} -> answered by {1}  (HTTP {2}, {3:N1}s)" -f $name, $answered[$name], $r.Status, $r.Seconds) -ForegroundColor DarkGray
    }
    Report "each logical name is served by a different real model" (($answered["chat-fast"] -ne "") -and ($answered["chat-default"] -ne "") -and ($answered["chat-fast"] -ne $answered["chat-default"])) ""
} else { Skip "two logical names" "needs real mode and model calls (tiny cost)" }

# ---- 5. gateway guardrail
Write-Host "5. Does the gateway guardrail stop a credential before it reaches a provider?"
if ($mode -eq "replay") {
    # In replay mode the router answers from recordings before it ever reaches the gateway, so no guardrail runs.
    Skip "gateway guardrail" "replay mode never reaches the gateway; use real or record mode"
} else {
    $r = Send-Router POST "/chat/completions" (Chat "chat-fast" "my key is AKIAABCDEFGHIJKLMNOP") @{ "x-ent-guardrail-profile" = "standard" }
    Report "blocked at the gateway (nothing sent to a provider)" (($r.Status -eq 400) -and ($r.Json.error.code -eq "guardrail_blocked")) ("HTTP $($r.Status): " + $r.Json.error.message)
}

# ---- 6. cache
Write-Host "6. Is an identical request answered from the cache the second time?"
if ($live) {
    $unique = "Reply with the single word: cache-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
    $first = Send-Router POST "/chat/completions" (Chat "chat-fast" $unique) @{ "x-ent-data-classification" = "internal" }
    $second = Send-Router POST "/chat/completions" (Chat "chat-fast" $unique) @{ "x-ent-data-classification" = "internal" }
    $a = [string]$first.Headers["x-router-cache"]; $b = [string]$second.Headers["x-router-cache"]
    Report "second identical call is a cache hit" (($a -eq "miss") -and ($b -eq "hit")) ("1st: x-router-cache=$a ({0:N2}s)   2nd: x-router-cache=$b ({1:N3}s)" -f $first.Seconds, $second.Seconds)
} else { Skip "cache" "needs real mode and a model call (tiny cost)" }

# ---- 7. the trace names the model that answered
Write-Host "7. Does the trace say which real model answered?"
if ($live -and $answered.Count -eq 2) {
    $served = @()
    for ($i = 0; $i -lt 20 -and $served.Count -lt 2; $i++) {
        try {
            $t = (Invoke-RestMethod -Uri "$Jaeger/api/traces/$traceId" -TimeoutSec 5).data[0]
            $served = @($t.spans | Where-Object { $_.operationName -like "router chat*" } | ForEach-Object {
                $tag = $_.tags | Where-Object { $_.key -eq "gen_ai.response.model" }
                if ($tag) { [string]$tag.value }
            })
        } catch { }
        if ($served.Count -lt 2) { Start-Sleep -Seconds 1 }
    }
    $expected = @($answered["chat-fast"], $answered["chat-default"]) | Sort-Object
    $ok = ($served.Count -eq 2) -and ((@($served) | Sort-Object) -join "|") -eq ($expected -join "|")
    Report "the router's trace span carries gen_ai.response.model" $ok ("trace $traceId  ->  " + ($served -join ", ") + "     (open: $Jaeger/trace/$traceId)")
    if (-not $ok -and $served.Count -eq 0) { Write-Host "        No such attribute yet: rebuild the router (docker compose --profile app up -d --build model-router) or check Jaeger is running." -ForegroundColor Yellow }
} else { Skip "trace" "needs the model calls of case 4" }

Write-Host ""
$colour = if ($script:fail -gt 0) { "Red" } else { "Green" }
Write-Host ("Result: {0} passed, {1} failed, {2} skipped" -f $script:pass, $script:fail, $script:skip) -ForegroundColor $colour
if ($script:fail -gt 0) { exit 1 }

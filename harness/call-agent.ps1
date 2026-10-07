# Calls a running agent, prints its answer, and shows where to see that call in Jaeger.
#
#   .\harness\call-agent.ps1 -Port 8091 -Prompt "What can you do?"
#   .\harness\call-agent.ps1 -Port 8091 -Open                      # also opens the trace in your browser
#   .\harness\call-agent.ps1 -Port 8091 -Session my-chat-0000000000000000000001   # continue a conversation
#   .\harness\call-agent.ps1 -TraceId 4bf7a8466fc54ee2b7aafaca3dfe1b8e             # just show an existing trace
#
# What you see: the answer, the trace ID, a link straight to the trace, and the trace's call tree
# (agent -> graph steps -> model calls -> tools) with timings. In Jaeger itself: http://localhost:16686,
# pick the agent's name under "Service" and press "Find Traces", or paste the trace ID in the search box.
#
# For a trace to exist, the agent must export traces. Start an agent on the host like this
# (my-first-agent as the example; PORT avoids clashing with the example agents on 8080/8081):
#
#   $env:PORT = '8091'
#   $env:OTEL_EXPORTER_OTLP_ENDPOINT = 'http://localhost:4318'     # Jaeger's OTLP port
#   $env:ENT_MODEL_GATEWAY_URL = 'http://localhost:8788/v1'        # the model router
#   $env:ENT_MODEL_GATEWAY_KEY = '<this agent's key>'              # must be listed in ROUTER_GATEWAY_KEYS (compose.yaml)
#   uv run my-first-agent
param(
    [string]$Prompt = "Hello! What can you do?",
    [int]$Port = 8080,
    [string]$Session = "",
    [string]$TraceId = "",     # look up an existing trace (no new call is made)
    [string]$Jaeger = "http://localhost:16686",
    [switch]$Open
)

# Windows PowerShell 5.1 spends over a second on proxy detection for every localhost call: skip it.
[System.Net.WebRequest]::DefaultWebProxy = $null

if (-not $TraceId) {
$url = "http://localhost:$Port/invocations"
$headers = @{}
if ($Session) { $headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"] = $Session }
$body = [Text.Encoding]::UTF8.GetBytes((@{ prompt = $Prompt } | ConvertTo-Json -Compress))

Write-Host "Asking http://localhost:$Port ..." -ForegroundColor DarkGray
$status = 0; $traceId = ""; $text = ""
try {
    $resp = Invoke-WebRequest -Uri $url -Method Post -Body $body -ContentType "application/json; charset=utf-8" `
        -Headers $headers -UseBasicParsing -TimeoutSec 240
    $status = [int]$resp.StatusCode
    $traceId = [string]$resp.Headers["x-ent-trace-id"]
    $text = [Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
}
catch {
    $r = $_.Exception.Response
    if ($r) {
        $status = [int]$r.StatusCode
        $traceId = [string]$r.Headers["x-ent-trace-id"]
        # Windows PowerShell 5.1 has already read the error body into ErrorDetails; the stream is empty by now.
        $text = [string]$_.ErrorDetails.Message
        if (-not $text) { $text = (New-Object IO.StreamReader($r.GetResponseStream(), [Text.Encoding]::UTF8)).ReadToEnd() }
    }
    else {
        Write-Host "Could not reach the agent on port ${Port}: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Is it running? Check:  Invoke-RestMethod http://localhost:$Port/ping" -ForegroundColor Yellow
        exit 1
    }
}

$data = $null
try { $data = $text | ConvertFrom-Json } catch { }

Write-Host ""
if ($status -ge 200 -and $status -lt 300 -and $data -and $data.output) {
    Write-Host "Answer:" -ForegroundColor Green
    Write-Host $data.output
}
else {
    $err = if ($data -and $data.error) { "$($data.error.type): $($data.error.message)" } else { $text }
    Write-Host "The agent answered with HTTP $status" -ForegroundColor Red
    Write-Host $err
    if ($status -eq 401 -or $err -match "401|gateway") {
        Write-Host "Hint: the model router did not accept this agent's key. Its ENT_MODEL_GATEWAY_KEY must be listed in" -ForegroundColor Yellow
        Write-Host "      ROUTER_GATEWAY_KEYS in compose.yaml (then recreate the router: docker compose --profile app up -d --no-build model-router)." -ForegroundColor Yellow
    }
    if ($status -eq 503) { Write-Host "Hint: the model router may not be running:  .\harness\dev.ps1 up -Mode real" -ForegroundColor Yellow }
}
}   # end of "make a call" (skipped when -TraceId is given)

if (-not $traceId) { Write-Host "`nNo trace ID came back (the agent did not answer through the SDK)." -ForegroundColor Yellow; exit 0 }

$traceUrl = "$Jaeger/trace/$traceId"
Write-Host ""
Write-Host "Trace ID : $traceId"
Write-Host "Trace    : $traceUrl"

# Traces are sent in batches, so give Jaeger a few seconds to receive this one.
# The agent and the model router export in separate batches, so wait until the agent's own first step (the one
# with no parent) has arrived, not just the first spans of the router.
$trace = $null
for ($i = 0; $i -lt 20; $i++) {
    try { $found = Invoke-RestMethod -Uri "$Jaeger/api/traces/$traceId" -TimeoutSec 5; if ($found.data -and $found.data.Count -gt 0) { $trace = $found.data[0] } }
    catch { }
    if ($trace -and ($trace.spans | Where-Object { -not $_.references -or $_.references.Count -eq 0 })) { break }
    Start-Sleep -Seconds 1
}

if (-not $trace) {
    Write-Host ""
    Write-Host "Jaeger has not received this trace. Either Jaeger is not running (docker ps) or the agent is not exporting traces:" -ForegroundColor Yellow
    Write-Host "start the agent with  `$env:OTEL_EXPORTER_OTLP_ENDPOINT = 'http://localhost:4318'  and call it again." -ForegroundColor Yellow
    exit 0
}

$serviceOf = @{}
foreach ($p in $trace.processes.PSObject.Properties) { $serviceOf[$p.Name] = $p.Value.serviceName }
# The service to pick in Jaeger is the one that owns the root step (the agent), not the first one listed.
$root = $trace.spans | Where-Object { -not $_.references -or $_.references.Count -eq 0 } | Sort-Object startTime | Select-Object -First 1
$service = if ($root) { $serviceOf[$root.processID] } else { ($serviceOf.Values | Select-Object -First 1) }
Write-Host "Jaeger   : $Jaeger   (Service = $service -> Find Traces, or paste the trace ID)"
Write-Host ("Services : " + (($serviceOf.Values | Sort-Object -Unique) -join ", "))
Write-Host ""
Write-Host "What happened ($($trace.spans.Count) steps):" -ForegroundColor Green

$byId = @{}
foreach ($s in $trace.spans) { $byId[$s.spanID] = $s }
function Get-Depth($span) {
    $depth = 0; $current = $span
    while ($current.references -and $current.references.Count -gt 0 -and $byId.ContainsKey($current.references[0].spanID) -and $depth -lt 20) {
        $current = $byId[$current.references[0].spanID]; $depth++
    }
    return $depth
}
foreach ($s in ($trace.spans | Sort-Object startTime)) {
    $owner = $serviceOf[$s.processID]
    $tag = if ($owner -ne $service) { "  [$owner]" } else { "" }   # only mark steps that run in another service
    Write-Host ("{0}{1}{2}  ({3} ms)" -f ("  " * (Get-Depth $s)), $s.operationName, $tag, [math]::Round($s.duration / 1000))
}

if ($Open) { Start-Process $traceUrl }

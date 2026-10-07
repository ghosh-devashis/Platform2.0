# One-command local stack (RUN-01). Run from the repo root:  .\harness\dev.ps1 <command> [-Mode real|record|replay|local]
#   up      build and start the platform: Floci (local AWS), Floci UI, Portkey, Jaeger, model router, invoke router
#   down    stop everything (keeps Floci data; add -Wipe to also delete it)
#   seed    create the secrets, tables and AgentCore runtime an agent declares in its manifest:
#             .\harness\dev.ps1 seed -Manifest ..\my-agent\agent.yaml
#   status  show container health
# Agents are not part of this stack: create each one in its own folder (see instructions.txt).
param(
    [Parameter(Position = 0)][ValidateSet('up', 'down', 'seed', 'status')][string]$Command = 'status',
    [ValidateSet('real', 'record', 'replay', 'local')][string]$Mode = 'real',
    [string]$Manifest = '',
    [switch]$Wipe
)
# Native tools (docker, uv) write progress to stderr, which 'Stop' would treat as an error in Windows PowerShell:
# keep the default and check exit codes explicitly instead.
$env:VIRTUAL_ENV = $null
$compose = @('compose', '--profile', 'app')
$env:AWS_ENDPOINT_URL = 'http://localhost:4566'
$env:AWS_ACCESS_KEY_ID = 'test'
$env:AWS_SECRET_ACCESS_KEY = 'test'
$env:AWS_DEFAULT_REGION = 'us-east-1'

function Invoke-Seed {
    if (-not $Manifest) { throw "Say which agent to seed:  .\harness\dev.ps1 seed -Manifest ..\my-agent\agent.yaml" }
    uv run ent-agent dev seed --manifest $Manifest
    if ($LASTEXITCODE -ne 0) { throw "Seeding failed for $Manifest" }
}

switch ($Command) {
    'up' {
        $env:ROUTER_MODE = $Mode
        $env:ENT_GIT_SHA = (git rev-parse --short HEAD 2>$null)
        if (-not $env:ENT_GIT_SHA) { $env:ENT_GIT_SHA = 'local' }
        docker @compose up -d --build
        if ($LASTEXITCODE -ne 0) { throw 'docker compose up failed' }
        # Wait for Floci to answer.
        for ($i = 0; $i -lt 30; $i++) {
            try { Invoke-RestMethod http://localhost:4566/_floci/health -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep 2 }
        }
        Write-Host "`nStack is up (router mode: $Mode)."
        Write-Host '  model router http://localhost:8788   invoke router http://localhost:4567'
        Write-Host '  Portkey       http://localhost:8787   Floci        http://localhost:4566'
        Write-Host '  Jaeger        http://localhost:16686  Floci UI     http://localhost:4500'
        Write-Host 'Next: create an agent (uv run ent-agent new ...), then seed it:  .\harness\dev.ps1 seed -Manifest ..\my-agent\agent.yaml'
    }
    'down' {
        if ($Wipe) { docker @compose down -v } else { docker @compose down }
    }
    'seed' { Invoke-Seed }
    'status' { docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' }
}

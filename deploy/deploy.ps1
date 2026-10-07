<#
.SYNOPSIS
  Deploy an agent from its agent.yaml with Terraform, to real AWS, or rehearse the SAME Terraform against Floci (RUN-05).

.EXAMPLE
  .\deploy\deploy.ps1 -Target local -Manifest ..\my-agent\agent.yaml -NameOverride rehearsal-agent            # plan only
  .\deploy\deploy.ps1 -Target local -Manifest ..\my-agent\agent.yaml -NameOverride rehearsal-agent -Apply     # create + smoke checks
  .\deploy\deploy.ps1 -Target local -Manifest ..\my-agent\agent.yaml -NameOverride rehearsal-agent -Destroy   # remove it
  .\deploy\deploy.ps1 -Target aws -Manifest agent.yaml -Image 123456789012.dkr.ecr.eu-west-1.amazonaws.com/x:1.0 -Apply -ConfirmRealAws

  Without -Apply or -Destroy nothing is changed: it stops after `terraform plan`.
  Terraform must be on PATH (in this repo: `. .\harness\env.ps1`).
  Exit code is 0 only if every step and smoke check passed.
#>
param(
    [Parameter(Mandatory)][ValidateSet('local', 'aws')][string]$Target,
    [Parameter(Mandatory)][string]$Manifest,
    [string]$Image,
    [string]$NameOverride,            # deploy under a different name (rehearsals that must not clash with a real agent)
    [string[]]$ExtraSecrets = @(),    # secrets in addition to the manifest's (e.g. to rehearse more resources)
    [string]$Bucket = '',             # optional S3 bucket name
    [string]$Table = '',              # optional DynamoDB table name
    [string]$Region = 'us-east-1',
    [string]$Environment = 'dev',
    [string]$FlociEndpoint = 'http://localhost:4566',
    [string]$RouterEndpoint = 'http://localhost:4567',
    [switch]$Apply,
    [switch]$Destroy,
    [switch]$ConfirmRealAws           # required with -Target aws and -Apply/-Destroy
)

# NOTE: no $ErrorActionPreference='Stop' on purpose. Windows PowerShell 5.1 turns native-tool stderr into errors.
# Every native call is followed by an explicit $LASTEXITCODE check instead.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here
$tfDir = Join-Path $here 'terraform'
$failures = New-Object System.Collections.Generic.List[string]

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "FAIL: $msg" -ForegroundColor Red; $script:failures.Add($msg) }
function Pass($msg) { Write-Host "PASS: $msg" -ForegroundColor Green }
function Die($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

if ($Apply -and $Destroy) { Die '-Apply and -Destroy cannot be combined.' }
if ($Target -eq 'aws' -and ($Apply -or $Destroy) -and -not $ConfirmRealAws) {
    Die 'Target aws changes REAL AWS resources. Re-run with -ConfirmRealAws if you mean it.'
}
if (-not (Get-Command terraform -ErrorAction SilentlyContinue)) { Die 'terraform is not on PATH (run: . .\harness\env.ps1).' }
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { Die 'the AWS CLI is not on PATH.' }

# --- environment for the target -------------------------------------------------------------------------------
$env:AWS_DEFAULT_REGION = $Region
$awsEndpointArgs = @()
if ($Target -eq 'local') {
    # Dummy credentials, always: a rehearsal can never touch a real account by accident.
    $env:AWS_ACCESS_KEY_ID = 'test'; $env:AWS_SECRET_ACCESS_KEY = 'test'
    $env:AWS_SESSION_TOKEN = $null; $env:AWS_PROFILE = $null
    $awsEndpointArgs = @('--endpoint-url', $FlociEndpoint)
    try { $null = Invoke-RestMethod "$FlociEndpoint/_floci/health" -TimeoutSec 5 } catch { Die "Floci is not reachable at $FlociEndpoint (docker compose up -d)." }
}
$env:TF_IN_AUTOMATION = '1'
if (-not $env:TF_PLUGIN_CACHE_DIR) {
    $env:TF_PLUGIN_CACHE_DIR = Join-Path $env:LOCALAPPDATA 'terraform-plugin-cache'
}
if (-not (Test-Path $env:TF_PLUGIN_CACHE_DIR)) { New-Item -ItemType Directory -Force $env:TF_PLUGIN_CACHE_DIR | Out-Null }

# --- read the manifest (validated by the SDK's own parser) ------------------------------------------------------
Step "Reading $Manifest"
$manifestPath = (Resolve-Path $Manifest -ErrorAction SilentlyContinue).Path
if (-not $manifestPath) { Die "Manifest '$Manifest' not found." }
# Single quotes inside the Python: Windows PowerShell 5.1 strips embedded double quotes from native arguments.
$py = "import json,sys; from ent_agent_sdk.manifest import load; m=load(sys.argv[1]); print(json.dumps({'name':m.name,'team':m.team,'data_classification':m.data_classification,'description':m.description,'secrets':list(m.secrets)}))"
Push-Location $repo
$json = & uv run python -c $py $manifestPath
$rc = $LASTEXITCODE
Pop-Location
if ($rc -ne 0) { Die "The manifest is invalid or could not be read (exit $rc)." }
$m = $json | Select-Object -Last 1 | ConvertFrom-Json

$agentName = if ($NameOverride) { $NameOverride } else { $m.name }
if ($Image) { $image = $Image }
elseif ($Target -eq 'local') { $image = "local/${agentName}:dev" }
else { Die '-Image is required for -Target aws (an ECR image URI).' }
$secrets = @(@($m.secrets) + $ExtraSecrets | Where-Object { $_ } | Select-Object -Unique)
Write-Host "agent=$agentName team=$($m.team) classification=$($m.data_classification) target=$Target image=$image secrets=[$($secrets -join ', ')]"

# --- generated variables + per-target/agent state ---------------------------------------------------------------
$stateDir = Join-Path $here ".state\$Target-$agentName"
New-Item -ItemType Directory -Force $stateDir | Out-Null
$varsFile = Join-Path $stateDir 'agent.auto.tfvars.json'
@{
    target = $Target; region = $Region; local_endpoint = $FlociEndpoint; agent_name = $agentName
    team = $m.team; data_classification = $m.data_classification; description = [string]$m.description
    environment = $Environment; image_uri = $image; secrets = $secrets
    s3_bucket_name = $Bucket; dynamodb_table_name = $Table
} | ConvertTo-Json -Depth 4 | Set-Content -Encoding ASCII $varsFile
$statePath = Join-Path $stateDir 'terraform.tfstate'

function Tf {
    # Out-Host so the output is shown and only the exit code is returned.
    & terraform "-chdir=$tfDir" @args 2>&1 | Out-Host
    return $LASTEXITCODE
}

Step 'terraform init'
if ((Tf init -input=false -no-color -reconfigure "-backend-config=path=$statePath") -ne 0) { Die 'terraform init failed.' }
Step 'terraform validate'
if ((Tf validate -no-color) -ne 0) { Die 'terraform validate failed.' }

$common = @('-input=false', '-no-color', "-var-file=$varsFile")
$planFile = Join-Path $stateDir 'plan.tfplan'
if ($Destroy) {
    Step "terraform destroy ($Target / $agentName)"
    if ((Tf destroy @common -auto-approve) -ne 0) { Die 'terraform destroy failed.' }
} else {
    Step 'terraform plan'
    if ((Tf plan @common "-out=$planFile") -ne 0) { Die 'terraform plan failed.' }
    if (-not $Apply) {
        Write-Host "`nPlan only. Add -Apply to create these resources." -ForegroundColor Yellow
        exit 0
    }
    Step "terraform apply ($Target / $agentName)"
    if ((Tf apply -input=false -no-color $planFile) -ne 0) { Die 'terraform apply failed.' }
}

# --- smoke checks -----------------------------------------------------------------------------------------------
function Aws-Json {
    # Runs the AWS CLI, returns parsed JSON (or $null on failure; the CLI's own error is shown).
    $out = & aws @args --output json
    if ($LASTEXITCODE -ne 0) { return $null }
    if (-not $out) { return @{} }
    return ($out -join "`n") | ConvertFrom-Json
}

$runtimeName = $agentName -replace '-', '_'
if ($Destroy) {
    Step 'Smoke checks (resources must be gone)'
    foreach ($s in $secrets) {
        $null = & aws @awsEndpointArgs secretsmanager describe-secret --secret-id $s 2>$null
        if ($LASTEXITCODE -eq 0) {
            # A secret scheduled for deletion still "exists" in real AWS (recovery window); Floci removes it outright.
            $d = Aws-Json @awsEndpointArgs secretsmanager describe-secret --secret-id $s
            if ($d.DeletedDate) { Pass "secret $s is scheduled for deletion" } else { Fail "secret $s still exists" }
        } else { Pass "secret $s is gone" }
    }
    $rt = Aws-Json @awsEndpointArgs bedrock-agentcore-control list-agent-runtimes
    if ($null -eq $rt) { Fail 'could not list runtimes' }
    elseif (@($rt.agentRuntimes | Where-Object { $_.agentRuntimeName -eq $runtimeName }).Count -eq 0) { Pass "runtime $runtimeName is gone" }
    else { Fail "runtime $runtimeName still exists" }
} else {
    Step 'Smoke checks'
    foreach ($s in $secrets) {
        $d = Aws-Json @awsEndpointArgs secretsmanager describe-secret --secret-id $s
        if ($d -and $d.Name -eq $s) { Pass "secret $s exists" } else { Fail "secret $s not found" }
    }
    if ($Bucket) {
        & aws @awsEndpointArgs s3api head-bucket --bucket $Bucket 2>$null
        if ($LASTEXITCODE -eq 0) { Pass "bucket $Bucket exists" } else { Fail "bucket $Bucket not found" }
    }
    if ($Table) {
        $t = Aws-Json @awsEndpointArgs dynamodb describe-table --table-name $Table
        if ($t -and $t.Table.TableStatus -eq 'ACTIVE') { Pass "table $Table is ACTIVE" } else { Fail "table $Table not ACTIVE" }
    }
    $rid = (& terraform "-chdir=$tfDir" output -raw runtime_id)
    $rarn = (& terraform "-chdir=$tfDir" output -raw runtime_arn)
    if (-not $rid) { Fail 'terraform produced no runtime id' }
    else {
        $r = Aws-Json @awsEndpointArgs bedrock-agentcore-control get-agent-runtime --agent-runtime-id $rid
        if (-not $r) { Fail "runtime $rid could not be read" }
        elseif ($r.status -eq 'READY') { Pass "runtime $runtimeName is READY ($rarn)" }
        else { Fail "runtime $runtimeName status is '$($r.status)', expected READY" }
        if ($r -and $r.networkConfiguration.networkMode -ne 'PUBLIC') { Fail 'runtime network mode is not PUBLIC' }
    }
    if ($Target -eq 'local') {
        # The invoke router passes control-plane calls through to Floci, so it must list the runtime too.
        $rl = Aws-Json --endpoint-url $RouterEndpoint bedrock-agentcore-control list-agent-runtimes
        if ($null -eq $rl) { Fail "could not list runtimes through the invoke router ($RouterEndpoint)" }
        elseif (@($rl.agentRuntimes | Where-Object { $_.agentRuntimeName -eq $runtimeName }).Count -ge 1) { Pass "invoke router ($RouterEndpoint) lists $runtimeName" }
        else { Fail "invoke router does not list $runtimeName" }
    }
}

if ($failures.Count -gt 0) {
    Write-Host "`n$($failures.Count) check(s) failed." -ForegroundColor Red
    exit 1
}
Write-Host "`nAll steps and smoke checks passed." -ForegroundColor Green
exit 0

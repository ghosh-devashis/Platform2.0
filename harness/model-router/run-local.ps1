# Run the model router locally on :8788, reading provider keys from Floci's Secrets Manager.
# Usage (repo root):  .\harness\model-router\run-local.ps1
$env:VIRTUAL_ENV = $null
$env:AWS_ENDPOINT_URL = 'http://localhost:4566'
$env:AWS_ACCESS_KEY_ID = 'test'
$env:AWS_SECRET_ACCESS_KEY = 'test'
$env:AWS_DEFAULT_REGION = 'us-east-1'
uv run model-router

# One provider block, two targets. "local" points the services we use at Floci; "aws" uses the normal
# credential chain (env vars, profile, SSO, role). Attributes switch with conditional expressions because
# `endpoints` is a nested block: we use a dynamic block so it is simply absent for "aws".
locals {
  is_local       = var.target == "local"
  local_endpoint = var.local_endpoint
}

provider "aws" {
  region = var.region

  # Dummy keys ONLY for local; null means "use the default credential chain".
  access_key = local.is_local ? "test" : null
  secret_key = local.is_local ? "test" : null

  skip_credentials_validation = local.is_local
  skip_requesting_account_id  = local.is_local
  skip_metadata_api_check     = local.is_local
  skip_region_validation      = local.is_local
  s3_use_path_style           = local.is_local

  dynamic "endpoints" {
    for_each = local.is_local ? [1] : []
    content {
      s3               = local.local_endpoint
      dynamodb         = local.local_endpoint
      iam              = local.local_endpoint
      sts              = local.local_endpoint
      secretsmanager   = local.local_endpoint
      bedrockagentcore = local.local_endpoint
    }
  }

  default_tags {
    tags = local.tags
  }
}

locals {
  runtime_name = replace(var.agent_name, "-", "_") # AgentCore names: [a-zA-Z][a-zA-Z0-9_]{0,47}
  tags = {
    team                  = var.team
    "data-classification" = var.data_classification
    environment           = var.environment
    agent                 = var.agent_name
    "managed-by"          = "platform2-terraform"
  }
}

# --- Secrets: containers only. Real values are set out of band and never stored in Terraform. -------------------
resource "aws_secretsmanager_secret" "agent" {
  for_each    = toset(var.secrets)
  name        = each.value
  description = "Secret for agent ${var.agent_name} (value managed outside Terraform)"
  # Real AWS: 30-day recovery window (name stays reserved). Local: delete immediately so rehearsals can be repeated.
  recovery_window_in_days = local.is_local ? 0 : 30
}

resource "aws_secretsmanager_secret_version" "placeholder" {
  for_each      = aws_secretsmanager_secret.agent
  secret_id     = each.value.id
  secret_string = "placeholder-replace-me"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# --- Optional data stores ---------------------------------------------------------------------------------------
resource "aws_s3_bucket" "agent" {
  count  = var.s3_bucket_name == "" ? 0 : 1
  bucket = var.s3_bucket_name
}

resource "aws_dynamodb_table" "agent" {
  count        = var.dynamodb_table_name == "" ? 0 : 1
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }
}

# --- IAM role the runtime assumes -------------------------------------------------------------------------------
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "runtime" {
  # Least privilege: only this agent's secrets / bucket / table. Model calls go through the gateway, not Bedrock.
  dynamic "statement" {
    for_each = length(var.secrets) > 0 ? [1] : []
    content {
      sid       = "ReadOwnSecrets"
      actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      resources = [for s in aws_secretsmanager_secret.agent : s.arn]
    }
  }
  dynamic "statement" {
    for_each = var.s3_bucket_name == "" ? [] : [1]
    content {
      sid       = "OwnBucket"
      actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
      resources = [aws_s3_bucket.agent[0].arn, "${aws_s3_bucket.agent[0].arn}/*"]
    }
  }
  dynamic "statement" {
    for_each = var.dynamodb_table_name == "" ? [] : [1]
    content {
      sid       = "OwnTable"
      actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query"]
      resources = [aws_dynamodb_table.agent[0].arn]
    }
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["*"]
  }
}

resource "aws_iam_role" "runtime" {
  name               = "${var.agent_name}-runtime-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy" "runtime" {
  name   = "${var.agent_name}-runtime-policy"
  role   = aws_iam_role.runtime.id
  policy = data.aws_iam_policy_document.runtime.json
}

# --- The AgentCore runtime --------------------------------------------------------------------------------------
resource "aws_bedrockagentcore_agent_runtime" "agent" {
  agent_runtime_name    = local.runtime_name
  description           = var.description != "" ? var.description : "${var.agent_name} (${var.team})"
  role_arn              = aws_iam_role.runtime.arn
  environment_variables = length(var.environment_variables) > 0 ? var.environment_variables : null

  agent_runtime_artifact {
    container_configuration {
      container_uri = var.image_uri
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  tags = local.tags

  depends_on = [aws_iam_role_policy.runtime]
}

output "runtime_name" {
  value = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_name
}

output "runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn
}

output "runtime_id" {
  value = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_id
}

output "secret_names" {
  value = sort([for s in aws_secretsmanager_secret.agent : s.name])
}

output "role_arn" {
  value = aws_iam_role.runtime.arn
}

output "target" {
  value = var.target
}

terraform {
  required_version = ">= 1.6"

  # The state file path is passed by deploy.ps1 (-backend-config="path=..."): one state per target + agent.
  backend "local" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

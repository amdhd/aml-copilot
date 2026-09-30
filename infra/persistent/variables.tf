variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "budget_email" {
  description = "Where the $20/mo budget alarm goes."
  type        = string
}

variable "domain_zone" {
  description = "An existing public Route 53 zone the demo hostname goes in. Read, never managed: other records in it belong to other projects."
  type        = string
}

variable "hostname" {
  description = "The demo's HTTPS name, inside domain_zone, e.g. aml.example.org."
  type        = string
}

variable "allowed_cidrs" {
  description = "Who can reach the ALB. The UI has no auth and each case spends LLM credit, so not 0.0.0.0/0."
  type        = list(string)
}

variable "image_tag" {
  description = "Tag in ECR to run -- the git sha the image was built from."
  type        = string
}

variable "allowed_cidrs" {
  description = "Who can reach the ALB for this demo. scripts/demo.sh passes the IP it runs from, plus EXTRA_CIDRS."
  type        = list(string)
  validation {
    condition     = alltrue([for c in var.allowed_cidrs : can(cidrnetmask(c)) && c != "0.0.0.0/0"])
    error_message = "Each entry must be an IPv4 CIDR, and not 0.0.0.0/0: every case spends LLM credit."
  }
}

variable "graph_key" {
  description = "S3 key of the graph cache, prefixed by the first 16 hex of its sha256."
  type        = string
  default     = "graph/c27a18e5ae9bf284/HI-Small_Trans.None.graph.pt"
}

variable "llm_base_url" {
  type    = string
  default = "https://api.deepseek.com"
}

variable "llm_model" {
  type    = string
  default = "deepseek-flash"
}

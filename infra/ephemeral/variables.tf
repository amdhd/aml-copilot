variable "image_tag" {
  description = "Tag in ECR to run -- the git sha the image was built from."
  type        = string
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

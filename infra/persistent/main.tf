# Persistent layer (plan section 9): applied once and left alone. Everything
# here is either free while idle or slow to rebuild. Nothing that bills by the
# hour belongs in this file -- that is infra/ephemeral.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  # Local state. infra/ephemeral reads it through terraform_remote_state, so
  # this file is the one thing in infra/ that must not be lost.
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "aml-copilot", Layer = "persistent" }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name = "aml-copilot"
  # Two AZs: an ALB and an RDS subnet group both refuse to exist in one.
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

# --- Network ---------------------------------------------------------------
#
# No NAT Gateway (section 9: ~$32/mo whether or not anything runs). Tasks sit in
# public subnets with public IPs and reach ECR, S3 and DeepSeek directly;
# security groups are what keep anything from reaching them. Production would
# put tasks in private subnets behind VPC endpoints -- this is the demo-only
# compromise section 9 says to be able to name.
#
# RDS gets private subnets with no route out at all. It never
# initiates a connection, so it needs none, and that costs nothing.

resource "aws_vpc" "main" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
  tags                    = { Name = "${local.name}-public-${local.azs[count.index]}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index + 10)
  availability_zone = local.azs[count.index]
  tags              = { Name = "${local.name}-private-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# --- Security groups -------------------------------------------------------
#
# The chain is ALB -> tasks -> RDS, each hop allowing only the one before.
# The ALB is the only thing reachable from outside, and only from the addresses
# a demo is run for: every case opened spends DeepSeek credit.
#
# Its ingress rules are ephemeral (aws_vpc_security_group_ingress_rule in
# infra/ephemeral), written by each demo-up for the IP it runs from. A fixed
# home IP here outlived the demos it was for, and kept access wherever the ISP
# reassigned it. No ingress block here, and there must never be one: inline
# rules make this resource authoritative for ingress, and the next apply would
# delete the ephemeral layer's rules. Omitted, the provider leaves ingress
# alone -- which is also why the old inline /32 rule, once removed from here,
# still had to be revoked by hand.

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Demo ALB, reachable from allowed_cidrs only"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "tasks" {
  name        = "${local.name}-tasks"
  description = "API and worker tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "API, from the ALB only"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  # Redis runs as a sidecar in the api task (on-demand, so a Spot reclaim of
  # the worker cannot take the queue with it). The worker reaches it across
  # tasks, so the group admits itself on 6379 and nothing else does.
  ingress {
    description = "Redis sidecar, worker to api task"
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    self        = true
  }
  egress {
    description = "ECR, S3, CloudWatch, DeepSeek -- no NAT, so directly"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "Postgres, from tasks only"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }
}

# --- Image and artifacts ---------------------------------------------------

# Tags are git commits (scripts/demo.sh), so a tag is a promise about what is
# inside. Mutable, anyone with push rights could put different code behind one.
resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

# The image is 1.4GB; ECR bills storage, so old ones should not pile up.
# Counts tagged images only. Images are pushed with --provenance=false, so a
# tag is one manifest; an untagged one is a build whose tag moved on.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "untagged: superseded builds"
        selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 1 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "keep the last 3 tagged images"
        selection    = { tagStatus = "tagged", tagPatternList = ["*"], countType = "imageCountMoreThan", countNumber = 3 }
        action       = { type = "expire" }
      },
    ]
  })
}

# The 1.84GB graph cache (artifacts.py, AML_GRAPH_S3). Account id in the name
# because bucket names are global.
resource "aws_s3_bucket" "artifacts" {
  bucket = "${local.name}-artifacts-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_caller_identity" "current" {}

# 7 days, not the default of never (section 9). Here rather than in ephemeral
# so a demo's logs survive the destroy that follows it.
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 7
}

# --- TLS -------------------------------------------------------------------
#
# Reviewers log in with basic auth (api/auth.py), which sends the password on
# every request, so the ALB serves HTTPS. The certificate is free and validates
# in minutes, but not instantly, so it lives here rather than in ephemeral: an
# apply before a demo should not wait on DNS. The alias record pointing the
# hostname at the ALB is ephemeral, and exists only while a demo does.
#
# The zone is looked up, not created. It is shared with other projects, and
# this layer adds exactly one record to it: the validation CNAME, which ACM
# also needs to renew.

data "aws_route53_zone" "demo" {
  name         = var.domain_zone
  private_zone = false
}

resource "aws_acm_certificate" "demo" {
  domain_name       = var.hostname
  validation_method = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for o in aws_acm_certificate.demo.domain_validation_options : o.domain_name => o
  }
  zone_id = data.aws_route53_zone.demo.zone_id
  name    = each.value.resource_record_name
  type    = each.value.resource_record_type
  records = [each.value.resource_record_value]
  ttl     = 300
}

resource "aws_acm_certificate_validation" "demo" {
  certificate_arn         = aws_acm_certificate.demo.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# --- App parameters --------------------------------------------------------
#
# The LLM key and the reviewer logins (api/auth.py: username -> PBKDF2 hash,
# as JSON) are SSM SecureStrings -- free at this tier, against $0.40/mo each
# for Secrets Manager -- created and set from the CLI only (README, Deploy):
#
#   aws ssm put-parameter --name /aml-copilot/llm-api-key --type SecureString \
#     --overwrite --value "$KEY"
#   aws ssm put-parameter --name /aml-copilot/reviewers --type SecureString \
#     --overwrite --value "$(uv run python -m api.auth alice bob)"
#
# Terraform used to create them with a placeholder and ignore_changes on the
# value, on the understanding that the real value would then never enter its
# state. It did: every plan refreshes an aws_ssm_parameter, decrypting the
# value into terraform.tfstate. Write-only values (value_wo) would avoid that
# for new parameters, but moving an existing one onto them sends the
# write-only value -- the placeholder -- over the real key. So Terraform no
# longer manages them at all; the task definitions address them by name.
#
# The removed blocks drop them from state without deleting them from AWS.
# They can go once every copy of the state has been applied past them.

removed {
  from = aws_ssm_parameter.llm_api_key
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_ssm_parameter.reviewers
  lifecycle {
    destroy = false
  }
}

locals {
  parameters = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${local.name}"
}

# --- IAM -------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Execution role: what ECS itself needs to start a task -- pull the image,
# write logs, and read the key into the container's environment.
resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_ssm" {
  name = "read-app-parameters"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["ssm:GetParameters"]
      # The LLM key here, and the database DSN that each ephemeral apply
      # writes beside it with a fresh password.
      Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${local.name}/*"
    }]
  })
}

# Task role: what the application code needs at run time. Read-only on the
# artifacts bucket and nothing else.
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "task_s3" {
  name = "read-artifacts"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = "${aws_s3_bucket.artifacts.arn}/*"
    }]
  })
}

# --- Budget ----------------------------------------------------------------
#
# Counts this project only. The account also runs other work -- Bedrock alone
# was ~$24 of $30.64 in September 2026 -- and an account-wide figure would
# stop demo-up for spend that is not this project's. Every resource carries
# Project = aml-copilot (default_tags in both layers); billing attributes cost
# to a tag only once it is activated, and only from then on.

resource "aws_ce_cost_allocation_tag" "project" {
  tag_key = "Project"
  status  = "Active"
}
#
# Before the first ephemeral apply, not after the first surprise (section 9).
# The steady state here is ~$2-4/mo, so crossing 50% of $20 means something
# expensive was left running.

resource "aws_budgets_budget" "monthly" {
  name         = local.name
  budget_type  = "COST"
  limit_amount = "20"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Usage before credits. Net of them, the spend reads $0.00 until the credits
  # run out, and neither these alarms nor demo-up's spend guard would fire.
  cost_types {
    include_credit = false
  }

  cost_filter {
    name   = "TagKeyValue"
    values = [format("user:Project$%s", local.name)]
  }
  depends_on = [aws_ce_cost_allocation_tag.project]

  dynamic "notification" {
    for_each = [
      { threshold = 50, type = "ACTUAL" },
      { threshold = 100, type = "ACTUAL" },
      { threshold = 100, type = "FORECASTED" },
    ]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.threshold
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value.type
      subscriber_email_addresses = [var.budget_email]
    }
  }
}

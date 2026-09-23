# Ephemeral layer (plan section 9): `apply` before a demo, `destroy` after.
# Everything that bills by the hour is here and nowhere else.
#
#   terraform apply -var image_tag=<git sha>
#   aws ecs run-task ... (the seed task, see outputs.seed_command)
#   terraform destroy

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

data "terraform_remote_state" "persistent" {
  backend = "local"
  config  = { path = "${path.module}/../persistent/terraform.tfstate" }
}

locals {
  name  = "aml-copilot"
  p     = data.terraform_remote_state.persistent.outputs
  image = "${local.p.ecr_repository_url}:${var.image_tag}"
  # Service discovery name for the api task, which carries the Redis sidecar.
  redis_host = "api.${local.name}.local"
}

provider "aws" {
  region = local.p.region
  default_tags {
    tags = { Project = "aml-copilot", Layer = "ephemeral" }
  }
}

# --- Database --------------------------------------------------------------
#
# Smallest RDS there is. Rebuilt on every apply and loaded from the seed in
# seconds (section 9), which is why there are no backups and no final snapshot:
# there is nothing on it that the seed cannot reproduce, except the cases opened
# during the demo, and those are the demo.

resource "random_password" "db" {
  length  = 32
  special = false # it goes into a URL
}

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = local.p.private_subnet_ids
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = "16"
  instance_class = "db.t4g.micro"

  allocated_storage = 20
  storage_type      = "gp3"

  db_name  = "aml"
  username = "aml"
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [local.p.db_sg_id]
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period = 0
  skip_final_snapshot     = true
  deletion_protection     = false
  apply_immediately       = true
}

# The DSN carries the password, so it reaches containers through SSM rather
# than as a plain environment variable visible in the task definition.
resource "aws_ssm_parameter" "dsn" {
  name  = "/${local.name}/dsn"
  type  = "SecureString"
  value = "postgresql://aml:${random_password.db.result}@${aws_db_instance.main.address}:5432/aml"
}

# --- Service discovery -----------------------------------------------------
#
# The worker finds the api task's Redis sidecar by name. A private DNS
# namespace is a Route 53 hosted zone, which AWS does not bill if it is deleted
# within 12 hours -- true of any demo.

resource "aws_service_discovery_private_dns_namespace" "main" {
  name = "${local.name}.local"
  vpc  = local.p.vpc_id
}

resource "aws_service_discovery_service" "api" {
  name = "api"
  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.main.id
    dns_records {
      type = "A"
      ttl  = 10
    }
    routing_policy = "MULTIVALUE"
  }
}

# --- Load balancer ---------------------------------------------------------

resource "aws_lb" "main" {
  name               = local.name
  load_balancer_type = "application"
  subnets            = local.p.public_subnet_ids
  security_groups    = [local.p.alb_sg_id]
}

resource "aws_lb_target_group" "api" {
  name        = local.name
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = local.p.vpc_id

  # / is the built UI: static, no database, so a slow RDS cannot fail the check
  # and cycle a healthy task.
  health_check {
    path                = "/"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
  }
  deregistration_delay = 5
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# --- ECS -------------------------------------------------------------------

resource "aws_ecs_cluster" "main" {
  name = local.name
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
}

locals {
  llm_env = [
    { name = "AML_LLM_BASE_URL", value = var.llm_base_url },
    { name = "AML_LLM_MODEL", value = var.llm_model },
  ]
  dsn_secret = { name = "AML_DSN", valueFrom = aws_ssm_parameter.dsn.arn }

  logs = { for c in ["api", "redis", "worker", "seed"] : c => {
    logDriver = "awslogs"
    options = {
      awslogs-group         = local.p.log_group
      awslogs-region        = local.p.region
      awslogs-stream-prefix = c
    }
  } }
}

# All three tasks are ARM64: the image is built on an Apple-silicon laptop, and
# Graviton Fargate is ~20% cheaper per vCPU-hour than x86.

# api task: FastAPI + the built UI, and Redis beside it on localhost.
# On-demand, not Spot: it holds the queue, and it is what the analyst sees.
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 1024
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }
  execution_role_arn = local.p.execution_role_arn
  task_role_arn      = local.p.task_role_arn

  container_definitions = jsonencode([
    {
      name             = "api"
      image            = local.image
      essential        = true
      portMappings     = [{ containerPort = 8000 }]
      environment      = [{ name = "AML_REDIS", value = "redis://localhost:6379" }]
      secrets          = [local.dsn_secret]
      dependsOn        = [{ containerName = "redis", condition = "START" }]
      logConfiguration = local.logs["api"]
    },
    {
      # ECR Public's mirror of the official image: Docker Hub rate-limits
      # anonymous pulls per IP, and Fargate's public IPs are shared.
      name             = "redis"
      image            = "public.ecr.aws/docker/library/redis:7-alpine"
      essential        = true
      portMappings     = [{ containerPort = 6379 }]
      command          = ["redis-server", "--save", "", "--appendonly", "no"]
      logConfiguration = local.logs["redis"]
    },
  ])
}

# worker task: LangGraph + the GAT. 6GB because the graph alone is 2.16GB
# resident, beside torch and the 0.7GB embedding model.
resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 6144
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }
  execution_role_arn = local.p.execution_role_arn
  task_role_arn      = local.p.task_role_arn

  container_definitions = jsonencode([{
    name      = "worker"
    image     = local.image
    essential = true
    command   = ["arq", "api.worker.WorkerSettings"]
    environment = concat(local.llm_env, [
      { name = "AML_REDIS", value = "redis://${local.redis_host}:6379" },
      { name = "AML_GRAPH_S3", value = "s3://${local.p.artifacts_bucket}/${var.graph_key}" },
      { name = "AML_MAX_JOBS", value = "1" },
    ])
    secrets = [
      local.dsn_secret,
      { name = "AML_LLM_API_KEY", valueFrom = local.p.llm_api_key_arn },
    ]
    logConfiguration = local.logs["worker"]
  }])
}

# seed task: one-shot, run by hand after apply (outputs.seed_command).
resource "aws_ecs_task_definition" "seed" {
  family                   = "${local.name}-seed"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }
  execution_role_arn = local.p.execution_role_arn
  task_role_arn      = local.p.task_role_arn

  container_definitions = jsonencode([{
    name             = "seed"
    image            = local.image
    essential        = true
    command          = ["python", "-m", "scripts.load_seed"]
    secrets          = [local.dsn_secret]
    logConfiguration = local.logs["seed"]
  }])
}

resource "aws_ecs_service" "api" {
  name            = "api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # Public IP because there is no NAT (persistent/main.tf): it is how the task
  # reaches ECR to pull its own image. The security group admits only the ALB.
  network_configuration {
    subnets          = local.p.public_subnet_ids
    security_groups  = [local.p.tasks_sg_id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }
  service_registries {
    registry_arn = aws_service_discovery_service.api.arn
  }
  depends_on = [aws_lb_listener.http]
}

resource "aws_ecs_service" "worker" {
  name            = "worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = 1

  # Spot because run state lives in the Postgres checkpointer (section 8), not
  # in the task: a reclaimed worker loses its process, not the case.
  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
  }
  network_configuration {
    subnets          = local.p.public_subnet_ids
    security_groups  = [local.p.tasks_sg_id]
    assign_public_ip = true
  }
  depends_on = [aws_ecs_cluster_capacity_providers.main]
}

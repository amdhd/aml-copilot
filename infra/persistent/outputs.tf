# Read by infra/ephemeral through terraform_remote_state.

output "region" { value = var.region }
output "vpc_id" { value = aws_vpc.main.id }
output "public_subnet_ids" { value = aws_subnet.public[*].id }
output "private_subnet_ids" { value = aws_subnet.private[*].id }

output "alb_sg_id" { value = aws_security_group.alb.id }
output "tasks_sg_id" { value = aws_security_group.tasks.id }
output "db_sg_id" { value = aws_security_group.db.id }

output "ecr_repository_url" { value = aws_ecr_repository.app.repository_url }
output "artifacts_bucket" { value = aws_s3_bucket.artifacts.bucket }
output "log_group" { value = aws_cloudwatch_log_group.app.name }
output "llm_api_key_arn" { value = aws_ssm_parameter.llm_api_key.arn }
output "reviewers_arn" { value = aws_ssm_parameter.reviewers.arn }

output "execution_role_arn" { value = aws_iam_role.execution.arn }
output "task_role_arn" { value = aws_iam_role.task.arn }

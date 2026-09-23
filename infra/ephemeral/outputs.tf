output "url" {
  value = "http://${aws_lb.main.dns_name}"
}

# One-shot: load the seed into the fresh RDS. Run once after each apply.
output "seed_command" {
  value = join(" ", [
    "aws ecs run-task --cluster ${aws_ecs_cluster.main.name}",
    "--task-definition ${aws_ecs_task_definition.seed.family}",
    "--launch-type FARGATE",
    "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", local.p.public_subnet_ids)}],securityGroups=[${local.p.tasks_sg_id}],assignPublicIp=ENABLED}'",
  ])
}

output "url" {
  value = "http://${aws_lb.main.dns_name}"
}

# For scripts/demo.sh, which runs the one-shot seed task against them.
output "cluster" {
  value = aws_ecs_cluster.main.name
}

output "network_configuration" {
  value = "awsvpcConfiguration={subnets=[${join(",", local.p.public_subnet_ids)}],securityGroups=[${local.p.tasks_sg_id}],assignPublicIp=ENABLED}"
}

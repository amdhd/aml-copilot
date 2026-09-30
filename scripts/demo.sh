#!/bin/sh
# Plan section 9's demo lifecycle in three commands. The image tag is the git
# commit, so what runs is always something that can be checked out.
#
#   make push        build and push this commit's image to ECR
#   make demo-up     apply the ephemeral layer, seed RDS, wait until it serves
#   make demo-down   destroy it and check nothing is left billing
#
# demo-up and demo-down change billed AWS resources; each one says so and
# asks before it starts.
set -eu

cd "$(dirname "$0")/.."
TF="terraform -chdir=infra/ephemeral"
TAG=$(git rev-parse --short HEAD)
REPO=$(terraform -chdir=infra/persistent output -raw ecr_repository_url)
REGION=$(terraform -chdir=infra/persistent output -raw region)
export AWS_REGION="$REGION"

confirm() {
  printf '%s [y/N] ' "$1"
  read -r answer
  [ "$answer" = y ] || { echo "stopped"; exit 1; }
}

push() {
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "uncommitted changes: the image would not match $TAG. Commit first."
    exit 1
  fi
  # Tags are immutable in ECR, and this commit's image is already this commit.
  if aws ecr describe-images --repository-name aml-copilot \
       --image-ids imageTag="$TAG" >/dev/null 2>&1; then
    echo "$TAG is already in ECR"
    return
  fi
  # --provenance=false: one manifest per tag, which the ECR lifecycle rule
  # (infra/persistent) counts correctly.
  docker build --provenance=false --sbom=false -t "$REPO:$TAG" .
  aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}"
  docker push "$REPO:$TAG"
}

up() {
  if ! aws ecr describe-images --repository-name aml-copilot \
       --image-ids imageTag="$TAG" >/dev/null 2>&1; then
    echo "no image $TAG in ECR -- run make push first"
    exit 1
  fi
  # The ALB admits the address this runs from, for this demo only. EXTRA_CIDRS
  # adds others, comma-separated, e.g. an interviewer's: EXTRA_CIDRS=198.51.100.4/32
  ip=$(curl -sf https://checkip.amazonaws.com)
  echo "$ip" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' \
    || { echo "could not determine this machine's public IPv4 address"; exit 1; }
  cidrs="\"$ip/32\""
  for extra in $(echo "${EXTRA_CIDRS:-}" | tr ',' ' '); do cidrs="$cidrs,\"$extra\""; done

  confirm "Create the demo stack for $TAG, reachable from [$cidrs]? It bills ~\$0.11/hr until demo-down."
  $TF init -input=false >/dev/null
  $TF apply -input=false -auto-approve -var image_tag="$TAG" -var "allowed_cidrs=[$cidrs]"

  url=$($TF output -raw url)
  cluster=$($TF output -raw cluster)
  network=$($TF output -raw network_configuration)

  echo "seeding RDS..."
  task=$(aws ecs run-task --cluster "$cluster" --task-definition aml-copilot-seed \
    --launch-type FARGATE --network-configuration "$network" \
    --query 'tasks[0].taskArn' --output text)
  aws ecs wait tasks-stopped --cluster "$cluster" --tasks "$task"
  code=$(aws ecs describe-tasks --cluster "$cluster" --tasks "$task" \
    --query 'tasks[0].containers[0].exitCode' --output text)
  [ "$code" = 0 ] || { echo "seed task exited $code -- see /ecs/aml-copilot, stream seed/"; exit 1; }

  echo "waiting for the API..."
  i=0
  # /healthz: everything else is behind the reviewer login (api/auth.py).
  until curl -sf -o /dev/null "$url/healthz"; do
    i=$((i + 1)); [ $i -lt 60 ] || { echo "API not up after 5 min"; exit 1; }
    sleep 5
  done

  echo "waiting for the worker..."
  i=0
  until aws logs filter-log-events --log-group-name /ecs/aml-copilot \
        --log-stream-name-prefix worker --filter-pattern '"worker ready"' \
        --start-time $(( ($(date +%s) - 900) * 1000 )) \
        --query 'events[0].message' --output text | grep -q ready; do
    i=$((i + 1)); [ $i -lt 60 ] || { echo "worker not ready after 5 min"; exit 1; }
    sleep 5
  done
  # A replaced worker is how the start-up race showed itself (api/worker.py).
  restarts=$(aws ecs list-tasks --cluster "$cluster" --service-name worker \
    --desired-status STOPPED --query 'length(taskArns)' --output text)
  echo "worker ready ($restarts restarts)"
  echo
  echo "  $url"
  echo
  echo "Run make demo-down when finished."
}

down() {
  confirm "Destroy the demo stack?"
  $TF init -input=false >/dev/null
  # image_tag and allowed_cidrs are required by the config but play no part in
  # a destroy.
  $TF destroy -input=false -auto-approve -var image_tag="$TAG" -var 'allowed_cidrs=[]'

  # Section 9: check, rather than trust, that nothing is left billing. By name,
  # not by tag: deregistered task definitions keep their tags forever.
  left=""
  aws rds describe-db-instances --db-instance-identifier aml-copilot >/dev/null 2>&1 \
    && left="$left rds"
  aws elbv2 describe-load-balancers --names aml-copilot >/dev/null 2>&1 \
    && left="$left alb"
  [ "$(aws ecs describe-clusters --clusters aml-copilot \
        --query 'clusters[?status==`ACTIVE`] | length(@)' --output text)" = 0 ] \
    || left="$left ecs-cluster"
  [ "$(aws ec2 describe-addresses --query 'length(Addresses)' --output text)" = 0 ] \
    || left="$left elastic-ip"
  if [ -n "$left" ]; then
    echo "LEFT BEHIND:$left -- check the console"
    exit 1
  fi
  echo "destroyed; no RDS, ALB, ECS cluster or elastic IP remains"
}

case "${1:-}" in
  push) push ;;
  up) up ;;
  down) down ;;
  *) echo "usage: $0 push|up|down"; exit 2 ;;
esac

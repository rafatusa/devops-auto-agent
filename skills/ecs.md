# Amazon ECS (Fargate) Deployment Skill

## Overview
Deploy containerized apps on ECS Fargate using Terraform + GitHub Actions.
No EC2, no Ansible — just ECR + ECS + ALB.

## Files needed
- terraform/main.tf         — ECR repo + ECS cluster + task + service + ALB + security groups
- Dockerfile                — app container
- .github/workflows/deploy.yml  — build image → push ECR → update ECS service
- .github/workflows/destroy.yml — terraform destroy

## Terraform pattern (ECS Fargate)
```hcl
provider "aws" {
  region = var.aws_region
}

variable "aws_region"    { description = "AWS region" }
variable "tf_state_bucket" { description = "S3 bucket for terraform state" }
variable "project_name"  {}
# Bootstrap image — used ONLY by Terraform on first apply.
# The pipeline overwrites this immediately after pushing to ECR.
# Using a public Docker Hub image avoids chicken-and-egg (ECR empty on first deploy).
variable "bootstrap_image" { default = "nginx:alpine" }
variable "task_cpu"      { default = "256" }
variable "task_memory"   { default = "512" }

# ECR
resource "aws_ecr_repository" "app" {
  name                 = var.project_name
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

# ECS Cluster
resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-cluster"
}

# VPC (use default)
data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" {
  filter { name = "vpc-id" values = [data.aws_vpc.default.id] }
}

# Security Groups
resource "aws_security_group" "alb" {
  name   = "${var.project_name}-alb-sg"
  vpc_id = data.aws_vpc.default.id
  ingress { from_port = 80  to_port = 80  protocol = "tcp" cidr_blocks = ["0.0.0.0/0"] }
  egress  { from_port = 0   to_port = 0   protocol = "-1"  cidr_blocks = ["0.0.0.0/0"] }
}

resource "aws_security_group" "ecs" {
  name   = "${var.project_name}-ecs-sg"
  vpc_id = data.aws_vpc.default.id
  ingress { from_port = 80  to_port = 80  protocol = "tcp" security_groups = [aws_security_group.alb.id] }
  egress  { from_port = 0   to_port = 0   protocol = "-1"  cidr_blocks = ["0.0.0.0/0"] }
}

# ALB
resource "aws_lb" "main" {
  name               = "${var.project_name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.default.ids
}

resource "aws_lb_target_group" "app" {
  name        = "${var.project_name}-tg"
  port        = 80
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.default.id
  target_type = "ip"
  health_check { path = "/" healthy_threshold = 2 }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# IAM
resource "aws_iam_role" "ecs_task" {
  name = "${var.project_name}-ecs-task-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow" Principal = { Service = "ecs-tasks.amazonaws.com" } Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task" {
  role       = aws_iam_role.ecs_task.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task Definition
resource "aws_ecs_task_definition" "app" {
  family                   = var.project_name
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_task.arn
  container_definitions = jsonencode([{
    name      = var.project_name
    image     = var.bootstrap_image  # pipeline updates this to ECR image after first push
    essential = true
    portMappings = [{ containerPort = 80 protocol = "tcp" }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/ecs/${var.project_name}"
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }
  }])
}

# CloudWatch log group
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.project_name}"
  retention_in_days = 7
}

# ECS Service
resource "aws_ecs_service" "app" {
  name            = "${var.project_name}-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = 0   # pipeline sets to 1 after first image push
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = var.project_name
    container_port   = 80
  }
  depends_on = [aws_lb_listener.http]
}

output "alb_url" {
  value = "http://${aws_lb.main.dns_name}"
}
```

## Pipeline pattern (ECS deploy)
```yaml
jobs:
  build-and-push:
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
      - uses: aws-actions/amazon-ecr-login@v2
      - name: Build and push
        run: |
          IMAGE="${{ steps.login-ecr.outputs.registry }}/${{ secrets.PROJECT_NAME }}:${{ github.sha }}"
          docker build -t $IMAGE .
          docker push $IMAGE
          echo "image=$IMAGE" >> $GITHUB_OUTPUT

  deploy:
    needs: build-and-push
    steps:
      - name: Update ECS service
        run: |
          aws ecs update-service \
            --cluster ${{ secrets.PROJECT_NAME }}-cluster \
            --service ${{ secrets.PROJECT_NAME }}-service \
            --task-definition "$NEW_ARN" \
            --desired-count 1 \
            --force-new-deployment
```

## Key Rules
- ECS uses ALB DNS name as URL (not EC2 IP)
- No SSH keys needed — no EC2
- No Ansible — ECS pulls from ECR directly
- Always use FARGATE launch type
- Always use awsvpc network mode
- Task CPU/memory: 256/512 for small apps
- Use default VPC and subnets for simplicity
- ECR repo must exist before pipeline runs (terraform creates it)
- Secrets needed: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, PROJECT_NAME
- Output: alb_url from terraform

## CRITICAL: Single File Rule
- ALL terraform code goes in ONE file: `terraform/main.tf`
- NEVER create separate `outputs.tf`, `variables.tf`, or `providers.tf`
- Duplicate output names will cause `terraform init` to fail immediately

## CRITICAL: Bootstrap image rule — NEVER use ECR :latest in Terraform

**The chicken-and-egg problem:**
Terraform creates the ECS task definition on first apply. At that moment, ECR is empty —
no image has been pushed yet. If the task def points to `ECR_URL:latest`, ECS immediately
tries to pull it, fails with `CannotPullContainerError: not found`, and the service enters
a crash loop that looks like a pipeline bug.

**The fix — always use a public Docker Hub image as bootstrap:**
```hcl
# CORRECT — bootstrap with public image, pipeline overwrites after first push
variable "bootstrap_image" { default = "nginx:alpine" }

resource "aws_ecs_task_definition" "app" {
  container_definitions = jsonencode([{
    image = var.bootstrap_image   # pipeline updates this via register-task-definition
  }])
}
```

```hcl
# WRONG — ECR is empty on first Terraform apply, ECS will crash loop
variable "image_tag" { default = "latest" }
image = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
```

**The pipeline then takes over:**
After `docker push` succeeds, `deploy-ecs` job calls `aws ecs register-task-definition`
with the real ECR image URI (SHA tag), then `aws ecs update-service` to point to it.
Terraform never touches the image again after the first apply.

## CRITICAL: ECS service must not desired_count=0 on first apply

Set `desired_count = 0` in Terraform so ECS doesn't try to start tasks before the pipeline
pushes the real image. The pipeline sets it to 1 via `update-service --desired-count 1`.

```hcl
resource "aws_ecs_service" "app" {
  desired_count = 0   # pipeline sets to 1 after first image push
}
```
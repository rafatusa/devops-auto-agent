# Terraform AWS Best Practices

## Backend — ALWAYS use S3 for state
```hcl
terraform {
  backend "s3" {
    bucket         = "devops-agent-tfstate"
    key            = "${var.project_name}/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
  }
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
```

## Provider
```hcl
provider "aws" {
  region = var.aws_region
}
```

## Variables — always define these
```hcl
variable "project_name" {
  type    = string
}
variable "aws_region" {
  type    = string
  default = "us-east-1"
}
variable "public_key" {
  type = string
}
variable "instance_type" {
  type    = string
  default = "t3.micro"
}
```

## Data sources — use existing VPC, never create
```hcl
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "availabilityZone"
    values = ["${var.aws_region}a", "${var.aws_region}b", "${var.aws_region}c"]
  }
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}
```

## Key pair — with lifecycle to avoid duplicate errors
```hcl
resource "aws_key_pair" "deployer" {
  key_name   = "${var.project_name}-key"
  public_key = var.public_key
  lifecycle {
    ignore_changes = [public_key]
  }
}
```

## Security group — with create_before_destroy
```hcl
resource "aws_security_group" "sg" {
  name        = "${var.project_name}-sg"
  description = "DevOps Agent managed"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  lifecycle {
    create_before_destroy = true
  }
  tags = {
    Project   = var.project_name
    ManagedBy = "devops-agent"
  }
}
```

## EC2 instance
```hcl
resource "aws_instance" "server" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  key_name               = aws_key_pair.deployer.key_name
  subnet_id              = tolist(data.aws_subnets.default.ids)[0]
  vpc_security_group_ids = [aws_security_group.sg.id]

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  tags = {
    Name      = var.project_name
    Project   = var.project_name
    ManagedBy = "devops-agent"
  }
}
```

## Output
```hcl
output "public_ip" {
  value = aws_instance.server.public_ip
}
output "instance_id" {
  value = aws_instance.server.id
}
```

## CRITICAL RULES
- ALL filter blocks MUST be multi-line — never single line
- NEVER create VPC or subnets — always use data sources
- NEVER hardcode availability_zone — let AWS pick via subnet_id only
- ALWAYS filter subnets to zones a, b, c — avoid zones d/e/f which may not support all instance types
- ALWAYS use lifecycle ignore_changes on key_pair
- ALWAYS use create_before_destroy on security_group
- ALWAYS use S3 backend with encrypt=true
- Tag every resource with Project and ManagedBy

## Destroy pipeline — variable handling
- Always pass all variables even for destroy
- Use `-var="public_key=placeholder"` for destroy since key is not needed
- Add `|| true` after destroy to prevent false failures on already-deleted resources
- Use `-target` to destroy specific resources if full destroy fails


## CRITICAL: Single File Rule
- ALL terraform code goes in ONE file: `terraform/main.tf`
- NEVER create separate `outputs.tf`, `variables.tf`, or `providers.tf`
- Outputs defined in `main.tf` must NOT be repeated anywhere else
- Duplicate output definitions will cause `terraform init` to fail
# GitHub Actions Pipeline Best Practices

## CRITICAL: Pipeline triggers — deploy.yml must NEVER run on fix/destroy commits

```yaml
on:
  push:
    branches: [ main ]
    paths-ignore:
      - 'README.md'       # bot updates README on every deploy — don't retrigger
      - '.gitignore'
  workflow_dispatch:      # always allow manual trigger

# Prevent deploy from running when destroy pushes a fix commit
# by filtering commit messages in the first job:
jobs:
  provision:
    runs-on: ubuntu-latest
    # Skip if commit message contains fix:, destroy, docs:, chore:
    if: |
      !contains(github.event.head_commit.message, 'Fix destroy') &&
      !contains(github.event.head_commit.message, 'fix: clean') &&
      !contains(github.event.head_commit.message, 'fix: update terraform backend for destroy') &&
      !contains(github.event.head_commit.message, 'docs: update README') &&
      !startsWith(github.event.head_commit.message, 'chore:')
```

**Rule: ALWAYS add the `if:` guard on the first job. Without it, every fix commit pushed during destroy triggers a new deploy.**

## Structure — always these jobs in order
1. provision  — terraform (skip if EC2 exists)
2. configure  — ansible
3. verify     — health check
4. notify     — print live URL

## EC2 existence check — always add before terraform
```yaml
- name: Check existing EC2
  id: check_ec2
  run: |
    EXISTING_IP=$(aws ec2 describe-instances \
      --filters "Name=tag:Project,Values=${{ secrets.PROJECT_NAME }}" \
                "Name=instance-state-name,Values=running" \
      --query "Reservations[0].Instances[0].PublicIpAddress" \
      --output text 2>/dev/null || echo "None")
    if [ "$EXISTING_IP" != "None" ] && [ "$EXISTING_IP" != "null" ] && [ -n "$EXISTING_IP" ]; then
      echo "exists=true"  >> $GITHUB_OUTPUT
      echo "ip=$EXISTING_IP" >> $GITHUB_OUTPUT
    else
      echo "exists=false" >> $GITHUB_OUTPUT
    fi
```

## Terraform steps — conditional on EC2 not existing
```yaml
- name: Terraform Init
  if: steps.check_ec2.outputs.exists != 'true'
  run: |
    terraform init \
      -backend-config="bucket=${{ secrets.TF_STATE_BUCKET }}" \
      -backend-config="region=${{ secrets.AWS_REGION }}"
  working-directory: terraform

- name: Terraform Plan
  if: steps.check_ec2.outputs.exists != 'true'
  run: |
    terraform plan \
      -var="public_key=${{ secrets.SSH_PUBLIC_KEY }}" \
      -var="project_name=${{ secrets.PROJECT_NAME }}" \
      -var="aws_region=${{ secrets.AWS_REGION }}" \
      -out=tfplan
  working-directory: terraform

- name: Terraform Apply
  if: steps.check_ec2.outputs.exists != 'true'
  run: terraform apply -auto-approve tfplan
  working-directory: terraform
```

## Get IP — handle both existing and new EC2
```yaml
- name: Get IP
  id: get_ip
  run: |
    if [ "${{ steps.check_ec2.outputs.exists }}" == "true" ]; then
      echo "ip=${{ steps.check_ec2.outputs.ip }}" >> $GITHUB_OUTPUT
    else
      echo "ip=$(terraform output -raw public_ip)" >> $GITHUB_OUTPUT
    fi
  working-directory: terraform
```

## SSH wait — always wait before ansible
```yaml
- name: Wait for SSH
  run: |
    for i in $(seq 1 30); do
      ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
        -i /tmp/deploy_key ${{ secrets.SSH_USER || 'ubuntu' }}@${{ needs.provision.outputs.server_ip }} echo ok \
        && break || sleep 10
    done
```

## S3 bucket creation — before terraform init
Always use secrets.TF_STATE_BUCKET and secrets.AWS_REGION — never hardcode bucket name or region.

CRITICAL: Never use `|| true` or `2>/dev/null` on bucket creation — silent failures cause terraform init to fail.
The bucket MUST exist and be confirmed before terraform init runs.

```yaml
- name: Create S3 state bucket
  if: steps.check_ec2.outputs.exists != 'true'
  run: |
    BUCKET="${{ secrets.TF_STATE_BUCKET }}"
    REGION="${{ secrets.AWS_REGION }}"

    # Check if bucket already exists
    if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
      echo "✅ Bucket already exists: $BUCKET"
      exit 0
    fi

    echo "Creating bucket: $BUCKET in $REGION"
    if [ "$REGION" = "us-east-1" ]; then
      aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
    else
      aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"         --create-bucket-configuration LocationConstraint="$REGION"
    fi

    # Wait and confirm — do NOT proceed if bucket doesn't exist
    echo "Waiting for bucket to be ready..."
    for i in $(seq 1 12); do
      if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
        echo "✅ Bucket ready: $BUCKET"
        exit 0
      fi
      echo "  attempt $i/12 — waiting 5s..."
      sleep 5
    done

    echo "❌ Bucket not ready after 60s — aborting"
    exit 1
```

**Rule: Always `exit 1` if bucket creation fails — never swallow the error with `|| true`. Terraform init WILL fail if the bucket doesn't exist.**

## Secrets always needed
- AWS_ACCESS_KEY_ID
- AWS_SECRET_ACCESS_KEY
- AWS_REGION
- SSH_PRIVATE_KEY
- SSH_PUBLIC_KEY
- PROJECT_NAME
- TF_STATE_BUCKET  ← account-specific S3 bucket name, never hardcode this
- SSH_USER         ← default: ubuntu

## Concurrency — prevent parallel runs
```yaml
concurrency:
  group: deploy-${{ github.repository }}
  cancel-in-progress: false
```

## Always check ALL existing resources before terraform
```yaml
- name: Check existing AWS resources
  id: check_ec2
  run: |
    PROJECT="${{ secrets.PROJECT_NAME }}"
    EXISTING_IP=$(aws ec2 describe-instances \
      --filters "Name=tag:Project,Values=$PROJECT" \
                "Name=instance-state-name,Values=running" \
      --query "Reservations[0].Instances[0].PublicIpAddress" \
      --output text 2>/dev/null || echo "None")
    KEY_EXISTS=$(aws ec2 describe-key-pairs \
      --filters "Name=key-name,Values=$PROJECT-key" \
      --query "KeyPairs[0].KeyName" --output text 2>/dev/null || echo "None")
    SG_ID=$(aws ec2 describe-security-groups \
      --filters "Name=group-name,Values=$PROJECT-sg" \
      --query "SecurityGroups[0].GroupId" --output text 2>/dev/null || echo "None")
    [ "$EXISTING_IP" != "None" ] && echo "exists=true" >> $GITHUB_OUTPUT || echo "exists=false" >> $GITHUB_OUTPUT
    echo "key_exists=$KEY_EXISTS" >> $GITHUB_OUTPUT
    echo "sg_id=$SG_ID" >> $GITHUB_OUTPUT
```

## Import existing resources before terraform plan
```yaml
- name: Import existing resources
  if: steps.check_ec2.outputs.exists != 'true'
  run: |
    KEY="${{ steps.check_ec2.outputs.key_exists }}"
    SG="${{ steps.check_ec2.outputs.sg_id }}"
    [ "$KEY" != "None" ] && terraform import -var="public_key=${{ secrets.SSH_PUBLIC_KEY }}" aws_key_pair.deployer "${{ secrets.PROJECT_NAME }}-key" 2>/dev/null || true
    [ "$SG" != "None" ] && terraform import -var="public_key=${{ secrets.SSH_PUBLIC_KEY }}" aws_security_group.sg "$SG" 2>/dev/null || true
  working-directory: terraform
```

## destroy.yml — critical rules

**terraform destroy does NOT need -var flags.** It reads existing state. Passing vars not declared in main.tf causes destroy to fail immediately.

```yaml
# CORRECT
- name: Terraform destroy
  run: terraform destroy -auto-approve

# WRONG — causes "Value for undeclared variable" error
- name: Terraform destroy
  run: terraform destroy -auto-approve -var="public_key=placeholder" -var="project_name=..."
```

**Never use `|| true` after `terraform destroy`** — it hides real failures and reports success to the bot even when resources were NOT destroyed.

```yaml
# CORRECT — fail visibly
- run: terraform destroy -auto-approve

# WRONG — hides failures
- run: terraform destroy -auto-approve || true
```

**destroy.yml trigger must be `workflow_dispatch` only** — never `on: push`. Destroy should never run automatically.
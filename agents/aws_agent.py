"""
AWS Agent — pure boto3, no AI
Handles: EC2 check, SSH key generation, SSM, S3 bucket
"""
import os
import json
import subprocess
import logging
from pathlib import Path

import boto3

logger = logging.getLogger(__name__)


class AWSAgent:

    def __init__(self):
        self.region = os.getenv("AWS_REGION", "us-east-1")

    def _ec2(self):
        return boto3.client("ec2", region_name=self.region)

    def _ssm(self):
        return boto3.client("ssm", region_name=self.region)

    def _s3(self):
        return boto3.client("s3", region_name=self.region)

    # ── EC2 ───────────────────────────────────────────────────────────────────

    def check_ec2(self, project: str) -> dict:
        """Check if EC2 exists for project. Returns {exists, ip, instance_id}"""
        try:
            r = self._ec2().describe_instances(Filters=[
                {"Name": "tag:Project",         "Values": [project]},
                {"Name": "instance-state-name", "Values": ["running", "pending"]},
            ])
            instances = []
            for res in r["Reservations"]:
                for i in res["Instances"]:
                    instances.append({
                        "instance_id": i["InstanceId"],
                        "ip":          i.get("PublicIpAddress", ""),
                        "launch_time": str(i["LaunchTime"]),
                    })

            if not instances:
                return {"exists": False}

            # Keep newest, terminate duplicates
            instances.sort(key=lambda x: x["launch_time"])
            keep = instances[-1]
            if len(instances) > 1:
                kill = [i["instance_id"] for i in instances[:-1]]
                self._ec2().terminate_instances(InstanceIds=kill)
                logger.info(f"Terminated duplicate instances: {kill}")

            return {"exists": True, "ip": keep["ip"], "instance_id": keep["instance_id"]}

        except Exception as e:
            logger.error(f"check_ec2 error: {e}")
            return {"exists": False, "error": str(e)}

    def terminate_ec2(self, project: str) -> dict:
        """Terminate all EC2 instances for project."""
        try:
            r = self._ec2().describe_instances(Filters=[
                {"Name": "tag:Project",         "Values": [project]},
                {"Name": "instance-state-name", "Values": ["running", "pending", "stopped"]},
            ])
            ids = []
            for res in r["Reservations"]:
                for i in res["Instances"]:
                    ids.append(i["InstanceId"])

            if ids:
                self._ec2().terminate_instances(InstanceIds=ids)
                return {"terminated": ids}
            return {"terminated": []}
        except Exception as e:
            return {"error": str(e)}

    # ── SSH Keys ──────────────────────────────────────────────────────────────

    def generate_ssh_key(self, project: str) -> dict:
        """Generate RSA SSH key pair. Returns {public_key, private_key}"""
        try:
            key_dir  = Path(f"/tmp/devops-agent/{project}")
            key_dir.mkdir(parents=True, exist_ok=True)
            key_path = key_dir / "ssh_key"

            # Remove old keys
            key_path.unlink(missing_ok=True)
            Path(str(key_path) + ".pub").unlink(missing_ok=True)

            r = subprocess.run(
                f"ssh-keygen -t rsa -b 2048 -f {key_path} -N '' -q",
                shell=True, capture_output=True, text=True, timeout=30
            )
            if r.returncode != 0:
                raise Exception(f"ssh-keygen failed: {r.stderr}")

            private_key = key_path.read_text()
            public_key  = Path(str(key_path) + ".pub").read_text().strip()

            # Store in SSM
            self._store_ssm(f"/devops-agent/{project}/ssh-private", private_key)
            self._store_ssm(f"/devops-agent/{project}/ssh-public",  public_key)

            logger.info(f"SSH key generated for {project}")
            return {"public_key": public_key, "private_key": private_key}

        except Exception as e:
            logger.error(f"generate_ssh_key error: {e}")
            return {"error": str(e)}

    def get_ssh_keys(self, project: str) -> dict:
        """Retrieve SSH keys from SSM."""
        private = self._get_ssm(f"/devops-agent/{project}/ssh-private")
        public  = self._get_ssm(f"/devops-agent/{project}/ssh-public")
        if private and public:
            return {"private_key": private, "public_key": public}
        # Try local files
        key_path = Path(f"/tmp/devops-agent/{project}/ssh_key")
        if key_path.exists():
            return {
                "private_key": key_path.read_text(),
                "public_key":  Path(str(key_path) + ".pub").read_text().strip(),
            }
        return {"error": "SSH keys not found"}

    # ── S3 ────────────────────────────────────────────────────────────────────

    def get_account_id(self) -> str:
        """Get the current AWS account ID via STS."""
        try:
            sts = boto3.client(
                "sts",
                aws_access_key_id     = self.access_key,
                aws_secret_access_key = self.secret_key,
                region_name           = self.region,
            )
            return sts.get_caller_identity()["Account"]
        except Exception as e:
            logger.warning(f"Could not get AWS account ID: {e}")
            return "unknown"

    def get_state_bucket_name(self) -> str:
        """
        Generate a bucket name that is unique per AWS account.
        Uses account ID suffix so different accounts never share a bucket.
        If TF_STATE_BUCKET env var is set explicitly, use that instead.
        """
        explicit = os.getenv("TF_STATE_BUCKET")
        if explicit:
            return explicit
        account_id = self.get_account_id()
        # Bucket names must be lowercase, 3-63 chars, no underscores
        # Format: devops-tfstate-<last8ofaccountid>
        suffix = account_id[-8:] if account_id != "unknown" else "default"
        return f"devops-tfstate-{suffix}"

    def ensure_s3_bucket(self, bucket: str = None) -> dict:
        """Create S3 bucket for terraform state if it doesn't exist."""
        bucket = bucket or self.get_state_bucket_name()
        try:
            s3 = self._s3()
            try:
                s3.head_bucket(Bucket=bucket)
                return {"exists": True, "bucket": bucket}
            except Exception:
                if self.region == "us-east-1":
                    s3.create_bucket(Bucket=bucket)
                else:
                    s3.create_bucket(
                        Bucket=bucket,
                        CreateBucketConfiguration={"LocationConstraint": self.region}
                    )
                logger.info(f"Created S3 bucket: {bucket}")
                return {"created": True, "bucket": bucket}
        except Exception as e:
            return {"error": str(e)}

    def delete_s3_state(self, project: str, bucket: str = None) -> dict:
        """Delete terraform state from S3."""
        bucket = bucket or self.get_state_bucket_name()
        try:
            key = f"{project}/terraform.tfstate"
            self._s3().delete_object(Bucket=bucket, Key=key)
            return {"deleted": key}
        except Exception as e:
            return {"error": str(e)}

    def clear_tf_state(self, project: str, bucket: str = None) -> dict:
        """
        Delete ALL terraform state objects for a project from S3.
        Clears: tfstate, tfstate.backup, lock file.
        """
        bucket = bucket or self.get_state_bucket_name()
        s3     = self._s3()
        keys   = [
            f"{project}/terraform.tfstate",
            f"{project}/terraform.tfstate.backup",
            f"{project}/.terraform.lock.hcl",
        ]
        deleted = []
        errors  = []
        for key in keys:
            try:
                s3.delete_object(Bucket=bucket, Key=key)
                deleted.append(key)
                logger.info(f"Deleted S3 object: s3://{bucket}/{key}")
            except Exception as e:
                errors.append(f"{key}: {e}")

        # Also check for any other objects under project prefix
        try:
            resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"{project}/")
            for obj in resp.get("Contents", []):
                key = obj["Key"]
                if key not in keys:
                    s3.delete_object(Bucket=bucket, Key=key)
                    deleted.append(key)
        except Exception as e:
            errors.append(f"list: {e}")

        return {"deleted": deleted, "errors": errors, "bucket": bucket, "project": project}

    def nuke_s3_bucket(self, bucket: str = None) -> dict:
        """
        Delete ALL objects in the TF state bucket, then delete the bucket itself.
        WARNING: This removes ALL projects' state.
        """
        bucket = bucket or self.get_state_bucket_name()
        s3     = self._s3()
        deleted_objects = []
        try:
            # Delete all objects (including all versions if versioned)
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket):
                objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if objs:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
                    deleted_objects.extend([o["Key"] for o in objs])

            # Delete versioned objects if versioning is enabled
            try:
                ver_paginator = s3.get_paginator("list_object_versions")
                for page in ver_paginator.paginate(Bucket=bucket):
                    versions = [
                        {"Key": v["Key"], "VersionId": v["VersionId"]}
                        for v in page.get("Versions", []) + page.get("DeleteMarkers", [])
                    ]
                    if versions:
                        s3.delete_objects(Bucket=bucket, Delete={"Objects": versions})
            except Exception:
                pass  # versioning not enabled

            s3.delete_bucket(Bucket=bucket)
            return {"status": "deleted", "bucket": bucket, "objects_deleted": len(deleted_objects)}
        except Exception as e:
            return {"error": str(e), "bucket": bucket}

    def list_tf_states(self, bucket: str = None) -> dict:
        """List all terraform state files in the S3 bucket."""
        bucket = bucket or self.get_state_bucket_name()
        try:
            s3   = self._s3()
            resp = s3.list_objects_v2(Bucket=bucket)
            keys = [o["Key"] for o in resp.get("Contents", [])]
            # Group by project
            projects = {}
            for key in keys:
                proj = key.split("/")[0]
                projects.setdefault(proj, []).append(key)
            return {"bucket": bucket, "projects": projects, "total": len(keys)}
        except Exception as e:
            return {"error": str(e)}

    # ── SSM ───────────────────────────────────────────────────────────────────

    def _store_ssm(self, key: str, value: str):
        try:
            self._ssm().put_parameter(
                Name=key, Value=value,
                Type="SecureString", Overwrite=True
            )
        except Exception as e:
            logger.warning(f"SSM store failed for {key}: {e}")

    def _get_ssm(self, key: str):
        try:
            return self._ssm().get_parameter(
                Name=key, WithDecryption=True
            )["Parameter"]["Value"]
        except Exception:
            return None

    def delete_ssm_keys(self, project: str) -> dict:
        """Delete all SSM keys for project."""
        keys = [
            f"/devops-agent/{project}/ssh-private",
            f"/devops-agent/{project}/ssh-public",
        ]
        deleted = []
        for key in keys:
            try:
                self._ssm().delete_parameter(Name=key)
                deleted.append(key)
            except Exception:
                pass
        return {"deleted": deleted}

    # ── Credentials ───────────────────────────────────────────────────────────

    def get_credentials(self) -> dict:
        """Return current AWS credentials from environment."""
        return {
            "AWS_ACCESS_KEY_ID":     os.getenv("AWS_ACCESS_KEY_ID", ""),
            "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY", ""),
            "AWS_REGION":            self.region,
        }

    def check_all_resources(self, project: str) -> dict:
        """Check ALL existing AWS resources for a project."""
        resources = {}

        # EC2
        resources["ec2"] = self.check_ec2(project)

        # Key pair
        try:
            r = self._ec2().describe_key_pairs(
                Filters=[{"Name": "key-name", "Values": [f"{project}-key"]}]
            )
            resources["key_pair"] = {
                "exists": len(r["KeyPairs"]) > 0,
                "name":   f"{project}-key",
            }
        except Exception as e:
            resources["key_pair"] = {"exists": False, "error": str(e)}

        # Security group
        try:
            r = self._ec2().describe_security_groups(
                Filters=[
                    {"Name": "group-name", "Values": [f"{project}-sg"]},
                ]
            )
            sgs = r["SecurityGroups"]
            resources["security_group"] = {
                "exists":   len(sgs) > 0,
                "group_id": sgs[0]["GroupId"] if sgs else None,
                "name":     f"{project}-sg",
            }
        except Exception as e:
            resources["security_group"] = {"exists": False, "error": str(e)}

        # S3 state
        try:
            self._s3().head_object(
                Bucket=self.get_state_bucket_name(),
                Key=f"{project}/terraform.tfstate"
            )
            resources["s3_state"] = {"exists": True}
        except Exception:
            resources["s3_state"] = {"exists": False}

        # SSM keys
        private = self._get_ssm(f"/devops-agent/{project}/ssh-private")
        resources["ssh_keys"] = {"exists": private is not None}

        return resources

    def prepare(self, project: str) -> dict:
        """
        Full preparation for a project:
        - Check ALL existing resources
        - Generate SSH key only if needed
        - Ensure S3 bucket
        Returns everything needed for github_agent to set secrets.
        """
        result = {}

        # Check all resources first
        all_resources = self.check_all_resources(project)
        result["existing"] = all_resources

        # EC2 check
        result["ec2"] = all_resources["ec2"]

        # SSH keys — reuse if exist in SSM, otherwise generate fresh
        if all_resources["ssh_keys"]["exists"]:
            keys = self.get_ssh_keys(project)
            logger.info(f"Reusing existing SSH keys for {project}")
        else:
            keys = self.generate_ssh_key(project)
            logger.info(f"Generated new SSH keys for {project}")
        result["ssh"] = keys

        # S3 bucket
        s3 = self.ensure_s3_bucket()
        result["s3"] = s3

        # Credentials
        result["credentials"] = self.get_credentials()

        return result

    def delete_key_pair(self, project: str) -> dict:
        """Delete EC2 key pair for project."""
        try:
            self._ec2().delete_key_pair(KeyName=f"{project}-key")
            return {"deleted": f"{project}-key"}
        except Exception as e:
            return {"error": str(e)}

    def delete_security_group(self, project: str) -> dict:
        """Delete security group for project."""
        try:
            self._ec2().delete_security_group(GroupName=f"{project}-sg")
            return {"deleted": f"{project}-sg"}
        except Exception as e:
            return {"error": str(e)}

    def cleanup(self, project: str) -> dict:
        """Clean up ALL AWS resources for a project."""
        results = {}
        results["ec2"] = self.terminate_ec2(project)

        # Wait a moment for EC2 to start terminating before deleting SG
        import time
        if results["ec2"].get("terminated"):
            time.sleep(5)

        results["sg"]  = self.delete_security_group(project)
        results["key"] = self.delete_key_pair(project)
        results["ssm"] = self.delete_ssm_keys(project)
        results["s3"]  = self.delete_s3_state(project)
        return results


    def handle(self, action: str, args: dict) -> dict:
        """
        Flexible standalone handler.
        Actions: prepare, check_ec2, gen_ssh_key, get_ssh_keys,
                 ensure_s3, list_ec2, cleanup, credentials
        """
        try:
            if action == "prepare":
                return self.prepare(args["project"])

            elif action == "check_ec2":
                return self.check_ec2(args["project"])

            elif action == "list_ec2":
                r = self._ec2().describe_instances(Filters=[
                    {"Name": "instance-state-name", "Values": ["running", "pending"]}
                ])
                instances = []
                for res in r["Reservations"]:
                    for i in res["Instances"]:
                        tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                        instances.append({
                            "id":      i["InstanceId"],
                            "ip":      i.get("PublicIpAddress", ""),
                            "type":    i["InstanceType"],
                            "project": tags.get("Project", ""),
                            "state":   i["State"]["Name"],
                        })
                return {"status": "ok", "instances": instances}

            elif action == "gen_ssh_key":
                return self.generate_ssh_key(args["project"])

            elif action == "get_ssh_keys":
                return self.get_ssh_keys(args["project"])

            elif action == "ensure_s3":
                bucket = args.get("bucket") or self.get_state_bucket_name()
                return self.ensure_s3_bucket(bucket)

            elif action == "credentials":
                return {"status": "ok", "credentials": self.get_credentials()}

            elif action == "cleanup":
                return self.cleanup(args["project"])

            elif action == "terminate_ec2":
                return self.terminate_ec2(args["project"])

            else:
                return {"status": "error", "error": f"Unknown action: {action}"}

        except Exception as e:
            logger.error(f"AWSAgent error: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}


aws_agent = AWSAgent()
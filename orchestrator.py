"""
Orchestrator — coordinates all agents
No AI here. Pure coordination logic.
"""
import os
import asyncio
import logging
from typing import Callable, Optional

import state
from agents.aws_agent    import aws_agent
from agents.github_agent import github_agent
from agents.code_agent   import code_agent
from agents.error_agent  import error_agent

def _patch_terraform_bucket(files: dict, correct_bucket: str, correct_region: str) -> None:
    """
    Bucket and region are passed via -backend-config flags at terraform init time.
    Removes hardcoded bucket/region from backend "s3" blocks so they don't
    conflict with the -backend-config flags the pipeline passes.
    """
    import re as _re

    def clean_backend(m):
        block = m.group(0)
        block = _re.sub(r'[ \t]*bucket[ \t]*=[ \t]*"[^"]*"\n', '', block)
        block = _re.sub(r'[ \t]*region[ \t]*=[ \t]*"[^"]*"\n', '', block)
        return block

    for path, file_content in list(files.items()):
        if not path.endswith(".tf"):
            continue
        patched = _re.sub(
            r'backend\s+"s3"\s*\{[^}]+\}',
            clean_backend,
            file_content,
            flags=_re.DOTALL,
        )
        if patched != file_content:
            files[path] = patched
            logger.info(f"_patch_terraform_bucket: cleaned backend block in {path}")



def _extract_display_error(raw_log: str) -> str:
    """Extract a clean error summary from raw combined job log for display."""
    import re
    if not raw_log:
        return "Unknown error"
    patterns = [
        r"(?i)fatal:.*",
        r"(?i)error:.*process completed.*",
        r"(?i)could not find or access.*",
        r"(?i)could not match supplied host.*",
        r"(?i)skipping: no hosts matched",
        r"(?i)permission denied.*",
        r"(?i)no such file.*",
    ]
    found = []
    for line in raw_log.splitlines():
        s = re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.Z]+ *", "", line.strip())
        s = re.sub(r"^##\[.*?\] *", "", s).strip()
        if not s or s.startswith("==="):
            continue
        for pat in patterns:
            if re.search(pat, s):
                found.append(s[:200])
                break
        if len(found) >= 2:
            break
    if found:
        return " | ".join(found)
    for line in reversed(raw_log.splitlines()):
        s = line.strip()
        if s and len(s) > 10 and "===" not in s:
            return s[:300]
    return raw_log[:300]


logger = logging.getLogger(__name__)

MAX_RETRIES         = 5
MAX_DESTROY_RETRIES = 3


class Orchestrator:

    def __init__(self):
        self._stop_flags: dict[int, bool] = {}

    # ── Stop control ──────────────────────────────────────────────────────────

    def stop(self, user_id: int):
        self._stop_flags[user_id] = True

    def resume(self, user_id: int):
        self._stop_flags[user_id] = False

    def is_stopped(self, user_id: int) -> bool:
        return self._stop_flags.get(user_id, False)

    def _check_stop(self, user_id: int):
        if self.is_stopped(user_id):
            raise StopIteration("Stopped by user")

    # ── Deploy ────────────────────────────────────────────────────────────────

    async def deploy(
        self,
        user_id:     int,
        project:     str,
        app:         str,
        repo_name:   str,
        region:      str = "us-east-1",
        branch:      str = "main",
        target:      str = "ec2",
        progress_cb: Optional[Callable] = None,
    ) -> dict:
        self.resume(user_id)
        cb = progress_cb or (lambda m: None)

        async def step(name, msg):
            self._check_stop(user_id)
            await cb(msg)
            state.log_step(project, name, "running")

        try:
            state.save_deployment(project, app, repo_name, region=region)

            # ── Check if previously deployed successfully ──────────────────────
            dep = state.get_deployment(project)
            if dep and dep.get("status") == "deployed" and dep.get("ec2_ip"):
                await cb(f"Project '{project}' was previously deployed successfully.")
                await cb(f"Skipping file generation — retriggering pipeline only...")

                cancelled = github_agent.cancel_running_pipelines(repo_name)
                if cancelled.get("cancelled"):
                    await cb(f"Cancelled {len(cancelled['cancelled'])} running pipeline(s)")
                    await asyncio.sleep(5)

                trigger_result = github_agent.trigger_pipeline(repo_name, "deploy.yml")
                if trigger_result.get("status") == "error":
                    raise Exception(f"Trigger failed: {trigger_result['error']}")
                await cb(f"Pipeline triggered: {trigger_result.get('url')}")

                pipeline = await github_agent.poll_pipeline(
                    repo_name, interval=30,
                    stop_flag=lambda: self.is_stopped(user_id),
                    progress_cb=cb,
                )
                if pipeline.get("conclusion") == "success":
                    ip = dep["ec2_ip"]
                    return {"status": "success", "ip": ip, "url": f"http://{ip}", "project": project}
                else:
                    await cb(f"Pipeline failed: {pipeline.get('run_url','')}")
                    return {"status": "failed", "message": "Pipeline failed on rerun"}

            # ── Step 1: AWS Prepare ───────────────────────────────────────────
            await step("aws_prepare", "Preparing AWS resources...")
            aws_result = aws_agent.prepare(project)

            if "error" in aws_result.get("ssh", {}):
                raise Exception(f"SSH key generation failed: {aws_result['ssh']['error']}")

            ssh_keys = aws_result["ssh"]
            creds    = aws_result["credentials"]
            ec2      = aws_result["ec2"]
            existing = aws_result.get("existing", {})

            # Report all existing resources
            ec2_status = "exists at " + ec2.get("ip", "") if ec2.get("exists") else "none"
            kp_status  = "exists" if existing.get("key_pair",        {}).get("exists") else "none"
            sg_status  = "exists" if existing.get("security_group",  {}).get("exists") else "none"
            s3_status  = "exists" if existing.get("s3_state",        {}).get("exists") else "none"
            sk_status  = "exists" if existing.get("ssh_keys",        {}).get("exists") else "none"

            state.log_step(project, "aws_prepare", "done", result=str(existing))
            await cb(
                f"AWS resources for '{project}':\n"
                f"  EC2:            {ec2_status}\n"
                f"  Key pair:       {kp_status}\n"
                f"  Security group: {sg_status}\n"
                f"  S3 state:       {s3_status}\n"
                f"  SSH keys:       {sk_status}"
            )

            # ── Step 2: GitHub Setup ──────────────────────────────────────────
            self._check_stop(user_id)
            await step("github_setup", f"Setting up GitHub repo {repo_name}...")

            repo_result = github_agent.create_repo(repo_name, f"DevOps Agent — {project}")
            await cb(f"Repo: {repo_result.get('url', repo_name)}")

            # Create branch from main FIRST — so branch inherits all existing files
            if branch != "main":
                br = github_agent.create_branch(repo_name, branch, "main")
                await cb(f"Branch '{branch}' {br.get('status', 'ready')} (from main)")

            # ── Step 3: Files ─────────────────────────────────────────────────
            await step("generate_files", f"Reading files from branch '{branch}'...")

            # Read ALL existing files from the branch
            # (if branch was just created from main, it already has all main files)
            repo_files = github_agent.get_existing_files(repo_name, branch=branch)
            await cb(f"Found {len(repo_files)} files in branch '{branch}'")

            # Context-aware generation:
            # Pass existing files so code_agent can see what's there
            # and only generate what actually needs to change for this app
            await cb(f"Analysing what needs to change for '{app}' on {target}...")

            # plan_deployment reads FULL file contents so Claude decides intelligently
            plan = code_agent.plan_deployment(project, app, region, target, repo_files)

            # Show user exactly what will change before touching anything
            if plan["keep"]:
                await cb(f"✔ Keeping unchanged: {plan['keep']}")
            if plan["update"]:
                await cb(f"✏ Updating: {plan['update']}")
            if plan["create"]:
                await cb(f"➕ Creating new: {plan['create']}")
            if plan["delete"]:
                await cb(f"🗑 Removing: {plan['delete']}")
            await cb(f"Reason: {plan['reasoning']}")

            to_generate = plan["update"] + plan["create"]

            if not to_generate and not plan["delete"]:
                await cb(f"✅ No changes needed — branch '{branch}' is already up to date")
                files_to_push = {}
            else:
                # Generate only what changed
                files_to_push = code_agent.generate_files(
                    project, app, region,
                    existing_files=repo_files,
                    target=target,
                )

            state.log_step(project, "generate_files", "done",
                           result=f"{len(files_to_push)} files to push")

            if files_to_push:
                await cb(f"Pushing {len(files_to_push)} file(s) to '{branch}'...")
                push_result = github_agent.push_files(repo_name, files_to_push, branch=branch)
                if push_result.get("failed"):
                    await cb(f"Warning: failed to push: {push_result['failed']}")
                await cb(f"Pushed: {push_result.get('pushed', [])}")

            # Delete files no longer needed
            for path in plan.get("delete", []):
                try:
                    if hasattr(github_agent, "delete_file"):
                        del_result = github_agent.delete_file(repo_name, path, branch=branch)
                        await cb(f"Deleted {path}: {del_result.get('status', 'done')}")
                    else:
                        await cb(f"Skipping delete {path} (update github_agent to enable)")
                except Exception as e:
                    await cb(f"Could not delete {path}: {e}")

            # Ensure S3 terraform state bucket exists in THIS AWS account
            # Bucket name is auto-derived from account ID — different per account
            bucket_name = aws_agent.get_state_bucket_name()
            await cb(f"Ensuring S3 state bucket '{bucket_name}' exists...")
            bucket_result = aws_agent.ensure_s3_bucket(bucket_name)
            if bucket_result.get("status") == "created":
                await cb(f"✓ Created S3 bucket: {bucket_name}")
            elif bucket_result.get("status") == "exists":
                await cb(f"✓ S3 bucket ready: {bucket_name}")
            else:
                await cb(f"⚠️ S3 bucket warning: {bucket_result.get('error','unknown')}")

            # Patch any terraform files in the branch that have a wrong/old bucket name
            # This handles the case where the branch was generated with a different account's bucket
            _patch_terraform_bucket(repo_files, bucket_name, region)

            # Set secrets — TF_STATE_BUCKET uses the account-specific bucket name
            secrets = {
                "AWS_ACCESS_KEY_ID":     creds["AWS_ACCESS_KEY_ID"],
                "AWS_SECRET_ACCESS_KEY": creds["AWS_SECRET_ACCESS_KEY"],
                "AWS_REGION":            region,
                "SSH_PRIVATE_KEY":       ssh_keys["private_key"],
                "SSH_PUBLIC_KEY":        ssh_keys["public_key"],
                "PROJECT_NAME":          project,
                "TF_STATE_BUCKET":       bucket_name,
                "SSH_USER":              os.getenv("SSH_USER", "ubuntu"),
            }
            secret_result = github_agent.set_secrets(repo_name, secrets)
            await cb(f"Set {len(secret_result.get('set', []))} secrets")
            state.log_step(project, "github_setup", "done")

            # ── Step 4: Trigger Pipeline ──────────────────────────────────────
            self._check_stop(user_id)
            await step("trigger", "Checking for running pipelines...")

            cancelled = github_agent.cancel_running_pipelines(repo_name)
            if cancelled.get("cancelled"):
                await cb(f"Cancelled {len(cancelled['cancelled'])} running pipeline(s)")
                await asyncio.sleep(5)

            await cb(f"Triggering pipeline on branch '{branch}'...")
            trigger_result = github_agent.trigger_pipeline(repo_name, "deploy.yml", branch)
            if trigger_result.get("status") == "error":
                raise Exception(f"Trigger failed: {trigger_result['error']}")
            await cb(f"Pipeline triggered: {trigger_result.get('url')}")
            state.log_step(project, "trigger", "done")

            # ── Step 5: Poll + Auto-fix loop (no approval needed) ─────────────
            retry      = 0
            last_error = "Unknown error"

            while retry <= MAX_RETRIES:
                self._check_stop(user_id)

                # Only poll on first iteration — retrigger handles subsequent ones
                if retry > 0:
                    await cb(f"Retriggering pipeline (attempt {retry}/{MAX_RETRIES})...")
                    trigger2 = github_agent.trigger_pipeline(repo_name, "deploy.yml", branch)
                    if trigger2.get("status") == "error":
                        last_error = trigger2["error"]
                        await cb(f"Retrigger failed: {last_error}")
                        break
                    await cb(f"Pipeline: {trigger2.get('url', '')}")

                await cb(f"Polling... (attempt {retry + 1}/{MAX_RETRIES + 1})")
                pipeline = await github_agent.poll_pipeline(
                    repo_name,
                    interval=30,
                    branch=branch,
                    stop_flag=lambda: self.is_stopped(user_id),
                    progress_cb=cb,
                )

                if pipeline.get("status") == "stopped":
                    state.log_step(project, "pipeline", "stopped")
                    return {"status": "stopped"}

                if pipeline.get("status") == "timeout":
                    state.log_step(project, "pipeline", "timeout")
                    return {"status": "timeout", "message": "Pipeline timed out"}

                if pipeline.get("conclusion") == "success":
                    if target == "ecs":
                        # ECS uses ALB DNS name from terraform output
                        url = self._extract_url(pipeline) or ""
                        ip  = url.replace("http://", "")
                    else:
                        ip  = self._extract_ip(pipeline)
                        if not ip and ec2.get("exists"):
                            ip = ec2.get("ip", "")
                        if not ip:
                            fresh_ec2 = aws_agent.check_ec2(project)
                            ip = fresh_ec2.get("ip", "")
                        url = f"http://{ip}" if ip else ""
                    state.update_deployment(project, status="deployed", ec2_ip=ip)
                    state.log_step(project, "pipeline", "done", result=ip)
                    return {"status": "success", "ip": ip, "url": url, "project": project}

                # Pipeline failed — capture error before checking retry limit
                analysis   = error_agent.analyze(
                    pipeline.get("failed_jobs", []),
                    all_jobs=pipeline.get("all_jobs", []),
                )
                # last_error for display — extract clean summary, not raw log
                raw_log    = analysis.get("log_context", "") or analysis.get("full_log", "") or ""
                last_error = _extract_display_error(raw_log)
                last_error_short = last_error[:300]

                if retry >= MAX_RETRIES:
                    await cb(f"Failed after {MAX_RETRIES} attempts.")
                    await cb(f"Last error: {last_error_short}")
                    await cb(f"Pipeline logs: {pipeline.get('run_url', '')}")
                    break

                retry += 1
                await cb(f"Pipeline failed — auto-fixing (attempt {retry}/{MAX_RETRIES})...")
                await cb(f"Failed job: {analysis.get('job_name', 'unknown')}")

                # Always read LIVE files from the actual branch — never trust local state
                await cb(f"Reading current files from branch '{branch}'...")
                repo_files_now = github_agent.get_existing_files(repo_name, branch=branch)

                # Sync to local state so fix_file reads the real current content
                for path, fcontent in repo_files_now.items():
                    state.save_file(project, path, fcontent)

                await cb(f"Analysing error against {len(repo_files_now)} live files...")

                # Pass live files directly so Claude sees exactly what's in the repo
                fix_result = code_agent.analyze_and_fix(
                    project,
                    analysis.get("log_context", "") or analysis.get("full_log", ""),
                    all_files=repo_files_now,
                )

                if "error" in fix_result:
                    last_error = fix_result["error"]
                    await cb(
                        f"Cannot auto-fix: {last_error}\n"
                        f"Check logs — validator may have rejected AI output.\n"
                        f"Pipeline: {pipeline.get('run_url', '')}"
                    )
                    break

                # Push ALL fixed files (may be multiple)
                all_fixes = fix_result.get("all_fixes", [fix_result])
                for fx in all_fixes:
                    fx_path    = fx.get("file") or fix_result.get("file")
                    fx_content = fx.get("content") or fx.get("fixed_content")
                    if fx_path and fx_content:
                        github_agent.push_single_file(
                            repo_name, fx_path, fx_content,
                            f"fix: {fx.get('error','')[:60]} (attempt {retry})",
                            branch=branch,
                        )
                        await cb(f"Pushed fix: {fx_path}")

                last_error = fix_result.get("error_summary", "unknown")
                await cb(
                    f"Fixed {fix_result['file']}\n"
                    f"Error was: {last_error}\n"
                    f"Change: {fix_result['diff_summary']}"
                )

                push = github_agent.push_single_file(
                    repo_name,
                    fix_result["file"],
                    fix_result["fixed_content"],
                    f"Auto-fix attempt {retry}: {fix_result['file']}",
                    branch=branch,
                )
                if push.get("failed"):
                    last_error = str(push["failed"])
                    await cb(f"Push failed: {last_error}")
                    break

                state.log_step(project, f"fix_{retry}", "done", result=fix_result["file"])
                await asyncio.sleep(3)

            state.log_step(project, "pipeline", "failed")
            state.update_deployment(project, status="failed")
            return {
                "status":  "failed",
                "message": f"Failed after {MAX_RETRIES} attempts. Last error: {last_error}",
            }

        except StopIteration:
            state.log_step(project, "stopped", "stopped")
            return {"status": "stopped"}
        except Exception as e:
            logger.error(f"Deploy error: {e}", exc_info=True)
            state.log_step(project, "error", "error", error=str(e))
            return {"status": "error", "message": str(e)}

    # ── Apply fix and retry (kept for manual use from bot) ───────────────────

    async def apply_fix_and_retry(
        self,
        user_id:       int,
        project:       str,
        repo_name:     str,
        file_path:     str,
        fixed_content: str,
        retry:         int,
        progress_cb:   Optional[Callable] = None,
    ) -> dict:
        cb = progress_cb or (lambda m: None)
        self._check_stop(user_id)

        await cb(f"Pushing fix for {file_path}...")
        push = github_agent.push_single_file(
            repo_name, file_path, fixed_content,
            f"Fix: {file_path} (attempt {retry})"
        )
        if push.get("failed"):
            return {"status": "error", "message": f"Push failed: {push['failed']}"}

        await cb("Waiting for any running pipelines...")
        github_agent.wait_for_idle(repo_name, timeout=120)

        await cb("Retriggering pipeline...")
        trigger = github_agent.trigger_pipeline(repo_name, "deploy.yml")
        if trigger.get("status") == "error":
            return {"status": "error", "message": trigger["error"]}

        await cb(f"Pipeline retriggered: {trigger.get('url')}")
        state.log_step(project, f"fix_{retry}", "done")

        pipeline = await github_agent.poll_pipeline(
            repo_name,
            interval=30,
            stop_flag=lambda: self.is_stopped(user_id),
            progress_cb=cb,
        )

        if pipeline.get("conclusion") == "success":
            ip = self._extract_ip(pipeline)
            state.update_deployment(project, status="deployed", ec2_ip=ip)
            return {"status": "success", "ip": ip, "url": f"http://{ip}"}

        return {"status": "failed", "pipeline": pipeline}

    # ── Update file ───────────────────────────────────────────────────────────

    async def update_file(
        self,
        user_id:     int,
        project:     str,
        repo_name:   str,
        file_path:   str,
        content:     str,
        progress_cb: Optional[Callable] = None,
    ) -> dict:
        self.resume(user_id)
        cb = progress_cb or (lambda m: None)

        try:
            self._check_stop(user_id)
            await cb(f"Pushing {file_path} to {repo_name}...")
            state.save_file(project, file_path, content)

            push = github_agent.push_single_file(repo_name, file_path, content)
            if push.get("failed"):
                return {"status": "error", "message": str(push["failed"])}

            await cb("File pushed. Triggering pipeline...")

            cancelled = github_agent.cancel_running_pipelines(repo_name)
            if cancelled.get("cancelled"):
                await cb(f"Cancelled {len(cancelled['cancelled'])} running pipeline(s)")
                await asyncio.sleep(5)

            trigger = github_agent.trigger_pipeline(repo_name, "deploy.yml")
            if trigger.get("status") == "error":
                return {"status": "error", "message": trigger["error"]}

            await cb(f"Pipeline triggered: {trigger.get('url')}")

            pipeline = await github_agent.poll_pipeline(
                repo_name,
                interval=30,
                stop_flag=lambda: self.is_stopped(user_id),
                progress_cb=cb,
            )

            if pipeline.get("conclusion") == "success":
                dep = state.get_deployment(project)
                ip  = dep.get("ec2_ip") if dep else None
                return {"status": "success", "ip": ip, "url": f"http://{ip}" if ip else "done"}

            return {"status": "failed", "pipeline": pipeline}

        except StopIteration:
            return {"status": "stopped"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── Destroy ───────────────────────────────────────────────────────────────

    async def destroy(
        self,
        user_id:     int,
        project:     str,
        repo_name:   str,
        delete_repo: bool = False,
        progress_cb: Optional[Callable] = None,
    ) -> dict:
        self.resume(user_id)
        cb = progress_cb or (lambda m: None)

        try:
            self._check_stop(user_id)

            # Cancel any running pipelines first
            cancelled = github_agent.cancel_running_pipelines(repo_name)
            if cancelled.get("cancelled"):
                await cb(f"Cancelled {len(cancelled['cancelled'])} running pipeline(s)")
                await asyncio.sleep(5)

            retry = 0
            while retry <= MAX_DESTROY_RETRIES:
                self._check_stop(user_id)
                await cb(f"Triggering destroy pipeline... (attempt {retry + 1})")

                trigger = github_agent.trigger_pipeline(repo_name, "destroy.yml")
                if trigger.get("status") == "error":
                    await cb(f"Trigger failed: {trigger['error']}")
                    break

                await cb(f"Pipeline: {trigger.get('url', '')}")
                pipeline = await github_agent.poll_pipeline(
                    repo_name,
                    interval=30,
                    stop_flag=lambda: self.is_stopped(user_id),
                    progress_cb=cb,
                )

                if pipeline.get("status") == "stopped":
                    return {"status": "stopped"}

                if pipeline.get("conclusion") == "success":
                    await cb("Destroy succeeded — cleaning up SSM and S3...")
                    aws_agent.delete_ssm_keys(project)
                    aws_agent.delete_s3_state(project)

                    if delete_repo:
                        await cb("Deleting GitHub repo...")
                        github_agent.cleanup(repo_name, delete_repo=True)

                    state.update_deployment(project, status="destroyed")
                    state.log_step(project, "destroy", "done")
                    return {"status": "success", "message": f"Destroyed {project}"}

                # Pipeline failed — auto fix and retry
                if retry >= MAX_DESTROY_RETRIES:
                    await cb(f"Destroy failed after {MAX_DESTROY_RETRIES} attempts")
                    await cb(f"Check logs: {pipeline.get('run_url', '')}")
                    await cb("Use /aws cleanup to remove resources manually")
                    break

                retry += 1
                await cb(f"Destroy pipeline failed — fixing (retry {retry}/{MAX_DESTROY_RETRIES})...")

                # Fetch latest files from repo before fixing
                repo_files_now = github_agent.get_existing_files(repo_name)
                for path, fcontent in repo_files_now.items():
                    state.save_file(project, path, fcontent)

                analysis   = error_agent.analyze(
                    pipeline.get("failed_jobs", []),
                    all_jobs=pipeline.get("all_jobs", []),
                )
                fix_result = code_agent.analyze_and_fix(
                    project,
                    analysis.get("log_context", "") or analysis.get("full_log", ""),
                )

                if "error" in fix_result:
                    await cb(f"Could not auto-fix: {fix_result['error']}")
                    break

                await cb(f"Fixed {fix_result['file']}: {fix_result['diff_summary']}")
                github_agent.push_single_file(
                    repo_name,
                    fix_result["file"],
                    fix_result["fixed_content"],
                    f"Fix destroy attempt {retry}",
                )
                await asyncio.sleep(3)

            state.update_deployment(project, status="destroy_failed")
            return {"status": "failed", "message": "Destroy pipeline failed"}

        except StopIteration:
            return {"status": "stopped"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self, project: str) -> dict:
        return {
            "deployment": state.get_deployment(project),
            "steps":      state.get_steps(project),
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract_url(self, pipeline: dict) -> str:
        """Extract ALB URL from ECS pipeline logs."""
        import re
        for job in pipeline.get("all_jobs", []):
            log = job.get("log", "")
            for pattern in [
                r"alb_url\s*=\s*(https?://[\w.-]+)",
                r"Live URL:\s*(https?://[\w.-]+)",
                r"http://([\w.-]+-\d+\.[\w.-]+\.elb\.amazonaws\.com)",
            ]:
                match = re.search(pattern, log)
                if match:
                    val = match.group(1)
                    return val if val.startswith("http") else f"http://{val}"
        return ""

    def _extract_ip(self, pipeline: dict) -> str:
        import re
        ip_pattern = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")
        for job in pipeline.get("all_jobs", []):
            log = job.get("log", "")
            # Try all common patterns in pipeline logs
            for pattern in [
                r"Live URL:\s*http://(\d+\.\d+\.\d+\.\d+)",
                r"Live at http://(\d+\.\d+\.\d+\.\d+)",
                r"Server IP:\s*(\d+\.\d+\.\d+\.\d+)",
                r"server_ip=(\d+\.\d+\.\d+\.\d+)",
                r"ip=(\d+\.\d+\.\d+\.\d+)",
                r"public_ip=(\d+\.\d+\.\d+\.\d+)",
                r"http://(\d+\.\d+\.\d+\.\d+)",
            ]:
                match = re.search(pattern, log)
                if match:
                    return match.group(1)
        return ""


orchestrator = Orchestrator()
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

            # ── Step 2: Files ─────────────────────────────────────────────────
            await step("generate_files", "Checking existing repo files...")

            repo_files = github_agent.get_existing_files(repo_name)

            # Let code_agent plan what files are needed for this app
            files_needed = code_agent.plan_files(project, app, region)
            missing      = [f for f in files_needed if f not in repo_files]

            if not missing:
                await cb(f"Found {len(repo_files)} existing files in repo — using them")
                for path, fcontent in repo_files.items():
                    state.save_file(project, path, fcontent)
                files = repo_files
            else:
                await cb(f"Missing {len(missing)} files — generating: {missing}")
                all_new = code_agent.generate_files(project, app, region)
                files   = {**repo_files, **{f: all_new[f] for f in missing if f in all_new}}
                for path, fcontent in files.items():
                    state.save_file(project, path, fcontent)

            state.log_step(project, "generate_files", "done", result=f"{len(files)} files")
            await cb(f"Ready with {len(files)} files")

            # ── Step 3: GitHub Setup ──────────────────────────────────────────
            self._check_stop(user_id)
            await step("github_setup", f"Setting up GitHub repo {repo_name}...")

            repo_result = github_agent.create_repo(repo_name, f"DevOps Agent — {project}")
            await cb(f"Repo: {repo_result.get('url', repo_name)}")

            # Push ONLY files missing from repo
            files_to_push = {
                path: fcontent
                for path, fcontent in files.items()
                if path not in repo_files
            }
            if files_to_push:
                push_result = github_agent.push_files(repo_name, files_to_push)
                if push_result.get("failed"):
                    await cb(f"Warning: failed to push some files")
                await cb(f"Pushed {len(push_result.get('pushed', []))} missing files")
            else:
                await cb("All files already in repo — nothing to push")

            # Set secrets
            secrets = {
                "AWS_ACCESS_KEY_ID":     creds["AWS_ACCESS_KEY_ID"],
                "AWS_SECRET_ACCESS_KEY": creds["AWS_SECRET_ACCESS_KEY"],
                "AWS_REGION":            region,
                "SSH_PRIVATE_KEY":       ssh_keys["private_key"],
                "SSH_PUBLIC_KEY":        ssh_keys["public_key"],
                "PROJECT_NAME":          project,
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

            await cb("Triggering pipeline...")
            trigger_result = github_agent.trigger_pipeline(repo_name, "deploy.yml")
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
                    trigger2 = github_agent.trigger_pipeline(repo_name, "deploy.yml")
                    if trigger2.get("status") == "error":
                        last_error = trigger2["error"]
                        await cb(f"Retrigger failed: {last_error}")
                        break
                    await cb(f"Pipeline: {trigger2.get('url', '')}")

                await cb(f"Polling... (attempt {retry + 1}/{MAX_RETRIES + 1})")
                pipeline = await github_agent.poll_pipeline(
                    repo_name,
                    interval=30,
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
                    ip = self._extract_ip(pipeline)
                    if not ip and ec2.get("exists"):
                        ip = ec2["ip"]
                    state.update_deployment(project, status="deployed", ec2_ip=ip)
                    state.log_step(project, "pipeline", "done", result=ip)
                    return {"status": "success", "ip": ip, "url": f"http://{ip}", "project": project}

                # Pipeline failed
                if retry >= MAX_RETRIES:
                    await cb(f"Failed after {MAX_RETRIES} attempts.")
                    await cb(f"Last error: {last_error}")
                    await cb(f"Pipeline logs: {pipeline.get('run_url', '')}")
                    break

                retry += 1
                await cb(f"Pipeline failed — auto-fixing (attempt {retry}/{MAX_RETRIES})...")

                # Fetch latest files from repo before fixing
                repo_files_now = github_agent.get_existing_files(repo_name)
                for path, fcontent in repo_files_now.items():
                    state.save_file(project, path, fcontent)

                analysis = error_agent.analyze(pipeline.get("failed_jobs", []))
                await cb(f"Failed job: {analysis.get('job_name', 'unknown')}")

                fix_result = code_agent.analyze_and_fix(
                    project,
                    analysis.get("log_context", "") or analysis.get("full_log", ""),
                )

                if "error" in fix_result:
                    last_error = fix_result["error"]
                    await cb(f"Cannot auto-fix: {last_error}")
                    await cb(f"Pipeline: {pipeline.get('run_url', '')}")
                    break

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

                analysis   = error_agent.analyze(pipeline.get("failed_jobs", []))
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

    def _extract_ip(self, pipeline: dict) -> str:
        import re
        for job in pipeline.get("all_jobs", []):
            match = re.search(r"Live at http://(\d+\.\d+\.\d+\.\d+)", job.get("log", ""))
            if match:
                return match.group(1)
        return ""


orchestrator = Orchestrator()
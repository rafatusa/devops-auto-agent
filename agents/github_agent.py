"""
GitHub Agent — pure PyGithub, no AI
Handles: repo, push, secrets, trigger, poll
"""
import os
import time
import logging
import requests
from github import Github
from github.GithubException import GithubException

logger = logging.getLogger(__name__)


class GitHubAgent:

    def __init__(self):
        self.token    = os.getenv("GITHUB_TOKEN", "")
        self.username = os.getenv("GITHUB_USERNAME", "")

    def _gh(self):
        return Github(self.token)

    def _user(self):
        return self._gh().get_user()

    # ── Repo ──────────────────────────────────────────────────────────────────

    def create_repo(self, name: str, description: str = "") -> dict:
        """Create repo if it doesn't exist."""
        try:
            user = self._user()
            try:
                repo = user.get_repo(name)
                return {"status": "exists", "url": repo.html_url}
            except GithubException:
                repo = user.create_repo(
                    name=name,
                    description=description or f"Deployed by DevOps Agent",
                    auto_init=True,
                    private=False,
                )
                time.sleep(2)
                logger.info(f"Created repo: {repo.html_url}")
                return {"status": "created", "url": repo.html_url}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def get_existing_files(self, repo_name: str) -> dict:
        """Get all existing files in repo. Returns {path: content}."""
        try:
            repo  = self._user().get_repo(repo_name)
            files = {}
            def _walk(path=""):
                try:
                    contents = repo.get_contents(path)
                    if not isinstance(contents, list):
                        contents = [contents]
                    for item in contents:
                        if item.type == "dir":
                            _walk(item.path)
                        else:
                            try:
                                files[item.path] = item.decoded_content.decode("utf-8")
                            except Exception:
                                pass
                except Exception:
                    pass
            _walk()
            return files
        except Exception as e:
            return {}

    def wait_for_idle(self, repo_name: str, timeout: int = 300) -> bool:
        """Wait until no pipeline is in_progress. Returns True when idle."""
        import time
        waited = 0
        while waited < timeout:
            try:
                repo = self._user().get_repo(repo_name)
                runs = list(repo.get_workflow_runs())
                in_progress = [r for r in runs if r.status in ("in_progress", "queued", "waiting")]
                if not in_progress:
                    return True
                logger.info(f"Waiting for {len(in_progress)} pipeline(s) to finish...")
                time.sleep(15)
                waited += 15
            except Exception:
                return True
        return False

    def cancel_running_pipelines(self, repo_name: str) -> dict:
        """Cancel all in-progress pipeline runs."""
        try:
            repo     = self._user().get_repo(repo_name)
            runs     = list(repo.get_workflow_runs())
            cancelled = []
            for r in runs:
                if r.status in ("in_progress", "queued", "waiting"):
                    try:
                        r.cancel()
                        cancelled.append(r.id)
                    except Exception:
                        pass
            return {"cancelled": cancelled}
        except Exception as e:
            return {"error": str(e)}

    def delete_repo(self, name: str) -> dict:
        try:
            self._user().get_repo(name).delete()
            return {"status": "deleted"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # ── Files ─────────────────────────────────────────────────────────────────

    def push_files(self, repo_name: str, files: dict, message: str = "Update via DevOps Agent") -> dict:
        """
        Push multiple files in one call.
        files = {"path/to/file.tf": "content", ...}
        """
        try:
            repo   = self._user().get_repo(repo_name)
            pushed = []
            failed = []

            for path, content in files.items():
                try:
                    try:
                        existing = repo.get_contents(path)
                        repo.update_file(path, message, content, existing.sha)
                    except GithubException:
                        repo.create_file(path, message, content)
                    pushed.append(path)
                    logger.info(f"Pushed: {path}")
                except Exception as e:
                    failed.append({"path": path, "error": str(e)})
                    logger.error(f"Failed to push {path}: {e}")

            return {"pushed": pushed, "failed": failed}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def push_single_file(self, repo_name: str, path: str, content: str, message: str = None) -> dict:
        """Push a single file."""
        msg = message or f"Update {path} via DevOps Agent"
        return self.push_files(repo_name, {path: content}, msg)

    # ── Secrets ───────────────────────────────────────────────────────────────

    def set_secrets(self, repo_name: str, secrets: dict) -> dict:
        """Set multiple GitHub secrets at once."""
        try:
            repo = self._user().get_repo(repo_name)
            set_keys = []
            for name, value in secrets.items():
                try:
                    repo.create_secret(name, str(value))
                    set_keys.append(name)
                except Exception as e:
                    logger.error(f"Failed to set secret {name}: {e}")
            return {"set": set_keys}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # ── Pipeline ──────────────────────────────────────────────────────────────

    def trigger_pipeline(self, repo_name: str, workflow: str = "deploy.yml", branch: str = "main") -> dict:
        """Trigger a GitHub Actions workflow."""
        try:
            repo = self._user().get_repo(repo_name)
            # Try by filename
            try:
                repo.get_workflow(workflow).create_dispatch(branch)
            except Exception:
                # Try by path
                for wf in repo.get_workflows():
                    if wf.path.endswith(workflow):
                        wf.create_dispatch(branch)
                        break
            logger.info(f"Triggered {workflow} on {repo_name}")
            return {
                "status":  "triggered",
                "workflow": workflow,
                "url":     f"https://github.com/{self.username}/{repo_name}/actions",
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def get_pipeline_status(self, repo_name: str) -> dict:
        """Get latest pipeline run status with full logs."""
        try:
            repo = self._user().get_repo(repo_name)
            runs = list(repo.get_workflow_runs())
            if not runs:
                return {"status": "no_runs"}

            latest     = runs[0]
            status     = latest.status
            conclusion = latest.conclusion
            failed_jobs = []
            all_jobs    = []

            if status == "completed":
                for job in latest.jobs():
                    log_text = self._fetch_job_log(repo_name, job.id)
                    job_info = {
                        "name":         job.name,
                        "conclusion":   job.conclusion,
                        "failed_steps": [s.name for s in job.steps if s.conclusion == "failure"],
                        "log":          log_text,
                    }
                    all_jobs.append(job_info)
                    if job.conclusion == "failure":
                        failed_jobs.append(job_info)

            return {
                "status":      status,
                "conclusion":  conclusion,
                "run_id":      latest.id,
                "run_url":     latest.html_url,
                "all_jobs":    all_jobs,
                "failed_jobs": failed_jobs,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _fetch_job_log(self, repo_name: str, job_id: int) -> str:
        """Fetch full log for a job."""
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{self.username}/{repo_name}/actions/jobs/{job_id}/logs",
                headers={"Authorization": f"token {self.token}"},
                allow_redirects=True,
                timeout=15,
            )
            return resp.text[-6000:] if resp.ok else ""
        except Exception:
            return ""

    async def poll_pipeline(self, repo_name: str, interval: int = 30, max_wait: int = 1800,
                      stop_flag=None, progress_cb=None) -> dict:
        """
        Poll pipeline until done or timeout.
        Returns final status dict.
        """
        import asyncio
        waited = 0
        while waited < max_wait:
            if stop_flag and stop_flag():
                return {"status": "stopped"}

            await asyncio.sleep(interval)
            waited += interval

            status = self.get_pipeline_status(repo_name)

            if progress_cb:
                await progress_cb(f"Pipeline: {status.get('status')} / {status.get('conclusion', '...')}")

            if status.get("status") == "completed":
                return status

        return {"status": "timeout"}

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def cleanup(self, repo_name: str, delete_repo: bool = False) -> dict:
        """Clean up GitHub resources."""
        result = {}
        if delete_repo:
            result["repo"] = self.delete_repo(repo_name)
        return result




    def handle(self, action: str, args: dict) -> dict:
        """
        Flexible standalone handler.
        Actions: create_repo, delete_repo, push, push_file,
                 set_secrets, trigger, status, poll, list_repos, cleanup
        """
        try:
            if action == "create_repo":
                return self.create_repo(args["name"], args.get("description", ""))

            elif action == "delete_repo":
                return self.delete_repo(args["name"])

            elif action == "list_repos":
                repos = []
                for r in self._user().get_repos():
                    repos.append({"name": r.name, "url": r.html_url, "private": r.private})
                return {"status": "ok", "repos": repos}

            elif action == "push":
                return self.push_files(args["repo"], args["files"], args.get("message", "Update"))

            elif action == "push_file":
                return self.push_single_file(
                    args["repo"], args["path"], args["content"], args.get("message")
                )

            elif action == "set_secrets":
                return self.set_secrets(args["repo"], args["secrets"])

            elif action == "trigger":
                return self.trigger_pipeline(
                    args["repo"],
                    args.get("workflow", "deploy.yml"),
                    args.get("branch", "main"),
                )

            elif action == "status":
                return self.get_pipeline_status(args["repo"])

            elif action == "poll":
                return self.poll_pipeline(
                    args["repo"],
                    interval=args.get("interval", 30),
                )

            elif action == "cleanup":
                return self.cleanup(args["repo"], args.get("delete_repo", False))

            else:
                return {"status": "error", "error": f"Unknown action: {action}"}

        except Exception as e:
            logger.error(f"GitHubAgent error: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}


github_agent = GitHubAgent()
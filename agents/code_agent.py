"""
Code Agent — ONLY agent that uses AI (Claude)
- Context-aware: reads existing files before generating/updating
- Generates only what needs to change, not the whole repo
- Fixes specific files given errors
"""
import os
import logging
import anthropic

import state
from skills import load_skill, load_skills

logger = logging.getLogger(__name__)
CLAUDE_MODEL = "claude-sonnet-4-6"


def _claude():
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _ask(prompt: str, system: str = None) -> str:
    kwargs = dict(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    if system:
        kwargs["system"] = system
    response = _claude().messages.create(**kwargs)
    return response.content[0].text.strip()


class CodeAgent:

    # ── Plan files ────────────────────────────────────────────────────────────

    def plan_files(self, project: str, app: str, region: str = "us-east-1",
                   target: str = "ec2") -> list:
        """
        target: "ec2" | "ec2-docker" | "ecs"
        Returns list of files needed.
        """
        if target == "ecs":
            files = [
                "terraform/main.tf",
                "Dockerfile",
                ".github/workflows/deploy.yml",
                ".github/workflows/destroy.yml",
            ]
            if any(w in app.lower() for w in ["nginx","apache","web","html","static"]):
                files.append("html/index.html")
            return files

        elif target == "ec2-docker":
            files = [
                "terraform/main.tf",
                "ansible/playbook.yml",
                "Dockerfile",
                ".github/workflows/deploy.yml",
                ".github/workflows/destroy.yml",
            ]
            if any(w in app.lower() for w in ["nginx","apache","web","html","static"]):
                files.append("html/index.html")
            return files

        else:  # ec2 direct
            files = [
                "terraform/main.tf",
                "ansible/playbook.yml",
                ".github/workflows/deploy.yml",
                ".github/workflows/destroy.yml",
            ]
            if any(w in app.lower() for w in ["nginx","apache","web","html","static"]):
                files.append("html/index.html")
            return files



    # ── Smart deployment planning — fully AI driven ──────────────────────────

    def plan_deployment(self, project: str, app: str, region: str,
                        target: str, existing_files: dict = None) -> dict:
        """
        One AI call decides everything:
        - What files are needed for this app + target
        - Which existing files to keep, update, create, or delete
        Returns {"keep": [...], "update": [...], "create": [...], "delete": [...], "reasoning": "..."}
        """
        existing_summary = ""
        if existing_files:
            existing_summary = "EXISTING FILES IN REPO:\n" + "\n".join(
                f"  - {p} ({len(c)} chars)" for p, c in existing_files.items()
            )

        skills = load_skills("ecs", "docker", "terraform-aws", "ansible", "pipeline")

        prompt = (
            f"You are a DevOps agent planning a deployment.\n\n"
            f"Project:  {project}\nApp:      {app}\nTarget:   {target}\nRegion:   {region}\n\n"
            + (f"{existing_summary}\n\n" if existing_summary else "")
            + "DEPLOYMENT TARGET MEANINGS:\n"
            "  ec2        = deploy app directly on EC2 (Ansible, no Docker)\n"
            "  ec2-docker = run app in Docker container ON EC2 (Ansible installs Docker, runs container)\n"
            "  ecs        = Amazon ECS Fargate (NO EC2, NO Ansible, ECR + ECS + ALB only)\n\n"
            "Decide exactly what files are needed and what to do with existing ones.\n"
            "Think through:\n"
            "  1. What infrastructure does this target need?\n"
            "  2. Is Ansible needed? (only for EC2-based targets)\n"
            "  3. Is a Dockerfile needed? (yes for docker/ecs targets)\n"
            "  4. What does the pipeline need to do?\n"
            "  5. What existing files can stay vs need to change?\n\n"
            f"SKILL REFERENCE:\n{skills}\n\n"
            "Respond in EXACTLY this format:\n"
            "KEEP:   path/to/file\n"
            "UPDATE: path/to/file\n"
            "CREATE: path/to/file\n"
            "DELETE: path/to/file\n"
            "REASON: one line summary\n\n"
            "Rules:\n"
            "- If target=ecs: DO NOT include ansible/playbook.yml\n"
            "- If target=ec2: DO NOT include Dockerfile unless app needs it\n"
            "- If target=ec2-docker: include both ansible/playbook.yml AND Dockerfile\n"
            "- Always include terraform/main.tf, deploy.yml, destroy.yml\n"
        )

        result = {"keep": [], "update": [], "create": [], "delete": [], "reasoning": ""}
        for line in _ask(prompt).splitlines():
            line = line.strip()
            if line.startswith("KEEP:"):
                result["keep"].append(line.replace("KEEP:", "").strip())
            elif line.startswith("UPDATE:"):
                result["update"].append(line.replace("UPDATE:", "").strip())
            elif line.startswith("CREATE:"):
                result["create"].append(line.replace("CREATE:", "").strip())
            elif line.startswith("DELETE:"):
                result["delete"].append(line.replace("DELETE:", "").strip())
            elif line.startswith("REASON:"):
                result["reasoning"] = line.replace("REASON:", "").strip()

        logger.info(f"Plan ({target}): keep={result['keep']} update={result['update']} "
                    f"create={result['create']} delete={result['delete']}\n"
                    f"Reason: {result['reasoning']}")
        return result

    def generate_files(self, project: str, app: str, region: str = "us-east-1",
                       existing_files: dict = None, target: str = "ec2") -> dict:
        """
        Fully dynamic — AI plans everything, then generates each file.
        Returns {path: content} — ONLY files that need to be pushed.
        """
        existing_files = existing_files or {}
        plan           = self.plan_deployment(project, app, region, target, existing_files)
        to_generate    = plan["update"] + plan["create"]

        if not to_generate:
            logger.info("Nothing to generate — all files up to date")
            return {}

        logger.info(f"Reason: {plan['reasoning']}")
        context = self._build_context(existing_files, plan)

        generated = {}
        for path in to_generate:
            logger.info(f"Generating: {path} (target={target})")
            content = self._generate_one(project, app, region, path,
                                          context, existing_files, target)
            if content:
                generated[path] = content
                state.save_file(project, path, content)
        return generated

    def _build_context(self, existing_files: dict, plan: dict = None) -> str:
        """Build context string — existing files + deployment plan reasoning."""
        parts = []
        if plan and plan.get("reasoning"):
            parts.append(f"DEPLOYMENT DECISION: {plan['reasoning']}\n")
        if existing_files:
            parts.append("EXISTING FILES IN REPO:")
            for path, file_content in existing_files.items():
                # Show full content for app-specific files
                if any(k in path for k in ["ansible", "Dockerfile", "app/", "src/"]):
                    parts.append(f"--- {path} ---\n{file_content}\n")
                else:
                    parts.append(f"--- {path} --- (exists, {len(file_content)} chars)")
        return "\n".join(parts)

    def _generate_one(self, project: str, app: str, region: str,
                      path: str, context: str, existing_files: dict,
                      target: str = "ec2") -> str:
        """Generate or update a single file with full context + target awareness."""
        existing = existing_files.get(path, "")
        action   = "UPDATE" if existing else "CREATE"

        target_desc = {
            "ec2":        "directly on EC2 (no Docker) using Ansible",
            "ec2-docker": "in a Docker container on EC2 (Ansible installs Docker, builds image, runs container)",
            "ecs":        "on Amazon ECS Fargate (no EC2, no Ansible, ECR + ECS + ALB)",
        }.get(target, "on EC2")

        if "destroy.yml" in path:
            return self._gen_destroy(project, region, target)

        # Load target-specific skills
        if "terraform" in path:
            skill = load_skills("ecs") if target == "ecs" else load_skills("terraform-aws")
        elif "ansible" in path or "playbook" in path:
            skill = load_skills("docker", "ansible") if target == "ec2-docker" else load_skills("ansible")
        elif ".github" in path:
            skill = load_skills("ecs") if target == "ecs" else load_skills("pipeline", "terraform-aws", "ansible")
        elif "Dockerfile" in path:
            skill = load_skill("docker") or ""
        else:
            skill = ""

        prompt = (
            f"{action} the file '{path}' to deploy '{app}' {target_desc}.\n\n"
            f"Project: {project} | App: {app} | Region: {region} | Target: {target}\n\n"
            + (f"{context}\n\n" if context else "")
            + (f"CURRENT {path}:\n{existing}\n\n" if existing else "")
            + (f"SKILL REFERENCE:\n{skill}\n\n" if skill else "")
            + "CRITICAL INSTRUCTIONS:\n"
            + _target_instructions(target, path)
            + "\n- Return ONLY the file content, no explanation, no markdown fences"
        )

        return _strip_fences(_ask(prompt))

    def _gen_destroy(self, project: str, region: str, target: str = "ec2") -> str:
        prompt = (
            f"Generate a GitHub Actions destroy.yml for project \"{project}\".\n"
            f"- Terraform destroy with S3 backend in region {region}\n"
            f"- S3 bucket: devops-agent-tfstate, key: {project}/terraform.tfstate\n"
            f"- Vars: -var=\"public_key=placeholder\" "
            f"-var=\"project_name=${{{{ secrets.PROJECT_NAME }}}}\" "
            f"-var=\"aws_region=${{{{ secrets.AWS_REGION }}}}\"\n"
            f"- Add || true after destroy\n"
            f"- Secrets: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, "
            f"SSH_PUBLIC_KEY, PROJECT_NAME\n"
            f"Return ONLY the YAML, no explanation, no markdown fences."
        )
        return _strip_fences(_ask(prompt))

    # ── Individual generators (kept for direct use) ───────────────────────────

    def gen_terraform(self, project: str, region: str = "us-east-1") -> str:
        skill   = load_skills("terraform-aws")
        content = _strip_fences(_ask(
            f"Generate terraform/main.tf for project \"{project}\" in \"{region}\".\n"
            f"Follow ALL rules below.\nReturn ONLY HCL, no fences.\n\n{skill}"
        ))
        if project: state.save_file(project, "terraform/main.tf", content)
        return content

    def gen_ansible(self, project: str, app: str, existing: str = "") -> str:
        skill = load_skills(app.lower().replace(" ", "-"), "ansible")
        prompt = (
            f"Generate ansible/playbook.yml for deploying {app} on Ubuntu 22.04.\n"
            f"Project: {project}\n\n"
            + (f"EXISTING PLAYBOOK (update this):\n{existing}\n\n" if existing else "")
            + f"Follow ALL skill rules below.\nReturn ONLY YAML, no fences.\n\n{skill}"
        )
        content = _strip_fences(_ask(prompt))
        if project: state.save_file(project, "ansible/playbook.yml", content)
        return content

    def gen_html(self, project: str, app: str = "") -> str:
        content = _strip_fences(_ask(
            f"Generate a clean dark-theme HTML page for project \"{project}\".\n"
            f"Return ONLY HTML, no explanation."
        ))
        if project: state.save_file(project, "html/index.html", content)
        return content

    def gen_pipeline(self, project: str, region: str = "us-east-1",
                     pipeline_type: str = "deploy") -> str:
        if pipeline_type == "destroy":
            return self._gen_destroy(project, region)
        skill   = load_skills("pipeline", "terraform-aws", "ansible")
        content = _strip_fences(_ask(
            f"Generate deploy.yml for project \"{project}\" in \"{region}\".\n"
            f"Jobs: provision→configure→verify→notify\n"
            f"Follow ALL rules below.\nReturn ONLY YAML, no fences.\n\n{skill}"
        ))
        if project: state.save_file(project, ".github/workflows/deploy.yml", content)
        return content

    # ── Fix file ──────────────────────────────────────────────────────────────

    def analyze_and_fix(self, project: str, log_context: str,
                        all_files: dict = None) -> dict:
        if all_files is None:
            all_files = state.get_all_files(project)
        if not all_files:
            return {"error": "No local files found"}

        file_listing = "\n".join(f"- {p}" for p in all_files)
        resp = _ask(
            f"Pipeline failed. Which file caused this error and what needs to change?\n\n"
            f"Files:\n{file_listing}\n\n"
            f"LOG:\n{log_context[-3000:]}\n\n"
            f"Respond:\nFILE: <path>\nERROR: <what to fix>"
        )

        file_path = error_desc = None
        for line in resp.splitlines():
            if line.startswith("FILE:"):  file_path  = line.replace("FILE:", "").strip()
            if line.startswith("ERROR:"): error_desc = line.replace("ERROR:", "").strip()

        if not file_path or file_path not in all_files:
            for f in all_files:
                if file_path and (file_path in f or f in str(file_path)):
                    file_path = f; break
            else:
                return self._create_missing(project, file_path, error_desc or log_context)

        return self.fix_file(project, file_path,
                             error_desc or "Fix error from log", log_context)

    def _create_missing(self, project: str, path: str, ctx: str) -> dict:
        dep = state.get_deployment(project) or {}
        app = dep.get("app", "nginx")
        created = _strip_fences(_ask(
            f"Create missing file '{path}' for {app} on AWS EC2.\n"
            f"Project: {project}\nContext: {ctx[:500]}\n"
            f"Return ONLY file content, no fences."
        ))
        state.save_file(project, path, created)
        return {"file": path, "fixed_content": created,
                "diff_summary": f"Created: {path}", "error_summary": f"Missing: {path}"}

    def fix_file(self, project: str, file_path: str, error: str,
                 log_context: str = "") -> dict:
        current = state.get_file(project, file_path)
        if not current:
            return {"error": f"File not found: {file_path}"}

        if "terraform" in file_path:   skill = load_skill("terraform-aws")
        elif "ansible" in file_path:   skill = load_skill("ansible")
        elif ".github" in file_path:   skill = load_skill("pipeline")
        else:                           skill = ""

        fixed = _strip_fences(_ask(
            f"Fix ONLY the broken lines in this file. Minimal change only.\n\n"
            f"FILE: {file_path}\nERROR: {error}\n"
            f"LOG:\n{log_context[-1500:]}\n\n"
            + (f"SKILL:\n{skill}\n\n" if skill else "")
            + f"CURRENT FILE:\n{current}\n\n"
            f"Return COMPLETE file with ONLY broken lines fixed. No fences."
        ))
        diff = _simple_diff(current, fixed)
        state.save_file(project, file_path, fixed)
        return {"file": file_path, "fixed_content": fixed,
                "diff_summary": diff, "error_summary": error}

    def update_file(self, project: str, file_path: str, instruction: str) -> dict:
        current = state.get_file(project, file_path) or ""
        updated = _strip_fences(_ask(
            f"{'Update' if current else 'Create'} file '{file_path}'.\n"
            f"INSTRUCTION: {instruction}\n\n"
            + (f"CURRENT:\n{current}\n\n" if current else "")
            + "Return ONLY complete file, no fences."
        ))
        diff = _simple_diff(current, updated)
        state.save_file(project, file_path, updated)
        return {"file": file_path, "content": updated, "diff_summary": diff}

    def ask(self, question: str) -> str:
        return _ask(question, system=(
            "You are a DevOps expert. Return clean code without markdown fences. "
            "Be concise and practical."
        ))

    def handle(self, action: str, args: dict) -> dict:
        from skills import list_skills, add_skill as _add_skill, delete_skill
        try:
            if action == "generate":
                files = self.generate_files(
                    project=args["project"], app=args["app"],
                    region=args.get("region", "us-east-1"),
                    existing_files=args.get("existing_files"),
                    target=args.get("target", "ec2"),
                )
                return {"status": "ok", "files": list(files.keys()), "content": files}
            elif action == "gen_terraform":
                return {"status": "ok", "content": self.gen_terraform(
                    args.get("project",""), args.get("region","us-east-1"))}
            elif action == "gen_ansible":
                return {"status": "ok", "content": self.gen_ansible(
                    args.get("project",""), args.get("app","nginx"), args.get("existing",""))}
            elif action == "gen_html":
                return {"status": "ok", "content": self.gen_html(
                    args.get("project",""), args.get("app",""))}
            elif action == "gen_pipeline":
                return {"status": "ok", "content": self.gen_pipeline(
                    args.get("project",""), args.get("region","us-east-1"), args.get("type","deploy"))}
            elif action == "fix":
                return {"status": "ok", **self.fix_file(
                    args["project"], args["file"], args.get("error",""), args.get("log",""))}
            elif action == "update":
                return {"status": "ok", **self.update_file(
                    args.get("project",""), args["file"], args["instruction"])}
            elif action == "ask":
                return {"status": "ok", "response": self.ask(args["question"])}
            elif action == "list_skills":
                return {"status": "ok", "skills": list_skills()}
            elif action == "add_skill":
                return {"status": "ok", "path": _add_skill(args["name"], args["content"])}
            elif action == "delete_skill":
                return {"status": "ok" if delete_skill(args["name"]) else "not_found"}
            else:
                return {"status": "error", "error": f"Unknown action: {action}"}
        except Exception as e:
            logger.error(f"CodeAgent error: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _target_instructions(target: str, path: str) -> str:
    """Return critical instructions based on deployment target and file."""
    if target == "ec2-docker" and ("ansible" in path or "playbook" in path):
        return (
            "- DO NOT install nginx or any app directly on the host\n"
            "- DO install Docker CE (not docker.io)\n"
            "- Copy all project files to /opt/app/ on the server\n"
            "- Build Docker image from /opt/app/\n"
            "- Run container with: docker run -d --name app -p 80:80 --restart always app:latest\n"
            "- Use '|| true' on docker stop/rm so they don't fail if container doesn't exist\n"
        )
    elif target == "ecs" and "terraform" in path:
        return (
            "- Generate ECS Fargate infrastructure (NOT EC2)\n"
            "- Include: ECR repo, ECS cluster, task definition, ECS service, ALB, security groups\n"
            "- Use default VPC and subnets\n"
            "- Output alb_url from ALB DNS name\n"
            "- No key pairs, no EC2 instances\n"
        )
    elif target == "ecs" and ".github" in path and "deploy" in path:
        return (
            "- Build Docker image and push to ECR\n"
            "- Update ECS service with force-new-deployment\n"
            "- DO NOT use Ansible or SSH\n"
            "- Jobs: terraform → build-push → deploy-ecs → verify\n"
        )
    elif target == "ec2" and ("ansible" in path or "playbook" in path):
        return (
            "- Install app directly on the host (no Docker)\n"
            "- For nginx: install nginx, copy html files, configure site\n"
        )
    else:
        return "- Generate appropriate content for the deployment target\n"


def _strip_fences(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith("```"): lines = lines[1:]
    if lines and lines[-1].startswith("```"): lines = lines[:-1]
    return "\n".join(lines).strip()


def _simple_diff(original: str, fixed: str) -> str:
    orig = original.splitlines(); new = fixed.splitlines()
    changed = []
    for i in range(min(len(orig), len(new))):
        if orig[i] != new[i]:
            changed.append(f"Line {i+1}:\n  - {orig[i]}\n  + {new[i]}")
        if len(changed) >= 5:
            changed.append("... more changes"); break
    if len(new) > len(orig):   changed.append(f"+ {len(new)-len(orig)} lines added")
    elif len(orig) > len(new): changed.append(f"- {len(orig)-len(new)} lines removed")
    return "\n".join(changed) if changed else "Minor changes"


code_agent = CodeAgent()
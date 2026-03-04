"""
Code Agent — ONLY agent that uses AI (Claude)
- Generates files using skills as context
- Fixes specific files given errors
- Usable standalone or via orchestrator
"""
import os
import logging
import anthropic
from pathlib import Path

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

    def plan_files(self, project: str, app: str, region: str = "us-east-1") -> list:
        """
        AI decides what files are needed for this app.
        Returns list of file paths to generate.
        """
        prompt = (
            f"You are a DevOps agent. List the exact files needed to deploy '{app}' on AWS EC2 "
            f"using Terraform and Ansible via GitHub Actions pipeline.\n\n"
            f"Project: {project}\n"
            f"App: {app}\n"
            f"Region: {region}\n\n"
            f"Rules:\n"
            f"- Always include: terraform/main.tf, ansible/playbook.yml, "
            f".github/workflows/deploy.yml, .github/workflows/destroy.yml\n"
            f"- Add html/index.html only for web servers (nginx, apache)\n"
            f"- Add app/ files only if app code is needed (node, python, spring-boot, react)\n"
            f"- List each file on its own line as: FILE: path/to/file\n"
            f"- No explanation, just the FILE: lines"
        )
        response = _ask(prompt)
        files_needed = []
        for line in response.splitlines():
            if line.startswith("FILE:"):
                path = line.replace("FILE:", "").strip()
                files_needed.append(path)

        # Always ensure core files are included
        core = [
            "terraform/main.tf",
            "ansible/playbook.yml",
            ".github/workflows/deploy.yml",
            ".github/workflows/destroy.yml",
        ]
        for f in core:
            if f not in files_needed:
                files_needed.append(f)

        # Force html for web servers — don't rely on AI to decide
        web_apps = ["nginx", "apache", "httpd", "web", "html", "static"]
        if any(w in app.lower() for w in web_apps):
            if "html/index.html" not in files_needed:
                files_needed.append("html/index.html")

        logger.info(f"Planned files for {project} ({app}): {files_needed}")
        return files_needed

    # ── Generate all files ────────────────────────────────────────────────────

    def generate_files(self, project: str, app: str, region: str = "us-east-1") -> dict:
        """
        AI plans what files are needed, then generates each one.
        Returns {path: content}
        """
        files_needed = self.plan_files(project, app, region)
        files = {}

        for path in files_needed:
            if path == "terraform/main.tf":
                files[path] = self.gen_terraform(project, region)
            elif path == "ansible/playbook.yml":
                files[path] = self.gen_ansible(project, app)
            elif path == "html/index.html":
                files[path] = self.gen_html(project, app)
            elif path == ".github/workflows/deploy.yml":
                files[path] = self.gen_pipeline(project, region, "deploy")
            elif path == ".github/workflows/destroy.yml":
                files[path] = self.gen_pipeline(project, region, "destroy")
            else:
                # Generate any other file using AI
                files[path] = self._gen_custom_file(project, app, path)

        for path, content in files.items():
            state.save_file(project, path, content)

        logger.info(f"Generated {len(files)} files for {project}")
        return files

    def _gen_custom_file(self, project: str, app: str, path: str) -> str:
        """Generate any file not covered by standard generators."""
        prompt = (
            f"Generate the file '{path}' for deploying {app} on AWS EC2.\n"
            f"Project: {project}\n"
            f"Return ONLY the file content, no explanation, no markdown fences."
        )
        return _strip_fences(_ask(prompt))

    # ── Individual generators ─────────────────────────────────────────────────

    def gen_terraform(self, project: str, region: str = "us-east-1") -> str:
        """Generate terraform/main.tf using terraform-aws skill."""
        skill = load_skills("terraform-aws")
        prompt = f"""Generate a complete Terraform main.tf for project "{project}" in region "{region}".

Follow ALL rules and patterns from the skills below exactly.
Return ONLY the terraform HCL code, no explanation, no markdown fences.

{skill}"""
        content = _ask(prompt)
        content = _strip_fences(content)
        if project:
            state.save_file(project, "terraform/main.tf", content)
        return content

    def gen_ansible(self, project: str, app: str) -> str:
        """Generate ansible/playbook.yml using app-specific skill."""
        app_skill  = load_skills(app.lower().replace(" ", "-").replace(".", ""))
        base_skill = load_skill("ansible")

        prompt = f"""Generate a complete Ansible playbook for deploying {app} on Ubuntu 22.04.

Project: {project}
App: {app}

Follow ALL rules and patterns from the skills below.
Return ONLY the YAML content, no explanation, no markdown fences.

{base_skill}

{app_skill}"""
        content = _ask(prompt)
        content = _strip_fences(content)
        if project:
            state.save_file(project, "ansible/playbook.yml", content)
        return content

    def gen_html(self, project: str, app: str = "") -> str:
        """Generate a clean HTML page."""
        skill = load_skill("html") or ""
        prompt = f"""Generate a clean, modern HTML page for project "{project}".
Dark theme, minimal design, show project name prominently.
{f'Additional guidance: {skill}' if skill else ''}
Return ONLY the HTML, no explanation."""
        content = _ask(prompt)
        content = _strip_fences(content)
        if project:
            state.save_file(project, "html/index.html", content)
        return content

    def gen_pipeline(self, project: str, region: str = "us-east-1", pipeline_type: str = "deploy") -> str:
        """Generate GitHub Actions pipeline using pipeline skill."""
        skill = load_skills("pipeline", "terraform-aws", "ansible")

        if pipeline_type == "destroy":
            prompt = f"""Generate a GitHub Actions destroy.yml workflow for project "{project}".

Requirements:
- Run terraform destroy with S3 backend in region {region}
- S3 state bucket: devops-agent-tfstate, key: {project}/terraform.tfstate
- Pass ALL required variables: public_key, project_name, aws_region
- Use: -var="public_key=placeholder" -var="project_name=${{{{ secrets.PROJECT_NAME }}}}" -var="aws_region=${{{{ secrets.AWS_REGION }}}}"
- Add || true after destroy so pipeline doesn't fail on already-deleted resources
- Use secrets: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, SSH_PUBLIC_KEY, PROJECT_NAME

Return ONLY the YAML, no explanation, no markdown fences."""
        else:
            prompt = f"""Generate a complete GitHub Actions deploy.yml workflow for project "{project}" in region "{region}".

Jobs: provision → configure → verify → notify
Follow ALL rules and patterns from the skills below exactly.
Return ONLY the YAML content, no explanation, no markdown fences.

{skill}"""

        content = _ask(prompt)
        content = _strip_fences(content)
        path = f".github/workflows/{pipeline_type}.yml"
        if project:
            state.save_file(project, path, content)
        return content

    # ── Fix file ──────────────────────────────────────────────────────────────

    def analyze_and_fix(self, project: str, log_context: str, all_files: dict = None) -> dict:
        """
        Dynamic fix — AI reads the log, identifies the broken file,
        and fixes it. No hardcoded patterns.
        Returns {file, fixed_content, diff_summary, error_summary}
        """
        if all_files is None:
            all_files = state.get_all_files(project)

        if not all_files:
            return {"error": "No local files found for project"}

        file_listing = "\n".join(f"- {path}" for path in all_files.keys())

        identify_prompt = f"""A deployment pipeline failed. Read the error log and identify:
1. Which file caused the error (exact path from the list)
2. What specific lines need to change and why

Available files:
{file_listing}

ERROR LOG:
{log_context[-3000:]}

Respond in this exact format:
FILE: <exact file path from the list above>
ERROR: <exactly what is wrong and what minimal change fixes it>"""

        identification = _ask(identify_prompt)

        file_path  = None
        error_desc = None
        for line in identification.splitlines():
            if line.startswith("FILE:"):
                file_path = line.replace("FILE:", "").strip()
            if line.startswith("ERROR:"):
                error_desc = line.replace("ERROR:", "").strip()

        # Validate file exists — try partial match if needed
        if not file_path or file_path not in all_files:
            matched = False
            for f in all_files:
                if file_path and (file_path in f or f in str(file_path)):
                    file_path = f
                    matched = True
                    break

            if not matched:
                # File doesn't exist at all — create it
                logger.info(f"File {file_path} missing — will create it")
                return self._create_missing_file(project, file_path, error_desc or log_context)

        return self.fix_file(project, file_path, error_desc or "Fix the error shown in the log", log_context)

    def _create_missing_file(self, project: str, file_path: str, error_context: str) -> dict:
        """Create a file that is missing entirely."""
        dep = state.get_deployment(project) or {}
        app = dep.get("app", "nginx")

        prompt = (
            f"A deployment failed because the file '{file_path}' is missing.\n"
            f"Project: {project}\n"
            f"App: {app}\n"
            f"Error context: {error_context[:500]}\n\n"
            f"Generate the complete content for '{file_path}'.\n"
            f"Return ONLY the file content, no explanation, no markdown fences."
        )
        created = _strip_fences(_ask(prompt))
        state.save_file(project, file_path, created)

        return {
            "file":          file_path,
            "fixed_content": created,
            "diff_summary":  f"Created missing file: {file_path}",
            "error_summary": f"Missing file {file_path} — created",
        }

    def fix_file(self, project: str, file_path: str, error: str, log_context: str = "") -> dict:
        """Fix a specific file — minimal change only."""
        current = state.get_file(project, file_path)
        if not current:
            return {"error": f"File not found locally: {file_path}"}

        # Load relevant skill
        if "terraform" in file_path:
            skill = load_skill("terraform-aws")
        elif "ansible" in file_path or "playbook" in file_path:
            skill = load_skill("ansible")
        elif "workflow" in file_path or ".github" in file_path:
            skill = load_skill("pipeline")
        else:
            skill = ""

        prompt = (
            f"You are fixing a deployment file. Make ONLY the minimal change needed.\n\n"
            f"FILE: {file_path}\n"
            f"ERROR: {error}\n\n"
            f"LOG CONTEXT:\n{log_context[-1500:] if log_context else 'none'}\n\n"
            f"{'SKILL REFERENCE:\n' + skill + chr(10) if skill else ''}"
            f"CURRENT FILE:\n{current}\n\n"
            f"Instructions:\n"
            f"- Find ONLY the lines causing the error\n"
            f"- Change ONLY those lines, do not touch anything else\n"
            f"- Do NOT rewrite or reformat the whole file\n"
            f"- Do NOT change variable names or logic unrelated to the error\n"
            f"- Return the COMPLETE file with ONLY the broken lines fixed\n"
            f"- No markdown fences, no explanation"
        )

        fixed = _ask(prompt)
        fixed = _strip_fences(fixed)

        diff = _simple_diff(current, fixed)
        state.save_file(project, file_path, fixed)

        return {
            "file":          file_path,
            "fixed_content": fixed,
            "diff_summary":  diff,
            "error_summary": error,
        }

    # ── Update file from instruction ──────────────────────────────────────────

    def update_file(self, project: str, file_path: str, instruction: str) -> dict:
        """Update a file based on natural language instruction."""
        current = state.get_file(project, file_path) or ""

        if current:
            prompt = f"""Update this file based on the instruction.
Return ONLY the complete updated file, no explanation, no markdown fences.

FILE: {file_path}
INSTRUCTION: {instruction}

CURRENT:
{current}"""
        else:
            prompt = f"""Create this file based on the instruction.
Return ONLY the complete file content, no explanation, no markdown fences.

FILE: {file_path}
INSTRUCTION: {instruction}"""

        updated = _ask(prompt)
        updated = _strip_fences(updated)
        diff    = _simple_diff(current, updated)
        state.save_file(project, file_path, updated)

        return {
            "file":         file_path,
            "content":      updated,
            "diff_summary": diff,
        }

    # ── Standalone / direct use ───────────────────────────────────────────────

    def ask(self, question: str) -> str:
        """Ask code agent anything."""
        system = (
            "You are a DevOps code expert. "
            "When generating code, return clean code without markdown fences unless asked. "
            "Be concise and practical."
        )
        return _ask(question, system=system)

    def handle(self, action: str, args: dict) -> dict:
        """
        Flexible standalone handler.
        Actions: generate, gen_terraform, gen_ansible, gen_pipeline,
                 gen_html, fix, update, ask, list_skills, add_skill, delete_skill
        """
        from skills import list_skills, add_skill as _add_skill, delete_skill

        try:
            if action == "generate":
                files = self.generate_files(
                    project=args["project"],
                    app=args["app"],
                    region=args.get("region", "us-east-1"),
                )
                return {"status": "ok", "files": list(files.keys()), "content": files}

            elif action == "gen_terraform":
                content = self.gen_terraform(args.get("project", ""), args.get("region", "us-east-1"))
                return {"status": "ok", "file": "terraform/main.tf", "content": content}

            elif action == "gen_ansible":
                content = self.gen_ansible(args.get("project", ""), args.get("app", "nginx"))
                return {"status": "ok", "file": "ansible/playbook.yml", "content": content}

            elif action == "gen_html":
                content = self.gen_html(args.get("project", ""), args.get("app", ""))
                return {"status": "ok", "file": "html/index.html", "content": content}

            elif action == "gen_pipeline":
                content = self.gen_pipeline(
                    args.get("project", ""),
                    args.get("region", "us-east-1"),
                    args.get("type", "deploy"),
                )
                return {"status": "ok", "file": f".github/workflows/{args.get('type','deploy')}.yml", "content": content}

            elif action == "fix":
                result = self.fix_file(
                    project=args["project"],
                    file_path=args["file"],
                    error=args.get("error", ""),
                    log_context=args.get("log", ""),
                )
                return {"status": "ok", **result}

            elif action == "update":
                result = self.update_file(
                    project=args.get("project", ""),
                    file_path=args["file"],
                    instruction=args["instruction"],
                )
                return {"status": "ok", **result}

            elif action == "ask":
                response = self.ask(args["question"])
                return {"status": "ok", "response": response}

            elif action == "list_skills":
                return {"status": "ok", "skills": list_skills()}

            elif action == "add_skill":
                path = _add_skill(args["name"], args["content"])
                return {"status": "ok", "path": path}

            elif action == "delete_skill":
                ok = delete_skill(args["name"])
                return {"status": "ok" if ok else "not_found"}

            else:
                return {"status": "error", "error": f"Unknown action: {action}"}

        except Exception as e:
            logger.error(f"CodeAgent error: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _simple_diff(original: str, fixed: str) -> str:
    orig    = original.splitlines()
    new     = fixed.splitlines()
    changed = []
    for i in range(min(len(orig), len(new))):
        if orig[i] != new[i]:
            changed.append(f"Line {i+1}:\n  - {orig[i]}\n  + {new[i]}")
        if len(changed) >= 5:
            changed.append("... more changes")
            break
    if len(new) > len(orig):
        changed.append(f"+ {len(new)-len(orig)} lines added")
    elif len(orig) > len(new):
        changed.append(f"- {len(orig)-len(new)} lines removed")
    return "\n".join(changed) if changed else "Minor changes"


code_agent = CodeAgent()
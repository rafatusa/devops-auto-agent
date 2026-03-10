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
        max_tokens=8192,
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
        One AI call decides everything.
        Claude reads the ACTUAL file contents — not just names/sizes —
        so it can tell if a file already does what's needed or must change.
        Returns {"keep": [...], "update": [...], "create": [...], "delete": [...], "reasoning": "..."}
        """
        existing_files = existing_files or {}

        # Build full file content section — Claude reads every file
        file_content_section = ""
        if existing_files:
            parts = ["EXISTING FILES IN REPO (full contents):\n"]
            for path, fcontent in existing_files.items():
                # Truncate very large files to keep prompt manageable
                preview = fcontent if len(fcontent) < 2000 else fcontent[:2000] + "\n... (truncated)"
                parts.append(f"=== {path} ===\n{preview}\n")
            file_content_section = "\n".join(parts)

        skills = load_skills("ecs", "docker", "terraform-aws", "ansible", "pipeline")

        prompt = (
            f"You are a DevOps agent planning a deployment.\n\n"
            f"Project: {project} | App: {app} | Target: {target} | Region: {region}\n\n"
            "DEPLOYMENT TARGET MEANINGS:\n"
            "  ec2        = deploy app directly on EC2 using Ansible (no Docker)\n"
            "  ec2-docker = run app in Docker container on EC2 (Ansible installs Docker + runs container)\n"
            "  ecs        = Amazon ECS Fargate (NO EC2, NO Ansible — ECR + ECS + ALB only)\n\n"
            + (f"{file_content_section}\n" if file_content_section else "No existing files.\n\n")
            + "YOUR TASK: Read every existing file above carefully. Then decide:\n"
            "  - Does this file already work correctly for the new request? → KEEP\n"
            "  - Does this file exist but needs changes for the new request? → UPDATE\n"
            "  - Does this file not exist yet but is needed? → CREATE\n"
            "  - Does this file exist but is no longer needed? → DELETE\n\n"
            "Think through each file:\n"
            "  1. Does the existing terraform already provision the right infra for this target?\n"
            "  2. Does the existing ansible do the right thing for this app + target?\n"
            "  3. Does the pipeline already match this target's deploy strategy?\n"
            "  4. Is a Dockerfile needed? Does one already exist and work?\n"
            "  5. Are there any files that are now wrong/unnecessary for this target?\n\n"
            f"SKILL REFERENCE:\n{skills}\n\n"
            "Respond in EXACTLY this format (one entry per line):\n"
            "KEEP:   path/to/file\n"
            "UPDATE: path/to/file\n"
            "CREATE: path/to/file\n"
            "DELETE: path/to/file\n"
            "REASON: one sentence explaining your decisions\n\n"
            "Hard rules:\n"
            "- target=ecs → never include ansible/playbook.yml\n"
            "- target=ec2 → no Dockerfile unless the app itself requires one\n"
            "- target=ec2-docker → must have ansible/playbook.yml AND Dockerfile\n"
            "- Always need: terraform/main.tf, deploy.yml, destroy.yml\n"
            "- Only mark a file UPDATE if the current content actually needs to change\n"
            "- If a file already correctly handles this app+target, mark it KEEP\n"
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

        logger.info(
            f"Plan ({target}): "
            f"keep={result['keep']} update={result['update']} "
            f"create={result['create']} delete={result['delete']}\n"
            f"Reason: {result['reasoning']}"
        )
        return result

    def generate_files(self, project: str, app: str, region: str = "us-east-1",
                       existing_files: dict = None, target: str = "ec2") -> dict:
        """
        Fully dynamic — AI plans everything, then generates each file.
        Returns {path: content} — ONLY files that need to be pushed.
        destroy.yml is ALWAYS regenerated — never trusted to the AI planner.
        """
        existing_files = existing_files or {}
        plan           = self.plan_deployment(project, app, region, target, existing_files)
        to_generate    = plan["update"] + plan["create"]

        # ALWAYS regenerate destroy.yml — AI planner frequently skips it
        # by marking it KEEP even when it doesn't exist in the repo
        destroy_path = ".github/workflows/destroy.yml"
        if destroy_path not in to_generate:
            logger.info("destroy.yml not in plan — forcing regeneration")
            to_generate.append(destroy_path)

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
            + (
                "\nCRITICAL S3 BUCKET RULE (deploy.yml): NEVER use '|| true' or '2>/dev/null' on bucket creation.\n"
                "The create-bucket step MUST: (1) check if bucket exists first, (2) create if not, "
                "(3) wait in a loop and confirm with head-bucket, (4) exit 1 if not ready after 60s.\n"
                "Silent failures cause terraform init to fail with 'S3 bucket does not exist'.\n"
                if ".github" in path and "deploy" in path else ""
            )
        )

        return _strip_fences(_ask(prompt))

    def _gen_destroy(self, project: str, region: str, target: str = "ec2") -> str:
        # Only pass -var flags for variables that actually exist in main.tf
        # ec2/ec2-docker need public_key; ecs does not
        var_flags = (
            f"-var=\"public_key=placeholder\" "
            f"-var=\"project_name=${{{{ secrets.PROJECT_NAME }}}}\" "
            f"-var=\"aws_region=${{{{ secrets.AWS_REGION }}}}\"" 
            if target != "ecs"
            else
            f"-var=\"project_name=${{{{ secrets.PROJECT_NAME }}}}\" "
            f"-var=\"aws_region=${{{{ secrets.AWS_REGION }}}}\"" 
        )
        prompt = (
            f"Generate a GitHub Actions destroy.yml for project \"{project}\".\n"
            f"- workflow_dispatch trigger only (never push)\n"
            f"- Terraform destroy with S3 backend\n"
            f"- terraform init with:\n"
            f"  -backend-config=\"bucket=${{{{ secrets.TF_STATE_BUCKET }}}}\"\n"
            f"  -backend-config=\"region=${{{{ secrets.AWS_REGION }}}}\"\n"
            f"  -backend-config=\"key={project}/terraform.tfstate\"\n"
            f"- terraform destroy step: use ONLY -auto-approve. NO -var flags whatsoever.\n"
            f"  Reason: terraform destroy reads existing state — it does not need input variables.\n"
            f"  Passing -var flags for variables not declared in main.tf causes destroy to FAIL.\n"
            f"  The correct command is exactly: terraform destroy -auto-approve\n"
            f"- CRITICAL: Do NOT add || true after terraform destroy.\n"
            f"  The pipeline must fail visibly if destroy fails.\n"
            f"  Only || true is acceptable on cleanup steps like: aws ec2 delete-key-pair, aws ec2 delete-security-group\n"
            f"- Secrets needed: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, PROJECT_NAME, TF_STATE_BUCKET\n"
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
            f"Generate ansible/playbook.yml for deploying {app} on the target server.\n"
            f"Project: {project}\n\n"
            + (f"EXISTING PLAYBOOK (update this):\n{existing}\n\n" if existing else "")
            + f"Follow ALL skill rules below.\nReturn ONLY YAML, no fences.\n\n{skill}\n\n"
            f"CRITICAL RULES FOR THIS PLAYBOOK:\n"
            f"1. Always use hosts: all (never hosts: web_servers or any other group name)\n"
            f"2. Never use copy module with src: pointing to a local path like ../app/ or ../src/\n"
            f"   Those paths do not exist on the GitHub Actions runner.\n"
            f"   Instead: use 'content: |' inline, or clone from git, or use template module.\n"
            f"3. If copying HTML — use src: ../html/index.html ONLY if html/ folder is in the repo\n"
            f"   Otherwise write the HTML inline with content: |\n"
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
        skill = load_skills("pipeline", "terraform-aws", "ansible")
        prompt = (
            f"Generate a complete deploy.yml for project \"{project}\" in \"{region}\".\n"
            f"Jobs: provision→configure→verify→notify\n"
            f"CRITICAL: Output the COMPLETE file — do not truncate or cut off mid-block.\n"
            f"The file must end with a complete YAML step. Never end mid-run block.\n"
            f"Follow ALL rules below. Return ONLY YAML, no fences.\n\n{skill}\n\n"
            f"CRITICAL TRIGGER RULE:\n"
            f"The deploy.yml on.push trigger MUST include paths-ignore for README.md.\n"
            f"The first job MUST have an if: condition that skips commits starting with:\n"
            f"  'fix:', 'Fix destroy', 'docs:', 'chore:'\n"
            f"Without this, every fix commit pushed during destroy triggers a new deploy.\n"
            f"Example:\n"
            f"  if: |\n"
            f"    !contains(github.event.head_commit.message, 'fix:') &&\n"
            f"    !contains(github.event.head_commit.message, 'Fix destroy') &&\n"
            f"    !contains(github.event.head_commit.message, 'docs: update README') &&\n"
            f"    !startsWith(github.event.head_commit.message, 'chore:')\n"
            f"CRITICAL S3 BUCKET RULE:\n"
            f"The S3 bucket creation step MUST use this exact pattern — no || true, no 2>/dev/null:\n"
            f"  if aws s3api head-bucket --bucket \"${{{{ secrets.TF_STATE_BUCKET }}}}\" --region \"${{{{ secrets.AWS_REGION }}}}\" 2>/dev/null; then\n"
            f"    echo Bucket exists; exit 0\n"
            f"  fi\n"
            f"  if [ \"${{{{ secrets.AWS_REGION }}}}\" = \"us-east-1\" ]; then\n"
            f"    aws s3api create-bucket --bucket \"${{{{ secrets.TF_STATE_BUCKET }}}}\" --region \"${{{{ secrets.AWS_REGION }}}}\"\n"
            f"  else\n"
            f"    aws s3api create-bucket --bucket \"${{{{ secrets.TF_STATE_BUCKET }}}}\" --region \"${{{{ secrets.AWS_REGION }}}}\" --create-bucket-configuration LocationConstraint=\"${{{{ secrets.AWS_REGION }}}}\"\n"
            f"  fi\n"
            f"  for i in $(seq 1 12); do\n"
            f"    aws s3api head-bucket --bucket \"${{{{ secrets.TF_STATE_BUCKET }}}}\" 2>/dev/null && exit 0 || sleep 5\n"
            f"  done\n"
            f"  exit 1  # fail hard if bucket not ready\n"
        )
        for attempt in range(3):
            result = _strip_fences(_ask(prompt))
            # Validate — reject if truncated (ends mid-block or very short)
            lines = result.strip().splitlines()
            last  = lines[-1].strip() if lines else ""
            if len(lines) > 20 and last not in ("", "|") and not last.endswith(("run", "run: |")):
                break
            logger.warning(f"gen_pipeline: output appears truncated (attempt {attempt+1}), retrying")
        if project:
            state.save_file(project, ".github/workflows/deploy.yml", result)
        return result

    # ── Fix file ──────────────────────────────────────────────────────────────

    def analyze_and_fix(self, project: str, log_context: str,
                        all_files: dict = None) -> dict:
        """
        Single AI call that sees ALL files + ALL logs together.
        Claude identifies the root cause file and outputs the fixed content directly.
        No two-step guessing — one call, full context, concrete output.
        """
        if all_files is None:
            all_files = state.get_all_files(project)
        if not all_files:
            return {"error": "No local files found"}

        dep    = state.get_deployment(project) or {}
        skills = load_skills("terraform-aws", "ansible", "pipeline")

        # Build numbered file blocks so Claude can reference exact lines
        file_blocks = []
        for path, file_content in all_files.items():
            numbered = "\n".join(f"{i+1:4}: {l}"
                                  for i, l in enumerate(file_content.splitlines()))
            file_blocks.append(f"--- FILE: {path} ---\n{numbered}")
        all_files_text = "\n\n".join(file_blocks)

        # Smart log slicing — include ALL job sections
        # Split by job sections and include every section (truncated if huge)
        log_sections = _split_job_sections(log_context)
        log_slice    = _build_log_slice(log_sections, max_chars=12000)

        resp = _ask(
            f"A GitHub Actions deployment pipeline failed.\n\n"

            f"=== STEP 1 — READ THE LOG FIRST ===\n"
            f"The pipeline log below contains the ACTUAL error from GitHub Actions.\n"
            f"Read it carefully before looking at any files.\n"
            f"The log is ground truth — do NOT infer errors from file content alone.\n\n"

            f"=== PIPELINE LOG ===\n"
            f"{log_slice}\n\n"

            f"=== STEP 2 — READ THE FILES ===\n"
            f"These are the live files from the repo. Only change what the log error points to.\n\n"
            f"{all_files_text}\n\n"

            f"=== STEP 3 — FIX RULES ===\n"
            f"GOLDEN RULE: Change THE MINIMUM number of lines needed to fix the error from the log.\n"
            f"Do NOT restructure, reformat, reorder, or rewrite anything the log does not complain about.\n"
            f"If the log shows one broken line — fix only that line. Leave everything else identical.\n\n"

            f"CRITICAL — DO NOT DO THESE:\n"
            f"  - Do NOT rewrite a file because it looks incomplete or ugly\n"
            f"  - Do NOT add or remove steps the log did not complain about\n"
            f"  - Do NOT change a file if the log error is in a DIFFERENT file\n"
            f"  - Do NOT guess the error — quote it exactly from the log in the ERROR: field\n\n"

            f"KNOWN PATTERNS — fix EXACTLY as described:\n"
            f"  1. 'Could not match supplied host pattern' / 'no hosts matched'\n"
            f"     → Change ONLY: hosts: X → hosts: all. Nothing else.\n\n"
            f"  2. 'Could not find or access src path on Ansible Controller'\n"
            f"     → Replace ONLY that copy task src: with content: | inline. Nothing else.\n\n"
            f"  3. 'Colons in unquoted values' at line N\n"
            f"     → Quote only that value on line N. Nothing else.\n\n"
            f"  4. S3 403 / bucket access error\n"
            f"     → This is NOT a file error. Do not touch any file. Output nothing.\n\n"
            f"  5. Any other error — quote the exact error line, fix only the line it points to.\n\n"

            f"OUTPUT FORMAT:\n"
            f"FILE: <exact path>\n"
            f"ERROR: <exact error message copied from the log>\n"
            f"FIXED_CONTENT:\n"
            f"<complete file with ONLY the broken lines changed>\n"
            f"END_FIXED_CONTENT\n\n"

            f"Multiple broken files? Output one FILE block per file.\n"
            f"If the error is NOT a file problem (e.g. S3 403, missing secret, network): output nothing.\n"
            f"No text before FILE: or after END_FIXED_CONTENT.\n"
        )

        # Parse the structured response — may contain MULTIPLE file fix blocks
        fixes = _parse_fix_blocks(resp)

        if not fixes:
            # Fallback — structured parse failed, use fix_file directly
            logger.warning("analyze_and_fix: no fix blocks parsed, falling back to fix_file")
            # Try to find file from any FILE: line
            file_path  = None
            error_desc = None
            for line in resp.splitlines():
                if line.startswith("FILE:"):  file_path  = line.replace("FILE:", "").strip()
                if line.startswith("ERROR:"): error_desc = line.replace("ERROR:", "").strip()
            if not file_path or file_path not in all_files:
                for f in all_files:
                    if file_path and (file_path in f or f in file_path):
                        file_path = f; break
                else:
                    return self._create_missing(project, file_path, error_desc or log_context)
            return self.fix_file(project, file_path,
                                 error_desc or "Fix error from log",
                                 log_context,
                                 current_content=all_files.get(file_path))

        # Apply ALL fixes found — push each one
        last_result  = None
        rejected     = []
        for fix in fixes:
            file_path     = fix["file"]
            error_desc    = fix["error"]
            fixed_content = fix["content"]

            # Resolve path if slightly different from repo path
            if file_path not in all_files:
                for f in all_files:
                    if file_path in f or f in file_path:
                        file_path = f; break

            if file_path not in all_files:
                # New file — create it
                state.save_file(project, file_path, fixed_content)
                last_result = {"file": file_path, "fixed_content": fixed_content,
                               "diff_summary": f"Created: {file_path}",
                               "error_summary": error_desc}
                continue

            original  = all_files.get(file_path, "")
            validated = _validate_fix(file_path, fixed_content, original)

            if validated == original:
                # AI returned same content — no change detected
                # Could mean: (a) AI couldn't identify the fix, (b) file is already correct
                logger.warning(f"analyze_and_fix: no change for {file_path} — AI may not have identified fix")
                rejected.append({"file": file_path, "error": error_desc, "content": fixed_content})
                continue

            diff = _simple_diff(original, validated)
            state.save_file(project, file_path, validated)
            last_result = {"file": file_path, "fixed_content": validated,
                           "diff_summary": diff, "error_summary": error_desc,
                           "all_fixes": fixes}
            logger.info(f"analyze_and_fix: applied fix to {file_path}: {error_desc[:80]}")

        if last_result:
            return last_result

        # All fixes produced no change — fall back to fix_file with the first rejected file
        # This gives fix_file a chance with a more focused single-file prompt
        if rejected:
            fb = rejected[0]
            fp = fb["file"]
            logger.warning(f"analyze_and_fix: falling back to fix_file for {fp}")
            if fp in all_files:
                return self.fix_file(project, fp, fb["error"],
                                     log_context, current_content=all_files[fp])

        # Last resort — ask again with a simpler prompt focused on the error only
        logger.warning("analyze_and_fix: all paths failed — retrying with minimal prompt")
        simple_resp = _ask(
            f"Pipeline error: {log_context[-2000:]}\n\n"
            f"Files:\n" + "\n".join(f"{p}:\n{c[:500]}" for p,c in list(all_files.items())[:4]) +
            f"\n\nWhich file has the bug and what is the one-line fix?\n"
            f"Reply:\nFILE: <path>\nFIX: <description of exact change>\n"
            f"FIXED_CONTENT:\n<complete fixed file>\nEND_FIXED_CONTENT"
        )
        fallback_fixes = _parse_fix_blocks(simple_resp)
        for fix in fallback_fixes:
            fp      = fix["file"]
            content = fix["content"]
            if fp not in all_files:
                for f in all_files:
                    if fp in f or f in fp: fp = f; break
            if fp in all_files and content != all_files[fp]:
                validated = _validate_fix(fp, content, all_files[fp])
                if validated != all_files[fp]:
                    diff = _simple_diff(all_files[fp], validated)
                    state.save_file(project, fp, validated)
                    return {"file": fp, "fixed_content": validated,
                            "diff_summary": diff, "error_summary": fix["error"]}

        return {"error": f"Could not determine fix. Check pipeline logs manually."}

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
                 log_context: str = "", current_content: str = None) -> dict:
        current = current_content or state.get_file(project, file_path)
        if not current:
            return {"error": f"File not found: {file_path}"}

        if "terraform" in file_path:   skill = load_skill("terraform-aws")
        elif "ansible" in file_path:   skill = load_skill("ansible")
        elif ".github" in file_path:   skill = load_skill("pipeline")
        else:                           skill = ""

        # Number the lines so AI can find exact line from error message
        numbered = "\n".join(f"{i+1:3}: {l}" for i, l in enumerate(current.splitlines()))

        fixed = _strip_fences(_ask(
            f"You are fixing a broken deployment file. Output ONLY the corrected file — nothing else.\n\n"
            f"=== PIPELINE ERROR LOG ===\n"
            f"{log_context[-4000:]}\n\n"
            f"=== FILE TO FIX: {file_path} ===\n"
            f"{numbered}\n\n"
            + (f"=== SKILL REFERENCE ===\n{skill}\n\n" if skill else "")
            + f"=== YOUR TASK ===\n"
            f"1. Read the error log above and find the EXACT line number and column mentioned\n"
            f"2. Look at that line number in the file above\n"
            f"3. Apply the fix that resolves that specific error\n"
            f"4. Output the COMPLETE fixed file — every line, including unchanged ones\n\n"
            f"=== OUTPUT FORMAT ===\n"
            f"Your entire response must be the file content only.\n"
            f"Start your response with the first line of the file.\n"
            f"No explanations. No markdown. No code fences. No preamble.\n"
            f"Just the complete fixed file, ready to be saved as {file_path}\n"
        ))

        # Validate output is actual file content, not explanation text
        # Retry with even stricter prompt if AI wrote prose instead of code
        fixed = _validate_fix(file_path, fixed, current)
        if fixed == current:
            logger.warning(f"fix_file: first attempt wrote prose — retrying with strict prompt")
            fixed = _strip_fences(_ask(
                f"Output ONLY the content of {file_path} with this one fix applied.\n"
                f"Do not write any words. Start with the first line of the file immediately.\n\n"
                f"ERROR TO FIX: {error}\n\n"
                f"CURRENT FILE:\n{current}\n\n"
                f"Fixed file content:"
            ))
            fixed = _validate_fix(file_path, fixed, current)

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


def _parse_fix_blocks(resp: str) -> list:
    """
    Parse one or more FILE/ERROR/FIXED_CONTENT/END_FIXED_CONTENT blocks from AI response.
    Returns list of {"file": ..., "error": ..., "content": ...}
    """
    fixes = []
    lines = resp.splitlines()

    current_file    = None
    current_error   = None
    current_content = []
    in_fixed        = False

    for line in lines:
        if line.startswith("FILE:") and not in_fixed:
            # Save previous block if exists
            if current_file and current_content:
                fixes.append({
                    "file":    current_file,
                    "error":   current_error or "",
                    "content": "\n".join(current_content).strip(),
                })
            current_file    = line.replace("FILE:", "").strip()
            current_error   = None
            current_content = []
            in_fixed        = False
        elif line.startswith("ERROR:") and not in_fixed:
            current_error = line.replace("ERROR:", "").strip()
        elif line.strip() == "FIXED_CONTENT:":
            in_fixed = True
        elif line.strip() == "END_FIXED_CONTENT":
            in_fixed = False
            if current_file and current_content:
                fixes.append({
                    "file":    current_file,
                    "error":   current_error or "",
                    "content": "\n".join(current_content).strip(),
                })
            current_file    = None
            current_error   = None
            current_content = []
        elif in_fixed:
            current_content.append(line)

    # Catch block without END_FIXED_CONTENT
    if current_file and current_content:
        fixes.append({
            "file":    current_file,
            "error":   current_error or "",
            "content": "\n".join(current_content).strip(),
        })

    return fixes


def _split_job_sections(log: str) -> list:
    """Split combined log into individual job sections."""
    sections = []
    current_name = "unknown"
    current_lines = []

    for line in log.splitlines():
        if line.startswith("=== JOB:") and "===" in line[8:]:
            if current_lines:
                sections.append({"name": current_name, "log": "\n".join(current_lines)})
            current_name  = line
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections.append({"name": current_name, "log": "\n".join(current_lines)})

    return sections


def _build_log_slice(sections: list, max_chars: int = 8000) -> str:
    """
    Include every job section but truncate long ones.
    Passed jobs with warnings get their full log — they're often the root cause.
    Failed jobs get truncated to save space if they're noisy.
    """
    if not sections:
        return ""

    # Budget per section — passed jobs get priority (may contain silent failures)
    # Give each section a base budget, then distribute remaining chars
    base_per_section = max_chars // max(len(sections), 1)

    parts = []
    for section in sections:
        log   = section["log"]
        name  = section["name"]
        is_failed = "[FAILED]" in name

        if is_failed:
            # For failed jobs: take head (setup) + tail (actual error)
            budget = base_per_section
            if len(log) > budget:
                half = budget // 2
                log  = log[:half] + "\n...(truncated)...\n" + log[-half:]
        else:
            # For passed jobs: keep full log — warnings are here
            budget = base_per_section * 2
            if len(log) > budget:
                log = log[:budget] + "\n...(truncated)..."

        parts.append(log)

    return "\n\n".join(parts)


def _validate_fix(file_path: str, fixed: str, original: str) -> str:
    """
    Only reject output if it is clearly explanation prose, not file content.
    Be conservative — if in doubt, accept the fix and let the pipeline decide.
    Rejecting a valid fix is worse than accepting a slightly wrong one.
    """
    if not fixed or len(fixed.strip()) < 5:
        logger.warning(f"_validate_fix: empty output for {file_path} — keeping original")
        return original

    # Strip BOM and whitespace
    stripped = fixed.strip().lstrip("\ufeff")
    first300 = stripped[:300].lower()

    # Only reject if the FIRST LINE is clearly an explanation sentence
    # (not a valid file keyword). We check only the very first line.
    first_line = stripped.splitlines()[0].lower().strip() if stripped.splitlines() else ""

    # Phrases that ONLY appear at the start of prose, never in real files
    prose_starters = [
        "looking at the",
        "the actual error",
        "based on the log",
        "analyzing the",
        "the pipeline log",
        "i can see that",
        "examining the",
        "to fix this",
        "the fix is",
        "here is the fixed",
        "here's the fixed",
    ]

    for sign in prose_starters:
        if first_line.startswith(sign):
            logger.warning(f"_validate_fix: prose detected in {file_path} ('{sign}') — keeping original")
            return original

    # For YAML — accept anything that looks remotely like YAML
    # Valid YAML can start with: ---, -, #, a key (word:), or even 'name:'
    if file_path.endswith((".yml", ".yaml")):
        import re
        if not re.match(r"^(---|#|-[ \t]|\w)", stripped):
            logger.warning(f"_validate_fix: YAML {file_path} starts with unexpected char — keeping original")
            return original

    # For terraform — expanded list of valid starters
    if file_path.endswith(".tf"):
        tf_starters = ["terraform", "provider", "resource", "variable", "output",
                       "data", "locals", "module", "#", "//"]
        if not any(stripped.startswith(kw) for kw in tf_starters):
            logger.warning(f"_validate_fix: HCL {file_path} starts with unexpected content — keeping original")
            return original

    return fixed


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
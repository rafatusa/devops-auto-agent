"""
Telegram Bot — natural language + commands
No AI here — pure regex/keyword extraction
"""
import asyncio
import logging
import os
import re
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv(override=True)

from orchestrator import orchestrator
from agents.code_agent   import code_agent
from agents.github_agent import github_agent
from agents.aws_agent    import aws_agent
from skills import list_skills, add_skill, delete_skill
import state

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

ALLOWED_USER = int(os.getenv("TELEGRAM_ALLOWED_USER", "0"))

sessions: dict[int, dict] = {}
running:  dict[int, bool] = {}

def is_running(uid): return running.get(uid, False)
def set_running(uid, v): running[uid] = v


# ── Natural language extractor — NO AI ───────────────────────────────────────

def extract_intent(text: str) -> dict:
    t = text.lower()
    result = {}

    # Intent
    if any(w in t for w in ["destroy", "delete", "remove", "tear down", "teardown"]):
        result["intent"] = "destroy"
    elif any(w in t for w in ["update", "change", "modify", "replace", "push file"]):
        result["intent"] = "update"
    elif any(w in t for w in ["trigger", "retrigger", "run pipeline", "rerun pipeline", "restart pipeline"]):
        result["intent"] = "trigger"
    elif any(w in t for w in ["deploy", "launch", "create", "setup", "set up", "start", "run", "build", "redeploy"]):
        result["intent"] = "deploy"
    else:
        result["intent"] = None

    # App
    apps = {
        "nginx":       ["nginx"],
        "node":        ["node", "nodejs", "node.js", "express"],
        "python":      ["python", "flask", "fastapi", "django"],
        "spring-boot": ["spring", "spring-boot", "springboot", "java"],
        "react":       ["react", "nextjs", "next.js"],
        "docker":      ["docker", "container"],
    }
    for app, keywords in apps.items():
        if any(k in t for k in keywords):
            result["app"] = app
            break

    # Cloud
    if any(w in t for w in ["aws", "ec2", "amazon"]):   result["cloud"] = "AWS"
    elif any(w in t for w in ["azure", "microsoft"]):    result["cloud"] = "Azure"
    elif any(w in t for w in ["gcp", "google cloud"]):   result["cloud"] = "GCP"

    # IaC / Config
    if "pulumi"    in t: result["iac"] = "pulumi"
    elif "terraform" in t: result["iac"] = "terraform"
    if "ansible" in t: result["config"] = "ansible"

    # Region
    region_match = re.search(r"(us-east-[12]|us-west-[12]|eu-west-[123]|ap-southeast-[12]|ap-northeast-[12])", t)
    if region_match:
        result["region"] = region_match.group(1)

    # Repo URL
    url_match = re.search(r"https?://github\.com/([\w-]+/[\w-]+)", text)
    if url_match:
        result["repo_url"]  = url_match.group(0)
        result["repo_name"] = url_match.group(1).split("/")[-1]
        result["project"]   = result["repo_name"]

    # Repo name from "in repo X" or "repo X"
    repo_match = re.search(r"(?:in|to|for|repo|repository)\s+([\w-]+)", t)
    if repo_match:
        candidate = repo_match.group(1)
        skip = {"aws","ec2","amazon","nginx","node","python","spring","react","docker",
                "terraform","ansible","the","my","a","an","to","in","on","with"}
        if candidate not in skip:
            result["repo_name"] = candidate
            if "project" not in result:
                result["project"] = candidate

    # Project name
    if "project" not in result:
        patterns = [
            r"(?:deploy|launch|setup|for|project|named?|called?)\s+([\w-]+)",
            r"([\w-]+)\s+(?:project|app|service|repo)",
        ]
        for pattern in patterns:
            match = re.search(pattern, t)
            if match:
                candidate = match.group(1)
                skip = {"nginx","node","python","spring","react","docker","aws","ec2",
                        "terraform","ansible","the","my","a","an","to","in","on","with",
                        "and","or","using","use","deploy","launch","setup","create","html",
                        "file","repo","pipeline","trigger"}
                if candidate not in skip and len(candidate) > 1:
                    result["project"] = candidate
                    break

    # File path (for update intent)
    file_match = re.search(r"([\w/.-]+\.(?:html|yml|yaml|tf|py|js|json|md|sh))", text)
    if file_match:
        result["file"] = file_match.group(1)

    return result


def missing_fields(answers: dict) -> list:
    needed = []
    if not answers.get("project"):  needed.append("project")
    if not answers.get("app"):      needed.append("app")
    if not answers.get("repo"):     needed.append("repo")
    return needed


FIELD_QUESTIONS = {
    "project": "Project name?",
    "app":     "What to deploy? (nginx / node / python / spring-boot)",
    "repo":    "GitHub repo name?",
    "region":  "AWS region? (default: us-east-1)",
}


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "DevOps Agent — just tell me what you want:\n\n"
        "Examples:\n"
        "  deploy nginx to aws\n"
        "  update html in repo web-app2\n"
        "  replace html/index.html in web-app2\n"
        "  trigger pipeline in web-app2\n"
        "  destroy web-app\n\n"
        "Commands:\n"
        "  /deploy /update /destroy /trigger /status /projects\n"
        "  /code /github /aws /skills\n"
        "  /stop /reset"
    )

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    orchestrator.stop(uid)
    sessions.pop(uid, None)
    set_running(uid, False)
    await update.message.reply_text("Stopping...")

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions.pop(uid, None)
    orchestrator.resume(uid)
    set_running(uid, False)
    await update.message.reply_text("Reset done.")

async def cmd_projects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    projects = state.list_projects()
    if not projects:
        await update.message.reply_text("No projects yet.")
        return
    lines = ["Projects:\n"]
    for p in projects:
        ip = f" → http://{p['ec2_ip']}" if p.get("ec2_ip") else ""
        lines.append(f"  {p['project']} ({p['status']}){ip}")
    await update.message.reply_text("\n".join(lines))

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    sess    = sessions.get(uid, {})
    project = sess.get("project") or sess.get("answers", {}).get("project")
    if not project:
        await update.message.reply_text("No active project.")
        return
    s     = orchestrator.get_status(project)
    dep   = s.get("deployment") or {}
    steps = s.get("steps", [])
    last  = steps[-1] if steps else {}
    await update.message.reply_text(
        f"Project: {project}\n"
        f"Status:  {dep.get('status','unknown')}\n"
        f"IP:      {dep.get('ec2_ip','none')}\n"
        f"Last:    {last.get('step')} — {last.get('status')}"
    )

async def cmd_deploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_running(uid):
        await update.message.reply_text("Job running. /stop to cancel.")
        return
    sessions[uid] = {"mode": "collect", "answers": {}, "missing": ["project","app","repo"]}
    await update.message.reply_text(FIELD_QUESTIONS["project"])

async def cmd_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions[uid] = {"mode": "update_repo", "answers": {}}
    await update.message.reply_text("Repo name? (e.g. web-app2)")

async def cmd_destroy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions[uid] = {"mode": "destroy_project", "answers": {}}
    await update.message.reply_text("Which project to destroy?")

async def cmd_trigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    args = ctx.args
    if args:
        repo = args[0]
        workflow = args[1] if len(args) > 1 else "deploy.yml"
        await update.message.reply_text(f"Triggering {workflow} in {repo}...")
        r = github_agent.handle("trigger", {"repo": repo, "workflow": workflow})
        await update.message.reply_text(f"{r.get('status')} — {r.get('url', r.get('error',''))}")
    else:
        sessions[uid] = {"mode": "trigger_repo", "answers": {}}
        await update.message.reply_text("Repo name to trigger pipeline?")


# ── Agent commands ────────────────────────────────────────────────────────────

async def cmd_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    uid  = update.effective_user.id
    if not args:
        await update.message.reply_text(
            "/code ask <question>\n"
            "/code gen terraform <project>\n"
            "/code gen ansible <project> <app>\n"
            "/code gen html <project>\n"
            "/code gen pipeline <project>\n"
            "/code generate <project> <app>\n"
            "/code fix <project> <file> <error>\n"
            "/code update <project> <file> <instruction>"
        )
        return
    action = args[0].lower()

    if action == "ask":
        await update.message.reply_text("Thinking...")
        response = code_agent.ask(" ".join(args[1:]))
        sessions[uid] = {"mode": "code_response", "last_code": response}
        await _send_long(update, response)

    elif action == "gen" and len(args) >= 3:
        sub     = args[1].lower()
        project = args[2]
        await update.message.reply_text(f"Generating {sub}...")
        if sub == "terraform":
            r = code_agent.handle("gen_terraform", {"project": project, "region": args[3] if len(args)>3 else "us-east-1"})
        elif sub == "ansible":
            r = code_agent.handle("gen_ansible", {"project": project, "app": args[3] if len(args)>3 else "nginx"})
        elif sub == "html":
            r = code_agent.handle("gen_html", {"project": project})
        elif sub == "pipeline":
            r = code_agent.handle("gen_pipeline", {"project": project, "region": args[3] if len(args)>3 else "us-east-1"})
        else:
            await update.message.reply_text(f"Unknown: {sub}"); return
        sessions[uid] = {"mode": "code_response", "last_code": r.get("content",""), "last_file": r.get("file")}
        await update.message.reply_text(f"File: {r.get('file')}")
        await _send_long(update, f"```\n{r.get('content','')[:3000]}\n```")

    elif action == "generate":
        project = args[1] if len(args)>1 else ""
        app     = args[2] if len(args)>2 else "nginx"
        region  = args[3] if len(args)>3 else "us-east-1"
        await update.message.reply_text(f"Generating all files for {project}...")
        r = code_agent.handle("generate", {"project": project, "app": app, "region": region})
        await update.message.reply_text("Generated:\n" + "\n".join(f"  {f}" for f in r.get("files",[])))

    elif action == "fix":
        project = args[1] if len(args)>1 else ""
        file_   = args[2] if len(args)>2 else ""
        error   = " ".join(args[3:])
        await update.message.reply_text(f"Fixing {file_}...")
        r = code_agent.handle("fix", {"project": project, "file": file_, "error": error})
        sessions[uid] = {"mode": "fix_approval", "fix": r, "answers": {"project": project, "repo_name": project}}
        await update.message.reply_text(f"Fix ready:\n{r.get('diff_summary')}\n\nApply? (yes/no)")

    elif action == "update":
        project     = args[1] if len(args)>1 else ""
        file_       = args[2] if len(args)>2 else ""
        instruction = " ".join(args[3:])
        await update.message.reply_text(f"Updating {file_}...")
        r = code_agent.handle("update", {"project": project, "file": file_, "instruction": instruction})
        sessions[uid] = {"mode": "push_approval", "file": file_, "content": r.get("content"), "answers": {"project": project}}
        await update.message.reply_text(f"Updated:\n{r.get('diff_summary')}\n\nPush to GitHub? (yes/no)")


async def cmd_github(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    uid  = update.effective_user.id

    # No args — start conversational menu
    if not args:
        sessions[uid] = {"mode": "gh_menu", "answers": {}}
        await update.message.reply_text(
            "GitHub Agent — what do you want to do?\n\n"
            "  push    — push a file to repo\n"
            "  pull    — get a file from repo\n"
            "  trigger — run pipeline\n"
            "  status  — pipeline status\n"
            "  logs    — last pipeline logs\n"
            "  list    — list repos\n"
            "  files   — list files in repo\n"
            "  create  — create repo\n"
            "  delete  — delete repo\n"
            "  secrets — set secrets\n"
        )
        return

    action = args[0].lower()

    if action == "create":
        r = github_agent.handle("create_repo", {"name": args[1]})
        await update.message.reply_text(f"{r.get('status')} — {r.get('url', r.get('error',''))}")
    elif action == "delete":
        r = github_agent.handle("delete_repo", {"name": args[1]})
        await update.message.reply_text(f"Delete: {r.get('status')}")
    elif action == "list":
        r = github_agent.handle("list_repos", {})
        lines = [f"{x['name']} — {x['url']}" for x in r.get("repos",[])[:15]]
        await update.message.reply_text("Repos:\n" + "\n".join(lines) if lines else "No repos")
    elif action == "files":
        repo  = args[1] if len(args)>1 else ""
        if not repo:
            sessions[uid] = {"mode": "gh_files_repo", "answers": {}}
            await update.message.reply_text("Repo name?")
            return
        files = github_agent.get_existing_files(repo)
        lines = list(files.keys())
        await update.message.reply_text(f"Files in {repo}:\n" + "\n".join(f"  {f}" for f in lines) if lines else "Empty repo")
    elif action == "pull":
        repo = args[1] if len(args)>1 else ""
        file = args[2] if len(args)>2 else ""
        if not repo:
            sessions[uid] = {"mode": "gh_pull_repo", "answers": {}}
            await update.message.reply_text("Repo name?")
            return
        if not file:
            sessions[uid] = {"mode": "gh_pull_file", "answers": {"repo": repo}}
            await update.message.reply_text("File path? (e.g. html/index.html)")
            return
        files = github_agent.get_existing_files(repo)
        cnt   = files.get(file, "")
        await (_send_long(update, f"```\n{cnt[:3500]}\n```") if cnt else update.message.reply_text(f"Not found: {file}"))
    elif action == "push":
        repo = args[1] if len(args)>1 else ""
        file = args[2] if len(args)>2 else ""
        if not repo:
            sessions[uid] = {"mode": "gh_push_repo", "answers": {}}
            await update.message.reply_text("Repo name?")
            return
        if not file:
            sessions[uid] = {"mode": "gh_push_file", "answers": {"repo": repo}}
            await update.message.reply_text("File path? (e.g. html/index.html)")
            return
        sessions[uid] = {"mode": "gh_push_content", "answers": {"repo": repo, "file": file}}
        await update.message.reply_text(f"Paste new content for {file}:")
    elif action == "secrets":
        repo    = args[1] if len(args)>1 else ""
        secrets = dict(kv.split("=",1) for kv in args[2:] if "=" in kv)
        r = github_agent.handle("set_secrets", {"repo": repo, "secrets": secrets})
        await update.message.reply_text(f"Set: {r.get('set', r.get('error'))}")
    elif action == "trigger":
        repo     = args[1] if len(args)>1 else ""
        workflow = args[2] if len(args)>2 else "deploy.yml"
        if not repo:
            sessions[uid] = {"mode": "gh_trigger_repo", "answers": {}}
            await update.message.reply_text("Repo name?")
            return
        r = github_agent.trigger_pipeline(repo, workflow)
        await update.message.reply_text(f"{r.get('status')} — {r.get('url', r.get('error',''))}")
    elif action == "status":
        repo = args[1] if len(args)>1 else ""
        if not repo:
            sessions[uid] = {"mode": "gh_status_repo", "answers": {}}
            await update.message.reply_text("Repo name?")
            return
        r = github_agent.get_pipeline_status(repo)
        await update.message.reply_text(f"Status: {r.get('status')}\nConclusion: {r.get('conclusion','...')}\n{r.get('run_url','')}")
    elif action == "logs":
        repo = args[1] if len(args)>1 else ""
        if not repo:
            sessions[uid] = {"mode": "gh_logs_repo", "answers": {}}
            await update.message.reply_text("Repo name?")
            return
        result = github_agent.get_pipeline_status(repo)
        for job in result.get("failed_jobs", [])[:2]:
            await _send_long(update, f"=== {job['name']} ===\n{job.get('log','')[-2000:]}")
        if not result.get("failed_jobs"):
            await update.message.reply_text("No failed jobs found")


async def cmd_aws(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "/aws check <project>\n/aws list\n/aws sshkey <project>\n"
            "/aws s3\n/aws cleanup <project>\n/aws creds"
        )
        return
    action = args[0].lower()
    if action == "check":
        r = aws_agent.handle("check_ec2", {"project": args[1]})
        await update.message.reply_text(f"EC2: {r['ip']}" if r.get("exists") else f"No EC2 for {args[1]}")
    elif action == "list":
        r = aws_agent.handle("list_ec2", {})
        lines = [f"{i['project'] or 'unknown'}: {i['ip']} ({i['type']})" for i in r.get("instances",[])]
        await update.message.reply_text("Running:\n" + "\n".join(lines) if lines else "No instances")
    elif action == "sshkey":
        r = aws_agent.handle("gen_ssh_key", {"project": args[1]})
        await update.message.reply_text("SSH key generated" if "error" not in r else f"Error: {r['error']}")
    elif action == "s3":
        r = aws_agent.handle("ensure_s3", {})
        await update.message.reply_text(f"S3: {r.get('bucket')} — {'exists' if r.get('exists') else 'created'}")
    elif action == "cleanup":
        r = aws_agent.handle("cleanup", {"project": args[1]})
        await update.message.reply_text(f"Cleaned: {r}")
    elif action == "creds":
        r     = aws_agent.handle("credentials", {})
        creds = r.get("credentials", {})
        await update.message.reply_text(f"Key: {creds.get('AWS_ACCESS_KEY_ID','')[:8]}...\nRegion: {creds.get('AWS_REGION')}")


async def cmd_skills(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    skills = list_skills()
    lines  = [f"  [{s['type']}] {s['name']}" for s in skills]
    await update.message.reply_text("Skills:\n" + "\n".join(lines) + "\n\n/addskill to add custom")

async def cmd_addskill(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    args = ctx.args
    if args:
        sessions[uid] = {"mode": "add_skill", "skill_name": args[0]}
        await update.message.reply_text(f"Paste skill content for '{args[0]}':")
    else:
        sessions[uid] = {"mode": "add_skill_name"}
        await update.message.reply_text("Skill name?")

async def cmd_delskill(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /delskill <name>")
        return
    ok = delete_skill(args[0])
    await update.message.reply_text(f"Deleted: {args[0]}" if ok else f"Not found: {args[0]}")


# ── Main message handler ──────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()

    if ALLOWED_USER and uid != ALLOWED_USER:
        await update.message.reply_text("Unauthorized.")
        return

    if is_running(uid):
        await update.message.reply_text("Job running. /stop to cancel.")
        return

    sess = sessions.get(uid, {})
    mode = sess.get("mode", "")

    # ── Session modes ─────────────────────────────────────────────────────────

    # ── GitHub conversational menu ───────────────────────────────────────────
    if mode == "gh_menu":
        action = text.strip().lower()
        if action in ("push", "pull", "trigger", "status", "logs", "files", "list", "create", "delete"):
            if action == "list":
                r = github_agent.handle("list_repos", {})
                lines = [f"{x['name']} — {x['url']}" for x in r.get("repos",[])[:15]]
                await update.message.reply_text("Repos:\n" + "\n".join(lines) if lines else "No repos")
                sessions.pop(uid, None)
            else:
                sessions[uid] = {"mode": f"gh_{action}_repo", "answers": {}}
                await update.message.reply_text("Repo name?")
        else:
            await update.message.reply_text("Choose: push / pull / trigger / status / logs / files / list / create / delete")
        return

    if mode == "gh_push_repo":
        sess["answers"]["repo"] = text.strip()
        sessions[uid] = {"mode": "gh_push_file", "answers": sess["answers"]}
        await update.message.reply_text("File path? (e.g. html/index.html)")
        return

    if mode == "gh_push_file":
        sess["answers"]["file"] = text.strip()
        sessions[uid] = {"mode": "gh_push_content", "answers": sess["answers"]}
        await update.message.reply_text(f"Paste new content for {text.strip()}:")
        return

    if mode == "gh_push_content":
        repo = sess["answers"]["repo"]
        file = sess["answers"]["file"]
        r    = github_agent.push_single_file(repo, file, text, f"Update: {file}")
        sessions.pop(uid, None)
        pushed = r.get("pushed", [])
        failed = r.get("failed", [])
        if pushed:
            await update.message.reply_text(f"Pushed {file} to {repo}\nTrigger pipeline? (yes/no)")
            sessions[uid] = {"mode": "gh_push_trigger", "answers": {"repo": repo}}
        else:
            await update.message.reply_text(f"Failed: {failed}")
        return

    if mode == "gh_push_trigger":
        if text.strip().lower() in ("yes", "y"):
            repo = sess["answers"]["repo"]
            r    = github_agent.trigger_pipeline(repo, "deploy.yml")
            await update.message.reply_text(f"Pipeline triggered: {r.get('url', r.get('error',''))}")
        else:
            await update.message.reply_text("Done — pipeline not triggered.")
        sessions.pop(uid, None)
        return

    if mode == "gh_pull_repo":
        sess["answers"]["repo"] = text.strip()
        sessions[uid] = {"mode": "gh_pull_file", "answers": sess["answers"]}
        await update.message.reply_text("File path?")
        return

    if mode == "gh_pull_file":
        repo  = sess["answers"]["repo"]
        file  = text.strip()
        files = github_agent.get_existing_files(repo)
        cnt   = files.get(file, "")
        sessions.pop(uid, None)
        if cnt:
            await _send_long(update, f"```\n{cnt[:3500]}\n```")
        else:
            await update.message.reply_text(f"Not found: {file}\nFiles: {list(files.keys())[:10]}")
        return

    if mode == "gh_trigger_repo":
        repo = text.strip()
        sessions.pop(uid, None)
        r = github_agent.trigger_pipeline(repo, "deploy.yml")
        await update.message.reply_text(f"Pipeline triggered: {r.get('url', r.get('error',''))}")
        return

    if mode == "gh_status_repo":
        repo = text.strip()
        sessions.pop(uid, None)
        r = github_agent.get_pipeline_status(repo)
        await update.message.reply_text(f"Status: {r.get('status')}\nConclusion: {r.get('conclusion','...')}\n{r.get('run_url','')}")
        return

    if mode == "gh_logs_repo":
        repo   = text.strip()
        sessions.pop(uid, None)
        result = github_agent.get_pipeline_status(repo)
        for job in result.get("failed_jobs", [])[:2]:
            await _send_long(update, f"=== {job['name']} ===\n{job.get('log','')[-2000:]}")
        if not result.get("failed_jobs"):
            await update.message.reply_text("No failed jobs")
        return

    if mode == "gh_files_repo":
        repo  = text.strip()
        sessions.pop(uid, None)
        files = github_agent.get_existing_files(repo)
        lines = list(files.keys())
        await update.message.reply_text(f"Files in {repo}:\n" + "\n".join(f"  {f}" for f in lines) if lines else "Empty repo")
        return

    if mode == "gh_create_repo":
        repo = text.strip()
        sessions.pop(uid, None)
        r = github_agent.handle("create_repo", {"name": repo})
        await update.message.reply_text(f"{r.get('status')} — {r.get('url', r.get('error',''))}")
        return

    if mode == "gh_delete_repo":
        repo = text.strip()
        sessions.pop(uid, None)
        r = github_agent.handle("delete_repo", {"name": repo})
        await update.message.reply_text(f"Delete: {r.get('status')}")
        return

    if mode == "github_push":
        r = github_agent.handle("push_file", {"repo": sess["repo"], "path": sess["file"], "content": text})
        sessions.pop(uid, None)
        await update.message.reply_text(f"Pushed: {r.get('pushed', r.get('error'))}")
        return

    if mode == "push_approval":
        if text.lower() in ("yes","y"):
            repo = sess["answers"].get("repo") or sess["answers"].get("project","")
            r    = github_agent.handle("push_file", {"repo": repo, "path": sess["file"], "content": sess["content"]})
            await update.message.reply_text(f"Pushed: {r.get('pushed', r.get('error'))}")
        else:
            await update.message.reply_text("Cancelled.")
        sessions.pop(uid, None)
        return

    if mode == "fix_approval":
        if text.lower() in ("yes","y","ok"):
            await _apply_fix(update, uid, sess)
        else:
            sessions.pop(uid, None)
            await update.message.reply_text("Fix cancelled.")
        return

    if mode == "add_skill_name":
        sessions[uid] = {"mode": "add_skill", "skill_name": text.strip()}
        await update.message.reply_text(f"Paste skill content for '{text.strip()}':")
        return

    if mode == "add_skill":
        add_skill(sess["skill_name"], text)
        sessions.pop(uid, None)
        await update.message.reply_text(f"Skill '{sess['skill_name']}' saved.")
        return

    if mode == "code_response":
        match = re.search(r"push(?:\s+it)?\s+to\s+([\w/-]+)", text, re.IGNORECASE)
        if match:
            repo = match.group(1)
            file = sess.get("last_file", "output.txt")
            github_agent.handle("push_file", {"repo": repo, "path": file, "content": sess["last_code"]})
            sessions.pop(uid, None)
            await update.message.reply_text(f"Pushed to {repo}/{file}")
        return

    if mode == "confirm_deploy":
        if text.lower() in ("yes","y","ok","go","proceed"):
            asyncio.create_task(_run_deploy(update, uid, sess["answers"]))
        else:
            sessions.pop(uid, None)
            await update.message.reply_text("Cancelled.")
        return

    if mode == "collect":
        missing = sess.get("missing", [])
        answers = sess.get("answers", {})
        if missing:
            key = missing[0]
            val = text.strip()
            answers[key] = val
            missing.pop(0)
            sess["missing"] = missing
            sess["answers"] = answers
            if missing:
                await update.message.reply_text(FIELD_QUESTIONS.get(missing[0], f"{missing[0]}?"))
            else:
                await _show_confirm(update, uid, answers)
        return

    # ── Update flow: repo → file → content → push + trigger ──────────────────
    if mode == "update_repo":
        sess["answers"]["repo"] = text.strip()
        sessions[uid] = {"mode": "update_file", "answers": sess["answers"]}
        await update.message.reply_text("Which file to update? (e.g. html/index.html)")
        return

    if mode == "update_file":
        sess["answers"]["file"] = text.strip()
        sessions[uid] = {"mode": "update_content", "answers": sess["answers"]}
        await update.message.reply_text("Paste the new content:")
        return

    if mode == "update_content":
        sess["answers"]["content"] = text
        asyncio.create_task(_run_update(update, uid, sess["answers"]))
        return

    # ── Trigger flow ──────────────────────────────────────────────────────────
    if mode == "trigger_repo":
        repo = text.strip()
        sessions.pop(uid, None)
        await update.message.reply_text(f"Triggering deploy.yml in {repo}...")
        r = github_agent.handle("trigger", {"repo": repo, "workflow": "deploy.yml"})
        await update.message.reply_text(f"{r.get('status')} — {r.get('url', r.get('error',''))}")
        return

    # ── Destroy flow ──────────────────────────────────────────────────────────
    if mode == "destroy_project":
        project = text.strip()
        sessions[uid] = {"mode": "destroy_confirm", "answers": {"project": project}}
        await update.message.reply_text(f"Destroy {project}? Also delete GitHub repo? (yes / yes+repo / no)")
        return

    if mode == "destroy_confirm":
        answers = sess["answers"]
        answers["del_repo"] = "yes" if "repo" in text.lower() else "no"
        if text.lower().startswith("yes"):
            asyncio.create_task(_run_destroy(update, uid, answers))
        else:
            sessions.pop(uid, None)
            await update.message.reply_text("Cancelled.")
        return

    # ── Natural language ──────────────────────────────────────────────────────
    intent = extract_intent(text)

    if intent.get("intent") == "trigger":
        repo = intent.get("repo_name") or intent.get("project")
        if repo:
            await update.message.reply_text(f"Triggering pipeline in {repo}...")
            r = github_agent.handle("trigger", {"repo": repo, "workflow": "deploy.yml"})
            await update.message.reply_text(f"{r.get('status')} — {r.get('url', r.get('error',''))}")
        else:
            sessions[uid] = {"mode": "trigger_repo", "answers": {}}
            await update.message.reply_text("Repo name?")
        return

    if intent.get("intent") == "update":
        repo    = intent.get("repo_name")
        file_   = intent.get("file")
        if repo and file_:
            # Have both — go straight to content
            sessions[uid] = {"mode": "update_content", "answers": {"repo": repo, "file": file_}}
            await update.message.reply_text(f"Paste new content for {file_} in {repo}:")
        elif repo:
            sessions[uid] = {"mode": "update_file", "answers": {"repo": repo}}
            await update.message.reply_text("Which file to update? (e.g. html/index.html)")
        else:
            sessions[uid] = {"mode": "update_repo", "answers": {}}
            await update.message.reply_text("Repo name? (e.g. web-app2)")
        return

    if intent.get("intent") == "deploy":
        answers = {
            "project": intent.get("project"),
            "app":     intent.get("app"),
            "repo":    intent.get("repo_name") or intent.get("project"),
            "region":  intent.get("region", "us-east-1"),
            "cloud":   intent.get("cloud", "AWS"),
            "iac":     intent.get("iac", "terraform"),
            "config":  intent.get("config", "ansible"),
        }
        missing = [f for f in ["project","app","repo"] if not answers.get(f)]
        if missing:
            sessions[uid] = {"mode": "collect", "answers": answers, "missing": missing}
            await update.message.reply_text(FIELD_QUESTIONS.get(missing[0], f"{missing[0]}?"))
        else:
            answers["repo_name"] = answers["repo"]
            await _show_confirm(update, uid, answers)
        return

    if intent.get("intent") == "destroy":
        project = intent.get("project")
        if project:
            sessions[uid] = {"mode": "destroy_confirm", "answers": {"project": project}}
            await update.message.reply_text(f"Destroy {project}? Also delete GitHub repo? (yes / yes+repo / no)")
        else:
            sessions[uid] = {"mode": "destroy_project", "answers": {}}
            await update.message.reply_text("Which project to destroy?")
        return

    await update.message.reply_text(
        "I didn't understand that. Try:\n"
        "  deploy nginx to aws\n"
        "  update html in repo web-app2\n"
        "  trigger pipeline in web-app2\n"
        "  destroy web-app\n"
        "Or use /deploy /update /trigger /destroy"
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _show_confirm(update, uid, answers):
    sessions[uid] = {"mode": "confirm_deploy", "answers": answers}
    await update.message.reply_text(
        f"Ready:\n"
        f"  Project: {answers.get('project')}\n"
        f"  App:     {answers.get('app')}\n"
        f"  Repo:    {answers.get('repo_name') or answers.get('repo')}\n"
        f"  Region:  {answers.get('region','us-east-1')}\n\n"
        f"Proceed? (yes/no)"
    )

async def _send_long(update, text):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i+4000])

async def _run_deploy(update, uid, answers):
    sessions[uid] = {"mode": "running", "project": answers.get("project"), "answers": answers}
    set_running(uid, True)

    async def cb(msg):
        try: await update.message.reply_text(msg)
        except Exception: pass

    try:
        result = await orchestrator.deploy(
            user_id     = uid,
            project     = answers["project"],
            app         = answers["app"],
            repo_name   = answers.get("repo_name") or answers.get("repo") or answers["project"],
            region      = answers.get("region", "us-east-1"),
            progress_cb = cb,
        )
        if result["status"] == "success":
            await update.message.reply_text(
                f"Deployed!\nProject: {answers['project']}\nURL: http://{result['ip']}"
            )
        else:
            await update.message.reply_text(f"{result['status']}: {result.get('message','')}")
        sessions.pop(uid, None)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        sessions.pop(uid, None)
    finally:
        set_running(uid, False)


async def _apply_fix(update, uid, sess):
    set_running(uid, True)
    fix = sess["fix"]; answers = sess["answers"]

    async def cb(msg):
        try: await update.message.reply_text(msg)
        except Exception: pass

    try:
        result = await orchestrator.apply_fix_and_retry(
            user_id=uid, project=answers["project"],
            repo_name=answers.get("repo_name") or answers.get("repo"),
            file_path=fix["file"], fixed_content=fix["fixed_content"],
            retry=fix.get("retry", 1), progress_cb=cb,
        )
        await update.message.reply_text(
            f"Fixed! URL: {result.get('url')}" if result["status"] == "success"
            else f"Still failing: {result.get('message', result['status'])}"
        )
        sessions.pop(uid, None)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        sessions.pop(uid, None)
    finally:
        set_running(uid, False)


async def _run_update(update, uid, answers):
    """Push file to repo then trigger pipeline. No infra changes."""
    set_running(uid, True)
    repo      = answers.get("repo", "")
    file_path = answers.get("file", "")
    content   = answers.get("content", "")

    async def cb(msg):
        try: await update.message.reply_text(msg)
        except Exception: pass

    try:
        await cb(f"Pushing {file_path} to {repo}...")
        push = github_agent.push_single_file(repo, file_path, content, f"Update: {file_path}")

        if push.get("failed"):
            await update.message.reply_text(f"Push failed: {push['failed']}")
            return

        await cb(f"Pushed. Triggering pipeline...")
        # Cancel any running first
        cancelled = github_agent.cancel_running_pipelines(repo)
        if cancelled.get("cancelled"):
            await cb(f"Cancelled {len(cancelled['cancelled'])} running pipeline(s)")
            await asyncio.sleep(5)

        trigger = github_agent.handle("trigger", {"repo": repo, "workflow": "deploy.yml"})
        if trigger.get("status") == "error":
            await update.message.reply_text(f"Trigger failed: {trigger.get('error')}")
            return

        await update.message.reply_text(f"Pipeline running: {trigger.get('url','')}")
        sessions.pop(uid, None)

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        sessions.pop(uid, None)
    finally:
        set_running(uid, False)


async def _run_destroy(update, uid, answers):
    set_running(uid, True)

    async def cb(msg):
        try: await update.message.reply_text(msg)
        except Exception: pass

    try:
        dep  = state.get_deployment(answers["project"])
        repo = dep.get("repo", answers["project"]) if dep else answers["project"]
        result = await orchestrator.destroy(
            user_id=uid, project=answers["project"], repo_name=repo,
            delete_repo=answers.get("del_repo","no").lower()=="yes", progress_cb=cb,
        )
        await update.message.reply_text(result.get("message", result["status"]))
        sessions.pop(uid, None)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        sessions.pop(uid, None)
    finally:
        set_running(uid, False)


# ── App ───────────────────────────────────────────────────────────────────────

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(CommandHandler("reset",    cmd_reset))
    app.add_handler(CommandHandler("deploy",   cmd_deploy))
    app.add_handler(CommandHandler("update",   cmd_update))
    app.add_handler(CommandHandler("destroy",  cmd_destroy))
    app.add_handler(CommandHandler("trigger",  cmd_trigger))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("code",     cmd_code))
    app.add_handler(CommandHandler("github",   cmd_github))
    app.add_handler(CommandHandler("aws",      cmd_aws))
    app.add_handler(CommandHandler("skills",   cmd_skills))
    app.add_handler(CommandHandler("addskill", cmd_addskill))
    app.add_handler(CommandHandler("delskill", cmd_delskill))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
# DevOps Auto Agent — v1.0

Built by **Rafat Rahman**

Autonomous DevOps agent — deploy anything to AWS from a single Telegram message.

---

## What It Does

Send a message like `deploy nginx to aws` and the bot handles everything:
- Provisions EC2 on AWS (Terraform)
- Configures the server (Ansible)
- Pushes code to GitHub
- Runs the pipeline (GitHub Actions)
- Auto-fixes errors and retries up to 5 times
- Reports the live URL when done

---

## Prerequisites

| Requirement | Details |
|---|---|
| Python 3.10+ | `python --version` to check |
| AWS Account | IAM user with Access Key + Secret Key |
| GitHub Account | Personal Access Token with `repo` + `workflow` + `admin:repo_hook` scopes |
| Telegram Bot | Create via [@BotFather](https://t.me/BotFather) — copy the token |
| Anthropic API Key | From [console.anthropic.com](https://console.anthropic.com) |

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/rafatusa/devops-auto-agent.git
cd devops-auto-agent
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file

```env
TELEGRAM_TOKEN=your_telegram_bot_token
GITHUB_TOKEN=your_github_personal_access_token
GITHUB_USERNAME=your_github_username
AWS_ACCESS_KEY_ID=your_aws_access_key
AWS_SECRET_ACCESS_KEY=your_aws_secret_key
AWS_DEFAULT_REGION=us-east-1
ANTHROPIC_API_KEY=your_anthropic_api_key
```

> ⚠️ Never commit `.env` to GitHub — it is already in `.gitignore`

### 4. Run the bot

```bash
python bot.py
```

---

## Connect to the Bot

1. Open Telegram
2. Search for your bot username
3. Press **Start** or send `/start`
4. Done — you're connected

---

## Deploying

### Natural language

```
deploy nginx to aws
deploy nginx with docker to aws
deploy node app to aws
```

The bot asks step by step:

```
Bot: Project name?
You: my-app

Bot: What to deploy?
You: nginx with docker

Bot: GitHub repo name?
You: web-app2

Bot: Which branch?
You: feature/docker

Bot: AWS region?
You: us-east-1

Bot: Ready — proceed? (yes/no)
You: yes

Bot: ✓ Branch feature/docker created from main
     ✓ Generated 1 new file (Dockerfile)
     ✓ Pipeline running...
     ✓ Deployed! URL: http://54.x.x.x
     Pipeline succeeded on branch 'feature/docker'
     Create PR? (yes/no)
```

### Commands

```
/deploy          — guided deploy
/destroy         — destroy a deployment
/update          — update a file in a repo
/trigger <repo>  — trigger pipeline manually
/status          — project status
/projects        — list all projects
/stop            — stop current job
```

---

## GitHub Operations

Type `/github` for the interactive menu:

```
push      — push a file to any repo/branch
pull      — read a file from repo
trigger   — run pipeline on any branch
status    — pipeline status
logs      — last failed pipeline logs
files     — list files in repo
branches  — list all branches
branch    — create a branch from any source
pr        — create a pull request
merge     — merge any branch into another
```

### Example — feature branch workflow

```
/github → branch
Repo: web-app2  |  New branch: feature/docker  |  From: main
→ ✓ Branch created

(deploy to feature/docker — tests pass)

/github → pr
Repo: web-app2  |  From: feature/docker  |  To: main
→ ✓ PR: https://github.com/rafatusa/web-app2/pull/1
```

---

## Auto-Fix

If pipeline fails the bot automatically:
1. Reads the error logs
2. Fixes the broken file with AI
3. Pushes the fix
4. Retriggers the pipeline
5. Repeats up to **5 times**

---

## Project Structure

```
devops-auto-agent/
├── bot.py              # Telegram interface
├── orchestrator.py     # Deploy/destroy coordinator
├── state.py            # SQLite state tracking
├── requirements.txt
├── agents/
│   ├── code_agent.py   # AI file generation + fixing (Claude API)
│   ├── github_agent.py # GitHub operations (PyGithub)
│   ├── aws_agent.py    # AWS operations (boto3)
│   └── error_agent.py  # Log parsing
└── skills/
    ├── nginx.md
    ├── docker.md
    ├── terraform-aws.md
    ├── ansible.md
    └── pipeline.md
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Bot not responding | Make sure `python bot.py` is running |
| `404` on trigger | Check the exact repo name |
| Pipeline keeps failing | Run `/github logs <repo>` |
| EC2 stuck | Run `/aws cleanup <project>` |
| `.env` not loading | Check file is in project root, no spaces around `=` |

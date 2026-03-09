## CRITICAL: Inventory and hosts: must always match

The `hosts:` in playbook.yml must EXACTLY match the group name in the inventory.

```yaml
# In pipeline: ansible-playbook -i "1.2.3.4," playbook.yml
# The comma creates a single-host group called "all"
# So the playbook MUST use hosts: all

# WRONG — causes "Could not match supplied host pattern, ignoring: web_servers"
- name: Deploy app
  hosts: web_servers   # ← this group doesn't exist in the -i "1.2.3.4," inventory

# CORRECT
- name: Deploy app
  hosts: all           # ← always use "all" when inventory is -i "IP,"
```

**Rule: Always use `hosts: all` in playbook.yml when inventory is a raw IP with comma.**
The warning "Could not match supplied host pattern" means hosts: group ≠ inventory group.
This causes the entire play to skip silently — ansible exits 0 but does nothing.

# Ansible Best Practices

## Inventory format — always use comma after IP
```
ansible-playbook -i "1.2.3.4," --private-key /tmp/key -u ubuntu playbook.yml
```

## Always use become: yes for system tasks
## Always define handlers for service restarts
## Use copy module for files, template for Jinja2
## Use service module with enabled: yes
## Use apt with update_cache: yes and cache_valid_time: 3600

## Wait for SSH before running playbook
```bash
for i in $(seq 1 30); do
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
    -i /tmp/key ubuntu@SERVER_IP echo ok && break || sleep 10
done
```

## Common patterns
- Files: copy src to dest with mode and owner
- Services: state=started enabled=yes
- Packages: state=present with apt
- Directories: file module with state=directory

## CRITICAL: YAML Quoting Rules
These mistakes cause "YAML parsing failed: Colons in unquoted values" errors:

### Rule 1: ALWAYS quote name: values that contain colons
```yaml
# WRONG — colon in value causes YAML parse error
- name: Install docker-ce: latest version

# CORRECT — quoted
- name: "Install docker-ce: latest version"
```

### Rule 2: ALWAYS quote shell commands in name: fields
```yaml
# WRONG
- name: Run echo 'name: ansible'

# CORRECT
- name: "Run echo name ansible"
# Better — avoid colons in name fields entirely
- name: Echo test
```

### Rule 3: Never put Jinja2 {{ }} in name: without quotes
```yaml
# WRONG
- name: Deploy {{ app_name }}: production

# CORRECT
- name: "Deploy {{ app_name }}: production"
```

### Rule 4: shell/command values with colons need quoting too
```yaml
# WRONG
- shell: echo name: test

# CORRECT  
- shell: "echo name: test"
# Or use |
- shell: |
    echo 'name: test'
```

### Quick fix rule:
If error says "Colons in unquoted values" at line N:
→ Find line N in the playbook
→ Quote the entire value with double quotes
→ Or rewrite the name: to remove the colon entirely

## CRITICAL: copy module src paths must exist in the repo

The GitHub Actions runner only has files that are committed to the repo.
`src: ../app/` will ALWAYS fail if there is no `app/` folder in the repo.

### Rule: Never use copy module with a src path that isn't in the repo

```yaml
# WRONG — ../app/ doesn't exist in repo
- name: Copy application files
  copy:
    src: ../app/
    dest: /var/www/html/

# CORRECT option 1 — use content: inline for simple files
- name: Write index.html
  copy:
    content: |
      <html><body><h1>App deployed</h1></body></html>
    dest: /var/www/html/index.html

# CORRECT option 2 — clone/pull from git on the remote server
- name: Clone app from repo
  git:
    repo: "https://github.com/{{ lookup('env','GITHUB_REPOSITORY') }}.git"
    dest: /var/www/html
    version: HEAD
    force: yes

# CORRECT option 3 — use template module with inline content
- name: Deploy nginx config
  template:
    src: templates/nginx.conf.j2
    dest: /etc/nginx/sites-available/default
  # Only works if templates/ folder IS in the repo
```

### Rule: If src is a directory, that directory MUST be committed in the repo
- `src: ../html/` → only works if `html/` folder exists in the repo root
- `src: ../app/` → only works if `app/` folder exists in the repo root
- When in doubt — use `content:` inline or `git` module to pull from remote

### Rule: Path resolution in GitHub Actions
- Ansible runs from the `ansible/` directory
- `../html/` means the `html/` folder at repo root — must exist in repo
- `../app/` means the `app/` folder at repo root — must exist in repo
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

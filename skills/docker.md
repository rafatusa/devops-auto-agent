# Docker Deployment Skill

## Overview
Deploy a Dockerized app on AWS EC2 using Ansible.
Ansible installs Docker, builds or pulls image, runs container.

## Dockerfile (nginx example)
```dockerfile
FROM nginx:alpine
COPY html/index.html /usr/share/nginx/html/index.html
EXPOSE 80
```

## Ansible Playbook Pattern
```yaml
---
- name: Deploy Docker App
  hosts: all
  become: yes
  vars:
    project: "{{ lookup('env', 'PROJECT_NAME') }}"

  tasks:
    - name: Install dependencies
      apt:
        name: [apt-transport-https, ca-certificates, curl, gnupg, lsb-release]
        state: present
        update_cache: yes

    - name: Add Docker GPG key
      apt_key:
        url: https://download.docker.com/linux/ubuntu/gpg
        state: present

    - name: Add Docker repo
      apt_repository:
        repo: "deb [arch=amd64] https://download.docker.com/linux/ubuntu focal stable"
        state: present

    - name: Install Docker
      apt:
        name: [docker-ce, docker-ce-cli, containerd.io]
        state: present
        update_cache: yes

    - name: Start Docker
      service:
        name: docker
        state: started
        enabled: yes

    - name: Copy app files
      copy:
        src: ../
        dest: /opt/app/
        mode: '0755'

    - name: Stop existing container
      shell: docker stop app || true && docker rm app || true

    - name: Build image
      shell: docker build -t app:latest /opt/app/

    - name: Run container
      shell: docker run -d --name app -p 80:80 --restart always app:latest

    - name: Verify container running
      shell: docker ps | grep app
      register: result
      failed_when: result.rc != 0
```

## Key Rules
- Always install Docker CE (not docker.io)
- Always add `|| true` to stop/rm commands so they don't fail if container doesn't exist
- Use `--restart always` so container survives reboots
- Copy entire project directory to /opt/app/ so Dockerfile can access all files
- Build image on server from copied files — do NOT pull from registry unless specified
- Port mapping: always -p 80:80 for web apps
- Container name: always use project name or "app"

## Dockerfile Rules
- Use alpine variants for smaller images (nginx:alpine, node:alpine, python:slim)
- COPY html files into correct nginx path: /usr/share/nginx/html/
- For node apps: RUN npm install before COPY src
- EXPOSE the correct port

## Pipeline additions for Docker
No changes needed to deploy.yml — Ansible handles Docker installation
Terraform is identical — just EC2 + security group + key pair

## Files needed for docker-nginx
- terraform/main.tf
- ansible/playbook.yml
- html/index.html
- Dockerfile
- .github/workflows/deploy.yml
- .github/workflows/destroy.yml
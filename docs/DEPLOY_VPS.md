# Deploying on a VPS

A complete install on one Linux server with Docker: HTTPS, accounts, nightly
encrypted off-host backups, and upgrades with automatic rollback. Any provider
works (Hetzner, DigitalOcean, AWS Lightsail or EC2, Azure, an on-premises VM).
Nothing here is specific to one organisation: every setting is in
`deploy/.env.production`.

## 1. What you need

| | Minimum | Recommended |
|---|---|---|
| CPU / RAM | 2 vCPU / 4 GB | 4 vCPU / 8 GB for 20+ users |
| Disk | 40 GB SSD | 80 GB |
| OS | Ubuntu 24.04 LTS or Debian 12 | |
| DNS | an A (and AAAA) record for your domain, pointing at the server | |
| Off-host storage | an S3-compatible bucket (AWS S3, Backblaze B2, Wasabi, Cloudflare R2, MinIO) for backups | |
| An LLM key | Groq, OpenAI, Anthropic or Gemini | |

## 2. Harden the host first

Do this before installing anything that listens on the network. These follow
the CIS Ubuntu/Debian benchmarks' essentials.

```bash
# A non-root admin user with your SSH key
adduser ops && usermod -aG sudo ops
mkdir -p /home/ops/.ssh && cp ~/.ssh/authorized_keys /home/ops/.ssh/ && chown -R ops: /home/ops/.ssh

# SSH: keys only, no root, no passwords
sudo tee /etc/ssh/sshd_config.d/10-hardening.conf >/dev/null <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
MaxAuthTries 3
EOF
sudo systemctl restart ssh        # test a new session BEFORE closing this one

# Firewall: only SSH, HTTP and HTTPS
sudo apt-get update && sudo apt-get install -y ufw unattended-upgrades fail2ban
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw allow 443/udp
sudo ufw enable

# Automatic security updates
sudo dpkg-reconfigure -plow unattended-upgrades

# Accurate time (TLS, TOTP and logs depend on it)
timedatectl status | grep -q 'synchronized: yes' || sudo timedatectl set-ntp true
```

> **Docker and ufw:** ports that Docker publishes bypass ufw rules. That is why
> the compose file publishes **only** Caddy's 80/443. The databases have no
> published ports and sit on an internal network with no route out. Do not add
> `ports:` to any other service.

Restrict SSH further if your network allows it, for example
`sudo ufw allow from <office-ip> to any port 22`. Also consider your
provider's cloud firewall as a second layer.

## 3. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ops && newgrp docker
docker compose version          # must be v2
```

## 4. Install Brahmastra

```bash
git clone https://github.com/<your-org>/vaultgraph-qy.git brahmastra && cd brahmastra
git checkout v0.2.0                                   # a release tag, never a branch

cp deploy/.env.production.example deploy/.env.production
chmod 600 deploy/.env.production
nano deploy/.env.production    # fill in: LLM keys, RESTIC_REPOSITORY + AWS_* keys,
                               # ALERT_WEBHOOK_URL, heartbeat URLs (optional), TZ

cd deploy
./setup.sh --domain kb.example.com --email ops@example.com --admin you@example.com
```

`setup.sh`:
- checks Docker, memory, disk, free ports and DNS;
- generates every empty secret without printing it;
- builds and starts the stack;
- waits until it is healthy;
- creates the first admin, prompting for the password;
- takes a first backup.

It is safe to run again: it keeps existing secrets and accounts.

**Save `RESTIC_PASSWORD`** from `deploy/.env.production` in your password
manager now. It is the only key to your backups.

For unattended installs (Ansible, cloud-init), pass the password on stdin:

```bash
./setup.sh --domain kb.example.com --email ops@example.com \
  --admin you@example.com --admin-password-stdin < /run/secrets/admin_password
```

## 5. After install

1. Sign in at `https://kb.example.com` and add your team under accounts
   (`POST /v1/auth/users`, or the settings screen once it ships).
2. **Monitoring:** point an uptime monitor at `https://kb.example.com/healthz`.
   Set `BACKUP_HEARTBEAT_URL` and `RESTORE_TEST_HEARTBEAT_URL` to dead-man's
   switches.
3. **Test a restore now:**
   `docker compose -f docker-compose.prod.yml --env-file .env.production run --rm backup restore-test`.
4. Connect assistants: each person creates a personal API token. See
   [GETTING_STARTED.md](GETTING_STARTED.md).

## 6. What the install looks like

```
internet ──443──► Caddy (TLS, HSTS, /healthz)
                    ├──► dashboard (Next.js)  ──┐
                    └──► /v1 API (FastAPI)  ◄───┘
                              │  edge network: outbound to LLM APIs, Notion
                    ──────────┼────────────── internal network (no route out)
                              ├──► Postgres  (system of record: notes, accounts)
                              └──► Neo4j     (graph: rebuilt from notes)
                    backup ──► Postgres dump ──► restic (encrypted) ──► S3
```

Every container drops all Linux capabilities, adds back only what its
entrypoint needs, and runs with `no-new-privileges`, a memory limit and log
rotation. The app containers also run as non-root users on a read-only
filesystem.

Day-two operations are covered in [OPERATIONS.md](OPERATIONS.md): backups,
restores, upgrades, rollbacks and rebuilding the graph.

## Security notes and current limits

- **Secrets** live in `deploy/.env.production` (mode 600) and reach containers
  as environment variables, so anyone who can run `docker inspect` on the host
  can read them. Limit host access accordingly. Moving to Docker secrets or a
  vault is on the roadmap.
- **Remote MCP** (`/v1/mcp`, with `MCP_HTTP=1`) accepts admin tokens only,
  until its tools are scoped per user.
- **Single host:** this layout runs on one server. High availability (managed
  Postgres, several app instances) is a later phase.

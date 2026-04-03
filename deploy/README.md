# Deploying auto-ig

This guide covers deploying auto-ig on Railway (recommended) or a VPS.

---

## Prerequisites

- **API keys** ready:
  - Google Gemini API key (free tier)
  - Unsplash API key
  - Pexels API key
  - Meta Graph API long-lived token (see TODO.md section 1)
  - Discord bot token + channel ID

---

## Option A: Railway (recommended)

### 1. Create the project

1. Sign up at https://railway.com
2. Create a new project, connect your GitHub repo
3. Add a `Procfile` to the repo root:
   ```
   worker: python main.py --account veggie_alternatives
   ```

### 2. Set environment variables

In the Railway dashboard, add all env vars:

```
GEMINI_API_KEY=...
UNSPLASH_ACCESS_KEY=...
PEXELS_API_KEY=...
VEGGIE_IG_TOKEN=...              # Meta Graph API long-lived token
DISCORD_BOT_TOKEN=...            # From Discord Developer Portal
DISCORD_CHANNEL_ID=...           # Right-click channel > Copy Channel ID
PUBLIC_IP=                       # Leave empty — Railway provides a public URL
```

For multi-account, add additional account-specific variables:

```
FITNESS_IG_TOKEN=...
FITNESS_DISCORD_CHANNEL_ID=...
```

### 3. Attach a Volume

Create a Volume in Railway and mount it at `/app/data` for SQLite persistence.

### 4. Enable public URL

In the service's networking settings, enable the public URL. This is needed for Meta's servers to fetch images during publishing.

### 5. Deploy

Railway builds and deploys automatically on git push. Monitor logs in the dashboard.

---

## Option B: VPS deployment (Oracle Cloud / other)

### 1. Create the VM

SSH into your VM:

```bash
ssh -i ~/.ssh/auto-ig ubuntu@<PUBLIC_IP>
```

### 2. Install auto-ig

```bash
git clone https://github.com/Seb281/auto-ig.git
cd auto-ig
bash deploy/setup.sh
```

The setup script will:
- Install Python 3.11+ and create a virtualenv
- Install all Python dependencies
- Create `.env` from `.env.example`
- Open port 8765 in iptables
- Install and enable the systemd service

### 3. Configure environment variables

```bash
nano .env
```

Required variables:

```
GEMINI_API_KEY=...
UNSPLASH_ACCESS_KEY=...
PEXELS_API_KEY=...
VEGGIE_IG_TOKEN=...              # Meta Graph API long-lived token
DISCORD_BOT_TOKEN=...            # From Discord Developer Portal
DISCORD_CHANNEL_ID=...           # Right-click channel > Copy Channel ID
PUBLIC_IP=<your VM public IP>
```

### 4. Open port 8765

Meta Graph API must reach the temporary image server. Open the port in your cloud provider's firewall:

**Oracle Cloud**:
1. Go to Networking > Virtual Cloud Networks > your VCN > Subnets > your Subnet > Security Lists
2. Add an Ingress Rule: Source CIDR `0.0.0.0/0`, TCP, Port `8765`

**Other VPS**: Usually iptables is enough (handled by setup.sh).

### 5. Start the service

```bash
sudo systemctl start auto-ig
```

Check status and logs:

```bash
sudo systemctl status auto-ig
journalctl -u auto-ig -f
```

You should see:
```
auto-ig started for account 'veggie_alternatives'
AsyncIOScheduler created.
Starting Discord bot...
APScheduler started.
Discord bot is running. Press Ctrl+C to stop.
```

---

## Smoke test with --dry-run

Before enabling live publishing:

```bash
# On Railway: use the Railway CLI or dashboard to run a one-off command
# On VPS:
cd ~/auto-ig
.venv/bin/python main.py --account veggie_alternatives --dry-run
```

Then send `!run` in your Discord channel. The pipeline will:
- Generate a brief (Content Planner)
- Source an image (stock or Gemini)
- Write a caption
- Run the reviewer
- Send you the draft on Discord
- **Skip** the actual Instagram publish

---

## Service management (VPS)

| Command | Action |
|---|---|
| `sudo systemctl start auto-ig` | Start the service |
| `sudo systemctl stop auto-ig` | Stop the service |
| `sudo systemctl restart auto-ig` | Restart the service |
| `sudo systemctl status auto-ig` | Check service status |
| `journalctl -u auto-ig -f` | Follow live logs |
| `journalctl -u auto-ig --since today` | View today's logs |

---

## Updating

```bash
# VPS
cd ~/auto-ig
sudo systemctl stop auto-ig
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl start auto-ig

# Railway: just git push — auto-deploys
```

---

## Multi-account setup

Each account has:
- Its own `accounts/<account_id>/config.yaml` configuration
- Its own `post_history.db` SQLite database
- Its own scheduler job (independent posting frequency and timing)
- Its own Discord channel ID for routing commands

### Requirements

1. **Same Discord bot token**: All accounts share the same Discord bot. Each account routes commands via its unique `discord_channel_id_env` value.
2. **Different channel IDs**: Each account must have a distinct Discord channel ID.
3. **Different temp_http_port**: Each account should use a unique `temp_http_port` to avoid port conflicts during simultaneous publishes.

### Adding a new account

1. Create `accounts/my_new_account/config.yaml` (copy and modify from existing):
   ```bash
   mkdir -p accounts/my_new_account
   cp accounts/veggie_alternatives/config.yaml accounts/my_new_account/config.yaml
   ```
2. Edit key fields:
   - `account_id`: must match the directory name
   - `instagram_user_id_env`: unique env var name (e.g., `MY_NEW_IG_USER_ID`)
   - `access_token_env`: unique env var name (e.g., `MY_NEW_IG_TOKEN`)
   - `discord_channel_id_env`: unique env var name (e.g., `MY_NEW_DISCORD_CHANNEL_ID`)
   - `temp_http_port`: unique port number (e.g., 8767)
   - `niche`, `content_pillars`, `tone`, etc.
3. Add env vars to `.env` (or Railway dashboard)
4. Update `Procfile` or systemd `ExecStart`:
   ```
   worker: python main.py --account veggie_alternatives --account my_new_account
   ```

---

## Troubleshooting

### Service fails to start

```bash
journalctl -u auto-ig -n 50 --no-pager
```

Common causes:
- **Missing env vars**: `ValueError: Missing or empty environment variables: ...`
- **Config file not found**: check `accounts/<account_id>/config.yaml` exists
- **Python not found**: verify `.venv/bin/python` exists; re-run `deploy/setup.sh`

### Meta API returns auth errors

- Token expired: long-lived tokens last 60 days. Refresh before expiry.
- Permissions missing: ensure `instagram_content_publish` is granted.
- The bot sends a Discord alert on auth errors automatically.

### Port 8765 not reachable

1. Check cloud firewall (ingress rule for TCP 8765)
2. Check VM iptables: `sudo iptables -L -n | grep 8765`
3. Check nothing else is using the port: `sudo lsof -i :8765`

### Database locked errors

Ensure only one instance of auto-ig is running:

```bash
ps aux | grep main.py
```

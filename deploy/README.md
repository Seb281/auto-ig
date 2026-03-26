# Deploying auto-ig on Oracle Cloud Free Tier

This guide covers setting up auto-ig as a long-running systemd service on an Oracle Cloud Always Free VM.

---

## Prerequisites

- **Oracle Cloud account** with Always Free tier access
- **SSH client** (Terminal on macOS/Linux, PuTTY on Windows)
- **API keys** ready:
  - Anthropic API key (Claude)
  - OpenAI API key (DALL-E 3 fallback)
  - Unsplash API key
  - Pexels API key
  - Meta Graph API System User token (see below)
  - Telegram bot token + chat ID

---

## 1. Create the Oracle Cloud VM

### Instance shape

| Setting | Value |
|---|---|
| Shape | VM.Standard.A1.Flex (ARM) |
| OCPUs | 1–4 (free up to 4) |
| Memory | 6–24 GB (free up to 24 GB) |
| Image | Canonical Ubuntu 22.04 (or 24.04) |
| Boot volume | 50 GB (free up to 200 GB) |

### SSH key

Generate an SSH key pair if you do not have one:

```bash
ssh-keygen -t ed25519 -C "auto-ig" -f ~/.ssh/auto-ig
```

Upload the **public** key (`~/.ssh/auto-ig.pub`) when creating the instance.

### Networking

Oracle creates a VCN (Virtual Cloud Network) automatically. Note the **public IP** assigned to your instance — you will need it for:
- SSH access
- The `PUBLIC_IP` environment variable (Meta Graph API fetches images from this IP)

---

## 2. Open port 8765 (temp HTTP server)

Meta Graph API must be able to reach the temporary image server. This requires opening the port in **two** places:

### 2a. Oracle Cloud Security List (web console)

1. Go to **Networking > Virtual Cloud Networks** > click your VCN
2. Click your **Subnet** > click the **Security List**
3. **Add Ingress Rule**:
   - Source Type: CIDR
   - Source CIDR: `0.0.0.0/0`
   - IP Protocol: TCP
   - Destination Port Range: `8765`
4. Save

### 2b. VM-level firewall (iptables)

The setup script handles this automatically. To do it manually:

```bash
sudo iptables -I INPUT -p tcp --dport 8765 -j ACCEPT
sudo apt-get install -y iptables-persistent
sudo netfilter-persistent save
```

### Verify port is open

From your local machine:

```bash
# On the VM, start a quick test server:
python3 -m http.server 8765 &

# From your local machine:
curl -v http://<PUBLIC_IP>:8765/

# Kill the test server on the VM:
kill %1
```

---

## 3. Install auto-ig

SSH into your VM:

```bash
ssh -i ~/.ssh/auto-ig ubuntu@<PUBLIC_IP>
```

Clone the repository and run setup:

```bash
git clone https://github.com/your-repo/auto-ig.git
cd auto-ig
bash deploy/setup.sh
```

The setup script will:
- Install Python 3.11+ and create a virtualenv
- Install all Python dependencies
- Create `.env` from `.env.example`
- Open port 8765 in iptables
- Install and enable the systemd service

---

## 4. Configure environment variables

Edit `.env` with your actual API keys:

```bash
nano .env
```

Required variables:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
UNSPLASH_ACCESS_KEY=...
PEXELS_API_KEY=...
VEGGIE_IG_TOKEN=...         # Meta Graph API System User token
TELEGRAM_BOT_TOKEN=...      # From @BotFather
TELEGRAM_CHAT_ID=...        # Your Telegram chat ID
PUBLIC_IP=<your VM public IP>
```

### Getting a non-expiring Meta token

Standard User Access Tokens expire every 60 days. Use a **System User token** instead:

1. Go to [Meta Business Manager](https://business.facebook.com/settings)
2. Navigate to **Users > System Users**
3. Create a System User (Admin role)
4. Click **Generate New Token**
5. Select your app, grant `instagram_content_publish`, `instagram_basic`, `pages_read_engagement`
6. Copy the token — it **never expires**

### Getting your Telegram chat ID

1. Message your bot on Telegram
2. Visit `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
3. Find `"chat":{"id": <NUMBER>}` in the response — that number is your chat ID

---

## 5. Start the service

```bash
sudo systemctl start auto-ig
```

Check that it started successfully:

```bash
sudo systemctl status auto-ig
```

View live logs:

```bash
journalctl -u auto-ig -f
```

You should see:
```
auto-ig started for account 'veggie_alternatives'
AsyncIOScheduler created.
Loaded schedule config for 'veggie_alternatives': ...
Starting Telegram bot (polling)...
APScheduler started.
Telegram bot is running. Press Ctrl+C to stop.
```

---

## 6. Smoke test with --dry-run

Before enabling live publishing, run a dry-run to verify the full pipeline:

```bash
# Stop the service temporarily
sudo systemctl stop auto-ig

# Run manually with --dry-run
cd ~/auto-ig
.venv/bin/python main.py --account veggie_alternatives --dry-run
```

Then send `/run` to your Telegram bot. The pipeline will:
- Generate a brief (Content Planner)
- Source an image (stock or DALL-E)
- Write a caption
- Run the reviewer
- Send you the draft on Telegram

But it will **not** publish to Instagram.

Once satisfied, start the service again:

```bash
sudo systemctl start auto-ig
```

---

## 7. Service management

| Command | Action |
|---|---|
| `sudo systemctl start auto-ig` | Start the service |
| `sudo systemctl stop auto-ig` | Stop the service |
| `sudo systemctl restart auto-ig` | Restart the service |
| `sudo systemctl status auto-ig` | Check service status |
| `sudo systemctl enable auto-ig` | Enable start on boot (done by setup.sh) |
| `sudo systemctl disable auto-ig` | Disable start on boot |
| `journalctl -u auto-ig -f` | Follow live logs |
| `journalctl -u auto-ig --since "1 hour ago"` | View recent logs |
| `journalctl -u auto-ig --since today` | View today's logs |

---

## 8. Updating auto-ig

```bash
cd ~/auto-ig
sudo systemctl stop auto-ig
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl start auto-ig
```

---

## 9. Backup

The SQLite database at `accounts/veggie_alternatives/post_history.db` contains all post history, pending drafts, and schedule configuration. Back it up periodically:

```bash
# Manual backup
cp accounts/veggie_alternatives/post_history.db \
   accounts/veggie_alternatives/post_history.db.bak

# Automated daily backup (add to crontab)
crontab -e
# Add this line:
0 3 * * * cp ~/auto-ig/accounts/veggie_alternatives/post_history.db \
              ~/auto-ig/accounts/veggie_alternatives/post_history.$(date +\%Y\%m\%d).db
```

---

## Troubleshooting

### Service fails to start

```bash
journalctl -u auto-ig -n 50 --no-pager
```

Common causes:
- **Missing env vars**: `ValueError: Missing or empty environment variables: ...`
  - Fix: edit `.env` and fill in all required keys
- **Config file not found**: check `accounts/veggie_alternatives/config.yaml` exists
- **Python not found**: verify `.venv/bin/python` exists; re-run `deploy/setup.sh`

### Meta API returns auth errors

- Token expired: if you used a standard token, it expires after 60 days. Switch to a System User token (see section 4).
- Permissions missing: ensure `instagram_content_publish` is granted.
- The bot sends a Telegram alert on auth errors automatically.

### Port 8765 not reachable

1. Check Oracle Cloud Security List (ingress rule for TCP 8765)
2. Check VM iptables: `sudo iptables -L -n | grep 8765`
3. Check nothing else is using the port: `sudo lsof -i :8765`
4. Test from the VM itself: `curl http://localhost:8765/` (should return 404 or connection refused)

### Oracle Cloud idle reclaim

Oracle may reclaim Always Free instances that are idle (< 20% average CPU over 7 days). The Telegram bot's polling provides continuous light activity, which typically prevents reclaim. If reclaimed:
1. The instance is stopped (not deleted)
2. Start it again from the Oracle Cloud Console
3. The service will auto-start on boot (if enabled via `systemctl enable`)

### High memory usage

If the VM runs low on memory (especially with the 1 GB e2-micro backup plan):
- Check with `free -h` and `top`
- Consider reducing APScheduler's `misfire_grace_time`
- Ensure temp images are being cleaned up (check `storage/media/`)

### Database locked errors

SQLite locks can occur if multiple processes access the database. Ensure only one instance of auto-ig is running:

```bash
ps aux | grep main.py
```

If duplicates exist, kill the extras and rely on the systemd service only.

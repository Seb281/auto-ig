# TODO — What's needed to run auto-ig in production

The codebase is complete. Everything below is external setup and configuration.

---

## 1. Create API accounts and get credentials

Each service below needs an account and an API key. All keys go into `.env`.

### Google Gemini — powers all AI agents + image generation
- Sign up at https://ai.google.dev and open Google AI Studio
- Create an API key (no billing required)
- **Cost**: Free tier. 1,500 requests/day on Gemini 2.0 Flash, ~500 images/day on Gemini 2.5 Flash Image. More than enough for daily posting.
- **Covers**: content planning, caption writing, image scoring (vision), review, and fallback image generation (replaces both Claude and DALL-E 3)
- **Caveat**: Free tier data may be used by Google. No SLA. Limits can change without notice.
- `.env`: `GEMINI_API_KEY=...`

### Unsplash — primary stock photo search
- Sign up at https://unsplash.com/developers
- Register a new application, get the Access Key
- **Cost**: Free. 50 requests/hour.
- `.env`: `UNSPLASH_ACCESS_KEY=...`

### Pexels — secondary stock photo search
- Sign up at https://www.pexels.com/api
- Get your API key from the dashboard
- **Cost**: Free. 200 requests/hour.
- `.env`: `PEXELS_API_KEY=...`

### Discord Bot — control interface
1. Go to https://discord.com/developers/applications
2. Click "New Application", give it a name
3. Go to "Bot" in the left sidebar, click "Reset Token" to get your bot token
4. Under "Privileged Gateway Intents", enable **Message Content Intent**
5. Go to "OAuth2" > "URL Generator", select scopes: `bot`, permissions: `Send Messages`, `Attach Files`, `Read Message History`
6. Open the generated URL to invite the bot to your server
7. In Discord, right-click the text channel you want to use > "Copy Channel ID" (enable Developer Mode in Settings > Advanced if needed)
8. `.env`:
   ```
   DISCORD_BOT_TOKEN=your-bot-token
   DISCORD_CHANNEL_ID=123456789012345678
   ```

### Meta Graph API (Instagram) — publishing posts
You need a Business or Creator Instagram account connected to a Facebook Page.

**Prerequisites** (one-time, ~15-30 min):
1. **Convert your Instagram account** to a Business account (Settings > Account > Switch to Professional Account)
2. **Create a Facebook Page** and link your Instagram account to it (Page Settings > Instagram)
3. **Create a Meta Developer App** at https://developers.facebook.com
   - App type: "Business"
   - Add the "Instagram Graph API" product
4. **Get your Instagram User ID**: Use the Graph API Explorer to call `GET /me?fields=id` with an Instagram-scoped token, or find it in the Instagram app (Settings > Account > About)

**Get a token** (fastest path, ~10 min):
1. Go to https://developers.facebook.com/tools/explorer/
2. Select your app, click "Get User Access Token"
3. Check these permissions: `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`
4. Click "Generate Access Token", approve the dialog
5. Copy the short-lived token (~1-2 hours)
6. **Exchange it for a 60-day long-lived token**:
   ```
   GET https://graph.facebook.com/v22.0/oauth/access_token
     ?grant_type=fb_exchange_token
     &client_id={APP-ID}
     &client_secret={APP-SECRET}
     &fb_exchange_token={SHORT-LIVED-TOKEN}
   ```
   The response contains your long-lived token (valid 60 days).
7. Update config and .env:
   - `accounts/veggie_alternatives/config.yaml`: set `instagram_user_id` to your actual IG user ID
   - `.env`: `VEGGIE_IG_TOKEN=<long-lived-token>`

**Note**: This token expires in 60 days. Set a calendar reminder to refresh it. See section 7 for the production upgrade path (System User token, never expires).

---

## 2. Set up hosting

The bot needs to run 24/7. It also needs a public URL so that Meta's servers can fetch images during publishing.

### Option A: Railway (recommended for testing)
- **Cost**: Free 30-day trial ($5 credit), then $1/month
- **Specs**: 1 vCPU, 0.5 GB RAM, 0.5 GB volume storage
- **Why**: Public HTTPS URL out of the box (`*.up.railway.app`), git-push deploy, auto-restart on crash. No VPS management, no firewall config, no systemd.
- **Setup**:
  1. Sign up at https://railway.com
  2. Create a new project, connect your GitHub repo
  3. Add a `Procfile` to the repo root: `worker: python main.py --account veggie_alternatives`
  4. Set environment variables in the Railway dashboard (all the keys from step 1)
  5. Create a Volume in Railway and mount it at `/app/accounts` (for SQLite persistence)
  6. Deploy — Railway builds and runs automatically on git push
- **No `PUBLIC_IP` needed**: Railway provides a public HTTPS URL. Configure it in the service's networking settings.

### Option B: Oracle Cloud Always Free
- **Cost**: Free (Always Free tier)
- **Specs**: 4 ARM Ampere cores, 24 GB RAM, 200 GB storage
- **Setup**:
  1. Create account at https://www.oracle.com/cloud/free
  2. Launch a Compute instance: Shape `VM.Standard.A1.Flex`, Image: Ubuntu 22.04+
  3. Create and download an SSH key pair
  4. Note the public IP from the instance details page
  5. `.env`: `PUBLIC_IP=<your-vm-public-ip>`
- **Tradeoff**: Free but more complex setup (SSH, systemd, iptables, cloud firewall rules)

### Option C: Any VPS with a public IP
- Hetzner, DigitalOcean, Linode, Vultr — all work
- **Cost**: $4-6/month for the cheapest tier
- Must have a public IP and ability to open ports

### Avoid
- **Render**: Spins down on free tier (bot stops responding)

---

## 3. Deploy to the server

### Railway deployment
If using Railway (Option A), deployment is automatic on git push. Key steps:
1. Ensure `Procfile` exists in repo root
2. Set all env vars in the Railway dashboard
3. Attach a Volume for SQLite persistence (mount at `/app/accounts`)
4. Enable the public URL in networking settings for image serving
5. Monitor logs in the Railway dashboard

### VPS deployment (Oracle Cloud / other)
```bash
# SSH into your VM
ssh -i <key> ubuntu@<public-ip>

# Clone the repo
git clone https://github.com/Seb281/auto-ig.git
cd auto-ig

# Run the setup script (installs Python, venv, deps, systemd service, iptables)
bash deploy/setup.sh
```

The setup script handles:
- Python 3.11+ installation
- Virtualenv creation and dependency install
- Copying `.env.example` to `.env`
- Opening port 8765 in iptables
- Installing and enabling the systemd service

### Fill in credentials (VPS only)
```bash
nano .env
# Paste in all the API keys and tokens from step 1
```

### Open the temp HTTP port in your cloud provider's firewall (VPS only)

This is separate from the VM-level iptables (which setup.sh handles). Your cloud provider also has a network firewall.

**Oracle Cloud**:
1. Go to Networking > Virtual Cloud Networks > your VCN > Subnets > your Subnet > Security Lists
2. Add an Ingress Rule:
   - Source CIDR: `0.0.0.0/0`
   - IP Protocol: TCP
   - Destination Port Range: `8765`
3. If running multiple accounts, also open port `8766` (or whatever `temp_http_port` is set to in each account's config.yaml)

**Google Cloud**: Firewall rules > Create > Allow TCP 8765 ingress from 0.0.0.0/0

**Other VPS**: Usually no extra firewall — iptables is enough.

---

## 4. Configure your accounts

### Edit the existing account
File: `accounts/veggie_alternatives/config.yaml`

What to change:
- `instagram_user_id`: Replace `"<IG_USER_ID>"` with your actual Instagram User ID
- Everything else is pre-configured with sensible defaults for a vegetarian food account. Adjust `niche`, `tone`, `allowed_products`, `banned_topics`, `content_pillars`, and `visual_style` to match your brand.

### Adding a new account
1. Create the directory: `mkdir accounts/<account_id>`
2. Copy an existing config: `cp accounts/veggie_alternatives/config.yaml accounts/<account_id>/config.yaml`
3. Edit the new config:
   - `account_id`: must match the directory name exactly
   - `access_token_env`: a unique env var name (e.g., `"MYACCOUNT_IG_TOKEN"`)
   - `discord_channel_id_env`: unique if different channel, or shared if same Discord channel
   - `temp_http_port`: must be unique per account (e.g., 8766, 8767)
   - Customize niche, tone, products, pillars, etc.
4. Add the new env vars to `.env` (or Railway dashboard)
5. Open the new port in your cloud firewall (VPS only — Railway handles this)
6. Update the `Procfile` or systemd `ExecStart` to include `--account <account_id>`

### Removing the fitness_meals example account
The `accounts/fitness_meals/` config exists as an example. If you're not using it, just don't pass `--account fitness_meals` when starting. On Railway, update the Procfile. On VPS, edit the systemd service:
```bash
sudo nano /etc/systemd/system/auto-ig.service
# Change ExecStart to only include the accounts you want
sudo systemctl daemon-reload
sudo systemctl restart auto-ig
```

---

## 5. Test before going live

### Dry-run (full pipeline, no Instagram publish)
```bash
# On Railway: use the Railway CLI or dashboard to run a one-off command
# On VPS:
cd /home/ubuntu/auto-ig
.venv/bin/python main.py --account veggie_alternatives --dry-run
```
Then open Discord, send `!run` in the bot's channel. The pipeline will:
- Generate a topic (Gemini)
- Find/generate an image (Unsplash/Pexels/Gemini)
- Write a caption (Gemini)
- Run review (Gemini vision)
- Send you the draft on Discord
- **Skip** the actual Instagram publish

This validates that all API keys work, the pipeline runs end-to-end, and Discord is connected.

### First real posts
After dry-run works:
1. Start the service (VPS: `sudo systemctl start auto-ig` / Railway: deploy via git push)
2. Monitor logs (VPS: `journalctl -u auto-ig -f` / Railway: dashboard logs)
3. The scheduler will trigger at `preferred_time` in your timezone
4. Or send `!run` in Discord to trigger immediately
5. Review the first 5-10 drafts manually in Discord before trusting auto-publish

---

## 6. Ongoing operations

### Discord commands available
| Command | What it does |
|---------|-------------|
| `!start` | Show welcome message and command list |
| `!run` | Trigger pipeline immediately |
| `!status` | Show scheduler status, next run time |
| `!pause` | Pause the scheduler |
| `!resume` | Resume the scheduler |
| `!setfrequency <value>` | Change frequency: `1d`, `2d`, `3x`, `2x`, `1x`, or `HH:MM` for time |
| `!approve` | Approve pending draft for publish |
| `!skip` | Discard draft, generate a new one |
| `!edit <caption>` | Replace caption and publish |
| `!regenerate` | Discard and regenerate from scratch |
| `!approve_anyway` | Publish despite reviewer failure |
| `!skip_today` | Skip today's post entirely |
| `!suggest <topic>` | Suggest a topic for the next post |
| Send a photo | Upload a photo for the next post (skips stock/Gemini) |

### Log monitoring (VPS)
```bash
# Live logs
journalctl -u auto-ig -f

# Last 100 lines
journalctl -u auto-ig -n 100

# Logs from today
journalctl -u auto-ig --since today
```

### Restart / update
```bash
# VPS
cd /home/ubuntu/auto-ig
git pull
sudo systemctl restart auto-ig

# Railway: just git push — auto-deploys
```

---

## 7. Control interface alternatives

Currently using Discord. If reconsidering in the future, here are the evaluated options:

| Option | Cost | Async Python | Setup | Pros | Cons |
|--------|------|-------------|-------|------|------|
| **Discord** (current) | Free | `discord.py` — excellent | Bot token + channel ID | Closest to Telegram, mature library, 1:1 migration | Requires Discord account |
| **Slack** | Free (limited) | `slack-bolt` AsyncApp | OAuth scopes, Socket Mode | Good command framework, slash commands | 90-day history limit, 10 app cap, must `ack()` within 3s |
| **Matrix/Element** | Free | `matrix-nio` / `nio-bot` | Register bot on matrix.org | Open-source, self-hostable, no vendor lock-in | Smaller ecosystem, E2E encryption adds complexity |
| **Ntfy + web UI** | Free (self-host) or $5 one-time (Pushover) | `httpx` / `aiohttp` | Minimal | No chat platform dependency, lightweight | Less interactive, need to build a small web UI for commands |
| **Web dashboard** | Free (hosting cost only) | FastAPI | Build from scratch | Full control, best UX potential, no third-party dependency | 5-10x more code, need auth + frontend + push notifications |
| **WhatsApp** | ~$1-3/mo | No async SDK | Business verification (weeks) | Same Meta ecosystem | Complex setup, template approvals, AI bot restrictions |
| **Signal** | Free | `signalbot` (immature) | signal-cli Java daemon + spare phone number | Privacy-focused | Fragile, unofficial, Java dependency, two-process architecture |
| **Email** | Free | `aioimaplib` + `aiosmtplib` | Low | No new accounts needed | Terrible UX for interactive commands, latency, spam risk |

---

## 8. Not yet built (optional / future)

These are not blockers — the bot works fully without them.

- **Migrate to production AI providers**: Currently using Google Gemini free tier for all AI tasks (text, vision, image generation). When ready for production quality, consider Claude (better reasoning) and DALL-E 3 (better image generation) — would require adding `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` and updating `utils/ai_client.py`.
- **System User token for Instagram**: Current setup uses a 60-day long-lived User Token. For never-expiring access, create a System User token via Meta Business Manager (requires Business Verification — can take hours to days). See Meta docs on System Users.
- **Token refresh automation**: If staying with the 60-day User Token, add a cron job or manual process to refresh it before expiry.
- **Unit tests**: No test files exist. Consider adding `pytest` + `pytest-asyncio` tests for the agents and publisher if you want a safety net before making changes.
- **README.md**: No root-level README (PLAN.md and deploy/README.md serve as documentation). Add one if you plan to make the repo public.
- **Monitoring / alerting**: The bot sends Discord messages on pipeline errors, but there's no external uptime monitoring. Consider UptimeRobot (free) pointing at your service URL to detect downtime.
- **Backup**: SQLite databases under `accounts/*/post_history.db` contain your post history and pending drafts. Consider periodic backups (Railway Volume snapshots or `scp`/`rsync` from VPS).
- **Rate limiting**: The code doesn't track Meta API rate limits (25 posts/24h). At daily posting this is not a concern, but at higher frequencies or with many accounts it could be.

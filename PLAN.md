# Automated Instagram Post Creator — High-Level Plan

## Context
Build an AI-agent pipeline that autonomously generates and publishes Instagram content. The system must be account-agnostic (configurable per account/niche) but will start with a vegetarian meat alternatives account focused on **natural, whole-food products only** (not ultra-processed fake meat). The goal is a fully automated content calendar: plan → create → review → schedule → publish — controlled entirely from Telegram.

**Scope: single image posts only.** Carousel and Reels are out of scope for now.

---

## Architecture Overview

```
Scheduler / Telegram command / User-submitted photo
   └─→ Orchestrator Agent
           ├─→ Content Planner Agent    (topic, brief)
           ├─→ Image Sourcing Agent     (user photo → stock → AI-generated)
           ├─→ Caption Writer Agent     (copy + hashtags + alt text)
           ├─→ Reviewer Agent           (brand rules + duplicate check)
           └─→ Publisher                (Instagram Graph API)
                    ↕
           Telegram Bot (control + notifications + user input)
```

---

## Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Agent framework | **Claude API** (claude-sonnet-4-6) | Orchestrator + all text/vision agents |
| Stock photos | **Unsplash API** + **Pexels API** | Free tiers, searched by topic keywords |
| Image generation | **DALL-E 3** (OpenAI) | Fallback when no suitable stock photo found (~$0.04/image) |
| Instagram publishing | **Meta Graph API** | Requires Business account + Facebook Page |
| Scheduler | **APScheduler** (`AsyncIOScheduler`) | Shares asyncio event loop with Telegram bot |
| Image hosting | **VM self-hosted (temp HTTP)** | Python `http.server` on a port; Meta fetches image; deleted immediately after. No OCI SDK needed. |
| Storage | SQLite | Post history, pending draft state, schedule config |
| Config | YAML per account | Niche, tone, style, credentials |
| Control & notifications | **Telegram Bot** (polling, asyncio) | Single channel for everything |

**Estimated API running costs**: ~$5–15/month (Claude vision calls + DALL-E 3 fallback at daily cadence). Infrastructure is free.

---

## External Services & Accounts Needed

1. **Anthropic API key** — Claude for all agents
2. **OpenAI API key** — DALL-E 3 image generation (fallback)
3. **Unsplash API key** — free, 50 req/hour
4. **Pexels API key** — free, 200 req/hour
5. **Meta Developer App** — see setup note below
6. **Instagram Business Account** — must be Business type (not Personal or Creator); connected to a Facebook Page
7. **Telegram Bot** — create via `@BotFather` (free, instant)

---

## Image Sourcing Strategy (priority order)

```
1. User-supplied photo (sent via Telegram)      ← always used if provided
2. Stock photo (Unsplash → Pexels)              ← search by topic keywords from brief
3. AI-generated (DALL-E 3)                      ← fallback if no stock match found
```

**Stock search logic**: Image Sourcing Agent queries Unsplash first, then Pexels, using keywords from the Content Planner brief. A Claude vision call scores candidates for relevance + brand fit. If the best score is below threshold → fall back to DALL-E 3 generation.

**User-supplied photo flow**: User sends a photo to the Telegram bot (optionally with a caption/topic hint). This bypasses the Planner and Image steps — the Orchestrator goes straight to Caption Writer using the provided image and any hints.

---

## Agent Responsibilities

### 1. Content Planner Agent
- Reads account config (niche, past posts, content pillars)
- Chooses today's topic, angle, and visual keywords
- Outputs a structured brief: topic, angle, visual keywords, mood
- Avoids topics posted in the last 30 days (checked against `post_history.db`)

**Content pillars for veggie alternatives account:**
- Ingredient spotlights (tempeh, seitan, legumes, jackfruit, mushrooms…)
- Simple recipes
- Nutrition comparisons (natural plant protein vs. conventional meat)
- Shopping/sourcing tips
- Myth-busting posts

### 2. Image Sourcing Agent
- Follows priority order: user-supplied → stock → AI-generated
- For stock: generates search keywords from brief, queries Unsplash then Pexels, scores top results with Claude vision
- For AI: builds a detailed DALL-E 3 prompt enforcing visual style (natural food photography, bright, farm-fresh)
- Validates output dimensions for Instagram (1080×1080 or 1080×1350)
- Checks for visual similarity to recent posts (perceptual hash comparison) — rejects near-duplicates

### 3. Caption Writer Agent
- Writes hook line + body + CTA
- Appends **3–5 highly targeted hashtags** (Instagram's current best-practice recommendation over quantity)
- Generates alt text for accessibility
- Language: **English only**
- Enforces tone from config (educational, warm, inspiring — never fear-mongering)

### 4. Reviewer Agent
- Checks caption for brand rule violations (no ultra-processed products, factual claims only)
- Runs Claude vision on the final image to confirm brand fit
- Checks visual hash against `post_history.db` — no near-duplicate images allowed
- Returns PASS / FAIL with specific reasons
- On FAIL: retries upstream agent up to 2 times
- On 2nd FAIL: sends the draft to Telegram with failure reason and options: `/approve_anyway`, `/regenerate`, `/skip_today`

### 5. Publisher
- Temporarily serves image file via Python `http.server` on a local port (Meta needs a public URL to fetch)
- Creates one media container via Meta Graph API, waits for `status_code = FINISHED` (polling)
- Publishes the container
- Stops temp HTTP server; deletes local image file (always — success or failure)
- Saves post record (topic, image hash, caption snippet, timestamp) to `post_history.db`

---

## Data Contracts

All agents communicate via these dataclasses. They are the canonical interface — agents must produce and consume exactly these shapes.

```python
from dataclasses import dataclass

@dataclass
class PlannerBrief:
    topic: str              # e.g. "Tempeh: protein-packed fermented soybean"
    angle: str              # e.g. "Myth-busting: tempeh vs chicken protein"
    visual_keywords: list[str]  # e.g. ["tempeh slices", "wooden board", "natural light"]
    mood: str               # e.g. "warm, educational"
    content_pillar: str     # one of the account's content_pillars from config

@dataclass
class ImageResult:
    local_path: str         # absolute path to downloaded/generated image
    source: str             # "user" | "unsplash" | "pexels" | "dalle3"
    phash: str              # perceptual hash string (imagehash library)
    score: float            # vision relevance score 0.0–1.0 (1.0 for user-supplied)

@dataclass
class CaptionResult:
    caption: str            # hook + body + CTA, English only
    hashtags: list[str]     # 3–5 targeted hashtags (without #)
    alt_text: str           # accessibility description

@dataclass
class ReviewResult:
    status: str             # "PASS" or "FAIL"
    reasons: list[str]      # empty if PASS; specific failures if FAIL
    retry_type: str | None  # "image" | "caption" | None — which step to retry
```

---

## SQLite Schema

All tables live in `accounts/<account_id>/post_history.db`. Created by `init_db()` in Milestone 1.

```sql
CREATE TABLE IF NOT EXISTS post_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    content_pillar TEXT NOT NULL,
    image_phash TEXT NOT NULL,
    caption_snippet TEXT NOT NULL,   -- first 80 chars
    published_at TEXT NOT NULL,      -- ISO 8601
    instagram_media_id TEXT
);

CREATE TABLE IF NOT EXISTS pending_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    image_path TEXT NOT NULL,
    caption TEXT NOT NULL,
    hashtags TEXT NOT NULL,          -- JSON array
    alt_text TEXT NOT NULL,
    brief_json TEXT NOT NULL,        -- serialized PlannerBrief
    created_at TEXT NOT NULL,        -- ISO 8601
    publish_at TEXT NOT NULL,        -- ISO 8601, created_at + 2h
    status TEXT NOT NULL DEFAULT 'pending'  -- pending | approved | skipped | published
);

CREATE TABLE IF NOT EXISTS schedule_config (
    account_id TEXT PRIMARY KEY,
    frequency TEXT NOT NULL DEFAULT '1d',
    preferred_time TEXT NOT NULL DEFAULT '08:00',
    timezone TEXT NOT NULL DEFAULT 'America/New_York',
    paused INTEGER NOT NULL DEFAULT 0
);
```

---

## AccountConfig Dataclass

Loaded from `accounts/<account_id>/config.yaml` by `utils/config_loader.py`. Passed as argument to all functions that need config — never re-read from disk mid-pipeline.

```python
@dataclass
class ImageSourcingConfig:
    stock_score_threshold: float    # default 0.7
    sources: list[str]             # ["unsplash", "pexels"]
    fallback: str                  # "dalle3"

@dataclass
class AccountConfig:
    account_id: str
    instagram_user_id: str
    access_token_env: str          # env var name, not the token itself

    niche: str
    language: str
    allowed_products: list[str]
    banned_topics: list[str]
    tone: str
    visual_style: str

    post_frequency: str            # "1d" | "2d" | "3x" | "2x" | "1x"
    preferred_time: str            # "HH:MM"
    timezone: str

    telegram_bot_token_env: str    # env var name
    telegram_chat_id_env: str      # env var name
    auto_publish_timeout_hours: int

    content_pillars: list[str]
    image_sourcing: ImageSourcingConfig
    temp_http_port: int
```

---

## Telegram Bot — Control & Notifications

Single channel for everything. Implemented with `python-telegram-bot` v20+ (async/polling).

### Telegram bot state machine
Pending draft state is stored in SQLite (`pending_drafts` table: image path, caption, `publish_at` timestamp). On startup, the bot checks for overdue drafts and resumes them. This ensures the 2h timer survives crashes and reboots.

If a `/run` is triggered while a draft is already pending, the bot replies: "A draft is already pending. Use /approve, /skip, or wait for auto-publish."

### Automated notifications (bot → you):
| Event | Message |
|---|---|
| Draft ready | Image + caption sent for review |
| Auto-published | Confirmation after timeout with no response |
| Reviewer escalation | Failing draft + reason + options (`/approve_anyway`, `/regenerate`, `/skip_today`) |
| Pipeline failure | Error summary + which step failed |
| API error alert | Meta API returns auth error (token revoked or permissions changed) |

### Commands (you → bot):
| Command | Action |
|---|---|
| `/approve` | Publish the pending draft immediately |
| `/skip` | Discard draft, generate a new one |
| `/edit <new caption>` | Replace caption on pending draft, then publish |
| `/regenerate` | Discard draft, regenerate from scratch |
| `/approve_anyway` | Publish despite Reviewer FAIL (manual override) |
| `/skip_today` | Skip today's post entirely (no regeneration) |
| `/run` | Trigger a pipeline run right now |
| `/status` | Last run result + next scheduled time + current frequency |
| `/pause` | Pause the scheduler |
| `/resume` | Resume the scheduler |
| `/suggest <topic or hint>` | Queue a specific topic for the next run |
| `/setfrequency <value>` | Change posting schedule (see below) |

### `/setfrequency` values:
| Value | Meaning |
|---|---|
| `1d` | Once per day (default) |
| `2d` | Once every 2 days |
| `3x` | 3 times per week (Mon/Wed/Fri) |
| `2x` | 2 times per week (Mon/Thu) |
| `1x` | Once per week (Monday) |
| `<HH:MM>` | Change daily posting time (e.g. `/setfrequency 14:30`) |

New schedule is saved to SQLite and applied to APScheduler immediately (no restart needed).

### User-supplied photo flow:
Send a photo to the bot (optionally with a caption hint as the message text). Bot replies with the generated caption for approval. Normal approve/skip flow applies.

### Auto-publish timeout:
If no response within **2 hours** of draft delivery → auto-publish. Bot sends confirmation.

---

## Pipeline Flow (per run)

```
1.  Trigger: Scheduler (per configured frequency)
    — OR — Telegram /run
    — OR — User sends photo to Telegram bot

2.  Content Planner → generates brief (topic, visual keywords)
    [skipped if user supplied a photo]

3.  Image Sourcing Agent:
      a. User photo provided? → use it
      b. Else: search Unsplash → Pexels → score with Claude vision
      c. Score below threshold? → generate with DALL-E 3

4.  Caption Writer → draft caption + hashtags + alt text

5.  Reviewer → brand check + vision check + duplicate check
      PASS → continue
      FAIL (retry 1) → back to step 3 or 4 depending on failure type
      FAIL (retry 2) → send failing draft to Telegram with /approve_anyway / /regenerate / /skip_today

6.  Send draft to Telegram (image + caption preview)

7.  Wait for response (up to 2h):
      /approve or timeout → step 8
      /skip or /regenerate → back to step 2
      /skip_today → end run, log as skipped

8.  Temporarily serve image via Python http.server (VM public IP + port)

9.  Publisher → create media container → poll until FINISHED → publish
    Stop temp server; delete local image file (always, regardless of outcome)

10. Log to post_history.db + send Telegram confirmation
```

---

## Orchestrator Interface

`agents/orchestrator.py` is the pipeline coordinator. It is called by the scheduler, the Telegram `/run` command, and the user-photo handler.

```python
@dataclass
class PipelineResult:
    success: bool
    post_id: str | None             # Instagram media ID if published
    brief: PlannerBrief | None
    image: ImageResult | None
    caption: CaptionResult | None
    review: ReviewResult | None
    error: str | None               # human-readable error if failed
    skipped: bool                   # True if user chose /skip_today

async def run_pipeline(
    config: AccountConfig,
    db_path: str,
    user_photo_path: str | None = None,   # if user sent a photo
    user_hint: str | None = None,         # topic suggestion from /suggest or photo caption
    dry_run: bool = False,                # if True: skip publish, send draft to Telegram only
) -> PipelineResult:
    """Run the full content pipeline for one post."""
```

The orchestrator handles the reviewer retry logic internally (up to 2 retries based on `retry_type`). On 2nd FAIL, it returns the failing result — the Telegram bot decides whether to escalate to the user.

## `--dry-run` Behavior

When `main.py` is invoked with `--dry-run`, or `run_pipeline(dry_run=True)`:

- Steps 1–6 run normally (plan, source image, write caption, review, send draft to Telegram)
- Steps 8–9 are **skipped** (no temp HTTP server, no Meta API publish)
- Step 10: log to `post_history.db` with `instagram_media_id = NULL`; send Telegram confirmation with "[DRY RUN]" prefix
- The pending draft is still created and subject to the normal approve/skip flow

---

## Account Config Schema (`config.yaml`)

```yaml
account_id: "veggie_alternatives"
instagram_user_id: "<IG_USER_ID>"
access_token_env: "VEGGIE_IG_TOKEN"

niche: "Natural vegetarian meat alternatives"
language: "en"

allowed_products:
  - tempeh
  - seitan
  - legumes
  - lentils
  - jackfruit
  - mushrooms
  - tofu
  - eggs
  - fish   # optional, pescatarian-friendly

banned_topics:
  - ultra-processed fake meat
  - Beyond Meat / Impossible (processed)

tone: "educational, warm, inspiring"
visual_style: "natural food photography, bright, farm-fresh aesthetic"

# Schedule (also settable via /setfrequency in Telegram)
post_frequency: "1d"           # 1d | 2d | 3x | 2x | 1x
preferred_time: "08:00"
timezone: "America/New_York"

telegram_bot_token_env: "TELEGRAM_BOT_TOKEN"
telegram_chat_id_env: "TELEGRAM_CHAT_ID"
auto_publish_timeout_hours: 2

content_pillars:
  - ingredient_spotlight
  - recipe
  - nutrition_fact
  - shopping_tip
  - myth_bust

image_sourcing:
  stock_score_threshold: 0.7     # below this → use AI generation
  sources: [unsplash, pexels]    # order = priority
  fallback: dalle3

temp_http_port: 8765             # port used for temporary image serving
```

---

## File Structure

```
auto-ig/
├── accounts/
│   └── veggie_alternatives/
│       ├── config.yaml
│       └── post_history.db          # topics, image hashes, timestamps, pending drafts, schedule
├── agents/
│   ├── __init__.py
│   ├── orchestrator.py              # coordinates the full pipeline
│   ├── content_planner.py           # topic, brief
│   ├── image_sourcing.py            # stock search + AI generation + scoring
│   ├── caption_writer.py            # caption, hashtags, alt text
│   └── reviewer.py                  # brand + vision + duplicate check
├── publisher/
│   ├── __init__.py
│   ├── instagram.py                 # Meta Graph API wrapper
│   ├── temp_server.py               # lightweight HTTP server for temp image hosting
│   └── scheduler.py                 # APScheduler AsyncIOScheduler
├── control/
│   ├── __init__.py
│   └── telegram_bot.py              # polling bot: commands + notifications + photo intake + state
├── storage/
│   └── media/                       # temp images (deleted after each run); created on startup
├── utils/
│   ├── __init__.py
│   ├── config_loader.py             # loads AccountConfig dataclass from YAML
│   ├── image_utils.py               # resize, crop, perceptual hashing
│   ├── stock_search.py              # Unsplash + Pexels clients
│   └── prompts.py                   # reusable prompt templates
├── deploy/
│   └── auto-ig.service              # systemd unit file for Oracle Cloud
├── .env                             # API keys (never committed)
├── .env.example
├── .gitignore
├── requirements.txt
└── main.py                          # entry point: wires asyncio loop, scheduler, telegram bot
```

---

## Cloud Deployment

### Recommended: Oracle Cloud Free Tier (always free, no expiry)
- **Compute**: 4 ARM cores + 24 GB RAM (Ampere A1)
- **Storage**: 200 GB block storage
- **Cost**: $0 forever
- **Setup**: 1 VM, Python app runs as `systemd` service (auto-restart on crash/reboot)
- **Image hosting**: VM has a public IP; temp HTTP server serves images on a configurable port; open that port in Oracle's security list

**Backup — Google Cloud e2-micro**: 1 vCPU + 1 GB RAM, also always free. Sufficient for this workload.

**Avoid**: Render (spins down), Fly.io / Railway (no real free tier for new accounts), GitHub Actions (unreliable cron timing).

---

## Implementation Milestones

Each milestone lists its dependencies explicitly. Never start a milestone before its dependencies are complete.

| # | Name | Depends on | Key deliverables |
|---|---|---|---|
| 1 | **Plumbing** | — | `main.py` skeleton, `config_loader.py` + `AccountConfig`, `init_db()`, `.env.example`, `.gitignore`, `requirements.txt`, sample `config.yaml` |
| 2 | **Text pipeline** | 1 | `content_planner.py` → `PlannerBrief`, `caption_writer.py` → `CaptionResult`, `prompts.py`, `orchestrator.py` stub |
| 3 | **Image sourcing** | 1, 2 | `stock_search.py`, `image_sourcing.py` → `ImageResult`, `image_utils.py` (resize + phash) |
| 4 | **Reviewer** | 1, 2, 3 | `reviewer.py` → `ReviewResult`, orchestrator retry logic (up to 2 attempts) |
| 5 | **Publisher** | 1, 3 | `temp_server.py`, `instagram.py` (create container → poll → publish) |
| 6 | **Telegram bot** | 1–5 | `telegram_bot.py` with all commands, `ConversationHandler`, photo intake, auto-publish timer |
| 7 | **Scheduler** | 1, 6 | `scheduler.py` (`AsyncIOScheduler`), frequency persistence, `main.py` wire-up |
| 8 | **Deployment** | 1–7 | `auto-ig.service`, `--dry-run` flag, Oracle Cloud setup instructions |
| 9 | **Multi-account** | 1–7 | Parameterize all modules by `AccountConfig`, per-account scheduler jobs |

---

## Verification Plan

- **Unit**: Each agent tested in isolation with mock inputs
- **Integration**: `--dry-run` flag — runs full pipeline, skips publish, sends draft to Telegram only
- **End-to-end**: Post to a private test Instagram account
- **Manual review**: First 5–10 posts reviewed via Telegram before enabling 2h auto-publish
- **Monitoring**: Every run logged to file; Telegram alert on consecutive failures

---

## Meta App Setup

### App Review is NOT required for this use case

Meta App Review only gates apps where **other users connect their own accounts**. For a single-owner bot — where you own both the app and the Instagram account — **Development Mode is sufficient, permanently**.

### Setup steps

1. Create app at developers.facebook.com → add "Instagram Graph API" product
2. Connect your Facebook Page to your Instagram Business Account
3. Add yourself as **Developer** or **Admin** on the app
4. You now have full `instagram_content_publish` access — no review submission needed

Development Mode has no expiry and the same rate limits as Live Mode (25 API-published posts/24h).

### Token: use a System User token (non-expiring)

Standard User Access Tokens expire every 60 days. Instead, create a **System User** via Meta Business Manager and generate a System User token — these never expire. This eliminates the biggest operational risk for an unattended bot.

### What NOT to use

**`instagrapi` (unofficial private API)** — do not use. Violates Instagram ToS explicitly; real-world account bans reported after minimal use; no recourse if banned.

---

## Key Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Meta API token revoked | Use System User token (non-expiring) via Meta Business Manager; alert via Telegram on auth errors |
| Stock photos with poor brand fit | Vision scoring + threshold gate before using; DALL-E fallback |
| AI-generated images look synthetic | Strict prompt templates + Reviewer vision check |
| Duplicate/repetitive content | Post history DB (topics + image hashes); 30-day cooldown on topics |
| Meta API rate limits (25 posts/day) | Well within budget even at max frequency |
| Oracle Cloud idle reclaim (<20% CPU) | Telegram bot polling provides continuous light activity |
| User ignores Telegram draft | Auto-publish after 2h keeps schedule intact |
| Pending draft state lost on reboot | Stored in SQLite with `publish_at` timestamp; recovered on startup |
| Reviewer fails twice | Escalated to Telegram with failing draft + `/approve_anyway` / `/regenerate` / `/skip_today` |
| Temp HTTP server port blocked | Configurable port; add to Oracle security list during setup |
| API costs | Budget ~$5–15/month for Claude vision + DALL-E; monitor usage in Anthropic/OpenAI dashboards |

---

## Implementation Time Estimate

| Milestone | Complexity | Estimate |
|---|---|---|
| 1. Plumbing | Low | 1 session |
| 2. Text pipeline | Low–Med | 1–2 sessions |
| 3. Image sourcing | Medium | 2 sessions |
| 4. Reviewer | Medium | 1–2 sessions |
| 5. Publisher + temp HTTP | Medium | 1–2 sessions |
| 6. Telegram bot + setfrequency | High | 3–4 sessions |
| 7. Scheduler + frequency persistence | Low–Med | 1 session |
| 8. Deployment | Medium | 1–2 sessions |
| 9. Multi-account | Low | 1 session |

**Total: ~12–16 sessions** (~30–45 min each). **Active user time: ~7–10 hours.**

Heaviest part: Telegram bot (async state machine, photo intake, timer persistence, frequency management).

# auto-ig

Autonomous Instagram post creator. An AI pipeline that generates and publishes single-image posts, carousels, and reels — controlled entirely via Discord.

## How it works

1. **Content Planner** — picks a topic based on your niche, content pillars, and post history
2. **Image Sourcing** — searches Unsplash/Pexels for stock photos, falls back to Gemini AI generation
3. **Caption Writer** — writes a platform-ready caption with hashtags
4. **Reviewer** — AI quality gate that catches low-quality drafts before they reach you
5. **Discord Review** — sends the draft to your Discord channel for approval (auto-publishes after 2h if no response)
6. **Publisher** — publishes to Instagram (and optionally Facebook) via the Meta Graph API

## Features

- Multi-account support with independent schedules and configs
- Discord commands: `!run`, `!approve`, `!reject`, `!edit`, `!pause`, `!resume`, `!status`, and more
- Configurable posting frequency (`1d`, `2d`, `3x`, `2x`, `1x` per week)
- Duplicate image detection via perceptual hashing
- Supports single images, carousels, and reels
- Dry-run mode for testing without publishing

## Quick start

```bash
git clone https://github.com/Seb281/auto-ig.git
cd auto-ig
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # fill in your API keys
```

Create an account config:

```bash
mkdir -p accounts/my_account
cp accounts/example_config.yaml accounts/my_account/config.yaml
# edit config.yaml with your niche, tone, content pillars, etc.
```

Run in dry-run mode:

```bash
python main.py --account my_account --dry-run
```

Then send `!run` in your Discord channel to trigger a pipeline run.

## Configuration

Each account lives in `accounts/<account_id>/config.yaml`. See `accounts/example_config.yaml` for the full template with all available options.

API keys and tokens go in `.env` — see `.env.example` for the full list.

## Deployment

See [deploy/README.md](deploy/README.md) for Railway and VPS deployment guides.

## Tech stack

Python 3.11+, Gemini (google-genai), discord.py, APScheduler, aiosqlite, httpx, Pillow

## License

MIT

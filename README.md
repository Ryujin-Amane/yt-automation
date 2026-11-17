---
title: AutoTube AI
emoji: 🚀
colorFrom: purple
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

<div align="center">
  <h1>🚀 AutoTube AI</h1>
  <p><strong>Fully autonomous TikTok → YouTube Shorts repurposing pipeline with Live RAG Trend Scouting.</strong></p>
  <p>
    <a href="#-features"><img src="https://img.shields.io/badge/Features-✨-blueviolet?style=flat-square" alt="Features"></a>
    <a href="#-how-it-works"><img src="https://img.shields.io/badge/How_It_Works-🧠-green?style=flat-square" alt="How It Works"></a>
    <a href="#-deploy-on-hugging-face-spaces"><img src="https://img.shields.io/badge/Deploy-🤗_HF_Spaces-yellow?style=flat-square" alt="Deploy"></a>
  </p>
</div>

---

## ✨ Features

| Feature | Description |
|---|---|
| **🔄 Fully Autonomous Pipeline** | Fetches new videos from specified TikTok accounts, removes watermarks, processes them, and uploads to YouTube Shorts — on a configurable schedule. |
| **🎬 Smart Video Processing** | Applies speed shifts, dynamic zoom, saturation/contrast grading, and optional color profiles to bypass duplicate content filters. |
| **🧠 Live RAG SEO Agent** | 3-stage AI pipeline: extracts video context → scouts live YouTube trends → synthesizes hyper-optimized titles, descriptions, and hashtags. |
| **🛡️ Provider Fallback Chain** | Primary: Google Gemini Flash → Fallback: Groq Vision → Fallback: original captions. Near 100% uptime. |
| **📊 Multi-Channel Quota Manager** | Automatically rotates between YouTube channels when daily API upload limits are reached (round-robin). |
| **🗄️ Supabase Dedup Tracking** | Permanent record of all uploaded videos — guarantees zero duplicates across channels. |
| **🖥️ Premium Web Dashboard** | Password-protected live dashboard to manage queues, settings, channels, and trigger manual uploads — all from the browser. |
| **⚙️ Runtime Settings** | Change TikTok accounts, upload frequency, video processing params, and AI provider without redeploying. |

---

## 🧠 How It Works

When a new TikTok video is detected, the **Live RAG SEO Agent** (`ai_agent.py`) executes:

```
┌──────────────────────────────────────────────────────────────┐
│  1. EXTRACT — Vision Context                                 │
│     • Pulls 3 keyframes via OpenCV                           │
│     • Transcribes audio via faster-whisper (tiny model)      │
│     • Gemini/Groq analyzes media → JSON schema               │
│       (niche, subjects, mood, visual style)                  │
├──────────────────────────────────────────────────────────────┤
│  2. SCOUT — Live Trend Retrieval                             │
│     • Builds a niche-specific search query                   │
│     • yt-dlp executes live YouTube search                    │
│       (e.g. "ytsearch5:Anime Edit Gojo shorts")             │
│     • Retrieves titles, tags, view counts of top performers  │
├──────────────────────────────────────────────────────────────┤
│  3. SYNTHESIZE — SEO Generation                              │
│     • Gemini Flash fuses video context + live trend data     │
│     • Outputs optimized title, description, hashtag stack    │
│     • Engineered to capitalize on proven viral patterns      │
└──────────────────────────────────────────────────────────────┘
```

---

## 🛠️ Tech Stack

- **Python 3.11** on Docker
- **Google GenAI SDK** (Gemini Flash) & **Groq API** — AI providers
- **YouTube Data API v3** — uploading & live trend search
- **yt-dlp** — TikTok downloading & YouTube trend scraping
- **OpenCV & FFmpeg** — video processing pipeline
- **faster-whisper** — fast on-CPU audio transcription
- **Supabase** — PostgreSQL for dedup tracking
- **Flask + Gunicorn** — web dashboard & keep-alive server
- **APScheduler** — cron-style background job scheduling

---

## 🤗 Deploy on Hugging Face Spaces

This project is designed to run as a **Docker Space** on Hugging Face.

### 1. Create the Space

1. Go to [huggingface.co/new-space](https://huggingface.co/new-space).
2. Choose **Docker** as the SDK.
3. Set visibility to **Private** (recommended — this handles API keys).
4. Upload or push all project files to the Space repo.

### 2. Configure Secrets

Go to your Space → **Settings** → **Variables and secrets** → add each as a **New secret**:

| Secret Name | Required | Description |
|---|---|---|
| `SUPABASE_URL` | ✅ | Your Supabase project URL |
| `SUPABASE_KEY` | ✅ | Supabase anon/service key |
| `GEMINI_API_KEY` | ✅ | Google AI Studio API key |
| `GROQ_API_KEY` | ⬜ | Groq Cloud API key (fallback provider) |
| `YOUTUBE_API_KEY` | ✅ | Google Cloud YouTube Data API key (used for live trend search) |
| `TIKTOK_USERNAMES` | ✅ | Comma-separated TikTok handles (e.g. `account1,account2`) |
| `YOUTUBE_TOKEN_1` | ✅ | YouTube OAuth token JSON for channel 1 (see auth below) |
| `YOUTUBE_CLIENT_SECRET_1` | ✅ | YouTube OAuth client secret JSON for channel 1 |
| `HF_TOKEN` | ✅ | Hugging Face token (with write access) — **required** to keep the Space alive on private Spaces and to auto-restart via the HF API |
| `YOUTUBE_TOKEN_2` | ⬜ | Additional channel token (add as many as needed) |
| `YOUTUBE_CLIENT_SECRET_2` | ⬜ | Additional channel client secret |

### 3. Generate YouTube Tokens (Local Step)

You need to run the auth helper **once on your local machine** to generate OAuth tokens:

1. Get a `client_secret.json` from the [Google Cloud Console](https://console.cloud.google.com/) (OAuth 2.0 → Desktop App).
2. Run locally:
   ```bash
   pip install google-auth-oauthlib google-api-python-client
   python auth_setup.py
   ```
3. Follow the browser OAuth flow. The script outputs a JSON token.
4. Paste the full JSON string into the `YOUTUBE_TOKEN_1` secret on HF Spaces.
5. Paste the contents of your `client_secret.json` into `YOUTUBE_CLIENT_SECRET_1`.
6. Repeat for additional channels (`YOUTUBE_TOKEN_2`, etc.).

### 4. Optional Tuning Variables

These can be set as HF Spaces secrets or adjusted at runtime from the dashboard:

| Variable | Default | Description |
|---|---|---|
| `CHECK_INTERVAL_MINUTES` | `60` | How often to check for new TikToks |
| `MAX_UPLOADS_PER_CHANNEL` | `10` | Max daily uploads per YouTube channel |
| `UPLOADS_PER_CHANNEL_PER_RUN` | `1` | Videos to upload per channel per cycle |
| `VIDEO_MAX_AGE_HOURS` | `12` | Ignore TikToks older than this |
| `SPEED_FACTOR` | `1.05` | Playback speed multiplier |
| `ZOOM_PERCENT` | `1.02` | Zoom crop factor |
| `SATURATION_BOOST` | `1.1` | Color saturation multiplier |
| `CONTRAST_BOOST` | `1.05` | Contrast multiplier |
| `CUSTOM_HASHTAGS` | `#shorts #viral #trending #fyp` | Appended to every upload |
| `AI_PRIMARY_PROVIDER` | `gemini` | Primary AI provider (`gemini` or `groq`) |

### 5. Launch

Once secrets are set, the Space will build the Docker image and start automatically. The dashboard is available at your Space URL on port **7860**.

> **Tip:** Use [UptimeRobot](https://uptimerobot.com/) to ping your Space URL every 5 minutes and prevent HF Spaces from sleeping.

---

## 📁 Project Structure

```
.
├── bot.py                 # Entry point — Flask dashboard + APScheduler
├── ai_agent.py            # 3-stage Live RAG SEO agent
├── tiktok_scraper.py      # TikTok video discovery & download
├── video_processor.py     # FFmpeg/OpenCV video processing pipeline
├── youtube_uploader.py    # YouTube Data API upload logic
├── queue_manager.py       # Video queue management
├── database.py            # Supabase dedup tracking
├── config.py              # Environment + runtime settings loader
├── settings_manager.py    # Persistent settings (JSON-backed)
├── auth_setup.py          # Local OAuth token generator
├── gunicorn_conf.py       # Gunicorn production config
├── Dockerfile             # HF Spaces Docker build
├── requirements.txt       # Python dependencies
├── templates/
│   └── index.html         # Premium web dashboard UI
└── static/                # Dashboard static assets
```

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

> **Disclaimer:** This tool is for educational and personal use. Respect platform Terms of Service. Do not use for spam or content theft.

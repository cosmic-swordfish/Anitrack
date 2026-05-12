# AniTrack 🎌

A private self-hosted site that shows your AniList calendar and syncs it with MyAnimeList.

## Features
- 📅 **Calendar** — currently watching + planned anime with exact episode air times
- ➕ **Google Calendar** — add each episode as its own event
- 🔄 **Manual Sync** — see differences between AniList & MAL, apply one by one
- ⚡ **Auto Sync** — one click syncs everything from AniList → MAL

---

## Setup

### 1. Get MAL API credentials
1. Go to https://myanimelist.net/apiconfig
2. Click **Create ID**
3. Fill in App Name (e.g. "AniTrack"), App Type: **web**
4. Set redirect URI to: `http://localhost:5000/mal/callback`
5. Copy your **Client ID** and **Client Secret**

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env with your MAL_CLIENT_ID, MAL_CLIENT_SECRET, and a random SECRET_KEY
```

### 3. Install & run locally
```bash
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000 in your browser.

---

## Hosting privately (recommended: Railway or Render)

### Railway (free tier available)
1. Push this folder to a GitHub repo (private)
2. Go to https://railway.app → New Project → Deploy from GitHub
3. Add environment variables from your `.env` file
4. Set `MAL_REDIRECT_URI` to `https://your-app.railway.app/mal/callback`
5. Update the same redirect URI in your MAL app settings

### Render (free tier available)
1. Push to GitHub
2. Go to https://render.com → New Web Service
3. Connect your repo, set build command: `pip install -r requirements.txt`
4. Set start command: `gunicorn app:app`
5. Add environment variables

### VPS (nginx + gunicorn)
```bash
# Install
pip install -r requirements.txt

# Run with gunicorn
gunicorn app:app --bind 127.0.0.1:5000 --workers 2 --daemon

# Nginx config (sites-available/anitrack)
server {
    listen 80;
    server_name yourdomain.com;
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## Sync explained

| Action | Meaning |
|--------|---------|
| **Missing on MAL** | On AniList but not MAL → will add to MAL |
| **Out of sync** | Status/score/progress differs → MAL will be updated to match AniList |
| **Only on MAL** | On MAL but not AniList → shown for info, not auto-changed |

AniList is always treated as the **source of truth**.

---

## Auto-sync schedule
To run sync automatically every hour on a VPS, add a cron job:
```bash
# crontab -e
0 * * * * curl -X POST http://localhost:5000/api/sync/auto -b /path/to/session.cookie
```
Or use the built-in scheduler (see `app.py` comments).

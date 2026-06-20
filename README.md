<p align="center">
  <img src="static/anitrack-logo-transparent-512.png" width="96" alt="AniTrack logo"/>
</p>

# AniTrack

Track and sync your anime list between **AniList** and **MyAnimeList (MAL)**. AniTrack shows what's out of sync between the two platforms and lets you push changes in one click — with full **two-way sync** when both accounts are connected.

Anyone can visit the site and enter their own AniList username in Settings to view and sync their own list. The default username shown is the one set in the `ANILIST_USERNAME` environment variable.

Built with Python + Flask. Deployable on Vercel. Optionally backed by **Supabase / PostgreSQL** for persistent token storage and anime list backup across serverless restarts.

---

## Features

- View any AniList anime list (watching + planned) with airing countdowns
- Update episode progress directly from the card with `+` / `−` buttons
- **Edit any entry in place** — ✏️ button on every card opens a modal to change status, progress, score, and **Date Started / Date Completed**
- Search and add new anime to your list, or edit existing entries, from the same modal
- Sort and filter by: Default, Last Updated, Airing Soon, A→Z, Score, Progress, Episodes Left
- Connect MAL via OAuth 2.0 PKCE
- Connect AniList via OAuth 2.0 for two-way sync and episode updates
- Diff your AniList vs MAL lists — see what's missing or out of sync
- **Two-way sync**: higher value wins (e.g. MAL ep 3 vs AL ep 1 → both become ep 3)
- Auto-sync everything at once, or apply changes one by one
- **Per-user username** — visitors can set their own AniList username in Settings
- **Persistent token storage** — optional Postgres/Supabase DB keeps OAuth tokens across serverless restarts
- **Persistent list storage** — synced anime entries (including start/completion dates) are mirrored into Supabase for backup/history
- Dark / light theme toggle — all modals and controls follow the active theme
- Smooth animated pill navbar with icon animations
- Haptic feedback on all interactive elements (mobile)
- Full keyboard navigation on the bottom navbar (Tab / Arrow keys / Enter)
- Oneko pixel cat that follows your cursor (toggle in Settings)
- Navbar glass blur toggle in Settings
- Back-to-top button
- PWA — installable on mobile home screen with offline support

---

## How sync works

| Field    | Rule                                                          |
|----------|---------------------------------------------------------------|
| Progress | `max(AniList, MAL)` — higher episode count wins              |
| Score    | Higher score wins; 0 is ignored                              |
| Status   | More progressed status wins (Completed > Watching > Planning) |

When only MAL is connected, changes are pushed to MAL only (one-way).  
When both accounts are connected, the winning value is written to both platforms (two-way).

---

## Editing entries

Every card in **Watching** and **Plan to Watch** has a small ✏️ button next to the episode `+`/`−` controls. Clicking it (or tapping **✏ Edit** / **＋ Add** on a search result) opens a modal where you can set:

- Status (Watching / Completed / On Hold / Dropped / Plan to Watch)
- Progress (episodes watched)
- Score (0–10)
- **Date Started** and **Date Completed**

Saving writes the change to AniList (and MAL, if connected) immediately, then refreshes the list. If a database is configured, the same entry — including its dates — is also upserted into the `anime_list` table in Supabase right away, so the dates aren't only visible on AniList but also backed up in your own database.

The modal and search results use the app's theme variables (`--glass-bg`, `--border`, `--accent`, etc.), so they automatically match whichever theme (dark/light) is active — no separate styling needed per theme.

---

## Project Structure

```
anitrack/
├── app.py                          # Flask app — routes, OAuth, DB, sync logic
├── templates/
│   └── index.html                  # Full frontend UI (single page)
├── static/
│   ├── favicon.png                 # Browser tab icon
│   ├── logo-512.png                # App logo (PWA + apple touch icon)
│   ├── anitrack-logo-transparent-512.png  # Transparent logo variant
│   ├── manifest.json               # PWA manifest
│   ├── service-worker.js           # PWA offline caching
│   ├── glass.css                   # Glass morphism styles
│   ├── button.js                   # Navbar button animation helpers
│   └── container.js                # Glass container helpers
├── requirements.txt
├── vercel.json
└── .env.example
```

---

## Requirements

- Python 3.9+
- A [MyAnimeList API app](https://myanimelist.net/apiconfig) (free) — required for sync
- A [AniList API app](https://anilist.co/settings/developer) (free, optional) — required for two-way sync and episode updates
- A [Supabase](https://supabase.com) project (free, optional) — required for persistent token storage and anime list backup on serverless deployments

---

## Local Setup

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Create your `.env` file**

```bash
cp .env.example .env
```

Generate a secret key:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Fill in `.env`:

```env
SECRET_KEY=your_generated_key_here
ANILIST_USERNAME=your_anilist_username

# MAL OAuth (required for sync)
MAL_CLIENT_ID=your_mal_client_id
MAL_CLIENT_SECRET=your_mal_client_secret
MAL_REDIRECT_URI=http://localhost:5000/mal/callback

# AniList OAuth (optional — enables two-way sync + episode updates)
AL_CLIENT_ID=your_al_client_id
AL_CLIENT_SECRET=your_al_client_secret
AL_REDIRECT_URI=http://localhost:5000/al/callback

# Supabase / Postgres (optional — persistent token storage)
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

**3. MAL API app setup**

Go to https://myanimelist.net/apiconfig → create a new app:
- App Type: `web`
- Redirect URI: `http://localhost:5000/mal/callback`

**4. AniList API app setup** *(optional — two-way sync + episode updates)*

Go to https://anilist.co/settings/developer → New Client:
- Redirect URL: `http://localhost:5000/al/callback`

> The AniList client ID is a number (e.g. `21634`), not a hex string.

**5. Run**

```bash
python app.py
```

Open http://localhost:5000 — the default username from `.env` loads automatically. Go to **Settings** to connect MAL and/or AniList, or to switch to a different AniList username.

---

## Database (Supabase / PostgreSQL)

The database is **optional**. Without it, OAuth tokens are stored only in the session cookie and will be lost when the server restarts (common on serverless platforms like Vercel).

With a database connected, tokens are persisted in a `user_tokens` table and survive restarts — users stay logged in to MAL and AniList across deployments. Your synced anime list (including watch dates) is also mirrored into an `anime_list` table for backup/history.

### What gets stored

**`user_tokens`**

| Column       | Type        | Description                        |
|--------------|-------------|------------------------------------|
| `username`   | TEXT (PK)   | AniList username                   |
| `al_token`   | JSONB       | AniList OAuth token (access + refresh) |
| `mal_token`  | JSONB       | MAL OAuth token (access + refresh) |
| `updated_at` | TIMESTAMPTZ | Last updated timestamp             |

**`anime_list`**

| Column         | Type         | Description                                  |
|----------------|--------------|-----------------------------------------------|
| `username`     | TEXT         | AniList username                              |
| `al_id`        | INT          | AniList media ID                              |
| `mal_id`       | INT          | MyAnimeList media ID                          |
| `title`        | TEXT         | Anime title                                   |
| `status`       | TEXT         | List status (CURRENT, COMPLETED, etc.)        |
| `progress`     | INT          | Episodes watched                              |
| `score`        | NUMERIC(4,1) | Your score (0–10)                             |
| `episodes`     | INT          | Total episode count                           |
| `media_status` | TEXT         | Airing status of the anime itself             |
| `started_at`   | DATE         | Date you started watching                     |
| `completed_at` | DATE         | Date you finished watching                     |
| `updated_at`   | TIMESTAMPTZ  | Last updated timestamp                        |
| `synced_at`    | TIMESTAMPTZ  | Last time this row was synced from AniList    |

Both tables are created automatically on first run if they don't exist (`CREATE TABLE IF NOT EXISTS`). If you're upgrading from an older version of AniTrack, the app also runs `ALTER TABLE anime_list ADD COLUMN IF NOT EXISTS started_at/completed_at` automatically on startup, so existing rows pick up the new columns (as `NULL`) without any manual migration.

`anime_list` is refreshed whenever a sync runs (`/api/sync/diff`, `/api/sync/auto`, the cron job) and also immediately whenever you save an edit from the ✏️ modal — so Date Started / Date Completed land in Supabase right away rather than waiting for the next scheduled sync.

### Setting up Supabase (free tier)

1. Create a project at https://supabase.com
2. Go to **Project Settings → Database → Connection string → URI**
3. Copy the connection string — it looks like:
   ```
   postgresql://postgres:[password]@db.[ref].supabase.co:5432/postgres
   ```
4. Add it as `DATABASE_URL` in your `.env` (local) or Vercel env vars (production)

### Checking the database is working

**Option 1 — Watch the logs**

Run the app locally and connect MAL or AniList. You should see in the terminal:

```
[DB] Tokens saved for your_username
```

After a sync runs, you should also see:

```
[DB] anime_list: saved N entries for your_username
```

If you see `[DB] Table init error` or `[DB] Save error`, the `DATABASE_URL` is wrong or unreachable.

**Option 2 — Check via Python**

```python
import psycopg2, os
conn = psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require")
cur = conn.cursor()
cur.execute("SELECT username, updated_at FROM user_tokens;")
print(cur.fetchall())
cur.execute("SELECT title, status, started_at, completed_at FROM anime_list LIMIT 5;")
print(cur.fetchall())
cur.close(); conn.close()
```

**Option 3 — Check via Supabase dashboard**

Go to your Supabase project → **Table Editor** → `user_tokens` or `anime_list`. You should see a row for each user who has connected MAL or AniList, and a row per anime (with `started_at`/`completed_at` filled in for anything you've edited or that AniList already had dates for).

**Option 4 — psql (command line)**

```bash
psql "$DATABASE_URL" -c "SELECT username, updated_at FROM user_tokens;"
psql "$DATABASE_URL" -c "SELECT title, started_at, completed_at FROM anime_list ORDER BY synced_at DESC LIMIT 10;"
```

### Without a database

If `DATABASE_URL` is not set or `psycopg2` is not installed, the app falls back silently — all functions become no-ops, tokens are session-only, and the anime list is not mirrored to a database (it still works fully against AniList/MAL directly). No errors, no crashes.

---

## Vercel Deployment

**Environment variables — set in Vercel → Settings → Environment Variables:**

| Variable            | Required | Description                                         |
|---------------------|----------|-----------------------------------------------------|
| `SECRET_KEY`        | ✅       | Stable random key — generate once and keep fixed    |
| `HTTPS`             | ✅       | Set to `true`                                       |
| `ANILIST_USERNAME`  | ✅       | Default AniList username shown to all visitors      |
| `MAL_CLIENT_ID`     | ✅       | From myanimelist.net/apiconfig                      |
| `MAL_CLIENT_SECRET` | ✅       | From myanimelist.net/apiconfig                      |
| `MAL_REDIRECT_URI`  | ✅       | `https://yourdomain.com/mal/callback`               |
| `AL_CLIENT_ID`      | ⬜       | From anilist.co/settings/developer                  |
| `AL_CLIENT_SECRET`  | ⬜       | From anilist.co/settings/developer                  |
| `AL_REDIRECT_URI`   | ⬜       | `https://yourdomain.com/al/callback`                |
| `DATABASE_URL`      | ⬜       | Supabase/Postgres URI — for persistent token + list storage |

After adding env vars, redeploy from the Vercel dashboard (don't use cached build).

> **Important:** On Vercel, serverless functions are stateless — without `DATABASE_URL`, users will need to re-connect MAL/AniList after each cold start. Setting `DATABASE_URL` fixes this.

---

## Multi-user behaviour

`ANILIST_USERNAME` sets the **default** list shown to anyone who visits. Each visitor can go to **Settings → AniList Username** and enter their own username — this is saved in their browser's `localStorage` and is never sent to the server except as a query parameter to the sync endpoints.

MAL and AniList OAuth tokens are stored in the visitor's **session cookie** and, if a database is configured, also persisted in the `user_tokens` table keyed by AniList username.

---

## Troubleshooting

**MAL OAuth loops / keeps asking to log in**  
`SECRET_KEY` is not set or is changing between requests. Set it as a permanent fixed value in your Vercel env vars — never leave it blank.

**AniList 401 error**  
`AL_CLIENT_SECRET` or `AL_REDIRECT_URI` doesn't match your AniList app settings exactly. Copy-paste — don't retype.

**`redirect_uri mismatch` from MAL**  
`MAL_REDIRECT_URI` must exactly match what's registered at myanimelist.net/apiconfig.

**PKCE cookie missing**  
Cookies must be enabled in the browser. Also ensure `HTTPS=true` is set in Vercel env vars when deployed.

**Sync only going one way**  
Connect your AniList account via **Settings → Connect AniList**. Two-way sync requires both MAL and AniList to be connected.

**Episode `+`/`−` buttons do nothing**  
AniList OAuth must be connected (Settings → Connect AniList). Episode updates go through the AniList API and require an access token.

**Date Started / Date Completed not saving**  
Make sure AniList is connected — dates are written via the same AniList API call as status/progress/score, so it needs `al_token` in session. If you're also expecting the dates in Supabase, confirm `DATABASE_URL` is set; otherwise the dates are saved to AniList/MAL but not mirrored to a database.

**Oneko not appearing**  
Enable it in **Settings → Oneko** and move your cursor around the screen. Does not work on touch-only devices.

**Tokens lost after every Vercel deploy**  
Add `DATABASE_URL` to your Vercel env vars. Without a database, tokens only live in the session cookie which is tied to the server process.

**`[DB] Table init error` in logs**  
`DATABASE_URL` is set but incorrect, or the Supabase project is paused (free tier pauses after 1 week of inactivity). Wake it up from the Supabase dashboard.

---

## License

MIT

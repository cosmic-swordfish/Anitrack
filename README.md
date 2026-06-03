<p align="center">
  <img src="static/logo-512.png" width="96" alt="AniTrack logo"/>
</p>

# AniTrack

Track and sync your anime list between **AniList** and **MyAnimeList (MAL)**. AniTrack shows what's out of sync between the two platforms and lets you push changes in one click — with full **two-way sync** when both accounts are connected.

Anyone can visit the site and enter their own AniList username in Settings to view and sync their own list. The default username shown is the one set in the `ANILIST_USERNAME` environment variable.

Built with Python + Flask. Deployable on Vercel.

---

## Features

- View any AniList anime list (watching + planned) with airing countdowns
- Update episode progress directly from the card with `+` / `−` buttons
- Sort and filter by: Default, Last Updated, Airing Soon, A→Z, Score, Progress, Episodes Left
- Connect MAL via OAuth 2.0 PKCE
- Connect AniList via OAuth 2.0 for two-way sync and episode updates
- Diff your AniList vs MAL lists — see what's missing or out of sync
- **Two-way sync**: higher value wins (e.g. MAL ep 3 vs AL ep 1 → both become ep 3)
- Auto-sync everything at once, or apply changes one by one
- **Per-user username** — visitors can set their own AniList username in Settings
- Dark / light theme toggle
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

## Project Structure

```
anitrack/
├── app.py                          # Flask app — routes, OAuth, sync logic
├── templates/
│   └── index.html                  # Full frontend UI (single page)
├── static/
│   ├── favicon.png                 # Browser tab icon
│   ├── logo-512.png                # App logo (PWA + apple touch icon)
│   ├── manifest.json               # PWA manifest
│   ├── service-worker.js           # PWA offline caching
│   ├── glass.css                   # Glass morphism styles (loaded by service worker cache)
│   ├── button.js                   # Navbar button animation helpers
│   └── container.js                # Glass container helpers
├── requirements.txt
├── vercel.json
└── .env.example
```

> `anitrack-logo-transparent-512.png` is present in static but not referenced anywhere in the app — it can be safely deleted or kept as a design asset.

---

## Requirements

- Python 3.9+
- A [MyAnimeList API app](https://myanimelist.net/apiconfig) (free) — required for sync
- A [AniList API app](https://anilist.co/settings/developer) (free, optional) — required for two-way sync and episode updates

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

After adding env vars, redeploy from the Vercel dashboard (don't use cached build).

---

## Multi-user behaviour

`ANILIST_USERNAME` sets the **default** list shown to anyone who visits. Each visitor can go to **Settings → AniList Username** and enter their own username — this is saved in their browser's `localStorage` and is never sent to the server except as a query parameter to the sync endpoints.

MAL and AniList OAuth tokens are stored in the visitor's **session cookie** only — they are not shared between users and cannot affect another user's data.

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

**Oneko not appearing**  
Enable it in **Settings → Oneko** and move your cursor around the screen. Does not work on touch-only devices.

---

## License

MIT

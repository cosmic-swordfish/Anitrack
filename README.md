# AniTrack

Sync your anime list between **AniList** and **MyAnimeList (MAL)**. AniTrack shows you what's out of sync between the two platforms and lets you push changes to MAL in one click.

Built with Python + Flask. MAL login uses OAuth 2.0 PKCE — no passwords stored.

---

## Features

- View your AniList anime list in a clean UI
- Connect your MAL account via OAuth
- Diff your AniList vs MAL lists — see what's missing or out of sync
- Sync status, progress, and scores from AniList → MAL
- Auto-sync everything at once or apply changes one by one

---

## Requirements

- Python 3.9+
- A [MyAnimeList API app](https://myanimelist.net/apiconfig) (free)

---

## Setup

**1. Clone or extract the project**

```bash
git clone https://github.com/yourusername/anitrack
cd anitrack
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Create your `.env` file**

```bash
cp .env.example .env
```

Generate a secret key:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Then fill in `.env`:

```env
SECRET_KEY=your_generated_key
MAL_CLIENT_ID=your_client_id
MAL_CLIENT_SECRET=your_client_secret
MAL_REDIRECT_URI=http://localhost:5000/mal/callback
ANILIST_USERNAME=your_anilist_username
```

Install python-dotenv so Flask reads the `.env` file:

```bash
pip install python-dotenv
```

Add these two lines to the very top of `app.py`:

```python
from dotenv import load_dotenv
load_dotenv()
```

**4. MAL API app setup**

Go to https://myanimelist.net/apiconfig and create a new app:
- App Type: `web`
- Redirect URI: `http://localhost:5000/mal/callback`

Copy the Client ID and Client Secret into your `.env`.

> ⚠️ The redirect URI in your MAL app settings must **exactly** match `MAL_REDIRECT_URI` in your `.env`. A mismatch is the most common cause of OAuth failing.

**5. Run**

```bash
python app.py
```

Open http://localhost:5000, click **Connect MAL**, and log in.

---

## Running on Linux (Termux / Arch / any distro)

The steps are the same on any Linux terminal. The only difference is the package manager.

**Install Python and Git:**

```bash
# Termux (Android)
pkg update && pkg upgrade
pkg install python git

# Arch Linux
sudo pacman -Syu
sudo pacman -S python git

# Ubuntu / Debian
sudo apt update
sudo apt install python3 python3-pip git
```

**Then run the same way on all of them:**

```bash
git clone https://github.com/yourusername/anitrack
cd anitrack
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000 in your browser.

> ⚠️ Always `cd` into the project folder before running `python app.py`. Flask looks for the `templates/` folder relative to `app.py` — running it from the wrong directory causes a *TemplateNotFound* error.

**Copying files without Git:**

If you have the ZIP on your device instead:

```bash
# Termux — copy from phone storage
cp -r /sdcard/anitrack ~/anitrack

# Any Linux — extract the zip
unzip anitrack_fixed.zip
cd anitrack
```

---

## Docker

**Build and run:**

```bash
docker build -t anitrack .
docker run -d -p 5000:5000 --env-file .env anitrack
```

**With Docker Compose** (if you have other containers on the same server):

```yaml
services:
  anitrack:
    build: .
    ports:
      - "8080:5000"
    env_file:
      - .env
    restart: always
```

```bash
docker compose up -d
```

> Change `8080` to any free port on your machine to avoid conflicts with other containers.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | ✅ | Flask session key — must be stable or OAuth will loop |
| `MAL_CLIENT_ID` | ✅ | From your MAL API app |
| `MAL_CLIENT_SECRET` | ✅ | From your MAL API app |
| `MAL_REDIRECT_URI` | ✅ | Must match what's registered in your MAL app |
| `ANILIST_USERNAME` | ✅ | Your AniList username |
| `HTTPS` | ⬜ | Set to `true` when running behind HTTPS |

---

## Troubleshooting

**MAL OAuth loops / keeps asking to log in**
`SECRET_KEY` is regenerating on every restart. Set it as a permanent value in your `.env`.

**"Session expired before OAuth completed"**
Same cause — stable `SECRET_KEY` required.

**`redirect_uri mismatch` error from MAL**
`MAL_REDIRECT_URI` in `.env` must exactly match what you registered at myanimelist.net/apiconfig.

**`TemplateNotFound: index.html`**
Run `python app.py` from inside the `anitrack/` folder, not from a parent directory.

**Port already in use**
Change the port: `flask run --port 8080`, or with Docker: `-p 8080:5000`.

**`pip: command not found`**
Try `pip3` instead of `pip`.

---

## Project Structure

```
anitrack/
├── app.py              # Flask app — routes, OAuth, sync logic
├── templates/
│   └── index.html      # Frontend UI
├── requirements.txt
├── Procfile
├── Dockerfile
└── .env.example
```

---

## License

MIT

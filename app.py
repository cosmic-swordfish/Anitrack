import os, time, secrets, requests, hmac, hashlib, base64, json, threading
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, quote
from flask import Flask, redirect, request, session, jsonify, render_template, make_response

# Load .env only in local development
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Supabase / Postgres token store ───────────────────────────────────────────
try:
    import psycopg2, psycopg2.extras
    DB_URL = os.environ.get("DATABASE_URL", "")

    if not DB_URL:
        print("[DB] DATABASE_URL is not set — token persistence disabled. "
              "Add DATABASE_URL to your .env or Vercel env vars to enable it.")

    def _get_db():
        if not DB_URL:
            raise RuntimeError("DATABASE_URL is not set")
        return psycopg2.connect(DB_URL, sslmode="require")

    def _ensure_tables():
        if not DB_URL:
            return
        try:
            conn = _get_db(); cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_tokens (
                    username   TEXT PRIMARY KEY,
                    al_token   JSONB,
                    mal_token  JSONB,
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS anime_list (
                    id          BIGSERIAL PRIMARY KEY,
                    username    TEXT NOT NULL,
                    al_id       INT,
                    mal_id      INT,
                    title       TEXT NOT NULL,
                    status      TEXT,
                    progress    INT DEFAULT 0,
                    score       NUMERIC(4,1) DEFAULT 0,
                    episodes    INT,
                    media_status TEXT,
                    started_at   DATE,
                    completed_at DATE,
                    updated_at  TIMESTAMPTZ DEFAULT now(),
                    synced_at   TIMESTAMPTZ DEFAULT now(),
                    UNIQUE (username, al_id),
                    UNIQUE (username, mal_id)
                )
            """)
            # Backfill columns for tables created before started_at/completed_at existed.
            cur.execute("ALTER TABLE anime_list ADD COLUMN IF NOT EXISTS started_at DATE")
            cur.execute("ALTER TABLE anime_list ADD COLUMN IF NOT EXISTS completed_at DATE")
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_anime_list_username
                ON anime_list (username)
            """)
            conn.commit(); cur.close(); conn.close()
            print("[DB] Connected — user_tokens + anime_list tables ready.")
        except Exception as e:
            print(f"[DB] Table init error: {e}")
            print("[DB] Check that DATABASE_URL is correct and the database is reachable.")

    _ensure_tables()

    def db_save_tokens(username, al_token, mal_token):
        if not DB_URL: return
        try:
            conn = _get_db(); cur = conn.cursor()
            cur.execute("""
                INSERT INTO user_tokens (username, al_token, mal_token, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (username) DO UPDATE
                SET al_token=EXCLUDED.al_token, mal_token=EXCLUDED.mal_token, updated_at=now()
            """, (username, json.dumps(al_token), json.dumps(mal_token)))
            conn.commit(); cur.close(); conn.close()
            print(f"[DB] Tokens saved for {username}")
        except Exception as e:
            print(f"[DB] Save error: {e}")

    def db_load_all_tokens():
        if not DB_URL: return []
        try:
            conn = _get_db()
            cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT username, al_token, mal_token FROM user_tokens")
            rows = cur.fetchall(); cur.close(); conn.close()
            return rows
        except Exception as e:
            print(f"[DB] Load error: {e}"); return []

    def db_delete_tokens(username):
        if not DB_URL: return
        try:
            conn = _get_db(); cur = conn.cursor()
            cur.execute("DELETE FROM user_tokens WHERE username=%s", (username,))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[DB] Delete error: {e}")

    def _fuzzy_date_to_sql(d):
        """Convert an AniList FuzzyDate dict {year, month, day} (or an
        already-formatted 'YYYY-MM-DD' string) into a SQL date string.
        Returns None if the date is missing/incomplete.
        """
        if not d:
            return None
        if isinstance(d, str):
            return d or None
        if isinstance(d, dict):
            y, m, day = d.get("year"), d.get("month"), d.get("day")
            if y and m and day:
                return f"{int(y):04d}-{int(m):02d}-{int(day):02d}"
        return None

    def db_save_anime_list(username, entries):
        """Upsert a list of anime entries for a user into Supabase.
        entries: list of dicts from _parse_entry() (AL format) or mal entries.
        Each entry must have at least: alId or malId, title, status, progress, score.
        """
        if not DB_URL or not entries: return
        try:
            conn = _get_db(); cur = conn.cursor()
            for e in entries:
                al_id       = e.get("alId")
                mal_id      = e.get("malId")
                title       = e.get("title", "Unknown")
                status      = e.get("status", "")
                progress    = e.get("progress", 0)
                score       = e.get("score", 0)
                episodes    = e.get("episodes")
                media_status = e.get("mediaStatus")
                started_at   = _fuzzy_date_to_sql(e.get("startedAt"))
                completed_at = _fuzzy_date_to_sql(e.get("completedAt"))

                # Build the conflict target: prefer alId, fallback to malId
                if al_id:
                    cur.execute("""
                        INSERT INTO anime_list
                            (username, al_id, mal_id, title, status, progress, score, episodes, media_status, started_at, completed_at, updated_at, synced_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                        ON CONFLICT (username, al_id) DO UPDATE SET
                            mal_id       = EXCLUDED.mal_id,
                            title        = EXCLUDED.title,
                            status       = EXCLUDED.status,
                            progress     = EXCLUDED.progress,
                            score        = EXCLUDED.score,
                            episodes     = EXCLUDED.episodes,
                            media_status = EXCLUDED.media_status,
                            started_at   = EXCLUDED.started_at,
                            completed_at = EXCLUDED.completed_at,
                            updated_at   = EXCLUDED.updated_at,
                            synced_at    = now()
                    """, (username, al_id, mal_id, title, status, progress, score, episodes, media_status, started_at, completed_at))
                elif mal_id:
                    cur.execute("""
                        INSERT INTO anime_list
                            (username, al_id, mal_id, title, status, progress, score, episodes, media_status, started_at, completed_at, updated_at, synced_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                        ON CONFLICT (username, mal_id) DO UPDATE SET
                            al_id        = EXCLUDED.al_id,
                            title        = EXCLUDED.title,
                            status       = EXCLUDED.status,
                            progress     = EXCLUDED.progress,
                            score        = EXCLUDED.score,
                            episodes     = EXCLUDED.episodes,
                            media_status = EXCLUDED.media_status,
                            started_at   = EXCLUDED.started_at,
                            completed_at = EXCLUDED.completed_at,
                            updated_at   = EXCLUDED.updated_at,
                            synced_at    = now()
                    """, (username, al_id, mal_id, title, status, progress, score, episodes, media_status, started_at, completed_at))
            conn.commit(); cur.close(); conn.close()
            print(f"[DB] anime_list: saved {len(entries)} entries for {username}")
        except Exception as e:
            print(f"[DB] anime_list save error: {e}")

    def db_load_anime_list(username, status_filter=None):
        """Load stored anime list for a user. Optionally filter by status."""
        if not DB_URL: return []
        try:
            conn = _get_db()
            cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if status_filter:
                cur.execute(
                    "SELECT * FROM anime_list WHERE username=%s AND status=%s ORDER BY title",
                    (username, status_filter)
                )
            else:
                cur.execute(
                    "SELECT * FROM anime_list WHERE username=%s ORDER BY title",
                    (username,)
                )
            rows = cur.fetchall(); cur.close(); conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            print(f"[DB] anime_list load error: {e}"); return []

    def db_delete_anime_list(username):
        """Delete all anime entries for a user (e.g. on logout)."""
        if not DB_URL: return
        try:
            conn = _get_db(); cur = conn.cursor()
            cur.execute("DELETE FROM anime_list WHERE username=%s", (username,))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[DB] anime_list delete error: {e}")

    DB_AVAILABLE = True

    # ── Owner-only guard ──────────────────────────────────────────────────────
    # Set OWNER_USERNAME in your .env / Vercel env vars to your AniList username.
    # When set, only that username's data is ever written to the database.
    # Visitors can still use the site (sync, view diffs) but nothing is persisted.
    OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "").strip().lower()

    def _is_owner(username):
        if not OWNER_USERNAME:
            return True  # not set → no restriction (backwards-compatible)
        return username.strip().lower() == OWNER_USERNAME

    # Wrap save functions with the owner check
    _db_save_tokens_inner    = db_save_tokens
    _db_save_anime_list_inner = db_save_anime_list

    def db_save_tokens(username, al_token, mal_token):
        if not _is_owner(username):
            print(f"[DB] Skipped token save for non-owner: {username}")
            return
        _db_save_tokens_inner(username, al_token, mal_token)

    def db_save_anime_list(username, entries):
        if not _is_owner(username):
            print(f"[DB] Skipped anime_list save for non-owner: {username}")
            return
        _db_save_anime_list_inner(username, entries)

except ImportError:
    DB_AVAILABLE = False
    DB_URL = ""
    print("[DB] psycopg2 not installed — token persistence disabled. "
          "Run: pip install psycopg2-binary")
    def db_save_tokens(u, a, m): pass
    def db_load_all_tokens(): return []
    def db_delete_tokens(u): pass
    def db_save_anime_list(u, e): pass
    def db_load_anime_list(u, status_filter=None): return []
    def db_delete_anime_list(u): pass

app = Flask(__name__)

# ── HTTPS flag (needed before secret key check) ───────────────────────────────
IS_HTTPS = os.environ.get("HTTPS", "false").lower() == "true"

# ── Secret key ────────────────────────────────────────────────────────────────
_secret = os.environ.get("SECRET_KEY", "")
if not _secret:
    if IS_HTTPS:
        raise RuntimeError(
            "SECRET_KEY env var must be set in production. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    _secret = secrets.token_hex(32)
    print("=" * 70)
    print("WARNING: SECRET_KEY env var is not set.")
    print(f"Add to your .env:  SECRET_KEY={_secret}")
    print("=" * 70)

app.secret_key = _secret
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = IS_HTTPS
# Sessions default to browser-session-only cookies (no Max-Age), which is why
# reconnecting to AniList/MAL kept dropping — the cookie itself was expiring,
# not just the OAuth token. Make it a real 90-day persistent cookie instead,
# refreshed on every request so it keeps rolling forward while you're active.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=90)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

@app.before_request
def _make_session_permanent():
    session.permanent = True

# ── Config ────────────────────────────────────────────────────────────────────
ANILIST_USERNAME    = os.environ.get("ANILIST_USERNAME", "cosmicswordfish")
MAL_CLIENT_ID       = os.environ.get("MAL_CLIENT_ID", "")
MAL_CLIENT_SECRET   = os.environ.get("MAL_CLIENT_SECRET", "")
MAL_REDIRECT_URI    = os.environ.get("MAL_REDIRECT_URI", "http://localhost:5000/mal/callback")
AL_CLIENT_ID        = os.environ.get("AL_CLIENT_ID", "")
AL_CLIENT_SECRET    = os.environ.get("AL_CLIENT_SECRET", "")
AL_REDIRECT_URI     = os.environ.get("AL_REDIRECT_URI", "http://localhost:5000/al/callback")
CRON_SECRET         = os.environ.get("CRON_SECRET", "")

ANILIST_API   = "https://graphql.anilist.co"
MAL_AUTH_URL  = "https://myanimelist.net/v1/oauth2/authorize"
MAL_TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
MAL_API       = "https://api.myanimelist.net/v2"
AL_AUTH_URL   = "https://anilist.co/api/v2/oauth/authorize"
AL_TOKEN_URL  = "https://anilist.co/api/v2/oauth/token"

AL_TO_MAL = {
    "CURRENT":   "watching",
    "COMPLETED": "completed",
    "PAUSED":    "on_hold",
    "DROPPED":   "dropped",
    "PLANNING":  "plan_to_watch",
}
MAL_TO_AL = {v: k for k, v in AL_TO_MAL.items()}

# ── PKCE / OAuth cookie helpers ───────────────────────────────────────────────
def _sign(value: str) -> str:
    return hmac.new(_secret.encode(), value.encode(), hashlib.sha256).hexdigest()

def set_oauth_cookie(response, name: str, data: dict, max_age=600):
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    sig = _sign(payload)
    response.set_cookie(name, f"{payload}.{sig}", max_age=max_age, httponly=True,
                        samesite="Lax", secure=IS_HTTPS, path="/")

def get_oauth_cookie(name: str):
    raw = request.cookies.get(name, "")
    if "." not in raw:
        return None
    payload, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode())
    except Exception:
        return None

# ── Shared HTTP session (connection pooling — avoids re-doing TLS handshakes
#    on every single AniList/MAL request within a function invocation) ────────
_http = requests.Session()
_http.mount("https://", requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10))

# ── AniList helpers ───────────────────────────────────────────────────────────
# AniList's API has been running in a degraded state, capped at 30 req/min
# (vs. the usual 90) — see https://docs.anilist.co/guide/rate-limiting.
# Rather than sleeping a fixed amount before every single call (safe but slow
# for small lists), we read the X-RateLimit-Remaining / X-RateLimit-Reset
# headers AniList sends back on every response and only pause once the
# budget is actually about to run out — so short lists fetch at full speed
# and long ones throttle themselves right before they'd get a 429.
ANILIST_REQUEST_DELAY = 2.1  # fallback pacing if rate-limit headers are ever absent
ANILIST_MAX_RETRIES = 2
_al_rate_lock = threading.Lock()
_al_rate_state = {"remaining": None, "reset": 0}

def _al_throttle():
    with _al_rate_lock:
        remaining, reset = _al_rate_state["remaining"], _al_rate_state["reset"]
    if remaining is None:
        return  # no data yet (first call) — just go
    if remaining <= 1:
        wait = reset - time.time()
        if wait > 0:
            time.sleep(wait + 0.5)

def _al_record_rate_headers(headers):
    try:
        remaining = int(headers.get("X-RateLimit-Remaining"))
        reset = int(headers.get("X-RateLimit-Reset"))
    except (TypeError, ValueError):
        return
    with _al_rate_lock:
        _al_rate_state["remaining"], _al_rate_state["reset"] = remaining, reset

def anilist_query(query, variables=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token.get('access_token', '')}"
    for attempt in range(ANILIST_MAX_RETRIES + 1):
        _al_throttle()
        r = _http.post(ANILIST_API, json={"query": query, "variables": variables or {}},
                        headers=headers, timeout=15)
        _al_record_rate_headers(r.headers)
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("Retry-After", ANILIST_REQUEST_DELAY))
            except ValueError:
                wait = ANILIST_REQUEST_DELAY
            if attempt < ANILIST_MAX_RETRIES:
                time.sleep(min(wait, 30) + 0.5)
                continue
            raise RuntimeError(f"AniList rate limit hit, retry after {wait:.0f}s")
        return r.json()

GQL_LIST_COLLECTION = """
query($username: String) {
  MediaListCollection(userName: $username, type: ANIME) {
    lists {
      entries {
        mediaId status score(format: POINT_10) progress updatedAt
        startedAt { year month day }
        completedAt { year month day }
        media {
          id idMal title { romaji english } status episodes
          nextAiringEpisode { airingAt episode timeUntilAiring }
        }
      }
    }
  }
}"""

GQL_LOOKUP_BY_MAL = """
query($malId: Int) {
  Media(idMal: $malId, type: ANIME) { id idMal title { romaji } }
}"""

GQL_SEARCH = """
query($search: String, $page: Int) {
  Page(page: $page, perPage: 10) {
    media(search: $search, type: ANIME) {
      id idMal
      title { romaji english }
      status episodes
      coverImage { medium }
      mediaListEntry {
        status score(format: POINT_10) progress
        startedAt { year month day }
        completedAt { year month day }
      }
    }
  }
}"""

_AL_DELETE_GQL = """
mutation($id: Int) {
  DeleteMediaListEntry(id: $id) { deleted }
}"""

_AL_GET_LIST_ENTRY_GQL = """
query($mediaId: Int) {
  MediaList(mediaId: $mediaId, type: ANIME) { id }
}"""

def _parse_entry(e):
    t = e["media"]["title"]
    return {
        "alId":     e["media"]["id"],
        "malId":    e["media"]["idMal"],
        "title":    t.get("english") or t.get("romaji") or "Unknown",
        "status":   e["status"],
        "progress": e["progress"],
        "score":    e["score"],
        "updatedAt": e["updatedAt"],
        "mediaStatus": e["media"]["status"],
        "episodes":    e["media"]["episodes"],
        "nextAiring":  e["media"].get("nextAiringEpisode"),
        "startedAt":   e.get("startedAt"),
        "completedAt": e.get("completedAt"),
    }

def get_anilist_list(token=None, username=None):
    """Fetch the user's entire AniList in ONE request via MediaListCollection,
    instead of paginating through Page(mediaList) 50 entries at a time. This
    is the AniList-recommended query for "give me this user's whole list" —
    it returns every status group (watching/completed/etc.) in a single
    round trip, which is both far faster and much easier on the rate limit
    than N page requests. (Covers up to 11,000 entries — AniList's own cap
    for "give me everything" queries; well beyond any real list size.)"""
    username = username or ANILIST_USERNAME
    data = anilist_query(GQL_LIST_COLLECTION, {"username": username}, token=token)
    collection = data.get("data", {}).get("MediaListCollection") or {}
    entries = []
    for group in collection.get("lists", []):
        entries += [_parse_entry(e) for e in group.get("entries", [])]
    return entries

def al_headers():
    token = session.get("al_token")
    if token and token.get("expires_at", 0) < time.time() + 300:
        token = refresh_al_token(token)
    return token

def refresh_al_token(token):
    if not token.get("refresh_token"):
        session.pop("al_token", None); return None
    try:
        r = requests.post(AL_TOKEN_URL, json={
            "grant_type": "refresh_token", "client_id": AL_CLIENT_ID,
            "client_secret": AL_CLIENT_SECRET, "refresh_token": token["refresh_token"]
        }, timeout=15)
        new_token = r.json()
        if "access_token" in new_token:
            new_token["expires_at"] = time.time() + new_token.get("expires_in", 3600)
            session["al_token"] = new_token
            return new_token
    except Exception:
        pass
    session.pop("al_token", None); return None

_AL_UPDATE_GQL = """
mutation($mediaId: Int, $status: MediaListStatus, $progress: Int, $score: Float,
         $startedAt: FuzzyDateInput, $completedAt: FuzzyDateInput) {
    SaveMediaListEntry(mediaId: $mediaId, status: $status, progress: $progress, score: $score,
                       startedAt: $startedAt, completedAt: $completedAt) {
        id
    }
}"""

def al_update(media_id, status=None, progress=None, score=None, token=None,
              started_at=None, completed_at=None):
    token = token or al_headers()
    if not token: return False
    if status is None and progress is None and score is None:
        return False
    vars_ = {"mediaId": media_id}
    if status       is not None: vars_["status"]      = status
    if progress     is not None: vars_["progress"]    = progress
    if score        is not None: vars_["score"]       = float(score)
    if started_at   is not None: vars_["startedAt"]   = started_at
    if completed_at is not None: vars_["completedAt"] = completed_at
    r = anilist_query(_AL_UPDATE_GQL, vars_, token=token)
    return "errors" not in r

def al_get_list_entry_id(media_id, token=None):
    """Get the MediaList entry ID needed for deletion."""
    token = token or al_headers()
    if not token: return None
    data = anilist_query(_AL_GET_LIST_ENTRY_GQL, {"mediaId": media_id}, token=token)
    return (data.get("data") or {}).get("MediaList", {}).get("id")

def al_delete(media_id, token=None):
    """Delete an anime from AniList by media ID."""
    token = token or al_headers()
    if not token: return False
    entry_id = al_get_list_entry_id(media_id, token=token)
    if not entry_id: return False
    r = anilist_query(_AL_DELETE_GQL, {"id": entry_id}, token=token)
    return (r.get("data") or {}).get("DeleteMediaListEntry", {}).get("deleted", False)

# ── MAL helpers ───────────────────────────────────────────────────────────────
def mal_headers():
    token = session.get("mal_token")
    if not token: return None
    if token.get("expires_at", 0) < time.time() + 300:
        token = refresh_mal_token(token)
    return {"Authorization": f"Bearer {token.get('access_token', '')}"} if token else None

def refresh_mal_token(token):
    payload = {
        "grant_type": "refresh_token", "refresh_token": token.get("refresh_token", ""),
        "client_id":  MAL_CLIENT_ID,
    }
    if MAL_CLIENT_SECRET:
        payload["client_secret"] = MAL_CLIENT_SECRET
    try:
        r = requests.post(MAL_TOKEN_URL, data=payload, timeout=15)
        new_token = r.json()
        if "access_token" in new_token:
            new_token["expires_at"] = time.time() + new_token.get("expires_in", 3600)
            session["mal_token"] = new_token
            session.modified = True
            return new_token
    except Exception:
        pass
    session.pop("mal_token", None); return None

def get_mal_list():
    headers = mal_headers()
    return get_mal_list_with_headers(headers)

def get_mal_list_with_headers(headers):
    if not headers: return []
    items, offset = [], 0
    while True:
        r = _http.get(f"{MAL_API}/users/@me/animelist",
                      headers=headers,
                      params={"fields": "list_status{status,score,num_episodes_watched,updated_at}",
                              "limit": 1000, "offset": offset},
                      timeout=15)
        if r.status_code != 200: break
        data = r.json()
        for item in data.get("data", []):
            ls = item["list_status"]
            # MAL returns updated_at as ISO-8601; convert to unix timestamp for easy comparison
            raw_updated = ls.get("updated_at", "")
            try:
                import datetime
                updated_at = int(datetime.datetime.fromisoformat(
                    raw_updated.replace("Z", "+00:00")).timestamp()) if raw_updated else 0
            except Exception:
                updated_at = 0
            items.append({
                "malId":     item["node"]["id"],
                "title":     item["node"]["title"],
                "status":    ls.get("status", ""),
                "progress":  ls.get("num_episodes_watched", 0),
                "score":     ls.get("score", 0),
                "updatedAt": updated_at,
            })
        if not data.get("paging", {}).get("next"): break
        offset += 1000
    return items

def get_both_lists(al_token, username):
    """Fetch the AniList and MAL lists concurrently instead of sequentially —
    they're independent network calls to different APIs, so there's no reason
    to make the user wait for both round trips back-to-back.

    mal_headers() touches Flask's `session`, which is only valid on the
    request thread, so it must be resolved here (not inside the worker)
    before we hand off to the pool."""
    mal_hdrs = mal_headers()
    with ThreadPoolExecutor(max_workers=2) as ex:
        al_future  = ex.submit(get_anilist_list, al_token, username)
        mal_future = ex.submit(get_mal_list_with_headers, mal_hdrs)
        return al_future.result(), mal_future.result()

def mal_update(mal_id, status=None, score=None, progress=None):
    headers = mal_headers()
    if not headers: return False
    payload = {}
    if status:   payload["status"] = status
    if score is not None:    payload["score"] = score
    if progress is not None: payload["num_watched_episodes"] = progress
    if not payload: return False
    r = _http.patch(f"{MAL_API}/anime/{mal_id}/my_list_status",
                    headers=headers, data=payload, timeout=10)
    return r.status_code == 200

# ── Sync logic ────────────────────────────────────────────────────────────────
def resolve_fields(al, mal):
    """Pick winning values using most-recently-updated platform (Option A).

    al["status"]  is always an AL key  (e.g. "CURRENT")
    mal["status"] is always a MAL key  (e.g. "watching")
    Returns (win_progress, win_score, win_status) where win_status is an AL key.
    """
    al_newer = al["updatedAt"] >= mal.get("updatedAt", 0)

    # Progress: winner takes all
    win_progress = al["progress"] if al_newer else mal["progress"]

    # Score: winner's score, but fall back to the other if winner hasn't rated
    if al_newer:
        win_score = al["score"] if al["score"] > 0 else mal["score"]
    else:
        win_score = mal["score"] if mal["score"] > 0 else al["score"]

    # Status: winner's status, normalised to an AL key
    if al_newer:
        win_status = al["status"]  # already an AL key
    else:
        win_status = MAL_TO_AL.get(mal["status"], "PLANNING")  # convert MAL → AL

    return win_progress, win_score, win_status

def compute_diff(al_list, mal_list):
    mal_map = {e["malId"]: e for e in mal_list}
    diffs = []
    for al in al_list:
        mal_id = al["malId"]
        if not mal_id: continue
        mal = mal_map.get(mal_id)
        al_mal_status = AL_TO_MAL.get(al["status"])
        if not mal:
            diffs.append({
                "malId": mal_id, "alId": al["alId"], "title": al["title"],
                "action": "add_to_mal", "label": "Missing on MAL",
                "al_status": al["status"], "mal_status": None,
                "al_progress": al["progress"], "mal_progress": 0,
                "al_score": al["score"], "mal_score": 0,
                "needs_al_update": False,
            })
        else:
            changes = []
            win_progress, win_score, win_status = resolve_fields(al, mal)
            if al_mal_status and al_mal_status != mal["status"]:
                changes.append(f"status: AL={al['status']} MAL={mal['status']} → {win_status}")
            if al["progress"] != mal["progress"]:
                changes.append(f"progress: AL={al['progress']} MAL={mal['progress']} → {win_progress}")
            if al["score"] != mal["score"] and (al["score"] > 0 or mal["score"] > 0):
                changes.append(f"score: AL={al['score']} MAL={mal['score']} → {win_score}")
            if changes:
                needs_al = (win_status != al["status"] or win_progress != al["progress"] or win_score != al["score"])
                diffs.append({
                    "malId": mal_id, "alId": al["alId"], "title": al["title"],
                    "action": "update", "label": "Out of sync", "changes": changes,
                    "al_status": al["status"], "mal_status": mal["status"],
                    "al_progress": al["progress"], "mal_progress": mal["progress"],
                    "al_score": al["score"], "mal_score": mal["score"],
                    "win_status": win_status, "win_progress": win_progress, "win_score": win_score,
                    "needs_al_update": needs_al,
                })
    al_mal_ids = {a["malId"] for a in al_list}
    for mal in mal_list:
        if mal["malId"] not in al_mal_ids:
            diffs.append({
                "malId": mal["malId"], "alId": None, "title": mal["title"],
                "action": "only_on_mal", "label": "Only on MAL", "changes": [],
                "al_status": None, "mal_status": mal["status"],
                "al_progress": 0, "mal_progress": mal["progress"],
                "al_score": 0, "mal_score": mal["score"],
                "needs_al_update": False,
            })
    return diffs

def resolve_al_id_from_mal(mal_id, token=None):
    data = anilist_query(GQL_LOOKUP_BY_MAL, {"malId": mal_id}, token=token)
    return (data.get("data") or {}).get("Media", {}).get("id")

def apply_diff_item(diff, al_token=None):
    mal_ok = al_ok = False
    if diff["action"] == "add_to_mal":
        mal_status = AL_TO_MAL.get(diff["al_status"])
        if mal_status and diff.get("malId"):
            mal_ok = mal_update(diff["malId"], status=mal_status,
                                progress=diff.get("al_progress", 0),
                                score=diff.get("al_score", 0))
    elif diff["action"] == "update":
        wp, ws, wst = diff.get("win_progress", 0), diff.get("win_score", 0), diff.get("win_status", diff["al_status"])
        mal_status = AL_TO_MAL.get(wst)
        if mal_status and diff.get("malId"):
            mal_ok = mal_update(diff["malId"], status=mal_status, progress=wp, score=ws)
        if diff.get("needs_al_update") and diff.get("alId"):
            al_ok = al_update(diff["alId"], status=wst, progress=wp, score=ws, token=al_token)
    elif diff["action"] == "only_on_mal":
        al_id = diff.get("alId") or resolve_al_id_from_mal(diff["malId"], token=al_token)
        if al_id:
            al_status = MAL_TO_AL.get(diff["mal_status"], "PLANNING")
            al_ok = al_update(al_id, status=al_status,
                              progress=diff.get("mal_progress", 0),
                              score=diff.get("mal_score", 0),
                              token=al_token)
    return {"mal_ok": mal_ok, "al_ok": al_ok}

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/service-worker.js")
def service_worker():
    from flask import send_from_directory
    return send_from_directory("static", "service-worker.js",
                               mimetype="application/javascript")

@app.route("/api/db-status")
def db_status():
    """Debug endpoint — visit /api/db-status to check DB connectivity and rows."""
    if not DB_AVAILABLE:
        return jsonify({"status": "error", "detail": "psycopg2 not installed. Run: pip install psycopg2-binary"}), 500
    if not DB_URL:
        return jsonify({"status": "error", "detail": "DATABASE_URL env var is not set"}), 500
    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.user_tokens')")
        table_exists = cur.fetchone()[0] is not None
        rows = []
        if table_exists:
            cur.execute("SELECT username, updated_at, "
                        "(al_token IS NOT NULL) AS has_al, "
                        "(mal_token IS NOT NULL) AS has_mal "
                        "FROM user_tokens ORDER BY updated_at DESC")
            rows = [{"username": r[0], "updated_at": str(r[1]),
                     "al_connected": r[2], "mal_connected": r[3]}
                    for r in cur.fetchall()]
        cur.close(); conn.close()
        return jsonify({
            "status": "ok",
            "table_exists": table_exists,
            "row_count": len(rows),
            "rows": rows
        })
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route("/")
def index():
    return render_template("index.html",
        anilist_username = session.get("al_username", ANILIST_USERNAME),
        mal_connected    = "mal_token" in session,
        al_connected     = "al_token"  in session,
        mal_error        = request.args.get("mal_error"),
        mal_success      = request.args.get("mal_success"),
        al_error         = request.args.get("al_error"),
        al_success       = request.args.get("al_success"),
    )

@app.route("/api/anilist", methods=["POST"])
def anilist_proxy():
    body = request.get_json() or {}
    result = anilist_query(body.get("query"), body.get("variables"), token=al_headers())
    return jsonify(result)

# ── MAL OAuth ─────────────────────────────────────────────────────────────────
@app.route("/mal/login")
def mal_login():
    if not MAL_CLIENT_ID:
        return redirect("/?mal_error=" + quote("MAL_CLIENT_ID is not configured."))
    # MAL's PKCE implementation requires the "plain" method.
    # verifier == challenge; use token_urlsafe for RFC 7636-safe characters.
    verifier = secrets.token_urlsafe(32)   # 43-char URL-safe string, no padding
    state = secrets.token_hex(16)
    resp = make_response(redirect(MAL_AUTH_URL + "?" + urlencode({
        "response_type": "code", "client_id": MAL_CLIENT_ID,
        "redirect_uri": MAL_REDIRECT_URI, "state": state,
        "code_challenge": verifier, "code_challenge_method": "plain",
    })))
    set_oauth_cookie(resp, "mal_pkce", {"v": verifier, "s": state})
    return resp

@app.route("/mal/callback")
def mal_callback():
    if request.args.get("error"):
        return redirect("/?mal_error=" + quote(request.args.get("error_description", request.args["error"])))
    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    if not code:
        return redirect("/?mal_error=" + quote("No authorization code received"))
    pkce = get_oauth_cookie("mal_pkce")
    if not pkce:
        return redirect("/?mal_error=" + quote("PKCE cookie missing. Enable cookies and try again."))
    if not hmac.compare_digest(state, pkce["s"]):
        return redirect("/?mal_error=" + quote("State mismatch. Please try again."))
    payload = {"grant_type": "authorization_code", "code": code,
               "redirect_uri": MAL_REDIRECT_URI, "client_id": MAL_CLIENT_ID,
               "code_verifier": pkce["v"]}
    if MAL_CLIENT_SECRET:
        payload["client_secret"] = MAL_CLIENT_SECRET
    try:
        r = requests.post(MAL_TOKEN_URL, data=payload, timeout=15)
        token = r.json()
    except Exception as e:
        return redirect("/?mal_error=" + quote(f"Token request failed: {e}"))
    if "error" in token:
        return redirect("/?mal_error=" + quote(token.get("hint") or token.get("message") or token["error"]))
    if "access_token" not in token:
        return redirect("/?mal_error=" + quote(f"Unexpected MAL response (HTTP {r.status_code})"))
    token["expires_at"] = time.time() + token.get("expires_in", 3600)
    session["mal_token"] = token
    session.modified = True
    # Save to Supabase if AL is also connected
    if session.get("al_token"):
        username = session.get("al_username", ANILIST_USERNAME)
        db_save_tokens(username, session["al_token"], token)
    resp = make_response(redirect("/?mal_success=1"))
    resp.delete_cookie("mal_pkce", path="/")
    return resp

@app.route("/mal/logout")
def mal_logout():
    username = session.get("al_username", ANILIST_USERNAME)
    db_delete_tokens(username)
    session.pop("mal_token", None)
    return redirect("/")

# ── AniList OAuth ─────────────────────────────────────────────────────────────
@app.route("/al/login")
def al_login():
    if not AL_CLIENT_ID:
        return redirect("/?al_error=" + quote("AL_CLIENT_ID is not configured."))
    state = secrets.token_hex(16)
    resp = make_response(redirect(AL_AUTH_URL + "?" + urlencode({
        "client_id": AL_CLIENT_ID, "redirect_uri": AL_REDIRECT_URI,
        "response_type": "code", "state": state,
    })))
    set_oauth_cookie(resp, "al_state", {"s": state})
    return resp

@app.route("/al/callback")
def al_callback():
    if request.args.get("error"):
        return redirect("/?al_error=" + quote(request.args.get("error_description", request.args["error"])))
    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    if not code:
        return redirect("/?al_error=" + quote("No authorization code received from AniList"))
    saved = get_oauth_cookie("al_state")
    if not saved or not hmac.compare_digest(state, saved["s"]):
        return redirect("/?al_error=" + quote("State mismatch. Please try again."))
    try:
        r = requests.post(AL_TOKEN_URL, json={
            "grant_type": "authorization_code", "client_id": AL_CLIENT_ID,
            "client_secret": AL_CLIENT_SECRET, "redirect_uri": AL_REDIRECT_URI,
            "code": code,
        }, timeout=15)
        token = r.json()
    except Exception as e:
        return redirect("/?al_error=" + quote(f"Token request failed: {e}"))
    if "access_token" not in token:
        return redirect("/?al_error=" + quote(f"Unexpected AniList response (HTTP {r.status_code})"))
    token["expires_at"] = time.time() + token.get("expires_in", 3600)
    session["al_token"] = token
    session.modified = True
    # Fetch the authenticated user's AniList username and store in session
    try:
        viewer_data = anilist_query(
            "query { Viewer { name } }",
            token=token
        )
        al_viewer = (viewer_data.get("data") or {}).get("Viewer", {}).get("name", "")
        if al_viewer:
            session["al_username"] = al_viewer
    except Exception:
        pass
    # Save to Supabase if MAL is also connected
    if session.get("mal_token"):
        username = session.get("al_username", ANILIST_USERNAME)
        db_save_tokens(username, token, session["mal_token"])
    resp = make_response(redirect("/?al_success=1"))
    resp.delete_cookie("al_state", path="/")
    return resp

@app.route("/al/logout")
def al_logout():
    session.pop("al_token", None)
    session.pop("al_username", None)
    return redirect("/")

# ── Sync endpoints ────────────────────────────────────────────────────────────
@app.route("/api/sync/diff")
def sync_diff():
    if "mal_token" not in session:
        return jsonify({"error": "MAL not connected"}), 401
    username = request.args.get("username", ANILIST_USERNAME).strip() or ANILIST_USERNAME
    try:
        al_list, mal_list = get_both_lists(al_headers(), username)
        diffs    = compute_diff(al_list, mal_list)
        # Save fetched AL list to Supabase
        db_save_anime_list(username, al_list)
        return jsonify({"diffs": diffs, "al_count": len(al_list), "mal_count": len(mal_list)})
    except Exception as e:
        print(f"[sync_diff] {username}: {e}")
        if "rate limit" in str(e).lower():
            return jsonify({"error": "AniList rate limit hit — wait a moment and try again."}), 429
        return jsonify({"error": "Sync failed. Please try again."}), 500

@app.route("/api/sync/apply", methods=["POST"])
def sync_apply():
    if "mal_token" not in session:
        return jsonify({"error": "MAL not connected"}), 401
    items = (request.get_json(silent=True) or {}).get("items", [])
    results = []
    for item in items:
        if item.get("action") not in ("add_to_mal", "update", "only_on_mal"):
            continue
        r = apply_diff_item(item, al_token=al_headers())
        results.append({"malId": item["malId"], "title": item.get("title"), **r})
    return jsonify({"results": results})

@app.route("/api/sync/auto", methods=["POST"])
def sync_auto():
    if "mal_token" not in session:
        return jsonify({"error": "MAL not connected"}), 401
    username = request.args.get("username", ANILIST_USERNAME).strip() or ANILIST_USERNAME
    try:
        al_list, mal_list = get_both_lists(al_headers(), username)
        diffs    = compute_diff(al_list, mal_list)
        results  = []
        for diff in diffs:
            if diff["action"] in ("add_to_mal", "update", "only_on_mal") and diff.get("malId"):
                try:
                    r = apply_diff_item(diff, al_token=al_headers())
                    results.append({"title": diff["title"], **r})
                except Exception as item_err:
                    results.append({"title": diff.get("title", "?"), "error": str(item_err)})
                time.sleep(0.3)
        # Save post-sync AL list to Supabase
        db_save_anime_list(username, al_list)
        return jsonify({"synced": len(results), "results": results})
    except Exception as e:
        print(f"[sync_auto] {username}: {e}")
        if "rate limit" in str(e).lower():
            return jsonify({"error": "AniList rate limit hit — wait a moment and try again."}), 429
        return jsonify({"error": "Auto sync failed. Please try again."}), 500

# ── Cron auto-sync (called by Vercel Cron every 2 hours) ─────────────────────
@app.route("/api/cron/sync", methods=["GET", "POST"])
def cron_sync():
    # Protect with a secret so only Vercel Cron can call it
    auth = request.headers.get("Authorization", "")
    if CRON_SECRET and auth != f"Bearer {CRON_SECRET}":
        return jsonify({"error": "Unauthorized"}), 401

    rows = db_load_all_tokens()
    if not rows:
        return jsonify({"message": "No users registered", "synced_users": 0})

    total_synced = 0
    results = []
    for row in rows:
        username  = row["username"]
        al_token  = row["al_token"]
        mal_token = row["mal_token"]
        try:
            print(f"[Cron] Syncing {username}…")

            # ── Refresh MAL token if near expiry ──────────────────────────────
            if mal_token.get("expires_at", 0) < time.time() + 300:
                payload = {
                    "grant_type":    "refresh_token",
                    "refresh_token": mal_token.get("refresh_token", ""),
                    "client_id":     MAL_CLIENT_ID,
                }
                if MAL_CLIENT_SECRET:
                    payload["client_secret"] = MAL_CLIENT_SECRET
                try:
                    rr = requests.post(MAL_TOKEN_URL, data=payload, timeout=15)
                    new_tok = rr.json()
                    if "access_token" in new_tok:
                        new_tok["expires_at"] = time.time() + new_tok.get("expires_in", 3600)
                        mal_token = new_tok
                        db_save_tokens(username, al_token, mal_token)
                        print(f"[Cron] Refreshed MAL token for {username}")
                except Exception as te:
                    print(f"[Cron] MAL token refresh failed for {username}: {te}")

            # mal_headers() relies on Flask's `session`, which isn't valid here
            # (this route runs outside a normal request/session context per-user),
            # so headers are built directly from the stored mal_token instead.
            # Built once up-front so both the fetch below and the later
            # patch-back-to-MAL calls share the same headers.
            mal_headers_cron = {"Authorization": f"Bearer {mal_token.get('access_token', '')}"}

            # ── Fetch AniList + MAL lists concurrently (independent network
            #    calls — no reason to wait for one before starting the other) ──
            def _fetch_mal_items():
                items, offset = [], 0
                while True:
                    r = _http.get(f"{MAL_API}/users/@me/animelist",
                                  headers=mal_headers_cron,
                                  params={"fields": "list_status{status,score,num_episodes_watched,updated_at}",
                                          "limit": 1000, "offset": offset},
                                  timeout=15)
                    if r.status_code != 200: break
                    data = r.json()
                    for item in data.get("data", []):
                        ls = item["list_status"]
                        raw_updated = ls.get("updated_at", "")
                        try:
                            import datetime
                            updated_at = int(datetime.datetime.fromisoformat(
                                raw_updated.replace("Z", "+00:00")).timestamp()) if raw_updated else 0
                        except Exception:
                            updated_at = 0
                        items.append({
                            "malId":     item["node"]["id"],
                            "title":     item["node"]["title"],
                            "status":    ls.get("status", ""),
                            "progress":  ls.get("num_episodes_watched", 0),
                            "score":     ls.get("score", 0),
                            "updatedAt": updated_at,
                        })
                    if not data.get("paging", {}).get("next"): break
                    offset += 1000
                return items

            with ThreadPoolExecutor(max_workers=2) as ex:
                al_future  = ex.submit(get_anilist_list, al_token, username)
                mal_future = ex.submit(_fetch_mal_items)
                al_list    = al_future.result()
                mal_items  = mal_future.result()

            diffs = compute_diff(al_list, mal_items)
            synced = 0
            for diff in diffs:
                action = diff["action"]

                if action in ("add_to_mal", "update") and diff.get("malId"):
                    wp  = diff.get("win_progress", diff.get("al_progress", 0))
                    ws  = diff.get("win_score",    diff.get("al_score", 0))
                    wst = diff.get("win_status",   diff.get("al_status", "CURRENT"))
                    mal_status = AL_TO_MAL.get(wst)
                    if mal_status:
                        payload = {"status": mal_status}
                        if wp: payload["num_watched_episodes"] = wp
                        if ws: payload["score"] = ws
                        requests.patch(f"{MAL_API}/anime/{diff['malId']}/my_list_status",
                                       headers=mal_headers_cron, data=payload, timeout=10)
                        synced += 1
                        time.sleep(0.3)
                    # Also update AniList side if needed
                    if diff.get("needs_al_update") and diff.get("alId"):
                        al_update(diff["alId"], status=wst, progress=wp, score=ws, token=al_token)
                        time.sleep(0.3)

                elif action == "only_on_mal" and diff.get("malId"):
                    # sync MAL-only entries back to AniList
                    al_id = diff.get("alId") or resolve_al_id_from_mal(diff["malId"], token=al_token)
                    if al_id:
                        al_status = MAL_TO_AL.get(diff["mal_status"], "PLANNING")
                        al_update(al_id, status=al_status,
                                  progress=diff.get("mal_progress", 0),
                                  score=diff.get("mal_score", 0),
                                  token=al_token)
                        synced += 1
                        time.sleep(0.3)

            total_synced += synced
            results.append({"username": username, "synced": synced})
            # Save final synced list to Supabase
            db_save_anime_list(username, al_list)
            print(f"[Cron] {username}: {synced} items synced")
        except Exception as e:
            print(f"[Cron] Error for {username}: {e}")
            results.append({"username": username, "error": str(e)})

    return jsonify({"synced_users": len(rows), "total_synced": total_synced, "results": results})

# ── Supabase anime list API ───────────────────────────────────────────────────
@app.route("/api/db/anime-list")
def db_anime_list():
    """Return the stored anime list for a user from Supabase.
    Query params:
      username  — defaults to session AL username or ANILIST_USERNAME
      status    — optional AL status filter (CURRENT, COMPLETED, PAUSED, DROPPED, PLANNING)
    """
    if not DB_AVAILABLE or not DB_URL:
        return jsonify({"error": "Database not configured"}), 503
    username      = request.args.get("username", "").strip()
    username      = username or session.get("al_username", ANILIST_USERNAME)
    status_filter = request.args.get("status", "").strip().upper() or None
    entries = db_load_anime_list(username, status_filter=status_filter)
    return jsonify({"username": username, "count": len(entries), "entries": entries})

@app.route("/api/db/stats")
def db_stats():
    """Return per-status counts for a user's stored anime list."""
    if not DB_AVAILABLE or not DB_URL:
        return jsonify({"error": "Database not configured"}), 503
    username = request.args.get("username", "").strip()
    username = username or session.get("al_username", ANILIST_USERNAME)
    all_entries = db_load_anime_list(username)
    stats = {}
    for e in all_entries:
        s = e.get("status", "UNKNOWN")
        stats[s] = stats.get(s, 0) + 1
    return jsonify({"username": username, "total": len(all_entries), "by_status": stats})

# ── Anime search / add / edit / delete ───────────────────────────────────────
@app.route("/api/anime/search")
def anime_search():
    """Search anime by title via AniList.
    Query params: q (search string), page (default 1)
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "Missing search query"}), 400
    page = int(request.args.get("page", 1))
    data = anilist_query(GQL_SEARCH, {"search": q, "page": page}, token=al_headers())
    media = (data.get("data") or {}).get("Page", {}).get("media", [])
    results = []
    for m in media:
        t = m["title"]
        entry = m.get("mediaListEntry") or {}
        sa = entry.get("startedAt") or {}
        ca = entry.get("completedAt") or {}
        results.append({
            "alId":          m["id"],
            "malId":         m.get("idMal"),
            "title":         t.get("english") or t.get("romaji") or "Unknown",
            "episodes":      m.get("episodes"),
            "status":        m.get("status"),
            "cover":         (m.get("coverImage") or {}).get("medium"),
            "listStatus":    entry.get("status"),
            "listProgress":  entry.get("progress", 0),
            "listScore":     entry.get("score", 0),
            "startedAt":     sa if any(sa.values()) else None,
            "completedAt":   ca if any(ca.values()) else None,
        })
    return jsonify({"results": results})

@app.route("/api/anime/add", methods=["POST"])
def anime_add():
    """Add an anime to both AniList and MAL lists.
    Body: { alId, malId, status (AL key), progress, score }
    """
    if "al_token" not in session:
        return jsonify({"error": "AniList not connected"}), 401
    body     = request.get_json(silent=True) or {}
    al_id    = body.get("alId")
    mal_id   = body.get("malId")
    status   = body.get("status", "PLANNING")
    progress = int(body.get("progress", 0))
    score    = float(body.get("score", 0))
    started_at   = body.get("startedAt")
    completed_at = body.get("completedAt")
    if not al_id:
        return jsonify({"error": "alId is required"}), 400
    al_ok  = al_update(al_id, status=status, progress=progress, score=score,
                       token=al_headers(), started_at=started_at, completed_at=completed_at)
    mal_ok = False
    if mal_id and "mal_token" in session:
        mal_status = AL_TO_MAL.get(status, "plan_to_watch")
        mal_ok = mal_update(mal_id, status=mal_status, progress=progress, score=score)
    if al_ok:
        username = session.get("al_username", ANILIST_USERNAME)
        db_save_anime_list(username, [{
            "alId": al_id, "malId": mal_id, "title": body.get("title", "Unknown"),
            "status": status, "progress": progress, "score": score,
            "episodes": body.get("episodes"), "mediaStatus": body.get("mediaStatus"),
            "startedAt": started_at, "completedAt": completed_at,
        }])
    return jsonify({"al_ok": al_ok, "mal_ok": mal_ok})

@app.route("/api/anime/edit", methods=["POST"])
def anime_edit():
    """Edit an existing list entry on both AniList and MAL.
    Body: { alId, malId, status (AL key), progress, score, startedAt, completedAt }
    """
    if "al_token" not in session:
        return jsonify({"error": "AniList not connected"}), 401
    body     = request.get_json(silent=True) or {}
    al_id    = body.get("alId")
    mal_id   = body.get("malId")
    status   = body.get("status", "PLANNING")
    progress = int(body.get("progress", 0))
    score    = float(body.get("score", 0))
    started_at   = body.get("startedAt")
    completed_at = body.get("completedAt")
    if not al_id:
        return jsonify({"error": "alId is required"}), 400
    al_ok  = al_update(al_id, status=status, progress=progress, score=score,
                       token=al_headers(), started_at=started_at, completed_at=completed_at)
    mal_ok = False
    if mal_id and "mal_token" in session:
        mal_status = AL_TO_MAL.get(status, "plan_to_watch")
        mal_ok = mal_update(mal_id, status=mal_status, progress=progress, score=score)
    if al_ok:
        username = session.get("al_username", ANILIST_USERNAME)
        db_save_anime_list(username, [{
            "alId": al_id, "malId": mal_id, "title": body.get("title", "Unknown"),
            "status": status, "progress": progress, "score": score,
            "episodes": body.get("episodes"), "mediaStatus": body.get("mediaStatus"),
            "startedAt": started_at, "completedAt": completed_at,
        }])
    return jsonify({"al_ok": al_ok, "mal_ok": mal_ok})

@app.route("/api/anime/delete", methods=["POST"])
def anime_delete():
    """Delete an anime from both AniList and MAL.
    Body: { alId, malId }
    """
    if "al_token" not in session:
        return jsonify({"error": "AniList not connected"}), 401
    body   = request.get_json(silent=True) or {}
    al_id  = body.get("alId")
    mal_id = body.get("malId")
    if not al_id:
        return jsonify({"error": "alId is required"}), 400
    al_ok  = al_delete(al_id, token=al_headers())
    mal_ok = False
    if mal_id and "mal_token" in session:
        headers = mal_headers()
        if headers:
            r = requests.delete(f"{MAL_API}/anime/{mal_id}/my_list_status",
                                headers=headers, timeout=10)
            mal_ok = r.status_code == 200
    return jsonify({"al_ok": al_ok, "mal_ok": mal_ok})

# ── Vercel entry point ────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000)

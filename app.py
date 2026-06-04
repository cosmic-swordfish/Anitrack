import os, time, secrets, requests, hmac, hashlib, base64, json
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

    def _get_db():
        return psycopg2.connect(DB_URL, sslmode="require")

    def _ensure_table():
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
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[DB] Table init error: {e}")

    _ensure_table()

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

    DB_AVAILABLE = True

except ImportError:
    DB_AVAILABLE = False
    def db_save_tokens(u, a, m): pass
    def db_load_all_tokens(): return []
    def db_delete_tokens(u): pass

app = Flask(__name__)

# ── Secret key ────────────────────────────────────────────────────────────────
_secret = os.environ.get("SECRET_KEY", "")
if not _secret:
    _secret = secrets.token_hex(32)
    print("=" * 70)
    print("WARNING: SECRET_KEY env var is not set.")
    print(f"Add to your .env:  SECRET_KEY={_secret}")
    print("=" * 70)

app.secret_key = _secret
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
IS_HTTPS = os.environ.get("HTTPS", "false").lower() == "true"
app.config["SESSION_COOKIE_SECURE"] = IS_HTTPS

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

# ── AniList helpers ───────────────────────────────────────────────────────────
def anilist_query(query, variables=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token.get('access_token', '')}"
    r = requests.post(ANILIST_API, json={"query": query, "variables": variables or {}},
                      headers=headers, timeout=15)
    return r.json()

GQL_LIST = """
query($username: String, $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    mediaList(userName: $username, type: ANIME) {
      mediaId status score(format: POINT_10) progress updatedAt
      media {
        id idMal title { romaji english } status episodes
        nextAiringEpisode { airingAt episode timeUntilAiring }
      }
    }
  }
}"""

GQL_LOOKUP_BY_MAL = """
query($malId: Int) {
  Media(idMal: $malId, type: ANIME) { id idMal title { romaji } }
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
    }

def get_anilist_list(token=None, username=None):
    username = username or ANILIST_USERNAME
    entries, page = [], 1
    while True:
        data = anilist_query(GQL_LIST, {"username": username, "page": page}, token=token)
        page_data = data.get("data", {}).get("Page", {})
        entries += [_parse_entry(e) for e in page_data.get("mediaList", [])]
        if not page_data.get("pageInfo", {}).get("hasNextPage"):
            break
        page += 1
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

def al_update(media_id, status=None, progress=None, score=None):
    token = al_headers()
    if not token: return False
    vars_ = {"mediaId": media_id}
    sets  = []
    if status:   vars_["status"]   = status;   sets.append("$status: MediaListStatus")
    if progress is not None: vars_["progress"] = progress; sets.append("$progress: Int")
    if score is not None:    vars_["score"]    = score;    sets.append("$score: Float")
    if not sets: return False
    gql = f"mutation($mediaId:Int,{','.join(sets)}){{SaveMediaListEntry(mediaId:$mediaId{''.join([f',{s.split()[0][1:]}:{s.split()[0]}' for s in sets])})" + "{id}}"
    r = anilist_query(gql, vars_, token=token)
    return "errors" not in r

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
    if not headers: return []
    items, offset = [], 0
    while True:
        r = requests.get(f"{MAL_API}/users/@me/animelist",
                         headers=headers,
                         params={"fields": "list_status", "limit": 1000, "offset": offset},
                         timeout=15)
        if r.status_code != 200: break
        data = r.json()
        for item in data.get("data", []):
            items.append({
                "malId":    item["node"]["id"],
                "title":    item["node"]["title"],
                "status":   item["list_status"].get("status", ""),
                "progress": item["list_status"].get("num_episodes_watched", 0),
                "score":    item["list_status"].get("score", 0),
            })
        if not data.get("paging", {}).get("next"): break
        offset += 1000
    return items

def mal_update(mal_id, status=None, score=None, progress=None):
    headers = mal_headers()
    if not headers: return False
    payload = {}
    if status:   payload["status"] = status
    if score is not None:    payload["score"] = score
    if progress is not None: payload["num_watched_episodes"] = progress
    if not payload: return False
    r = requests.patch(f"{MAL_API}/anime/{mal_id}/my_list_status",
                       headers=headers, data=payload, timeout=10)
    return r.status_code == 200

# ── Sync logic ────────────────────────────────────────────────────────────────
STATUS_RANK = ["PLANNING","CURRENT","PAUSED","DROPPED","COMPLETED"]

def resolve_fields(al, mal):
    win_progress = max(al["progress"], mal["progress"])
    win_score    = al["score"] if al["score"] >= mal["score"] else mal["score"]
    al_rank  = STATUS_RANK.index(al["status"])  if al["status"]  in STATUS_RANK else 0
    mal_al   = MAL_TO_AL.get(mal["status"], "PLANNING")
    mal_rank = STATUS_RANK.index(mal_al) if mal_al in STATUS_RANK else 0
    win_status = al["status"] if al_rank >= mal_rank else mal_al
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
            mal_al_status = MAL_TO_AL.get(mal["status"], "PLANNING")
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
    for mal in mal_list:
        if mal["malId"] not in {a["malId"] for a in al_list}:
            diffs.append({
                "malId": mal["malId"], "alId": None, "title": mal["title"],
                "action": "only_on_mal", "label": "Only on MAL", "changes": [],
                "al_status": None, "mal_status": mal["status"],
                "al_progress": 0, "mal_progress": mal["progress"],
                "al_score": 0, "mal_score": mal["score"],
                "needs_al_update": False,
            })
    return diffs

def resolve_al_id_from_mal(mal_id):
    data = anilist_query(GQL_LOOKUP_BY_MAL, {"malId": mal_id})
    return (data.get("data") or {}).get("Media", {}).get("id")

def apply_diff_item(diff):
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
            al_ok = al_update(diff["alId"], status=wst, progress=wp, score=ws)
    elif diff["action"] == "only_on_mal":
        al_id = diff.get("alId") or resolve_al_id_from_mal(diff["malId"])
        if al_id:
            al_status = MAL_TO_AL.get(diff["mal_status"], "PLANNING")
            al_ok = al_update(al_id, status=al_status,
                              progress=diff.get("mal_progress", 0),
                              score=diff.get("mal_score", 0))
    return {"mal_ok": mal_ok, "al_ok": al_ok}

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/service-worker.js")
def service_worker():
    from flask import send_from_directory
    return send_from_directory("static", "service-worker.js",
                               mimetype="application/javascript")

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
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_hex(16)
    resp = make_response(redirect(MAL_AUTH_URL + "?" + urlencode({
        "response_type": "code", "client_id": MAL_CLIENT_ID,
        "redirect_uri": MAL_REDIRECT_URI, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
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
    return redirect("/")

# ── Sync endpoints ────────────────────────────────────────────────────────────
@app.route("/api/sync/diff")
def sync_diff():
    if "mal_token" not in session:
        return jsonify({"error": "MAL not connected"}), 401
    username = request.args.get("username", ANILIST_USERNAME).strip() or ANILIST_USERNAME
    try:
        al_list  = get_anilist_list(token=al_headers(), username=username)
        mal_list = get_mal_list()
        diffs    = compute_diff(al_list, mal_list)
        return jsonify({"diffs": diffs, "al_count": len(al_list), "mal_count": len(mal_list)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sync/apply", methods=["POST"])
def sync_apply():
    if "mal_token" not in session:
        return jsonify({"error": "MAL not connected"}), 401
    items = request.get_json().get("items", [])
    results = []
    for item in items:
        if item.get("action") not in ("add_to_mal", "update", "only_on_mal"):
            continue
        r = apply_diff_item(item)
        results.append({"malId": item["malId"], "title": item.get("title"), **r})
    return jsonify({"results": results})

@app.route("/api/sync/auto", methods=["POST"])
def sync_auto():
    if "mal_token" not in session:
        return jsonify({"error": "MAL not connected"}), 401
    username = request.args.get("username", ANILIST_USERNAME).strip() or ANILIST_USERNAME
    try:
        al_list  = get_anilist_list(token=al_headers(), username=username)
        mal_list = get_mal_list()
        diffs    = compute_diff(al_list, mal_list)
        results  = []
        for diff in diffs:
            if diff["action"] in ("add_to_mal", "update", "only_on_mal") and diff.get("malId"):
                r = apply_diff_item(diff)
                results.append({"title": diff["title"], **r})
                time.sleep(0.3)
        return jsonify({"synced": len(results), "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Cron auto-sync (called by Vercel Cron every 30 min) ───────────────────────
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
            al_list = get_anilist_list(token=al_token, username=username)
            # Fetch MAL list using stored token directly
            headers = {"Authorization": f"Bearer {mal_token.get('access_token', '')}"}
            mal_items, offset = [], 0
            while True:
                r = requests.get(f"{MAL_API}/users/@me/animelist", headers=headers,
                                 params={"fields": "list_status", "limit": 1000, "offset": offset},
                                 timeout=15)
                if r.status_code != 200: break
                data = r.json()
                for item in data.get("data", []):
                    mal_items.append({
                        "malId":    item["node"]["id"],
                        "title":    item["node"]["title"],
                        "status":   item["list_status"].get("status", ""),
                        "progress": item["list_status"].get("num_episodes_watched", 0),
                        "score":    item["list_status"].get("score", 0),
                    })
                if not data.get("paging", {}).get("next"): break
                offset += 1000

            diffs = compute_diff(al_list, mal_items)
            synced = 0
            for diff in diffs:
                if diff["action"] in ("add_to_mal", "update") and diff.get("malId"):
                    wp = diff.get("win_progress", diff.get("al_progress", 0))
                    ws = diff.get("win_score",    diff.get("al_score", 0))
                    wst = diff.get("win_status",  diff.get("al_status", "CURRENT"))
                    mal_status = AL_TO_MAL.get(wst)
                    if mal_status:
                        payload = {"status": mal_status}
                        if wp: payload["num_watched_episodes"] = wp
                        if ws: payload["score"] = ws
                        requests.patch(f"{MAL_API}/anime/{diff['malId']}/my_list_status",
                                       headers=headers, data=payload, timeout=10)
                        synced += 1
                        time.sleep(0.3)
            total_synced += synced
            results.append({"username": username, "synced": synced})
            print(f"[Cron] {username}: {synced} items synced")
        except Exception as e:
            print(f"[Cron] Error for {username}: {e}")
            results.append({"username": username, "error": str(e)})

    return jsonify({"synced_users": len(rows), "total_synced": total_synced, "results": results})

# ── Vercel entry point ────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000)

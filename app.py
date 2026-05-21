import os, time, secrets, requests
from urllib.parse import urlencode, quote
from flask import Flask, redirect, request, session, jsonify, render_template

# Load .env only in local development (python-dotenv is optional on Vercel)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

# ── Secret key ────────────────────────────────────────────────────────────────
# MUST be a stable env var. A new random key on every restart invalidates all
# sessions — which is exactly what caused the PKCE verifier to disappear mid-flow.
_secret = os.environ.get("SECRET_KEY", "")
if not _secret:
    _secret = secrets.token_hex(32)
    print("=" * 70)
    print("WARNING: SECRET_KEY env var is not set.")
    print("Flask sessions will be lost on every restart, breaking MAL OAuth.")
    print(f"Add to your .env:  SECRET_KEY={_secret}")
    print("=" * 70)

app.secret_key = _secret
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("HTTPS", "false").lower() == "true"

# ── Config ────────────────────────────────────────────────────────────────────
ANILIST_USERNAME  = os.environ.get("ANILIST_USERNAME", "cosmicswordfish")
MAL_CLIENT_ID     = os.environ.get("MAL_CLIENT_ID", "")
MAL_CLIENT_SECRET = os.environ.get("MAL_CLIENT_SECRET", "")
MAL_REDIRECT_URI  = os.environ.get("MAL_REDIRECT_URI", "http://localhost:5000/mal/callback")

ANILIST_API   = "https://graphql.anilist.co"
MAL_AUTH_URL  = "https://myanimelist.net/v1/oauth2/authorize"
MAL_TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
MAL_API       = "https://api.myanimelist.net/v2"

AL_TO_MAL = {
    "CURRENT":   "watching",
    "COMPLETED": "completed",
    "PAUSED":    "on_hold",
    "DROPPED":   "dropped",
    "PLANNING":  "plan_to_watch",
}
MAL_TO_AL = {v: k for k, v in AL_TO_MAL.items()}

# ── AniList helpers ───────────────────────────────────────────────────────────
def anilist_query(query, variables=None):
    r = requests.post(ANILIST_API,
        json={"query": query, "variables": variables or {}},
        headers={"Content-Type": "application/json"}, timeout=15)
    r.raise_for_status()
    return r.json()

GQL_USER_LIST = """
query($userName: String) {
  MediaListCollection(userName: $userName, type: ANIME) {
    lists {
      entries {
        mediaId status score progress
        media {
          id idMal title { romaji english } status episodes
          coverImage { large color }
          nextAiringEpisode { airingAt episode timeUntilAiring }
          airingSchedule(notYetAired: true) { nodes { episode airingAt } }
          startDate { year month day }
          siteUrl genres
        }
      }
    }
  }
}"""

def get_anilist_list():
    data = anilist_query(GQL_USER_LIST, {"userName": ANILIST_USERNAME})
    entries = []
    for lst in data["data"]["MediaListCollection"]["lists"]:
        for e in lst["entries"]:
            entries.append({
                "malId":    e["media"]["idMal"],
                "alId":     e["mediaId"],
                "title":    e["media"]["title"]["english"] or e["media"]["title"]["romaji"],
                "status":   e["status"],
                "score":    e["score"],
                "progress": e["progress"],
                "media":    e["media"],
            })
    return entries

# ── MAL helpers ───────────────────────────────────────────────────────────────
def mal_headers():
    token = session.get("mal_token")
    if not token:
        return None
    if time.time() > token.get("expires_at", 0) - 60:
        token = refresh_mal_token(token)
        if not token:
            return None
    return {"Authorization": f"Bearer {token['access_token']}"}

def refresh_mal_token(token):
    payload = {
        "grant_type":    "refresh_token",
        "refresh_token": token["refresh_token"],
        "client_id":     MAL_CLIENT_ID,
    }
    if MAL_CLIENT_SECRET:
        payload["client_secret"] = MAL_CLIENT_SECRET
    try:
        r = requests.post(MAL_TOKEN_URL, data=payload, timeout=15)
        new_token = r.json()
    except Exception:
        return None
    if "access_token" not in new_token:
        session.pop("mal_token", None)
        return None
    new_token["expires_at"] = time.time() + new_token.get("expires_in", 3600)
    session["mal_token"] = new_token
    session.modified = True
    return new_token

def get_mal_list():
    headers = mal_headers()
    if not headers:
        return []
    entries, offset = [], 0
    while True:
        r = requests.get(f"{MAL_API}/users/@me/animelist",
            headers=headers,
            params={"fields": "list_status,num_episodes", "limit": 1000, "offset": offset},
            timeout=15)
        if not r.ok:
            break
        data = r.json()
        for item in data.get("data", []):
            st = item["list_status"]
            entries.append({
                "malId":    item["node"]["id"],
                "title":    item["node"]["title"],
                "status":   st.get("status"),
                "score":    st.get("score", 0),
                "progress": st.get("num_episodes_watched", 0),
            })
        if not data.get("paging", {}).get("next"):
            break
        offset += 1000
    return entries

def mal_update(mal_id, status=None, score=None, progress=None):
    headers = mal_headers()
    if not headers:
        return False
    payload = {}
    if status:                          payload["status"] = status
    if score is not None and score > 0: payload["score"] = int(score)
    if progress is not None:            payload["num_watched_episodes"] = progress
    r = requests.patch(f"{MAL_API}/anime/{mal_id}/my_list_status",
        headers=headers, data=payload, timeout=15)
    return r.ok

# ── Sync logic ────────────────────────────────────────────────────────────────
def compute_diff(al_list, mal_list):
    mal_map = {e["malId"]: e for e in mal_list}
    diffs = []
    for al in al_list:
        mal_id = al["malId"]
        if not mal_id:
            continue
        mal = mal_map.get(mal_id)
        al_mal_status = AL_TO_MAL.get(al["status"])
        if not mal:
            diffs.append({
                "malId": mal_id, "alId": al["alId"], "title": al["title"],
                "action": "add_to_mal", "label": "Missing on MAL",
                "al_status": al["status"], "mal_status": None,
                "al_progress": al["progress"], "al_score": al["score"],
            })
        else:
            changes = []
            if al_mal_status and al_mal_status != mal["status"]:
                changes.append(f"status: AniList={al['status']} MAL={mal['status']}")
            if al["progress"] != mal["progress"]:
                changes.append(f"progress: AniList={al['progress']} MAL={mal['progress']}")
            if al["score"] != mal["score"] and (al["score"] > 0 or mal["score"] > 0):
                changes.append(f"score: AniList={al['score']} MAL={mal['score']}")
            if changes:
                diffs.append({
                    "malId": mal_id, "alId": al["alId"], "title": al["title"],
                    "action": "update", "label": "Out of sync", "changes": changes,
                    "al_status": al["status"], "mal_status": mal["status"],
                    "al_progress": al["progress"], "mal_progress": mal["progress"],
                    "al_score": al["score"], "mal_score": mal["score"],
                })
    al_mal_ids = {e["malId"] for e in al_list}
    for mal in mal_list:
        if mal["malId"] not in al_mal_ids:
            diffs.append({
                "malId": mal["malId"], "alId": None, "title": mal["title"],
                "action": "only_on_mal", "label": "Only on MAL",
                "mal_status": mal["status"], "al_status": None,
            })
    return diffs

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    mal_connected = ("mal_token" in session and
                     "access_token" in session.get("mal_token", {}))
    return render_template("index.html",
        anilist_username=ANILIST_USERNAME,
        mal_connected=mal_connected,
        mal_error=request.args.get("mal_error", ""),
        mal_success=request.args.get("mal_success", ""))

@app.route("/api/anilist", methods=["POST"])
def anilist_proxy():
    body = request.get_json()
    try:
        data = anilist_query(body.get("query"), body.get("variables", {}))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── MAL OAuth (PKCE) ──────────────────────────────────────────────────────────
@app.route("/mal/login")
def mal_login():
    if not MAL_CLIENT_ID:
        return redirect("/?mal_error=" + quote("MAL_CLIENT_ID is not configured in your environment"))

    verifier = secrets.token_urlsafe(96)
    state    = secrets.token_hex(16)

    session["pkce_verifier"] = verifier
    session["oauth_state"]   = state
    session.modified = True

    return redirect(MAL_AUTH_URL + "?" + urlencode({
        "response_type":         "code",
        "client_id":             MAL_CLIENT_ID,
        "redirect_uri":          MAL_REDIRECT_URI,
        "code_challenge":        verifier,
        "code_challenge_method": "plain",
        "state":                 state,
    }))

@app.route("/mal/callback")
def mal_callback():
    oauth_error = request.args.get("error")
    if oauth_error:
        desc = request.args.get("error_description", oauth_error)
        return redirect("/?mal_error=" + quote(str(desc)))

    code  = request.args.get("code", "")
    state = request.args.get("state", "")

    if not code:
        return redirect("/?mal_error=" + quote("No authorization code received from MAL"))

    if not state or state != session.get("oauth_state", ""):
        return redirect("/?mal_error=" + quote(
            "State mismatch — possible CSRF or session expired. Please try again."))

    verifier = session.pop("pkce_verifier", "")
    session.pop("oauth_state", None)

    if not verifier:
        return redirect("/?mal_error=" + quote(
            "Session expired before OAuth completed. "
            "Make sure SECRET_KEY is set as a stable environment variable and try again."))

    payload = {
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  MAL_REDIRECT_URI,
        "client_id":     MAL_CLIENT_ID,
        "code_verifier": verifier,
    }
    if MAL_CLIENT_SECRET:
        payload["client_secret"] = MAL_CLIENT_SECRET

    try:
        r = requests.post(MAL_TOKEN_URL, data=payload, timeout=15)
        token = r.json()
    except Exception as e:
        return redirect("/?mal_error=" + quote(f"Token request failed: {e}"))

    if "error" in token:
        msg = token.get("hint") or token.get("message") or token["error"]
        return redirect("/?mal_error=" + quote(str(msg)))

    if "access_token" not in token:
        return redirect("/?mal_error=" + quote(
            f"Unexpected response from MAL (HTTP {r.status_code}). "
            "Check that your redirect URI matches exactly what is registered in your MAL app."))

    token["expires_at"] = time.time() + token.get("expires_in", 3600)
    session["mal_token"] = token
    session.modified = True
    return redirect("/?mal_success=1")

@app.route("/mal/logout")
def mal_logout():
    session.pop("mal_token", None)
    return redirect("/")

# ── Sync endpoints ────────────────────────────────────────────────────────────
@app.route("/api/sync/diff")
def sync_diff():
    if "mal_token" not in session:
        return jsonify({"error": "MAL not connected"}), 401
    try:
        al_list  = get_anilist_list()
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
        mal_id = item.get("malId")
        if not mal_id or item.get("action") not in ("add_to_mal", "update"):
            continue
        ok = mal_update(mal_id,
            status=AL_TO_MAL.get(item.get("al_status")),
            score=item.get("al_score"),
            progress=item.get("al_progress"))
        results.append({"malId": mal_id, "title": item.get("title"), "ok": ok})
    return jsonify({"results": results})

@app.route("/api/sync/auto", methods=["POST"])
def sync_auto():
    if "mal_token" not in session:
        return jsonify({"error": "MAL not connected"}), 401
    try:
        al_list  = get_anilist_list()
        mal_list = get_mal_list()
        diffs    = compute_diff(al_list, mal_list)
        results  = []
        for diff in diffs:
            if diff["action"] in ("add_to_mal", "update") and diff.get("malId"):
                ok = mal_update(diff["malId"],
                    status=AL_TO_MAL.get(diff.get("al_status")),
                    score=diff.get("al_score"),
                    progress=diff.get("al_progress"))
                results.append({"title": diff["title"], "ok": ok})
                time.sleep(0.3)
        return jsonify({"synced": len(results), "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Vercel entry point ────────────────────────────────────────────────────────
# Vercel imports this module and looks for the `app` variable.
# The block below only runs when executing locally with `python app.py`.
if __name__ == "__main__":
    app.run(debug=True, port=5000)

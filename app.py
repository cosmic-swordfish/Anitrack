import os, time, secrets, requests, hmac, hashlib, base64, json
from urllib.parse import urlencode, quote
from flask import Flask, redirect, request, session, jsonify, render_template, make_response

# Load .env only in local development
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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
    response.set_cookie(name, f"{payload}.{sig}",
        max_age=max_age, httponly=True, secure=IS_HTTPS, samesite="Lax", path="/")

def get_oauth_cookie(name: str):
    val = request.cookies.get(name, "")
    if not val or "." not in val:
        return None
    payload, sig = val.rsplit(".", 1)
    if not hmac.compare_digest(_sign(payload), sig):
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except Exception:
        return None

# ── AniList helpers ───────────────────────────────────────────────────────────
def anilist_query(query, variables=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.post(ANILIST_API,
        json={"query": query, "variables": variables or {}},
        headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()

GQL_USER_LIST = """
query($userName: String) {
  MediaListCollection(userName: $userName, type: ANIME) {
    lists {
      entries {
        id mediaId status score progress
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

GQL_LOOKUP_BY_MAL = """
query($malId: Int) {
  Media(idMal: $malId, type: ANIME) {
    id idMal title { romaji english }
  }
}"""

GQL_UPDATE_ENTRY = """
mutation($mediaId: Int, $status: MediaListStatus, $progress: Int, $score: Float) {
  SaveMediaListEntry(mediaId: $mediaId, status: $status, progress: $progress, score: $score) {
    id status progress score
  }
}"""

def get_anilist_list(token=None):
    data = anilist_query(GQL_USER_LIST, {"userName": ANILIST_USERNAME}, token=token)
    entries = []
    for lst in data["data"]["MediaListCollection"]["lists"]:
        for e in lst["entries"]:
            entries.append({
                "entryId":  e["id"],
                "malId":    e["media"]["idMal"],
                "alId":     e["mediaId"],
                "title":    e["media"]["title"]["english"] or e["media"]["title"]["romaji"],
                "status":   e["status"],
                "score":    e["score"],
                "progress": e["progress"],
                "media":    e["media"],
            })
    return entries

def al_headers():
    token = session.get("al_token")
    if not token:
        return None
    if time.time() > token.get("expires_at", 0) - 60:
        token = refresh_al_token(token)
        if not token:
            return None
    return token["access_token"]

def refresh_al_token(token):
    try:
        r = requests.post(AL_TOKEN_URL, json={
            "grant_type":    "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id":     AL_CLIENT_ID,
            "client_secret": AL_CLIENT_SECRET,
        }, timeout=15)
        new_token = r.json()
    except Exception:
        return None
    if "access_token" not in new_token:
        session.pop("al_token", None)
        return None
    new_token["expires_at"] = time.time() + new_token.get("expires_in", 3600)
    session["al_token"] = new_token
    session.modified = True
    return new_token

def al_update(media_id, status=None, progress=None, score=None):
    access_token = al_headers()
    if not access_token:
        return False
    variables = {"mediaId": media_id}
    if status:                                 variables["status"]   = status
    if progress is not None and progress >= 0: variables["progress"] = progress
    if score is not None and score > 0:        variables["score"]    = score
    try:
        data = anilist_query(GQL_UPDATE_ENTRY, variables, token=access_token)
        return "errors" not in data
    except Exception:
        return False

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
    if status:                                 payload["status"] = status
    if score is not None and score > 0:        payload["score"]  = int(score)
    if progress is not None and progress >= 0: payload["num_watched_episodes"] = int(progress)
    if not payload:
        return True
    r = requests.patch(f"{MAL_API}/anime/{mal_id}/my_list_status",
        headers=headers, data=payload, timeout=15)
    return r.ok

# ── Sync logic ────────────────────────────────────────────────────────────────
STATUS_RANK = ["PLANNING", "CURRENT", "PAUSED", "DROPPED", "COMPLETED"]

def resolve_fields(al, mal):
    """Higher wins for progress and score. More progressed status wins."""
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
                "win_progress": al["progress"], "win_score": al["score"],
                "win_status": al["status"],
                "needs_al_update": False,
            })
        else:
            win_progress, win_score, win_status = resolve_fields(al, mal)
            changes = []
            mal_al_status = MAL_TO_AL.get(mal["status"], "PLANNING")
            if al_mal_status and al_mal_status != mal["status"]:
                changes.append(f"status: AL={al['status']} MAL={mal['status']} → {win_status}")
            if al["progress"] != mal["progress"]:
                changes.append(f"progress: AL={al['progress']} MAL={mal['progress']} → {win_progress}")
            if al["score"] != mal["score"] and (al["score"] > 0 or mal["score"] > 0):
                changes.append(f"score: AL={al['score']} MAL={mal['score']} → {win_score}")
            if changes:
                # Does AniList itself need updating?
                needs_al = (
                    win_progress != al["progress"] or
                    win_score    != al["score"]    or
                    win_status   != al["status"]
                )
                diffs.append({
                    "malId": mal_id, "alId": al["alId"], "title": al["title"],
                    "action": "update", "label": "Out of sync", "changes": changes,
                    "al_status": al["status"], "mal_status": mal["status"],
                    "al_progress": al["progress"], "mal_progress": mal["progress"],
                    "al_score": al["score"], "mal_score": mal["score"],
                    "win_progress": win_progress, "win_score": win_score,
                    "win_status": win_status,
                    "needs_al_update": needs_al,
                })
    al_mal_ids = {e["malId"] for e in al_list}
    for mal in mal_list:
        if mal["malId"] not in al_mal_ids:
            mal_al_status = MAL_TO_AL.get(mal["status"], "PLANNING")
            diffs.append({
                "malId": mal["malId"], "alId": None, "title": mal["title"],
                "action": "only_on_mal", "label": "Only on MAL",
                "mal_status": mal["status"], "al_status": None,
                "win_status": mal_al_status,
                "win_progress": mal.get("progress", 0),
                "win_score": mal.get("score", 0),
                "needs_al_update": False,
            })
    return diffs

def resolve_al_id_from_mal(mal_id):
    """Look up AniList mediaId using a MAL ID. Returns int or None."""
    try:
        data = anilist_query(GQL_LOOKUP_BY_MAL, {"malId": mal_id}, token=al_headers())
        return data["data"]["Media"]["id"]
    except Exception:
        return None

def apply_diff_item(diff):
    """Push winning values to MAL and (if connected) AniList. Returns {mal_ok, al_ok}."""
    action = diff.get("action")

    # ── Only on MAL → add to AniList ─────────────────────────────────────────
    if action == "only_on_mal":
        if "al_token" not in session:
            return {"mal_ok": False, "al_ok": False}
        al_id = resolve_al_id_from_mal(diff["malId"])
        if not al_id:
            return {"mal_ok": False, "al_ok": False}
        al_ok = al_update(
            al_id,
            status   = diff.get("win_status"),
            progress = diff.get("win_progress"),
            score    = diff.get("win_score"),
        )
        return {"mal_ok": True, "al_ok": al_ok}  # MAL already has it; nothing to write

    if action not in ("add_to_mal", "update"):
        return {"mal_ok": False, "al_ok": False}

    # ── add_to_mal / update → push to MAL (and AL if needed) ─────────────────
    mal_ok = mal_update(
        diff["malId"],
        status   = AL_TO_MAL.get(diff.get("win_status") or diff.get("al_status")),
        score    = diff.get("win_score"),
        progress = diff.get("win_progress"),
    )

    al_ok = True  # default: not needed
    if diff.get("needs_al_update") and diff.get("alId") and "al_token" in session:
        al_ok = al_update(
            diff["alId"],
            status   = diff.get("win_status"),
            progress = diff.get("win_progress"),
            score    = diff.get("win_score"),
        )

    return {"mal_ok": mal_ok, "al_ok": al_ok}

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    mal_connected = ("mal_token" in session and "access_token" in session.get("mal_token", {}))
    al_connected  = ("al_token"  in session and "access_token" in session.get("al_token",  {}))
    return render_template("index.html",
        anilist_username = ANILIST_USERNAME,
        mal_connected    = mal_connected,
        al_connected     = al_connected,
        al_client_set    = bool(AL_CLIENT_ID),
        mal_error        = request.args.get("mal_error", ""),
        mal_success      = request.args.get("mal_success", ""),
        al_error         = request.args.get("al_error", ""),
        al_success       = request.args.get("al_success", ""),
    )

@app.route("/api/anilist", methods=["POST"])
def anilist_proxy():
    body = request.get_json()
    try:
        token = al_headers()
        data = anilist_query(body.get("query"), body.get("variables", {}), token=token)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── MAL OAuth (PKCE) ──────────────────────────────────────────────────────────
@app.route("/mal/login")
def mal_login():
    if not MAL_CLIENT_ID:
        return redirect("/?mal_error=" + quote("MAL_CLIENT_ID is not configured"))
    verifier = secrets.token_urlsafe(96)
    state    = secrets.token_hex(16)
    resp = make_response(redirect(MAL_AUTH_URL + "?" + urlencode({
        "response_type": "code", "client_id": MAL_CLIENT_ID,
        "redirect_uri": MAL_REDIRECT_URI,
        "code_challenge": verifier, "code_challenge_method": "plain",
        "state": state,
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
    resp = make_response(redirect("/?mal_success=1"))
    resp.delete_cookie("mal_pkce", path="/")
    return resp

@app.route("/mal/logout")
def mal_logout():
    session.pop("mal_token", None)
    return redirect("/")

# ── AniList OAuth ─────────────────────────────────────────────────────────────
@app.route("/al/login")
def al_login():
    if not AL_CLIENT_ID:
        return redirect("/?al_error=" + quote("AL_CLIENT_ID is not configured. Add it to your environment variables."))
    state = secrets.token_hex(16)
    resp = make_response(redirect(AL_AUTH_URL + "?" + urlencode({
        "client_id":     AL_CLIENT_ID,
        "redirect_uri":  AL_REDIRECT_URI,
        "response_type": "code",
        "state":         state,
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
            "grant_type":    "authorization_code",
            "client_id":     AL_CLIENT_ID,
            "client_secret": AL_CLIENT_SECRET,
            "redirect_uri":  AL_REDIRECT_URI,
            "code":          code,
        }, timeout=15)
        token = r.json()
    except Exception as e:
        return redirect("/?al_error=" + quote(f"Token request failed: {e}"))
    if "access_token" not in token:
        return redirect("/?al_error=" + quote(f"Unexpected AniList response (HTTP {r.status_code})"))
    token["expires_at"] = time.time() + token.get("expires_in", 3600)
    session["al_token"] = token
    session.modified = True
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
    try:
        al_list  = get_anilist_list(token=al_headers())
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
        if item.get("action") not in ("add_to_mal", "update", "only_on_mal") or (item.get("action") != "only_on_mal" and not item.get("malId")):
            continue
        r = apply_diff_item(item)
        results.append({"malId": item["malId"], "title": item.get("title"), **r})
    return jsonify({"results": results})

@app.route("/api/sync/auto", methods=["POST"])
def sync_auto():
    if "mal_token" not in session:
        return jsonify({"error": "MAL not connected"}), 401
    try:
        al_list  = get_anilist_list(token=al_headers())
        mal_list = get_mal_list()
        diffs    = compute_diff(al_list, mal_list)
        results  = []
        for diff in diffs:
            if diff["action"] in ("add_to_mal", "update", "only_on_mal") and (diff.get("malId") or diff["action"] == "only_on_mal"):
                r = apply_diff_item(diff)
                results.append({"title": diff["title"], **r})
                time.sleep(0.3)
        return jsonify({"synced": len(results), "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Vercel entry point ────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000)

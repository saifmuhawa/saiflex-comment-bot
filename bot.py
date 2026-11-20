"""
Saiflex Comment Bot
-------------------
When someone comments one of the KEYWORDS on a recent reel/post of
@saiflex.u, the bot:
  1. replies publicly under the comment (this reply is also the bot's memory:
     a comment that already has our reply is never handled twice), and
  2. sends the commenter one private DM with the link (Meta "Private Replies").

Uses only the official Instagram API.

Run modes:
  - Web service (Render):  gunicorn bot:app      -> call GET /run?key=RUN_KEY
                                                    every few minutes from a
                                                    free cron pinger.
  - One-off from terminal: python bot.py
"""

import os
import re
import time
import logging
import threading
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, request, jsonify

# ---------------------------------------------------------------- settings
API_VERSION = os.environ.get("API_VERSION", "v23.0")
# Tried in order until one works with the token (Instagram Login vs. Unified/Facebook Login).
API_HOSTS = [h for h in os.environ.get(
    "API_HOSTS", "https://graph.instagram.com,https://graph.facebook.com").split(",") if h]
API = f"{API_HOSTS[0]}/{API_VERSION}"
IG_USER_ID = os.environ.get("IG_USER_ID", "17841424500830128")  # @saiflex.u
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "").strip()
RUN_KEY = os.environ.get("RUN_KEY", "")                     # protects /run
KEYWORDS = [k.strip() for k in os.environ.get(
    "KEYWORDS", "The Hunger Games,هنقر قيم,هنقر قيمز,هنجر جيمز"
).split(",") if k.strip()]
LINK = os.environ.get("LINK", "https://cinemana.site/search?q=the+hunger+games")
DM_TEXT = os.environ.get(
    "DM_TEXT",
    "هلا! هاد الرابط اللي وعدتك فيه 🎬\n{link}\n"
    "وإنت مين شخصيتك المفضلة بهنقر قيمز؟",
)
PUBLIC_REPLY = os.environ.get("PUBLIC_REPLY", "بعتلك الرابط على الخاص 📩")
MEDIA_DAYS = int(os.environ.get("MEDIA_DAYS", "14"))       # how far back to look at posts
MAX_MEDIA = int(os.environ.get("MAX_MEDIA", "10"))
# Optional: lets the bot save its refreshed token back into Render by itself.
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("comment-bot")
_lock = threading.Lock()
_state = {"token": TOKEN, "me": None, "last_refresh": 0.0, "last_run": None, "sent_total": 0}


# ---------------------------------------------------------------- helpers
_AR_DIACRITICS = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")


def normalize(text: str) -> str:
    """Lower-case, drop Arabic diacritics/tatweel and spaces, unify letter variants."""
    t = _AR_DIACRITICS.sub("", text or "").lower()
    t = re.sub("[\u0623\u0625\u0622]", "\u0627", t).replace("\u0629", "\u0647").replace("\u0649", "\u064A")
    return re.sub(r"\s+", "", t)


_KEYS = [normalize(k) for k in KEYWORDS]


def matches(text: str) -> bool:
    n = normalize(text)
    return any(k and k in n for k in _KEYS)


def api(method: str, path: str, **params):
    params["access_token"] = _state["token"]
    url = path if path.startswith("http") else f"{_state.get('api', API)}/{path}"
    if method == "GET":
        r = requests.get(url, params=params, timeout=30)
    else:
        body = params.pop("json", None)
        r = requests.post(url, params=params, json=body, timeout=30)
    data = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {data.get('error', data)}")
    return data


def paged(path: str, limit_pages: int = 10, **params):
    data = api("GET", path, **params)
    for _ in range(limit_pages):
        yield from data.get("data", [])
        nxt = data.get("paging", {}).get("next")
        if not nxt:
            return
        r = requests.get(nxt, timeout=30)
        data = r.json()


# ---------------------------------------------------------------- token
def refresh_token_if_needed():
    """Instagram-Login tokens last 60 days; refresh about once a day.
    System-user tokens (Unified login) never expire, so this simply no-ops for them."""
    if time.time() - _state["last_refresh"] < 24 * 3600:
        return
    _state["last_refresh"] = time.time()
    if "graph.instagram.com" not in _state.get("api", API):
        return
    try:
        r = requests.get(
            "https://graph.instagram.com/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": _state["token"]},
            timeout=30,
        ).json()
        if "access_token" in r:
            _state["token"] = r["access_token"]
            log.info("token refreshed, valid ~%s days", int(r.get("expires_in", 0)) // 86400)
            save_token_to_render(r["access_token"])
        else:
            log.warning("token refresh failed: %s", r)
    except Exception as e:  # never stop the bot over this
        log.warning("token refresh error: %s", e)


def save_token_to_render(token: str):
    if not (RENDER_API_KEY and RENDER_SERVICE_ID):
        return
    try:
        r = requests.put(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars/IG_ACCESS_TOKEN",
            headers={"Authorization": f"Bearer {RENDER_API_KEY}"},
            json={"value": token},
            timeout=30,
        )
        log.info("saved token to Render: %s", r.status_code)
    except Exception as e:
        log.warning("could not save token to Render: %s", e)


# ---------------------------------------------------------------- core
def get_me():
    """Find which API host accepts the token and who we are."""
    if _state["me"]:
        return _state["me"]
    errors = []
    for host in API_HOSTS:
        _state["api"] = f"{host.rstrip('/')}/{API_VERSION}"
        for path, fields in ((IG_USER_ID, "id,username"), ("me", "user_id,username")):
            if not path:
                continue
            try:
                me = api("GET", path, fields=fields)
                if "username" not in me:
                    continue
                ig_id = str(me.get("user_id") or me.get("id"))
                _state["me"] = {"id": ig_id, "username": me["username"]}
                log.info("running as @%s via %s", me["username"], _state["api"])
                return _state["me"]
            except Exception as e:
                errors.append(f"{host} {path}: {e}")
    raise RuntimeError("token not accepted: " + " | ".join(errors))


def already_handled(comment: dict, me: dict) -> bool:
    for rep in comment.get("replies", {}).get("data", []):
        if (rep.get("username") or "").lower() == me["username"].lower():
            return True
    return False


def handle_comment(comment: dict, me: dict) -> bool:
    cid = comment["id"]
    # 1) public reply first: it is the "done" marker even if the bot restarts
    api("POST", f"{cid}/replies", message=PUBLIC_REPLY)
    # 2) the private DM with the link
    api(
        "POST",
        f"{me['id']}/messages",
        json={"recipient": {"comment_id": cid}, "message": {"text": DM_TEXT.format(link=LINK)}},
    )
    log.info("sent link to @%s (comment %s)", comment.get("username"), cid)
    return True


def run_once() -> dict:
    if not _state["token"]:
        return {"ok": False, "error": "IG_ACCESS_TOKEN is not set"}
    with _lock:
        try:
            me = get_me()
        except Exception as e:
            _state["last_result"] = {"ok": False, "error": str(e)[:500]}
            raise
        refresh_token_if_needed()
        since = datetime.now(timezone.utc) - timedelta(days=MEDIA_DAYS)
        reply_window = datetime.now(timezone.utc) - timedelta(days=7)  # Meta: private reply within 7 days
        sent, checked, errors = 0, 0, []

        media_list = api("GET", f"{me['id']}/media", fields="id,timestamp,caption", limit=MAX_MEDIA).get("data", [])
        for media in media_list:
            if datetime.fromisoformat(media["timestamp"].replace("+0000", "+00:00")) < since:
                continue
            for c in paged(
                f"{media['id']}/comments",
                fields="id,text,timestamp,username,from{id,username},replies{username}",
                limit=50,
            ):
                checked += 1
                author_id = str((c.get("from") or {}).get("id", ""))
                if author_id == me["id"] or (c.get("username") or "").lower() == me["username"].lower():
                    continue
                if not matches(c.get("text", "")):
                    continue
                if datetime.fromisoformat(c["timestamp"].replace("+0000", "+00:00")) < reply_window:
                    continue
                if already_handled(c, me):
                    continue
                try:
                    handle_comment(c, me)
                    sent += 1
                    time.sleep(2)  # be gentle with rate limits
                except Exception as e:
                    errors.append(str(e))
                    log.error("comment %s: %s", c["id"], e)

        _state["sent_total"] += sent
        _state["last_run"] = datetime.now(timezone.utc).isoformat()
        result = {"ok": not errors, "media": len(media_list), "comments_checked": checked,
                  "sent": sent, "errors": errors[:5]}
        _state["last_result"] = result
        return result


# ---------------------------------------------------------------- background loop
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))  # check comments every 5 minutes


def _loop():
    time.sleep(10)
    while True:
        try:
            log.info("auto run: %s", run_once())
        except Exception as e:
            log.error("auto run failed: %s", e)
            _state["last_result"] = {"ok": False, "error": str(e)[:500]}
        time.sleep(POLL_SECONDS)


if POLL_SECONDS > 0 and os.environ.get("DISABLE_LOOP") != "1":
    threading.Thread(target=_loop, daemon=True).start()


# ---------------------------------------------------------------- web
app = Flask(__name__)


@app.get("/")
def health():
    """Public status page; a free uptime pinger hitting this keeps the service awake."""
    return jsonify(status="alive", keywords=KEYWORDS, last_run=_state["last_run"],
                   last_result=_state.get("last_result"), sent_total=_state["sent_total"],
                   account=(_state["me"] or {}).get("username"))


@app.get("/run")
def run_endpoint():
    if RUN_KEY and request.args.get("key") != RUN_KEY:
        return jsonify(ok=False, error="bad key"), 403
    try:
        return jsonify(run_once())
    except Exception as e:
        log.exception("run failed")
        return jsonify(ok=False, error=str(e)), 500


if __name__ == "__main__":
    print(run_once())

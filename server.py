#!/usr/bin/env python3
"""
Online shared video app — accounts, uploads, feed, subscribe.
Run:  python3 server.py
Then open http://YOUR_IP:8080
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
UPLOADS = ROOT / "uploads"
DATA = ROOT / "data"
DB_FILE = DATA / "db.json"
PORT = int(os.environ.get("PORT", "8081"))

STATIC.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)
DATA.mkdir(exist_ok=True)

# ---------- DB ----------

def default_db():
    return {"users": {}, "videos": [], "sessions": {}}


def load_db():
    if not DB_FILE.exists():
        db = default_db()
        save_db(db)
        return db
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
        for k, v in default_db().items():
            db.setdefault(k, v)
        return db
    except Exception:
        return default_db()


def save_db(db):
    tmp = DB_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    tmp.replace(DB_FILE)


def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def ensure_user_fields(u: dict) -> dict:
    u.setdefault("subscriptions", [])
    u.setdefault("playlists", [])
    u.setdefault("history", [])
    u.setdefault("notifications", [])
    return u


def public_user(u: dict) -> dict:
    ensure_user_fields(u)
    return {
        "id": u["id"],
        "username": u["username"],
        "display_name": u.get("display_name") or u["username"],
        "photo": u.get("photo") or "",
        "subscriptions": list(u.get("subscriptions") or []),
        "created_at": u.get("created_at", 0),
        "playlist_count": len(u.get("playlists") or []),
        "unread_notifications": sum(1 for n in (u.get("notifications") or []) if not n.get("read")),
    }


def notify_user(db, user_id: str, text: str, link: str = ""):
    u = db["users"].get(user_id)
    if not u:
        return
    ensure_user_fields(u)
    u["notifications"].insert(0, {
        "id": str(uuid.uuid4())[:10],
        "text": text[:200],
        "link": link,
        "read": False,
        "created_at": int(time.time()),
    })
    u["notifications"] = u["notifications"][:50]


# ---------- multipart (simple) ----------

def parse_multipart(body: bytes, content_type: str):
    """Return (fields: dict[str,str], files: dict[str, tuple[filename, bytes, content_type]])"""
    m = re.search(r"boundary=([^\s;]+)", content_type)
    if not m:
        return {}, {}
    boundary = m.group(1).strip().strip('"').encode()
    parts = body.split(b"--" + boundary)
    fields, files = {}, {}
    for part in parts:
        if not part or part in (b"--\r\n", b"--", b"\r\n", b"--\r\n\r\n"):
            continue
        if part.startswith(b"--"):
            continue
        if part[:2] == b"\r\n":
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        header_end = part.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        headers_raw = part[:header_end].decode("utf-8", errors="replace")
        data = part[header_end + 4 :]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        name_m = re.search(r'name="([^"]+)"', headers_raw)
        if not name_m:
            continue
        name = name_m.group(1)
        filename_m = re.search(r'filename="([^"]*)"', headers_raw)
        if filename_m is not None:
            filename = filename_m.group(1) or "upload.bin"
            ct_m = re.search(r"Content-Type:\s*([^\r\n]+)", headers_raw, re.I)
            ct = (ct_m.group(1).strip() if ct_m else "application/octet-stream")
            files[name] = (filename, data, ct)
        else:
            fields[name] = data.decode("utf-8", errors="replace")
    return fields, files


# ---------- handler ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "YTOnline/1.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {args[0] if args else fmt}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _session_user(self, db):
        sid = None
        # Bearer token (more reliable through tunnels / mobile browsers)
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            sid = auth[7:].strip()
        if not sid:
            cookie = self.headers.get("Cookie") or ""
            m = re.search(r"(?:^|;\s*)session=([a-zA-Z0-9]+)", cookie)
            if m:
                sid = m.group(1)
        if not sid:
            return None
        sess = db.get("sessions", {}).get(sid)
        if not sess:
            return None
        if sess.get("expires", 0) < time.time():
            db["sessions"].pop(sid, None)
            save_db(db)
            return None
        uid = sess.get("user_id")
        u = db["users"].get(uid)
        if u:
            ensure_user_fields(u)
        return u

    def _set_session(self, db, user_id: str):
        sid = secrets.token_hex(24)
        db.setdefault("sessions", {})[sid] = {
            "user_id": user_id,
            "expires": time.time() + 60 * 60 * 24 * 30,
        }
        save_db(db)
        return sid

    def _auth_payload(self, user, sid):
        return {"ok": True, "token": sid, "user": public_user(user)}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/"):
            return self._api_get(path, parsed)

        # static / uploads
        if path.startswith("/uploads/"):
            return self._serve_file(UPLOADS / path[len("/uploads/") :], download=False)

        # Talya-Tube only (mobile, EN/HE)
        if path in ("/", "/index.html", "/talya", "/talya/", "/talyatube", "/talyatube/", "/talya-tube", "/talya-tube/"):
            return self._serve_file(STATIC / "talya.html", content_type="text/html; charset=utf-8")

        # other static
        rel = path.lstrip("/")
        candidate = STATIC / rel
        if candidate.is_file() and STATIC in candidate.resolve().parents:
            return self._serve_file(candidate)

        self._json(404, {"error": "Not found"})

    def _serve_file(self, filepath: Path, content_type=None, download=False):
        filepath = filepath.resolve()
        if not filepath.is_file():
            self.send_response(404)
            self.end_headers()
            return
        data = filepath.read_bytes()
        if not content_type:
            ext = filepath.suffix.lower()
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript",
                ".css": "text/css",
                ".json": "application/json",
                ".mp4": "video/mp4",
                ".webm": "video/webm",
                ".mov": "video/quicktime",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }.get(ext, "application/octet-stream")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{filepath.name}"')
        self.end_headers()
        self.wfile.write(data)

    def _api_get(self, path, parsed):
        db = load_db()
        user = self._session_user(db)

        if path == "/api/me":
            if not user:
                return self._json(200, {"user": None})
            return self._json(200, {"user": public_user(user)})

        if path == "/api/videos":
            videos = sorted(db["videos"], key=lambda v: v.get("created_at", 0), reverse=True)
            # enrich channel photo
            users = db["users"]
            out = []
            for v in videos:
                item = dict(v)
                u = users.get(v.get("user_id"))
                if u:
                    item["channel_photo"] = u.get("photo") or ""
                    item["channel"] = u.get("display_name") or u.get("username")
                out.append(item)
            return self._json(200, {"videos": out})

        if path.startswith("/api/users/"):
            uname = unquote(path[len("/api/users/") :]).strip().lower()
            target = None
            for u in db["users"].values():
                if u["username"].lower() == uname or u["id"] == uname:
                    target = u
                    break
            if not target:
                return self._json(404, {"error": "User not found"})
            vids = [v for v in db["videos"] if v.get("user_id") == target["id"]]
            vids = sorted(vids, key=lambda v: v.get("created_at", 0), reverse=True)
            return self._json(200, {"user": public_user(target), "videos": vids})

        if path == "/api/playlists":
            if not user:
                return self._json(401, {"error": "Login required"})
            return self._json(200, {"playlists": user.get("playlists") or []})

        if path == "/api/history":
            if not user:
                return self._json(401, {"error": "Login required"})
            hist = user.get("history") or []
            by_id = {v["id"]: v for v in db["videos"]}
            out = []
            for h in hist:
                v = by_id.get(h.get("video_id"))
                if v:
                    item = dict(v)
                    item["watched_at"] = h.get("at", 0)
                    out.append(item)
            return self._json(200, {"history": out})

        if path == "/api/notifications":
            if not user:
                return self._json(401, {"error": "Login required"})
            return self._json(200, {"notifications": user.get("notifications") or []})

        return self._json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        db = load_db()
        user = self._session_user(db)
        ct = self.headers.get("Content-Type") or ""

        if path == "/api/register":
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "Invalid JSON"})
            username = (data.get("username") or "").strip()
            password = data.get("password") or ""
            display = (data.get("display_name") or username).strip()
            # Strip spaces; allow letters, numbers, underscore, hyphen, dot
            username = re.sub(r"\s+", "", username)
            if len(username) < 2 or len(username) > 24:
                return self._json(400, {"error": "Username must be 2 to 24 characters"})
            if not re.fullmatch(r"[A-Za-z0-9_\-.]+", username):
                return self._json(400, {"error": "Username: only letters, numbers, _ - ."})
            if len(password) < 3:
                return self._json(400, {"error": "Password must be at least 3 characters"})
            if any(u["username"].lower() == username.lower() for u in db["users"].values()):
                return self._json(400, {"error": "Username taken — Sign in instead, or pick a different name"})
            uid = str(uuid.uuid4())[:12]
            salt = secrets.token_hex(8)
            db["users"][uid] = {
                "id": uid,
                "username": username,
                "display_name": display or username,
                "password_hash": hash_pw(password, salt),
                "salt": salt,
                "photo": "",
                "subscriptions": [],
                "playlists": [],
                "history": [],
                "notifications": [],
                "created_at": int(time.time()),
            }
            sid = self._set_session(db, uid)
            self.send_response(201)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            cookie = f"session={sid}; Path=/; HttpOnly; SameSite=None; Secure; Max-Age=2592000"
            self.send_header("Set-Cookie", cookie)
            body_out = json.dumps(self._auth_payload(db["users"][uid], sid)).encode()
            self.send_header("Content-Length", str(len(body_out)))
            self.end_headers()
            self.wfile.write(body_out)
            return

        if path == "/api/login":
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "Invalid JSON"})
            username = re.sub(r"\s+", "", (data.get("username") or "").strip())
            password = data.get("password") or ""
            found = None
            for u in db["users"].values():
                if u["username"].lower() == username.lower():
                    found = u
                    break
            if not found or found["password_hash"] != hash_pw(password, found["salt"]):
                return self._json(401, {"error": "Wrong username or password"})
            sid = self._set_session(db, found["id"])
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            cookie = f"session={sid}; Path=/; HttpOnly; SameSite=None; Secure; Max-Age=2592000"
            self.send_header("Set-Cookie", cookie)
            body_out = json.dumps(self._auth_payload(found, sid)).encode()
            self.send_header("Content-Length", str(len(body_out)))
            self.end_headers()
            self.wfile.write(body_out)
            return

        if path == "/api/logout":
            cookie = self.headers.get("Cookie") or ""
            m = re.search(r"(?:^|;\s*)session=([a-zA-Z0-9]+)", cookie)
            if m:
                db.get("sessions", {}).pop(m.group(1), None)
                save_db(db)
            # also accept bearer logout
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                db.get("sessions", {}).pop(auth[7:].strip(), None)
                save_db(db)
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "session=; Path=/; Max-Age=0")
            body_out = b'{"ok":true}'
            self.send_header("Content-Length", str(len(body_out)))
            self.end_headers()
            self.wfile.write(body_out)
            return

        # Simple device login: display name + device id (no password)
        if path == "/api/device-login":
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "Invalid JSON"})
            device_id = (data.get("device_id") or "").strip()[:80]
            display = (data.get("display_name") or "").strip()[:40]
            if not device_id or len(device_id) < 8:
                return self._json(400, {"error": "Missing device"})
            if not display or len(display) < 1:
                return self._json(400, {"error": "Enter a name"})
            found = None
            for u in db["users"].values():
                if u.get("device_id") == device_id:
                    found = u
                    break
            if found:
                found["display_name"] = display
                ensure_user_fields(found)
            else:
                uid = str(uuid.uuid4())[:12]
                # unique username from display + short id
                base = re.sub(r"[^a-zA-Z0-9_\-]", "", display.replace(" ", "_"))[:16] or "user"
                uname = base
                n = 1
                while any(x["username"].lower() == uname.lower() for x in db["users"].values()):
                    n += 1
                    uname = f"{base}{n}"
                salt = secrets.token_hex(8)
                found = {
                    "id": uid,
                    "username": uname,
                    "display_name": display,
                    "password_hash": "",
                    "salt": salt,
                    "device_id": device_id,
                    "photo": "",
                    "subscriptions": [],
                    "playlists": [],
                    "history": [],
                    "notifications": [],
                    "created_at": int(time.time()),
                }
                db["users"][uid] = found
            sid = self._set_session(db, found["id"])
            save_db(db)
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            cookie = f"session={sid}; Path=/; HttpOnly; SameSite=None; Secure; Max-Age=2592000"
            self.send_header("Set-Cookie", cookie)
            body_out = json.dumps(self._auth_payload(found, sid)).encode()
            self.send_header("Content-Length", str(len(body_out)))
            self.end_headers()
            self.wfile.write(body_out)
            return

        if path == "/api/profile":
            if not user:
                return self._json(401, {"error": "Login required"})
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "Invalid JSON"})
            if "display_name" in data:
                user["display_name"] = (data["display_name"] or user["username"]).strip()[:40]
            if "photo" in data and isinstance(data["photo"], str):
                # allow data-url photos up to ~1.5MB text
                if len(data["photo"]) < 2_000_000:
                    user["photo"] = data["photo"]
            save_db(db)
            return self._json(200, {"ok": True, "user": public_user(user)})

        if path == "/api/subscribe":
            if not user:
                return self._json(401, {"error": "Login required"})
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "Invalid JSON"})
            target_id = data.get("user_id") or ""
            if target_id == user["id"]:
                return self._json(400, {"error": "You cannot subscribe to yourself"})
            if target_id not in db["users"]:
                return self._json(404, {"error": "User not found"})
            subs = user.setdefault("subscriptions", [])
            if target_id in subs:
                subs.remove(target_id)
                save_db(db)
                return self._json(200, {"ok": True, "subscribed": False})
            subs.insert(0, target_id)
            save_db(db)
            return self._json(200, {"ok": True, "subscribed": True})

        if path == "/api/videos":
            if not user:
                return self._json(401, {"error": "Login required"})
            if "multipart/form-data" not in ct:
                return self._json(400, {"error": "Expected multipart upload"})
            body = self._read_body()
            fields, files = parse_multipart(body, ct)
            if "file" not in files:
                return self._json(400, {"error": "No video file"})
            filename, data, file_ct = files["file"]
            if not data:
                return self._json(400, {"error": "Empty file"})
            if len(data) > 200 * 1024 * 1024:
                return self._json(400, {"error": "File too large (max 200MB)"})
            title = (fields.get("title") or "").strip() or Path(filename).stem or "Untitled"
            category = (fields.get("category") or "All").strip() or "All"
            is_short = (fields.get("is_short") or "").lower() in ("1", "true", "yes", "on")
            thumb_data_url = (fields.get("thumbnail") or "").strip()
            ext = Path(filename).suffix.lower() or ".mp4"
            if not ext:
                ext = ".mp4"
            vid = str(uuid.uuid4())[:12]
            stored_name = vid + ext
            (UPLOADS / stored_name).write_bytes(data)
            thumb_url = ""
            if thumb_data_url.startswith("data:image/") and len(thumb_data_url) < 2_500_000:
                try:
                    import base64
                    header, b64 = thumb_data_url.split(",", 1)
                    raw = base64.b64decode(b64)
                    tname = vid + ".jpg"
                    (UPLOADS / tname).write_bytes(raw)
                    thumb_url = "/uploads/" + tname
                except Exception:
                    thumb_url = ""
            entry = {
                "id": vid,
                "title": title[:200],
                "category": category[:40],
                "user_id": user["id"],
                "channel": user.get("display_name") or user["username"],
                "filename": stored_name,
                "url": "/uploads/" + stored_name,
                "thumbnail": thumb_url,
                "is_short": is_short,
                "created_at": int(time.time()),
                "likes": 0,
            }
            db["videos"].append(entry)
            channel = entry.get("channel") or user.get("username")
            for u in db["users"].values():
                if user["id"] in (u.get("subscriptions") or []):
                    notify_user(db, u["id"], f"{channel} uploaded: {entry['title']}", entry["id"])
            save_db(db)
            return self._json(201, {"ok": True, "video": entry})

        if path == "/api/videos/like":
            if not user:
                return self._json(401, {"error": "Login required"})
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "Invalid JSON"})
            vid = data.get("id")
            for v in db["videos"]:
                if v["id"] == vid:
                    v["likes"] = int(v.get("likes") or 0) + 1
                    save_db(db)
                    return self._json(200, {"ok": True, "likes": v["likes"]})
            return self._json(404, {"error": "Video not found"})

        if path == "/api/history":
            if not user:
                return self._json(401, {"error": "Login required"})
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "Invalid JSON"})
            vid = data.get("video_id") or ""
            if not any(v["id"] == vid for v in db["videos"]):
                return self._json(404, {"error": "Video not found"})
            hist = [h for h in (user.get("history") or []) if h.get("video_id") != vid]
            hist.insert(0, {"video_id": vid, "at": int(time.time())})
            user["history"] = hist[:100]
            save_db(db)
            return self._json(200, {"ok": True})

        if path == "/api/playlists":
            if not user:
                return self._json(401, {"error": "Login required"})
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "Invalid JSON"})
            name = (data.get("name") or "").strip()[:60] or "My playlist"
            pl = {"id": str(uuid.uuid4())[:10], "name": name, "video_ids": [], "created_at": int(time.time())}
            user.setdefault("playlists", []).insert(0, pl)
            save_db(db)
            return self._json(201, {"ok": True, "playlist": pl, "playlists": user["playlists"]})

        if path == "/api/playlists/add":
            if not user:
                return self._json(401, {"error": "Login required"})
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "Invalid JSON"})
            pid, vid = data.get("playlist_id") or "", data.get("video_id") or ""
            pl = next((p for p in user.get("playlists") or [] if p["id"] == pid), None)
            if not pl:
                return self._json(404, {"error": "Playlist not found"})
            if not any(v["id"] == vid for v in db["videos"]):
                return self._json(404, {"error": "Video not found"})
            if vid not in pl["video_ids"]:
                pl["video_ids"].insert(0, vid)
            save_db(db)
            return self._json(200, {"ok": True, "playlist": pl})

        if path == "/api/playlists/remove":
            if not user:
                return self._json(401, {"error": "Login required"})
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "Invalid JSON"})
            pid, vid = data.get("playlist_id") or "", data.get("video_id") or ""
            pl = next((p for p in user.get("playlists") or [] if p["id"] == pid), None)
            if not pl:
                return self._json(404, {"error": "Playlist not found"})
            pl["video_ids"] = [x for x in pl["video_ids"] if x != vid]
            save_db(db)
            return self._json(200, {"ok": True, "playlist": pl})

        if path == "/api/playlists/delete":
            if not user:
                return self._json(401, {"error": "Login required"})
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "Invalid JSON"})
            pid = data.get("playlist_id") or ""
            user["playlists"] = [p for p in user.get("playlists") or [] if p["id"] != pid]
            save_db(db)
            return self._json(200, {"ok": True, "playlists": user["playlists"]})

        if path == "/api/notifications/read":
            if not user:
                return self._json(401, {"error": "Login required"})
            for n in user.get("notifications") or []:
                n["read"] = True
            save_db(db)
            return self._json(200, {"ok": True})

        # POST delete (more reliable than DELETE through some proxies)
        if path == "/api/videos/delete":
            if not user:
                return self._json(401, {"error": "Login required"})
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "Invalid JSON"})
            return self._delete_video(db, user, data.get("id") or "")

        # Comments
        if path == "/api/comments":
            if not user:
                return self._json(401, {"error": "Login required"})
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "Invalid JSON"})
            vid = data.get("video_id") or ""
            text = (data.get("text") or "").strip()
            if not text:
                return self._json(400, {"error": "Comment is empty"})
            if len(text) > 1000:
                return self._json(400, {"error": "Comment too long"})
            found = None
            for v in db["videos"]:
                if v["id"] == vid:
                    found = v
                    break
            if not found:
                return self._json(404, {"error": "Video not found"})
            comment = {
                "id": str(uuid.uuid4())[:10],
                "user_id": user["id"],
                "username": user.get("display_name") or user["username"],
                "photo": user.get("photo") or "",
                "text": text,
                "created_at": int(time.time()),
            }
            found.setdefault("comments", []).append(comment)
            save_db(db)
            return self._json(201, {"ok": True, "comment": comment, "comments": found["comments"]})

        return self._json(404, {"error": "Not found"})

    def _delete_video(self, db, user, vid):
        found = None
        for v in db["videos"]:
            if v["id"] == vid:
                found = v
                break
        if not found:
            return self._json(404, {"error": "Video not found"})
        if found.get("user_id") != user["id"]:
            return self._json(403, {"error": "Not your video"})
        db["videos"] = [v for v in db["videos"] if v["id"] != vid]
        fp = UPLOADS / found.get("filename", "")
        if fp.is_file():
            try:
                fp.unlink()
            except Exception:
                pass
        thumb = found.get("thumbnail") or ""
        if thumb.startswith("/uploads/"):
            tp = UPLOADS / thumb.split("/")[-1]
            if tp.is_file():
                try:
                    tp.unlink()
                except Exception:
                    pass
        save_db(db)
        return self._json(200, {"ok": True})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        db = load_db()
        user = self._session_user(db)
        m = re.match(r"^/api/videos/([\w-]+)$", path)
        if m:
            if not user:
                return self._json(401, {"error": "Login required"})
            return self._delete_video(db, user, m.group(1))
        return self._json(404, {"error": "Not found"})


def main():
    # minimal index if missing
    index = STATIC / "index.html"
    if not index.exists():
        index.write_text("<!DOCTYPE html><html><body><p>Put frontend in static/index.html</p></body></html>", encoding="utf-8")

    host = "0.0.0.0"
    httpd = ThreadingHTTPServer((host, PORT), Handler)
    print(f"\n  Online video app running at:")
    print(f"  →  http://127.0.0.1:{PORT}")
    print(f"  →  http://0.0.0.0:{PORT}  (other devices on your network)")
    print(f"\n  Deploy this folder to any host that can run Python, or use a tunnel (ngrok, cloudflared).")
    print(f"  Data: {DATA}   Uploads: {UPLOADS}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

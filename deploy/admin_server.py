#!/usr/bin/env python3
"""Small, private-by-default 360News admin API/UI."""
import base64, hashlib, hmac, json, os, secrets, subprocess, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return "pbkdf2:240000:%s:%s" % (base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(digest).decode())


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt, expected = encoded.split(":", 3)
        if scheme != "pbkdf2": return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.urlsafe_b64decode(salt), int(rounds))
        return hmac.compare_digest(base64.urlsafe_b64encode(actual).decode(), expected)
    except (ValueError, TypeError):
        return False


class UserStore:
    def __init__(self, path: Path, bootstrap_user: str = "", bootstrap_hash: str = ""):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.users = self._load()
        if not self.users and bootstrap_user and bootstrap_hash:
            self.users = [{"username": bootstrap_user, "password_hash": bootstrap_hash, "role": "admin", "active": True}]
            self._save()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError): return []

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.users, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def authenticate(self, username, password):
        for user in self.users:
            if user.get("active", True) and hmac.compare_digest(user.get("username", ""), username) and verify_password(password, user.get("password_hash", "")):
                return {"username": username, "is_admin": user.get("role") == "admin"}
        return None

    def list_public(self):
        return [{"username": u.get("username"), "role": u.get("role", "operator"), "active": bool(u.get("active", True))} for u in self.users]

    def add(self, username, password, role="operator"):
        if not username or len(password) < 10 or role not in ("admin", "operator") or any(u.get("username") == username for u in self.users): raise ValueError("invalid or duplicate user")
        self.users.append({"username": username, "password_hash": hash_password(password), "role": role, "active": True}); self._save()

    def set_active(self, username, active):
        for user in self.users:
            if user.get("username") == username: user["active"] = bool(active); self._save(); return
        raise KeyError(username)

    def remove(self, username):
        if username == self.users[0].get("username") and len(self.users) == 1: raise ValueError("cannot remove last user")
        self.users = [u for u in self.users if u.get("username") != username]; self._save()


class AdminState:
    def __init__(self, config_dir: Path):
        self.config_dir = Path(config_dir)

    def snapshot(self):
        env = self.config_dir / ".env"
        values = {}
        if env.exists():
            for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()
        return {
            "configured": bool(values.get("TOKEN") and values.get("MANAGER")),
            "config_dir": str(self.config_dir),
            "files": sorted(p.name for p in self.config_dir.glob("*.json")),
            "xbridge": self._json("xbridge_config.json"),
            "routes": self._json("push_routes.json"),
        }

    def _json(self, name):
        try:
            data = json.loads((self.config_dir / name).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


HTML = """<!doctype html><meta charset=utf-8><title>360News Admin</title>
<style>body{font:16px system-ui;max-width:900px;margin:40px auto;padding:0 16px}pre{background:#111;color:#eee;padding:16px;overflow:auto}button{padding:8px 14px;margin-right:8px}</style>
<h1>360News 管理后台</h1><p>只显示脱敏状态，不显示 Token、Cookie 或会话内容。</p>
<button onclick="act('/api/start')">启动</button><button onclick="act('/api/stop')">停止</button><button onclick="load()">刷新</button><pre id=o>加载中…</pre>
<h2>用户</h2><input id=u placeholder="用户名"><input id=p type=password placeholder="密码（至少10位）"><button onclick="addUser()">新增用户</button><pre id=users>加载中…</pre>
<script>async function load(){o.textContent=JSON.stringify(await (await fetch('/api/status')).json(),null,2);users.textContent=JSON.stringify(await (await fetch('/api/users')).json(),null,2)} async function act(u){await fetch(u,{method:'POST',headers:{'X-Admin-Confirm':'1'}});load()} async function addUser(){await fetch('/api/users',{method:'POST',headers:{'X-Admin-Confirm':'1','Content-Type':'application/json'},body:JSON.stringify({username:u.value,password:p.value})});u.value=p.value='';load()}load()</script>"""


class Handler(BaseHTTPRequestHandler):
    state = None
    username = os.environ.get("ADMIN_USER", "admin")
    password_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")
    users = None
    compose_dir = Path(os.environ.get("RSSTT_DIR", "/app"))

    def _auth(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try: user, password = base64.b64decode(header[6:]).decode().split(":", 1)
        except (ValueError, UnicodeError): return False
        return self.users.authenticate(user, password)

    def _send(self, status, body, content_type="application/json"):
        raw = body if isinstance(body, bytes) else body.encode()
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def _guard(self):
        identity = self._auth()
        if identity: self.identity = identity; return True
        self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="360News"'); self.end_headers(); return False

    def do_GET(self):
        if not self._guard(): return
        if self.path == "/": return self._send(200, HTML, "text/html; charset=utf-8")
        if self.path == "/api/status": return self._send(200, json.dumps(self.state.snapshot(), ensure_ascii=False))
        if self.path == "/api/users": return self._send(200, json.dumps(self.users.list_public(), ensure_ascii=False))
        self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if not self._guard(): return
        if self.headers.get("X-Admin-Confirm") != "1": return self._send(403, '{"error":"confirmation header required"}')
        if self.path == "/api/users":
            if not getattr(self, "identity", {}).get("is_admin"): return self._send(403, '{"error":"admin role required"}')
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                self.users.add(str(body.get("username", "")).strip(), str(body.get("password", "")), str(body.get("role", "operator")))
                return self._send(201, '{"ok":true}')
            except (ValueError, TypeError): return self._send(400, '{"error":"invalid user or password (minimum 10 characters)"}')
        if self.path.startswith("/api/users/"):
            if not getattr(self, "identity", {}).get("is_admin"): return self._send(403, '{"error":"admin role required"}')
            username = self.path.rsplit("/", 1)[-1]
            try:
                self.users.set_active(username, False) if self.headers.get("X-User-Action") == "disable" else self.users.remove(username)
                return self._send(200, '{"ok":true}')
            except (KeyError, ValueError): return self._send(400, '{"error":"user operation rejected"}')
        action = {"/api/start": ["start", "360news-bot", "360news-bridge"], "/api/stop": ["stop", "360news-bot", "360news-bridge"]}.get(self.path)
        if not action: return self._send(404, '{"error":"not found"}')
        result = subprocess.run(["docker", *action], capture_output=True, text=True, timeout=30)
        self._send(200 if result.returncode == 0 else 500, json.dumps({"ok": result.returncode == 0, "output": result.stdout[-2000:], "error": result.stderr[-1000:]}))

    def log_message(self, *_): pass


def main():
    config = Path(os.environ.get("RSSTT_CONFIG_DIR", "/data/config"))
    Handler.state = AdminState(config)
    Handler.users = UserStore(config / "users.json", os.environ.get("ADMIN_USER", "admin"), os.environ.get("ADMIN_PASSWORD_HASH", ""))
    server = ThreadingHTTPServer((os.environ.get("ADMIN_BIND", "0.0.0.0"), int(os.environ.get("ADMIN_PORT", "8787"))), Handler)
    server.serve_forever()


if __name__ == "__main__": main()

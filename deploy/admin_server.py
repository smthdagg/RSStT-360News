#!/usr/bin/env python3
"""Small, private-by-default 360News admin API/UI."""
import base64, hashlib, hmac, json, os, secrets, subprocess, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return "pbkdf2$240000$%s$%s" % (base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(digest).decode())


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt, expected = encoded.split("$", 3)
        if scheme != "pbkdf2": return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.urlsafe_b64decode(salt), int(rounds))
        return hmac.compare_digest(base64.urlsafe_b64encode(actual).decode(), expected)
    except (ValueError, TypeError):
        return False


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
<script>async function load(){o.textContent=JSON.stringify(await (await fetch('/api/status')).json(),null,2)} async function act(u){await fetch(u,{method:'POST',headers:{'X-Admin-Confirm':'1'}});load()}load()</script>"""


class Handler(BaseHTTPRequestHandler):
    state = None
    username = os.environ.get("ADMIN_USER", "admin")
    password_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")
    compose_dir = Path(os.environ.get("RSSTT_DIR", "/app"))

    def _auth(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic ") or not self.password_hash:
            return False
        try: user, password = base64.b64decode(header[6:]).decode().split(":", 1)
        except (ValueError, UnicodeError): return False
        return hmac.compare_digest(user, self.username) and verify_password(password, self.password_hash)

    def _send(self, status, body, content_type="application/json"):
        raw = body if isinstance(body, bytes) else body.encode()
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def _guard(self):
        if self._auth(): return True
        self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="360News"'); self.end_headers(); return False

    def do_GET(self):
        if not self._guard(): return
        if self.path == "/": return self._send(200, HTML, "text/html; charset=utf-8")
        if self.path == "/api/status": return self._send(200, json.dumps(self.state.snapshot(), ensure_ascii=False))
        self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if not self._guard(): return
        if self.headers.get("X-Admin-Confirm") != "1": return self._send(403, '{"error":"confirmation header required"}')
        action = {"/api/start": ["start", "360news-bot", "360news-bridge"], "/api/stop": ["stop", "360news-bot", "360news-bridge"]}.get(self.path)
        if not action: return self._send(404, '{"error":"not found"}')
        result = subprocess.run(["docker", *action], capture_output=True, text=True, timeout=30)
        self._send(200 if result.returncode == 0 else 500, json.dumps({"ok": result.returncode == 0, "output": result.stdout[-2000:], "error": result.stderr[-1000:]}))

    def log_message(self, *_): pass


def main():
    config = Path(os.environ.get("RSSTT_CONFIG_DIR", "/data/config"))
    Handler.state = AdminState(config)
    server = ThreadingHTTPServer((os.environ.get("ADMIN_BIND", "127.0.0.1"), int(os.environ.get("ADMIN_PORT", "8787"))), Handler)
    server.serve_forever()


if __name__ == "__main__": main()

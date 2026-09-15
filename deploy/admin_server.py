#!/usr/bin/env python3
"""Small, private-by-default 360News admin API/UI."""
import base64, hashlib, hmac, json, os, secrets, subprocess, time, urllib.request
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


def parse_cookie_input(raw: str) -> tuple[list[dict], list[str]]:
    """Normalize Cookie-Editor/Playwright JSON, Netscape, or name=value text."""
    cookies, errors = [], []
    try:
        parsed = json.loads(raw)
        cookies = parsed if isinstance(parsed, list) else parsed.get("cookies", [])
        if not isinstance(cookies, list):
            raise ValueError("JSON cookies must be a list")
    except (ValueError, TypeError, AttributeError):
        for line_no, line in enumerate(raw.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) >= 7:
                try:
                    cookies.append({"domain": fields[0], "path": fields[2] or "/", "secure": fields[3].upper() == "TRUE",
                                    "expires": int(fields[4] or -1), "name": fields[5], "value": fields[6]})
                except ValueError:
                    errors.append(f"第 {line_no} 行过期时间无效")
            elif "=" in line:
                name, value = line.split("=", 1)
                cookies.append({"domain": ".x.com", "path": "/", "secure": True, "name": name.strip(), "value": value.strip()})
            else:
                errors.append(f"第 {line_no} 行无法识别")

    normalized, seen = [], set()
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name, value = str(item.get("name", "")).strip(), item.get("value")
        domain = str(item.get("domain", ".x.com")).strip().lower()
        if not name or value is None or domain.lstrip(".") not in ("x.com", "twitter.com"):
            continue
        cookie = {"name": name, "value": str(value), "domain": domain, "path": str(item.get("path") or "/"),
                  "secure": bool(item.get("secure", True)), "httpOnly": bool(item.get("httpOnly", False))}
        expires = item.get("expirationDate", item.get("expires"))
        if isinstance(expires, (int, float)) and expires > 0:
            cookie["expires"] = expires
        seen_key = (name, domain, cookie["path"])
        if seen_key not in seen:
            normalized.append(cookie)
            seen.add(seen_key)
    return normalized, errors


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

    def config_view(self):
        values = {}
        env = self.config_dir / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()

        def masked(value):
            return f"{value[:4]}…{value[-4:]}" if len(value) > 10 else ("已配置" if value else "未配置")

        return {
            "bot1": {"token": masked(values.get("TOKEN", "")), "manager": values.get("MANAGER", ""),
                     "api_id": values.get("API_ID", ""), "api_hash": masked(values.get("API_HASH", "")),
                     "proxy": values.get("TELEGRAM_PROXY", "")},
            "bot2": {"token": masked(values.get("ROUTED_BOT_TOKEN", "")),
                     "manager": values.get("ROUTED_MANAGER", "") or values.get("MANAGER", "")},
        }

    def cookie_view(self):
        session = self.config_dir / "x_session.json"
        try:
            raw = json.loads(session.read_text(encoding="utf-8"))
            cookies = raw.get("cookies", raw) if isinstance(raw, dict) else raw
            cookies = cookies if isinstance(cookies, list) else []
        except (OSError, ValueError):
            cookies = []
        names = {str(c.get("name", "")) for c in cookies if isinstance(c, dict)}
        return {"cookie_count": len(cookies), "has_auth_token": "auth_token" in names,
                "has_ct0": "ct0" in names, "updated_at": session.stat().st_mtime if session.exists() else None}

    def _json(self, name):
        try:
            data = json.loads((self.config_dir / name).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


def db_query(action, *args):
    result = subprocess.run(["python", "/app/db_query.py", action, *map(str, args)], env={**os.environ, "RSSTT_CONFIG_DIR": "/data/config"}, capture_output=True, text=True, timeout=30)
    try: return json.loads(result.stdout)
    except ValueError: return {"ok": False, "error": result.stderr[-500:] or "database query failed"}


HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>360News 管理后台</title>
<style>body{font:15px system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#172033;background:#f6f7fb}h1{margin-bottom:18px}nav{display:flex;gap:6px;flex-wrap:wrap;background:#fff;padding:10px;border:1px solid #dde2ec;border-radius:12px}button{padding:8px 13px;margin:3px;border:1px solid #aeb8c8;border-radius:7px;background:#fff;cursor:pointer}button:hover{background:#eef4ff}section{display:none;margin-top:18px}section.on{display:block}.panel{background:#fff;border:1px solid #dde2ec;border-radius:12px;padding:16px;box-shadow:0 2px 8px #1c31520d}textarea,input{padding:8px;margin:3px;max-width:100%;border:1px solid #b9c2d0;border-radius:6px}pre{background:#111827;color:#e5e7eb;padding:14px;overflow:auto;white-space:pre-wrap;border-radius:8px}.row{border:1px solid #dde2ec;padding:10px;margin:6px 0;border-radius:8px;background:#fff}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:12px 0}.card{padding:14px;border:1px solid #dde2ec;border-radius:10px;background:#fff}.label{font-size:12px;color:#64748b}.value{font-size:19px;font-weight:650;margin-top:4px}.ok{color:#087443}.warn{color:#a15c00}details{margin-top:14px}</style>
<h1>360News 管理后台</h1><nav><button onclick="tab('status')">状态</button><button onclick="tab('config')">Bot 配置</button><button onclick="tab('subs')">RSS 来源</button><button onclick="tab('routes')">来源路由</button><button onclick="tab('cookie')">X Cookie</button><button onclick="tab('history')">推送历史</button><button onclick="tab('users')">Telegram 用户</button><button onclick="tab('admins')">后台账号</button><button onclick="tab('logs')">日志</button></nav>
<section id=status class=on><div class=panel><button onclick="act('/api/start')">启动</button><button onclick="act('/api/stop')">停止</button><button onclick="load()">刷新</button><div id=summary class=cards></div><div id=detail class=row></div><details><summary>查看原始状态 JSON</summary><pre id=o></pre></details></div></section>
<section id=config><h2>Bot 配置</h2><div class=cards><div class=panel><h3>Bot1 主 Bot</h3><div id=bot1State class=label>读取中…</div><input id=bot1Token type=password placeholder="输入新 Bot1 Token（留空不修改）"><input id=bot1Manager placeholder="管理员 ID"><input id=apiId placeholder="API ID"><input id=apiHash type=password placeholder="输入新 API Hash（留空不修改）"><input id=proxy placeholder="Telegram 代理（可留空）"><br><button onclick="saveBot(1)">保存 Bot1</button></div><div class=panel><h3>Bot2 路由 Bot</h3><div id=bot2State class=label>读取中…</div><input id=bot2Token type=password placeholder="输入新 Bot2 Token（留空不修改）"><input id=bot2Manager placeholder="管理员 ID（可留空沿用 Bot1）"><br><button onclick="saveBot(2)">保存 Bot2</button></div></div></section>
<section id=subs><div class=panel><h2>RSS 来源</h2><div id=subList></div><input id=subUrl placeholder="RSS URL"><input id=subTitle placeholder="标题"><input id=subCat placeholder="分类"><button onclick="addSub()">新增来源</button></div></section>
<section id=routes><div class=panel><h2>来源 → Bot 路由</h2><p class=label>每条来源独立选择 Bot1 或 Bot2。</p><label>未匹配来源默认发送到 <select id=defaultBot><option value=primary>Bot1</option><option value=secondary>Bot2</option></select></label><div id=routeList></div><input id=routePattern placeholder="来源关键词，例如 daydayuplift"><select id=routeBot><option value=primary>Bot1</option><option value=secondary>Bot2</option></select><button onclick="addRoute()">新增规则</button><br><button onclick="saveRoutes()">保存路由</button></div></section>
<section id=cookie><div class=panel><h2>X Cookie</h2><div id=cookieState class=label>读取中…</div><p>支持 Cookie-Editor/Playwright JSON、Netscape 和 name=value 格式。</p><textarea id=cookieText rows=10 cols=80 placeholder="粘贴 Cookie 内容"></textarea><br><button onclick="previewCookie()">导入/解析</button><button onclick="saveCookie()">保存 Cookie</button><button onclick="validateCookie()">验证当前 Cookie</button><pre id=cookieResult></pre></div></section>
<section id=history><div class=panel><h2>推送历史</h2><button onclick="loadHistory()">刷新</button><button onclick="act('/api/history/clear')">清空</button><div id=historyText></div></div></section>
<section id=users><div class=panel><h2>Telegram 用户/群组</h2><input id=telegramId placeholder="Telegram 数字 ID"><button onclick="addTelegram()">添加</button><div id=telegramText></div></div></section>
<section id=admins><div class=panel><h2>后台账号</h2><input id=u placeholder="用户名"><input id=p type=password placeholder="密码（至少10位）"><button onclick="addUser()">新增账号</button><div id=adminText></div></div></section>
<section id=logs><h2>运行日志</h2><button onclick="loadLogs()">刷新</button><pre id=logText></pre></section>
<script>
async function j(u,o){return await (await fetch(u,o)).json()} function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))} async function tab(x){document.querySelectorAll('section').forEach(e=>e.classList.remove('on'));document.getElementById(x).classList.add('on');if(x==='status')load();if(x==='config')loadConfig();if(x==='subs')loadSubs();if(x==='routes')loadRoutes();if(x==='cookie')loadCookie();if(x==='history')loadHistory();if(x==='users')loadTelegram();if(x==='admins')loadAdmins();if(x==='logs')loadLogs()}
async function load(){let s=await j('/api/status');o.textContent=JSON.stringify(s,null,2);let x=s.xbridge||{},r=s.routes||{},rules=r.rules||[];summary.innerHTML=[['配置状态',s.configured?'已配置':'未配置',s.configured?'ok':'warn'],['X 会话',s.files.includes('x_session.json')?'已导入':'未导入',s.files.includes('x_session.json')?'ok':'warn'],['抓取用户',(x.users||[]).join(', ')||'未设置',''],['抓取间隔',((x.interval||0)/60)+' 分钟','']].map(c=>`<div class=card><div class=label>${c[0]}</div><div class="value ${c[2]}">${c[1]}</div></div>`).join('');detail.innerHTML=`<b>来源路由</b>：${rules.length?rules.map(x=>`${x.pattern} → ${x.bot==='secondary'?'Bot2':'Bot1'}`).join('；'):'未设置'}<br><span class=label>配置目录：${s.config_dir}</span>`} async function act(u){await j(u,{method:'POST',headers:{'X-Admin-Confirm':'1'}});load()} async function saveJson(u,id){let r=await j(u,{method:'POST',headers:{'X-Admin-Confirm':'1','Content-Type':'application/json'},body:document.getElementById(id).value});alert(JSON.stringify(r));} async function saveText(u,id){let r=await j(u,{method:'POST',headers:{'X-Admin-Confirm':'1','Content-Type':'text/plain'},body:document.getElementById(id).value});cookieResult.textContent=JSON.stringify(r,null,2)}
async function loadSubs(){let r=await j('/api/subscriptions');subList.innerHTML=(r.data||[]).map(x=>`<div class=row><b>#${esc(x.id)}</b> ${esc(x.feed_title||x.link)}<br><span class=label>状态：${x.state?'启用':'停用'} · ${esc(x.category||'未分类')}</span> <button onclick="subAct(${x.id},'toggle')">启用/禁用</button><button onclick="subAct(${x.id},'delete')">删除</button></div>`).join('')||'<div class=row>暂无 RSS 来源</div>'} async function subAct(id,a){await j('/api/subscriptions/'+id,{method:'POST',headers:{'X-Admin-Confirm':'1','X-Subscription-Action':a}});loadSubs()} async function addSub(){await j('/api/subscriptions',{method:'POST',headers:{'X-Admin-Confirm':'1','Content-Type':'application/json'},body:JSON.stringify({url:subUrl.value,title:subTitle.value,category:subCat.value})});loadSubs()}
async function loadConfig(){let c=await j('/api/config'),a=c.bot1||{},b=c.bot2||{};bot1State.textContent=`当前：Token ${esc(a.token)} · API Hash ${esc(a.api_hash)} · 管理员 ${esc(a.manager||'未设置')}`;bot2State.textContent=`当前：Token ${esc(b.token)} · 管理员 ${esc(b.manager||'未设置')}`;bot1Manager.value=a.manager||'';apiId.value=a.api_id||'';proxy.value=a.proxy||'';bot2Manager.value=b.manager||''}
async function loadCookie(){let s=await j('/api/cookie/status');cookieState.textContent=`当前 Cookie：${s.cookie_count||0} 项 · auth_token ${s.has_auth_token?'存在':'缺失'} · ct0 ${s.has_ct0?'存在':'缺失'}`}
async function cookieRequest(url,body){let r=await j(url,{method:'POST',headers:{'X-Admin-Confirm':'1','Content-Type':'text/plain'},body});cookieResult.textContent=JSON.stringify(r,null,2);loadCookie()}
async function previewCookie(){await cookieRequest('/api/cookie/validate',cookieText.value)} async function saveCookie(){await cookieRequest('/api/cookie',cookieText.value)} async function validateCookie(){await cookieRequest('/api/cookie/validate','')}
async function saveBot(n){let b=n===1?{TOKEN:bot1Token.value,MANAGER:bot1Manager.value,API_ID:apiId.value,API_HASH:apiHash.value,TELEGRAM_PROXY:proxy.value}:{ROUTED_BOT_TOKEN:bot2Token.value,ROUTED_MANAGER:bot2Manager.value};let r=await j('/api/config',{method:'POST',headers:{'X-Admin-Confirm':'1','Content-Type':'application/json'},body:JSON.stringify(b)});alert(JSON.stringify(r));[bot1Token,apiHash,bot2Token].forEach(x=>x.value='');loadConfig()}
let routeRules=[]; async function loadRoutes(){let s=await j('/api/status'),r=s.routes||{};defaultBot.value=r.defaultBot==='secondary'?'secondary':'primary';routeRules=(r.rules||[]).map(x=>({pattern:String(x.pattern||''),bot:x.bot==='secondary'?'secondary':'primary'}));renderRoutes()} function renderRoutes(){routeList.innerHTML=routeRules.length?routeRules.map((x,i)=>`<div class=row><input data-route-pattern="${i}" value="${esc(x.pattern)}"><select data-route-bot="${i}"><option value=primary ${x.bot==='primary'?'selected':''}>Bot1</option><option value=secondary ${x.bot==='secondary'?'selected':''}>Bot2</option></select><button onclick="removeRoute(${i})">删除</button></div>`).join(''):'<div class=row>暂无路由规则</div>'} function addRoute(){let p=routePattern.value.trim();if(!p)return;routeRules.push({pattern:p.toLowerCase(),bot:routeBot.value});routePattern.value='';renderRoutes()} function removeRoute(i){routeRules.splice(i,1);renderRoutes()} async function saveRoutes(){routeRules=routeRules.map((x,i)=>({pattern:document.querySelector(`[data-route-pattern="${i}"]`)?.value.trim().toLowerCase()||x.pattern,bot:document.querySelector(`[data-route-bot="${i}"]`)?.value||x.bot})).filter(x=>x.pattern);let r=await j('/api/routes',{method:'POST',headers:{'X-Admin-Confirm':'1','Content-Type':'application/json'},body:JSON.stringify({defaultBot:defaultBot.value,rules:routeRules})});alert(JSON.stringify(r));renderRoutes()}
async function loadHistory(){let r=await j('/api/history'),a=r.data||r;historyText.innerHTML=Array.isArray(a)&&a.length?a.slice(0,50).map(x=>`<div class=row><b>${esc(x.post_title||'推送')}</b><br><span class=label>${esc(x.feed_title||'')} · ${esc(x.push_time||'')}</span><br><a href="${esc(x.post_url||'#')}" target=_blank>${esc(x.post_url||'')}</a></div>`).join(''):'<div class=row>暂无推送记录</div>'} async function loadTelegram(){let r=await j('/api/bot-users'),a=r.data||r;telegramText.innerHTML=Array.isArray(a)&&a.length?a.map(x=>`<div class=row><b>${esc(x.id||x.user_id)}</b> · ${x.state===51?'群组':'用户'}</div>`).join(''):'<div class=row>暂无用户或群组</div>'} async function addTelegram(){await j('/api/bot-users',{method:'POST',headers:{'X-Admin-Confirm':'1','Content-Type':'application/json'},body:JSON.stringify({id:telegramId.value})});telegramId.value='';loadTelegram()} async function loadAdmins(){let a=await j('/api/users');adminText.innerHTML=Array.isArray(a)&&a.length?a.map(x=>`<div class=row><b>${esc(x.username)}</b> · ${esc(x.role)} · ${x.active?'启用':'停用'}</div>`).join(''):'<div class=row>暂无账号</div>'} async function addUser(){await j('/api/users',{method:'POST',headers:{'X-Admin-Confirm':'1','Content-Type':'application/json'},body:JSON.stringify({username:u.value,password:p.value})});u.value=p.value='';loadAdmins()} async function loadLogs(){logText.textContent=(await j('/api/logs')).logs||'暂无日志'} load();
</script>"""


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
        if self.path == "/api/config": return self._send(200, json.dumps(self.state.config_view(), ensure_ascii=False))
        if self.path == "/api/cookie/status": return self._send(200, json.dumps(self.state.cookie_view(), ensure_ascii=False))
        if self.path == "/api/logs":
            p = subprocess.run(["docker", "logs", "--tail", "300", "360news-bot"], capture_output=True, text=True, timeout=15)
            return self._send(200, json.dumps({"logs": (p.stdout + p.stderr)[-30000:]}, ensure_ascii=False))
        actions = {"/api/subscriptions": "list", "/api/history": "history_list", "/api/bot-users": "users_list", "/api/history/count": "history_count"}
        if self.path in actions: return self._send(200, json.dumps(db_query(actions[self.path]), ensure_ascii=False))
        self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if not self._guard(): return
        if self.headers.get("X-Admin-Confirm") != "1": return self._send(403, '{"error":"confirmation header required"}')
        try:
            if int(self.headers.get("Content-Length", "0")) > 2_000_000:
                return self._send(413, '{"error":"request too large"}')
        except ValueError:
            return self._send(400, '{"error":"invalid content length"}')
        if self.path.startswith("/api/subscriptions/"):
            action = "toggle" if self.headers.get("X-Subscription-Action") == "toggle" else "delete"
            return self._send(200, json.dumps(db_query(action, self.path.rsplit("/", 1)[-1]), ensure_ascii=False))
        if self.path == "/api/bot-users":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                action = "user_remove" if body.get("action") == "remove" else "user_add"
                return self._send(200, json.dumps(db_query(action, body.get("id")), ensure_ascii=False))
            except (ValueError, TypeError): return self._send(400, '{"error":"invalid Telegram user"}')
        if self.path == "/api/config":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                env_path = Path("/data/config/.env"); lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []; allowed = {"TOKEN","MANAGER","API_ID","API_HASH","TELEGRAM_PROXY","ROUTED_BOT_TOKEN","ROUTED_MANAGER"}; values = {k: str(body[k]).strip() for k in allowed if k in body and str(body[k]).strip()}
                seen=set(); out=[]
                for line in lines:
                    key=line.split("=",1)[0].strip() if "=" in line else ""
                    if key in values: out.append(f"{key}={values[key]}"); seen.add(key)
                    else: out.append(line)
                out.extend(f"{k}={v}" for k,v in values.items() if k not in seen); env_path.write_text("\n".join(out)+"\n", encoding="utf-8"); return self._send(200, '{"ok":true,"restartRequired":true}')
            except (ValueError, OSError, TypeError): return self._send(400, '{"error":"invalid Bot configuration"}')
        if self.path in ("/api/cookie", "/api/cookie/validate"):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8", "replace").strip()
            if not raw and self.path == "/api/cookie/validate":
                try:
                    raw = (Path("/data/config/x_session.json")).read_text(encoding="utf-8")
                except OSError:
                    raw = ""
            cookies, parse_errors = parse_cookie_input(raw) if raw else ([], [])
            names = {c["name"] for c in cookies}
            result = {"ok": bool(cookies) and {"auth_token", "ct0"}.issubset(names), "cookieCount": len(cookies),
                      "hasAuthToken": "auth_token" in names, "hasCt0": "ct0" in names, "errors": parse_errors}
            if self.path == "/api/cookie/validate":
                try:
                    bridge_url = os.environ.get("RSSTT_BRIDGE_URL", "http://bridge:1200").rstrip("/")
                    with urllib.request.urlopen(f"{bridge_url}/session", timeout=8) as response:
                        result["bridge"] = json.loads(response.read().decode("utf-8"))
                except Exception as exc:
                    result["bridge"] = {"reachable": False, "error": str(exc)}
                return self._send(200, json.dumps(result, ensure_ascii=False))
            if not result["ok"]:
                return self._send(400, json.dumps({**result, "error": "缺少 auth_token 或 ct0"}, ensure_ascii=False))
            target = Path("/data/config/x_session.json")
            target.write_text(json.dumps({"cookies": cookies, "origins": []}, ensure_ascii=False, indent=2), encoding="utf-8")
            restarted = subprocess.run(["docker", "restart", "360news-bridge"], capture_output=True, text=True, timeout=30).returncode == 0
            return self._send(200, json.dumps({**result, "saved": True, "bridgeRestarted": restarted}, ensure_ascii=False))
        if self.path == "/api/subscriptions":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                return self._send(200, json.dumps(db_query("add", body.get("url"), body.get("title", ""), body.get("category", ""), body.get("managerId", "")), ensure_ascii=False))
            except (ValueError, TypeError): return self._send(400, '{"error":"invalid subscription"}')
        if self.path == "/api/history/clear": return self._send(200, json.dumps(db_query("history_clear")))
        if self.path in ("/api/bridge", "/api/routes"):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                name = "xbridge_config.json" if self.path == "/api/bridge" else "push_routes.json"
                if self.path == "/api/routes":
                    body = {"defaultBot": "secondary" if body.get("defaultBot") == "secondary" else "primary", "rules": [{"pattern": str(x.get("pattern", "")).strip().lower(), "bot": x.get("bot")} for x in body.get("rules", []) if x.get("pattern") and x.get("bot") in ("primary", "secondary")]}
                target = Path("/data/config") / name; target.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"); return self._send(200, '{"ok":true,"restartRequired":true}')
            except (ValueError, OSError, TypeError): return self._send(400, '{"error":"invalid configuration"}')
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

#!/usr/bin/env python3
"""GLORY BOT HOST supervisor.

Runs your unchanged app.py as a child process, installs requirements.txt,
auto-restarts on crash, captures real logs and serves an authenticated dashboard.
"""
from __future__ import annotations

import asyncio, collections, hmac, os, platform, secrets, signal, subprocess, sys, threading, time
from pathlib import Path

import bcrypt, httpx, psutil
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR") or BASE / "data")
DATA.mkdir(parents=True, exist_ok=True)
LOGFILE = DATA / "glory-host.log"
TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()
PASSWORD = os.environ.get("DASHBOARD_PASSWORD") or ""
SESSION_SECRET = os.environ.get("SESSION_SECRET") or ""
SESSION_MAX_AGE = 7 * 86400
COOKIE = "gbh_session"

if not PASSWORD or len(PASSWORD) < 8:
    sys.exit("DASHBOARD_PASSWORD must be set (min 8 chars).")
if not SESSION_SECRET or len(SESSION_SECRET) < 32:
    sys.exit("SESSION_SECRET must be set (min 32 chars).")
PW_HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt())
signer = URLSafeTimedSerializer(SESSION_SECRET, salt="gbh")


def mask(s: str) -> str:
    if TOKEN:
        s = s.replace(TOKEN, "***BOT_TOKEN***")
    return s


class Logs:
    def __init__(self, n: int = 2000):
        self.buf: collections.deque = collections.deque(maxlen=n)
        self.seq = 0
        self.lock = threading.Lock()

    def add(self, src: str, line: str):
        line = mask(line.rstrip("\n"))
        with self.lock:
            self.seq += 1
            item = {"id": self.seq, "t": time.time(), "src": src, "line": line}
            self.buf.append(item)
        try:
            if LOGFILE.exists() and LOGFILE.stat().st_size > 10 * 1024 * 1024:
                LOGFILE.replace(LOGFILE.with_suffix(".log.1"))
            with open(LOGFILE, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{src}] {line}\n")
        except OSError:
            pass

    def since(self, i: int):
        with self.lock:
            return [x for x in self.buf if x["id"] > i]


logs = Logs()


class Bot:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.want_running = True
        self.restarts = 0
        self.last_exit: int | None = None
        self.last_error = ""
        self.deps = {"state": "pending", "detail": ""}
        self.username: str | None = None
        self.lock = threading.Lock()

    def install(self) -> bool:
        self.deps = {"state": "installing", "detail": ""}
        logs.add("pip", "Installing requirements.txt ...")
        p = subprocess.Popen([sys.executable, "-m", "pip", "install", "--no-cache-dir",
                              "--disable-pip-version-check", "-r", str(BASE / "requirements.txt")],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=BASE)
        tail = collections.deque(maxlen=15)
        for line in p.stdout:  # type: ignore
            logs.add("pip", line); tail.append(mask(line.rstrip()))
        rc = p.wait()
        if rc == 0:
            self.deps = {"state": "ok", "detail": "All requirements installed"}
            logs.add("host", "Dependencies installed OK")
            return True
        self.deps = {"state": "error", "detail": "\n".join(tail)}
        logs.add("host", f"pip install FAILED (exit {rc})")
        return False

    def _pump(self, proc):
        for line in proc.stdout:
            logs.add("bot", line)

    def start(self):
        with self.lock:
            self.want_running = True
            if self.proc and self.proc.poll() is None:
                return
            if not TOKEN:
                self.last_error = "BOT_TOKEN is not set"
                logs.add("host", "Cannot start: BOT_TOKEN env var missing")
                return
            env = dict(os.environ, PYTHONUNBUFFERED="1", DATA_DIR=str(DATA))
            env.pop("DASHBOARD_PASSWORD", None); env.pop("SESSION_SECRET", None)
            self.proc = subprocess.Popen([sys.executable, "-u", "app.py"], cwd=BASE, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1, start_new_session=True)
            self.started_at = time.time()
            logs.add("host", f"Started app.py (pid {self.proc.pid})")
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def stop(self, user: bool = True):
        with self.lock:
            if user:
                self.want_running = False
            p = self.proc
            if not p or p.poll() is not None:
                return
            logs.add("host", f"Stopping pid {p.pid} ...")
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(15)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL); p.wait()
            except ProcessLookupError:
                pass
            logs.add("host", f"Stopped (exit {p.returncode})")
            self.started_at = None

    def restart(self):
        self.stop(user=False)
        self.start()

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def watchdog(self):
        backoff = 2
        while True:
            time.sleep(2)
            if self.proc and self.proc.poll() is not None and self.started_at is not None:
                rc = self.proc.returncode
                self.last_exit = rc
                self.started_at = None
                self.last_error = f"app.py exited with code {rc}"
                logs.add("host", f"app.py crashed/exited (code {rc})")
                if self.want_running and self.deps["state"] == "ok":
                    logs.add("host", f"Auto-restart in {backoff}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    self.restarts += 1
                    self.start()
            elif self.running() and self.started_at and time.time() - self.started_at > 120:
                backoff = 2

    def stats(self):
        d = {"online": self.running(), "pid": None, "cpu": None, "mem_mb": None, "uptime": None}
        if d["online"]:
            try:
                pr = psutil.Process(self.proc.pid)  # type: ignore
                kids = pr.children(recursive=True)
                d["pid"] = pr.pid
                d["cpu"] = round(pr.cpu_percent(0.1) + sum(k.cpu_percent(0) for k in kids), 1)
                d["mem_mb"] = round((pr.memory_info().rss + sum(k.memory_info().rss for k in kids)) / 2**20, 1)
                d["children"] = len(kids)
            except psutil.Error:
                pass
            d["uptime"] = int(time.time() - (self.started_at or time.time()))
        return d


bot = Bot()


def boot():
    if bot.install():
        bot.start()
    threading.Thread(target=bot.watchdog, daemon=True).start()


async def fetch_username():
    if not TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = (await c.get(f"https://api.telegram.org/bot{TOKEN}/getMe")).json()
        if r.get("ok"):
            bot.username = r["result"].get("username")
        else:
            logs.add("host", f"Telegram getMe failed: {r.get('description')}")
    except Exception as e:
        logs.add("host", f"Telegram getMe error: {type(e).__name__}")


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.on_event("startup")
async def _startup():
    threading.Thread(target=boot, daemon=True).start()
    await fetch_username()


@app.on_event("shutdown")
def _shutdown():
    bot.stop(user=False)


# ---------- auth ----------
fails: dict[str, list[float]] = {}


def session(req: Request) -> dict | None:
    c = req.cookies.get(COOKIE)
    if not c:
        return None
    try:
        return signer.loads(c, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None


def require(req: Request, csrf: bool = False) -> dict:
    s = session(req)
    if not s:
        raise HTTPException(401, "Not authenticated")
    if csrf and not hmac.compare_digest(req.headers.get("x-csrf-token", ""), s["csrf"]):
        raise HTTPException(403, "Bad CSRF token")
    return s


@app.get("/login")
def login_page():
    return FileResponse(BASE / "dashboard" / "login.html")


@app.post("/login")
def login(req: Request, password: str = Form(...)):
    ip = req.headers.get("x-forwarded-for", req.client.host if req.client else "?").split(",")[0]
    now = time.time()
    fails[ip] = [t for t in fails.get(ip, []) if now - t < 900]
    if len(fails[ip]) >= 5:
        return RedirectResponse("/login?e=locked", 303)
    if not bcrypt.checkpw(password.encode()[:72], PW_HASH):
        fails[ip].append(now)
        return RedirectResponse("/login?e=1", 303)
    fails.pop(ip, None)
    r = RedirectResponse("/", 303)
    r.set_cookie(COOKIE, signer.dumps({"csrf": secrets.token_urlsafe(24)}), max_age=SESSION_MAX_AGE,
                 httponly=True, secure=req.url.scheme == "https" or req.headers.get("x-forwarded-proto") == "https",
                 samesite="strict")
    return r


@app.post("/logout")
def logout(req: Request):
    require(req, csrf=True)
    r = JSONResponse({"ok": True}); r.delete_cookie(COOKIE); return r


@app.get("/")
def index(req: Request):
    if not session(req):
        return RedirectResponse("/login", 303)
    return FileResponse(BASE / "dashboard" / "index.html")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/status")
async def status(req: Request):
    s = require(req)
    if not bot.username:
        await fetch_username()
    st = await asyncio.to_thread(bot.stats)
    return {**st, "username": bot.username, "python": platform.python_version(),
            "deps": bot.deps, "restarts": bot.restarts, "last_exit": bot.last_exit,
            "last_error": bot.last_error, "want_running": bot.want_running, "csrf": s["csrf"]}


@app.post("/api/{action}")
async def control(action: str, req: Request):
    require(req, csrf=True)
    fn = {"start": bot.start, "stop": bot.stop, "restart": bot.restart}.get(action)
    if not fn:
        raise HTTPException(404)
    logs.add("host", f"Dashboard action: {action}")
    await asyncio.to_thread(fn)
    return {"ok": True}


@app.get("/api/logs/stream")
async def stream(req: Request):
    require(req)

    async def gen():
        import json
        last = 0
        while not await req.is_disconnected():
            for item in logs.since(last):
                last = item["id"]
                yield f"data: {json.dumps(item)}\n\n"
            await asyncio.sleep(0.7)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), proxy_headers=True,
                forwarded_allow_ips="*")

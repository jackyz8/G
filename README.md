# GLORY BOT HOST

Runs **your unchanged `app.py`** 24/7 with a password-protected dashboard.

## Where the bot actually runs
Inside one Docker container on **Railway** (or Render). `supervisor.py` is the container's main
process: it runs `pip install -r requirements.txt`, launches `python app.py` as a child process,
restarts it automatically if it crashes, captures its real stdout/stderr, and serves the dashboard
on the same domain. Railway also restarts the whole container if it dies.

Netlify, Vercel and Lovable cannot run this: they only run short-lived serverless functions with
no background processes, no Python long-polling and no persistent disk.

## Deploy on Railway (about 10 minutes)
1. Create a **private** GitHub repo and upload every file in this folder (there is no `.env` here — keep it that way).
2. railway.app → New Project → Deploy from GitHub repo → pick the repo. It builds from the `Dockerfile`.
3. Service → **Settings → Volumes → Add volume**, mount path `/app/data` (keeps your bot's SQLite database, uploaded projects and encryption key across deploys).
4. Service → **Variables**, add (values from your own `.env`):
   - `BOT_TOKEN`, `ADMIN_IDS`, `MAX_PROJECTS_PER_USER`, `PROJECT_MEM_MB`, `PROJECT_CPUS`, `ALLOW_PUBLIC`
   - `SANDBOX_MODE=process` (Railway has no Docker-in-Docker; `docker` mode will fail)
   - `ALLOW_UNSANDBOXED_USERS` — only `true` if you trust every approved user, since `process` mode is not isolated
   - `DASHBOARD_PASSWORD` — a strong password (8+ chars)
   - `SESSION_SECRET` — 48+ random characters (e.g. `python -c "import secrets;print(secrets.token_urlsafe(48))"`)
5. **Settings → Networking → Generate Domain** (port 8000). Open it and sign in.
6. Run **only one replica** — two copies polling the same token cause Telegram `Conflict` errors.

Render: New → Blueprint → select the repo (uses `render.yaml`, paid Starter plan needed for the disk), then fill the secret variables.

## Dashboard
ONLINE/OFFLINE from the real process, @username via Telegram `getMe`, Start/Stop/Restart, live
logs (pip + bot + host events), CPU/RAM of the bot and its child projects, PID, uptime, Python
version, dependency install status with the actual pip error.

## Security
- Secrets live only in platform variables. `.env` is in `.gitignore` and `.dockerignore`.
- The bot token is masked in every log line; `DASHBOARD_PASSWORD`/`SESSION_SECRET` are not passed to `app.py`.
- Login: bcrypt-checked password, signed HttpOnly SameSite=Strict cookie, 5 attempts / 15 min lock.
- Control endpoints need the session and a CSRF token; only start/stop/restart exist. No shell, no uploads in the dashboard. ZIP uploads go through your bot's existing safe extractor.
- If your token was ever shared, rotate it in @BotFather (`/revoke`) and update `BOT_TOKEN`.

## Run locally (Linux/macOS)
```
pip install -r requirements-host.txt
export BOT_TOKEN=... ADMIN_IDS=... SANDBOX_MODE=process DASHBOARD_PASSWORD=... SESSION_SECRET=...
python supervisor.py   # http://localhost:8000
```

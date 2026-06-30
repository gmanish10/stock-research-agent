# Deploying the bot to Hetzner (24/7)

Runs the Telegram bot on an always-on Hetzner VPS under systemd. The bot is **outbound
long-polling** — no domain, no open ports, no reverse proxy. GLM runs via the Ollama **Cloud
API** (no local Ollama needed for it); **llama3.2 runs locally** as the helper LLM (resolver
intent + structural check).

> ⚠️ The Telegram token can be polled by **one process only**. Stop any other bot on this token
> (e.g. the one on your Mac) **before** starting the server: `pkill -f "python bot_telegram.py"`.

## 1. Push the code (your Mac)
`.env`, `runs/`, `reports/` are gitignored — they never leave your machine.
```bash
git check-ignore .env            # must print: .env
git add -A && git commit -m "…"  # if not already committed
gh repo create stock-research-agent --private --source=. --push   # or push to a repo you made
```

## 2. Create the server
- **Hetzner Cloud CAX21** (8 GB Arm, ~€6.5/mo) — or **CX32** (8 GB x86). Avoid 4 GB boxes; the
  llama3.2 3B model needs headroom.
- Image: **Ubuntu 24.04 LTS**. Add your **SSH public key** (password login off).
- **Cloud Firewall**: inbound allow **TCP 22 only**; outbound allow all.
- SSH in as root, create a sudo user named **`bot`**, copy your key to it, then work as `bot`:
  ```bash
  adduser --disabled-password --gecos "" bot && usermod -aG sudo bot
  rsync --archive ~/.ssh/authorized_keys /home/bot/.ssh/ && chown -R bot:bot /home/bot/.ssh
  ```

## 3. Provision (as `bot` on the server)
```bash
REPO_URL=git@github.com:<you>/stock-research-agent.git bash <(curl -fsSL \
  https://raw.githubusercontent.com/<you>/stock-research-agent/main/deploy/setup.sh)
# …or clone first, then: cd /opt/stock-research-agent && REPO_URL=… bash deploy/setup.sh
```
`setup.sh` installs apt deps, Ollama + `llama3.2`, the venv + Python deps, and the systemd service.

## 4. Secrets
Copy your working `.env` to the server (keep the **cloud GLM** route + **local helper LLM**):
```bash
# from your Mac:
scp .env bot@<server-ip>:/opt/stock-research-agent/.env
```
Relevant keys: `OLLAMA_BASE_URL=https://ollama.com/v1`, `MODEL=glm-5.2`,
`LOCAL_BASE_URL=http://localhost:11434/v1`, `LOCAL_MODEL=llama3.2`, `RESOLVER_LLM=1`,
`STRUCT_CHECK=1`, plus `OLLAMA_API_KEY`, `BRAVE_API_KEY`, `TELEGRAM_TOKEN`, `ALLOWED_IDS`.
The server sets `chmod 600 .env`.

## 5. Cutover & start
```bash
# on the Mac: stop the local poller first
pkill -f "python bot_telegram.py"
# on the server:
sudo systemctl start stock-research-bot
journalctl -u stock-research-bot -f      # expect: "Bot running (polling)."  (no 409 conflict)
```

## 6. Verify
1. `systemctl status stock-research-bot` → active (running).
2. Helper LLM: `curl -s localhost:11434/api/tags | grep llama3.2`.
3. GLM cloud auth → 200:
   ```bash
   cd /opt/stock-research-agent && .venv/bin/python - <<'PY'
   import os,requests; from dotenv import load_dotenv; load_dotenv()
   r=requests.post("https://ollama.com/v1/chat/completions",
     headers={"Authorization":"Bearer "+os.environ["OLLAMA_API_KEY"]},
     json={"model":"glm-5.2","messages":[{"role":"user","content":"hi"}]},timeout=60)
   print(r.status_code)
   PY
   ```
4. From Telegram (allow-listed user): `/research NVDA` → ack, then a dated **PDF** in ~1–3 min.
5. Confirm-gate + theme: `/research indian manufacturing sector` → bot asks → reply `yes` → brief.
   Check `ls runs/ reports/` and the log line `resolved … -> theme`.
6. `sudo reboot` → after boot the bot auto-starts; send `/research SOXX`.

## Ops
- **Logs**: `journalctl -u stock-research-bot -f`
- **Update**: `cd /opt/stock-research-agent && git pull && sudo systemctl restart stock-research-bot`
- **Refresh helper model**: `ollama pull llama3.2`
- **Disk**: `runs/` and `reports/` grow over time — prune or back up periodically.

## Cost (approx/month)
Hetzner CAX21 ~€6.5 · Ollama Cloud free tier→~$20 Pro if heavy · Brave ~$5 · llama3.2 local $0.

---

# Run on an Android phone (Termux)

A spare Android phone (e.g. LineageOS) running **Termux** works as the always-on host. Everything
is **cloud** here — GLM *and* the helper model run on Ollama Cloud, so there's no local model to
install. Recommended: **text-only delivery** (`REPORT_PDF=0`) to skip the heavy PDF deps that are
painful to compile on Termux.

> Install Termux from **F-Droid** (the Play Store build is outdated). Keep the phone **plugged in**.

### 1. Base packages
```bash
pkg update && pkg upgrade -y
pkg install -y python git
# numpy/pandas: Termux-native builds (PyPI wheels don't run on Termux's bionic libc)
pkg install -y python-numpy
pip install pandas        # if this stalls/fails: `pkg install tur-repo && pkg install python-pandas`
```

### 2. Get the code (private repo → use a GitHub token)
```bash
git clone https://<GITHUB_PAT>@github.com/gmanish10/stock-research-agent.git
cd stock-research-agent
pip install -r requirements-min.txt     # pure-python deps; reuses the pkg numpy/pandas
```

### 3. Configure `.env` (all cloud, text-only)
```bash
cp .env.example .env && nano .env
```
Set: `OLLAMA_API_KEY`, `OLLAMA_BASE_URL=https://ollama.com/v1`, `MODEL=glm-5.2`,
`BRAVE_API_KEY`, `TELEGRAM_TOKEN`, `ALLOWED_IDS`, and **`REPORT_PDF=0`**.
Leave **`LOCAL_*` unset** — the helper model then runs on Ollama Cloud (`deepseek-v4-flash`)
automatically. Optional: `SAVE_RUNS=0` to avoid filling phone storage.

### 4. Run it (keep awake + auto-restart)
```bash
pkg install -y termux-services tmux
termux-wake-lock                         # stop Android from sleeping the process
tmux new -s bot 'python bot_telegram.py' # detach with Ctrl-b d ; reattach: tmux attach -t bot
```
For start-on-boot, install the **Termux:Boot** addon (F-Droid) and drop a startup script that runs
`termux-wake-lock && python bot_telegram.py`.

### 5. Same rules as the server
- **One poller only** — stop the Mac bot (`pkill -f bot_telegram.py`) before starting the phone.
- Verify: send `/research NVDA` from Telegram → chunked-text report in ~1–3 min.
- Update: `git pull` then restart the tmux session.

### If you want PDFs on the phone later
Install the build deps and the PDF stack (slower, and `cryptography` needs Rust):
`pkg install rust binutils libjpeg-turbo libpng freetype libxml2 libxslt` then
`pip install markdown xhtml2pdf`, and set `REPORT_PDF=1`. Text-only is the reliable default.

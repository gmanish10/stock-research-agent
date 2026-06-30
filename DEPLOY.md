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

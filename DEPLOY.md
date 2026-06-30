# Run the bot 24/7 on an Android phone (Termux)

A spare Android phone (e.g. LineageOS) running **Termux** is the host. Everything is **cloud** —
GLM-5.2 *and* the small helper model both run on Ollama Cloud, so there's **no local model** to
install. Reports are delivered as **dated PDFs**.

> - Install **Termux from F-Droid** (the Play Store build is outdated and breaks).
> - Keep the phone **plugged in** and on Wi-Fi.
> - One poller per token: **stop any other instance** of the bot before starting this one.

## 1. Base packages
```bash
pkg update && pkg upgrade -y
pkg install -y python git
```

## 2. Heavy native deps via Termux (NOT pip)
Termux runs Android's bionic libc, so normal PyPI wheels don't work — install the compiled
packages from Termux's own repos first:
```bash
pkg install -y python-numpy python-lxml python-pillow python-cryptography
pkg install -y tur-repo && pkg install -y python-pandas
```
(If `python-pandas` isn't found, `pip install pandas` also works once `python-numpy` is present —
just slower.)

## 3. Get the code (private repo → GitHub token)
Create a read-only **Personal Access Token** (GitHub → Settings → Developer settings → Fine-grained
tokens, this repo only), then:
```bash
git clone https://<GITHUB_PAT>@github.com/gmanish10/stock-research-agent.git
cd stock-research-agent
```

## 4. Python deps (incl. the PDF stack)
The native deps from step 2 are already satisfied, so this only adds the pure/light ones
(openai, telegram, markdown, xhtml2pdf, reportlab, …):
```bash
pip install -r requirements.txt
```
If a PDF dep fails to build, see **Troubleshooting** below — you can run text-only temporarily.

## 5. Configure `.env` (all cloud, PDF on)
```bash
cp .env.example .env && nano .env
```
Set:
```
OLLAMA_API_KEY=...           # your ollama.com key
OLLAMA_BASE_URL=https://ollama.com/v1
MODEL=glm-5.2
BRAVE_API_KEY=...
TELEGRAM_TOKEN=...           # from @BotFather
ALLOWED_IDS=...              # your Telegram id from @userinfobot (comma-separate for more)
REPORT_PDF=1                 # deliver PDFs
```
**Leave every `LOCAL_*` line unset/commented** — that routes the helper model to Ollama Cloud
(`deepseek-v4-flash`) automatically. No local Ollama needed.

## 6. Run it (stays awake, survives disconnect)
```bash
pkg install -y tmux
termux-wake-lock                          # stop Android from sleeping the process
tmux new -s bot 'python bot_telegram.py'  # detach: Ctrl-b then d ; reattach: tmux attach -t bot
```
Expect `Bot running (polling).` in the log.

**Auto-start on reboot:** install the **Termux:Boot** addon (F-Droid), then:
```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start-bot <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd ~/stock-research-agent && exec python bot_telegram.py
EOF
chmod +x ~/.termux/boot/start-bot
```

## 7. Verify
From Telegram (as an allow-listed user):
- `/research NVDA` → ack, then a dated **PDF** in ~1–3 min.
- `/research indian manufacturing sector` → bot confirms → reply `yes` → thematic-brief PDF.

## Ops
- **Logs:** `tmux attach -t bot` (or run without tmux to watch live).
- **Update:** `cd ~/stock-research-agent && git pull` then restart the tmux session.
- **Storage:** `runs/` and `reports/` grow over time — delete old files, or set `SAVE_RUNS=0`.

## Troubleshooting
- **A PDF dep won't build** (e.g. `reportlab`/`cryptography`): install Rust + build tools and retry:
  `pkg install -y rust binutils libjpeg-turbo libpng freetype libxml2 libxslt` then
  `pip install -r requirements.txt`. As a stopgap, set `REPORT_PDF=0` to send text until PDFs build.
- **`409 Conflict` in the log:** another process is polling the same token — stop it (only one bot
  per token).
- **Private clone asks for a password:** your PAT is wrong/expired — regenerate it and re-clone.

## Cost (approx/month)
Phone host **$0** · Ollama Cloud free tier → ~$20 Pro if heavy · Brave ~$5.

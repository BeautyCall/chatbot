# Deploying to staging (plain Linux, no Docker)

Run everything below over SSH on the staging server unless marked **[local]**.

```
Slack ──HTTPS──> existing reverse proxy ──> slack-bot 127.0.0.1:3000 ──> 9router 127.0.0.1:20128
```

Neither service listens on a public interface. Isolation comes from dedicated unprivileged
users plus the systemd sandboxing and cgroup limits in the unit files here.

## 1. Users and directories

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin slackbot
sudo useradd --system --create-home --shell /usr/sbin/nologin ninerouter
sudo mkdir -p /opt/slack-bot /var/lib/slack-bot
sudo chown slackbot:slackbot /var/lib/slack-bot
sudo chmod 750 /var/lib/slack-bot
```

## 2. 9router

**Needs Node >= 20.9**, not 18. The package's `engines` field says `>=18.0.0` but is
stale: it bundles Next.js 16.2.1, which requires `>=20.9.0`. Give `ninerouter` its own
Node so the system one (used by other services) is untouched.

```bash
# Node, private to the ninerouter user
sudo -u ninerouter bash -c '
  ARCH=$(uname -m); case "$ARCH" in x86_64) A=x64;; aarch64) A=arm64;; *) echo "unsupported: $ARCH"; exit 1;; esac
  cd ~ && curl -fsSLO "https://nodejs.org/dist/v22.11.0/node-v22.11.0-linux-$A.tar.xz" &&
  tar -xJf node-v22.11.0-linux-$A.tar.xz && mv node-v22.11.0-linux-$A nodejs &&
  rm node-v22.11.0-linux-$A.tar.xz && ~/nodejs/bin/node --version'

# 9router into a per-user npm prefix (never /usr/local)
sudo -u ninerouter bash -c '
  export PATH=$HOME/nodejs/bin:$PATH
  npm config set prefix ~/.npm-global
  npm install -g 9router'

sudo install -m644 /opt/slack-bot/deploy/9router.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now 9router
systemctl status 9router --no-pager | head -5
```

**Connect providers** — the dashboard is a web UI, so tunnel to it from your Mac:

```bash
# [local]
ssh -L 20128:127.0.0.1:20128 you@staging
```

Then open <http://localhost:20128/dashboard>, connect your providers, and copy the API key.

> **Do not copy `~/.9router/db/data.sqlite` from your Mac.** It contains live OAuth access
> tokens bound to your personal accounts. Authenticate fresh on the server.

Confirm the model the bot expects actually resolves here:

```bash
curl -s localhost:20128/v1/models | grep -o '"id":"[^"]*"' | head -20
```

`.env` pins `LLM_MODEL=gc/gemini-2.5-flash`; that prefix only exists if you connect the same
provider. Adjust `LLM_MODEL` if you connect different ones.

## 3. The bot

```bash
sudo -u slackbot git clone https://github.com/BeautyCall/chatbot.git /opt/slack-bot
cd /opt/slack-bot
sudo -u slackbot python3 -m venv .venv
sudo -u slackbot .venv/bin/pip install -r requirements.txt
```

Create `/opt/slack-bot/.env` (mode 600, owned by `slackbot`) from `.env.example`. Copy the
values from your current local `.env`, except:

```ini
LLM_BASE_URL=http://127.0.0.1:20128/v1
LLM_API_KEY=<key from the 9router dashboard>
```

`NGROK_AUTHTOKEN` is no longer needed — delete that line.

```bash
sudo chmod 600 /opt/slack-bot/.env
sudo chown slackbot:slackbot /opt/slack-bot/.env
sudo install -m644 /opt/slack-bot/deploy/slack-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now slack-bot
```

## 4. Reverse proxy

Paste `nginx-slack-bot.conf` into the existing HTTPS `server{}` block for your staging domain,
then `sudo nginx -t && sudo systemctl reload nginx`.

## 5. Point Slack at staging

Slack app → **Event Subscriptions** → Request URL → `https://<staging-domain>/slack/events`.
It re-verifies on save, so the bot must already be running.

**Keep Socket Mode OFF.** Enabling it makes Slack deliver over a WebSocket and ignore the
Request URL entirely — no events reach this bot, with no error anywhere.

Then on the Mac: stop the s6 slack-bot service and kill ngrok, so two instances never serve
the same Slack app at once.

## Verification

```bash
systemctl status 9router slack-bot                     # both active
curl -s -o /dev/null -w '%{http_code}\n' -XPOST \
     -d '{}' -H 'Content-Type: application/json' \
     http://127.0.0.1:3000/slack/events                # 403 = signature check live
curl -s -o /dev/null -w '%{http_code}\n' -XPOST \
     -d '{}' -H 'Content-Type: application/json' \
     https://<staging-domain>/slack/events             # 403 = proxy path works
journalctl -u slack-bot -f                             # [MEMORY] restored history for N user(s)
systemd-cgtop -1 | grep slack-bot                      # well under 512M / 50%
```

Post a message in the mitra channel; the journal should show
`[INCOMING]` → `[RAG] mode=rag sections=[…]` → `[TOKENS]` → `[REPLY]`.

## Rollback

```bash
sudo systemctl stop slack-bot
sudo -u slackbot git -C /opt/slack-bot checkout <previous-sha>
sudo systemctl start slack-bot
```

Runtime state lives in `/var/lib/slack-bot`, independent of the checkout, so reverting code
never touches corrections or conversation history. To fall back to the Mac entirely, point the
Slack Request URL back at the ngrok domain and restart the s6 service there.

## Operational notes

- **`-w 1` in the unit is mandatory.** Conversation history, debounce timers, handoff state and
  cooldowns are all in process memory; a second worker gets its own copy and the debounce and
  handoff logic break. Scale with `--threads`, not workers.
- Measured footprint: **61 MB RSS idle, 0.3% CPU idle, 1.18 ms CPU per message**. 1,000
  messages/day costs about 1.2 s of CPU in total.
- `memory.json` is rewritten on every message under a lock via atomic temp-file replace.
  History is capped at 20 messages/user and users idle for 30 days are pruned, so the file
  stays bounded.

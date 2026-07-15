# CLT Houzcall Slack Bot

Bot CS Flask untuk Slack: jawab pertanyaan mitra via LLM (SOP), dengan handoff ke CLT manusia.

## File structure

```
slack-bot/
├── app.py            # Flask server (port 3000), Slack Events API
├── llm.py            # OpenAI-compatible client + system prompt
├── requirements.txt
├── .env.example      # Template credentials
├── .env              # Credentials lokal (jangan commit)
└── memory.json       # Runtime memory (auto-created, jangan commit)
```

## 1. Slack App setup

1. Buka [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch.
2. **OAuth & Permissions** → Bot Token Scopes, tambahkan minimal:
   - `chat:write`
   - `channels:history` / `groups:history` / `im:history` / `mpim:history` (sesuai channel yang dipakai)
   - `app_mentions:read` (kalau dipakai di channel dengan mention)
3. Install app ke workspace → copy **Bot User OAuth Token** (`xoxb-...`).
4. **Basic Information** → copy **Signing Secret**.
5. Invite bot ke channel CS dan channel mitra (`/invite @BotName`).
6. Copy **Channel ID** channel CLT (klik kanan channel → View channel details → ID di paling bawah) → isi `CS_CHANNEL`.

## 2. Local setup

```bash
git clone https://github.com/BeautyCall/chatbot.git
cd chatbot

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — isi semua variable wajib
```

### Variable `.env`

| Variable | Wajib | Keterangan |
|----------|-------|------------|
| `SLACK_BOT_TOKEN` | Ya | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | Ya | Signing Secret dari Slack App |
| `CS_CHANNEL` | Ya | Channel ID handoff CLT |
| `LLM_BASE_URL` | Ya | Base URL OpenAI-compatible (`.../v1`) |
| `LLM_API_KEY` | Ya | API key LLM |
| `LLM_MODEL` | Ya | Nama model, contoh `gc/gemini-2.5-flash` |

## 3. Jalankan bot

```bash
source .venv/bin/activate
python app.py
```

Bot listen di `http://0.0.0.0:3000`. Endpoint Slack: `POST /slack/events`.

## 4. Expose ke internet (ngrok)

Slack Events API butuh HTTPS publik.

```bash
# Install ngrok: https://ngrok.com/download
ngrok config add-authtoken <token-kamu>
ngrok http 3000
```

Copy URL publik (contoh `https://xxxx.ngrok-free.app`).

Di Slack App → **Event Subscriptions**:

1. Enable Events.
2. Request URL: `https://xxxx.ngrok-free.app/slack/events` (harus verified).
3. Subscribe to bot events, minimal:
   - `message.channels`
   - `message.groups`
   - `message.im`
   - `message.mpim`
   - (opsional) `app_mention`
4. Save changes.

> URL ngrok gratis berubah tiap restart — update Request URL di Slack setiap kali ganti URL.

## 5. Jalankan via Docker / Hermes (opsional)

Kalau folder ini di-mount ke container Hermes (`/workspace/slack-bot`):

```bash
cd /workspace/slack-bot
.venv/bin/python3 app.py
```

Pastikan `LLM_BASE_URL` memakai `http://host.docker.internal:20128/v1` (bukan `localhost`) supaya LLM di host Mac bisa dijangkau dari dalam container.

Port host: map `3000:3000`, lalu ngrok ke `localhost:3000`.

## Commands di Slack

**Mitra**

- Ketik pertanyaan seputar layanan
- `lupa` — reset riwayat percakapan
- `hubungi CLT` / keyword CS — handoff ke manusia
- `help` — daftar perintah

**CLT admin**

- `aktifkan bot` — aktifkan lagi bot untuk mitra
- `:koreksi: [jawaban]` — koreksi jawaban bot
- `tambah clt @user` / `hapus clt @user` / `list clt`
- `stats`
- `reset semua`

## CS handoff

Keyword: `cs`, `customer service`, `manusia`, `human`, `operator`, `hubungi cs`, dll. Notifikasi dikirim ke `CS_CHANNEL`.

## Notes

- Jangan commit `.env`, `memory.json`, atau `bot.log`.
- History dipotong (beberapa pesan terakhir per user).
- Token usage per reply dicatat di log sebagai `[TOKENS] ...`.
- System prompt besar → tiap chat biasanya ~5k token input.

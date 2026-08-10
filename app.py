import os
import json
import hashlib
import hmac
import time
import threading
import tempfile
import atexit
import signal
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from dotenv import load_dotenv
from llm import chat, build_messages

load_dotenv()

app = Flask(__name__)
slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
CS_CHANNEL = os.environ["CS_CHANNEL"]
BOT_USER_ID = slack.auth_test()["user_id"]

# Runtime state lives in DATA_DIR when set, so a deployed checkout stays
# read-only and `git pull` never collides with memory.json / bot.log.
# Falls back to the source directory for local development.
DATA_DIR = os.getenv("DATA_DIR") or os.path.dirname(__file__)
os.makedirs(DATA_DIR, exist_ok=True)

MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")

LOG_FILE = os.path.join(DATA_DIR, "bot.log")

conversation_history: dict[str, list[dict]] = {}
handoff_done: set[str] = set()
processed_events: set[str] = set()

# ── Debounce / Message Buffer ────────────────────────────────────
user_buffers: dict[str, dict] = {}
user_cooldowns: dict[str, float] = {}  # user_id → timestamp cooldown berakhir
DEBOUNCE_SECONDS = 8.0   # tunggu 8 detik sebelum reply
COOLDOWN_SECONDS = 3.0  # setelah reply, ignore pesan baru 3 detik
MIN_CHARS = 2            # ignore pesan terlalu pendek (bukan command)


def log(msg: str):
    """Write log message to file and stdout."""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── Debounce helpers ─────────────────────────────────────────────

def flush_buffer(user_id: str):
    """Called after debounce timer expires — process all buffered messages as one."""
    buf = user_buffers.pop(user_id, None)
    if not buf or not buf["buffer"]:
        return

    channel = buf["channel"]
    combined = " ".join(buf["buffer"])
    log(f"[DEBOUNCE FLUSH] user={user_id} messages={len(buf['buffer'])} combined={combined[:100]}")

    # Process in background
    threading.Thread(target=_do_process_and_cooldown, args=(user_id, channel, combined), daemon=True).start()


def _do_process_and_cooldown(user_id: str, channel: str, combined: str):
    """Process message and set cooldown after reply."""
    try:
        _do_process(user_id, channel, combined)
    finally:
        # Set cooldown — new messages during this period are silently dropped
        user_cooldowns[user_id] = time.time() + COOLDOWN_SECONDS
        log(f"[COOLDOWN] user={user_id} for {COOLDOWN_SECONDS}s")


def buffer_message(user_id: str, channel: str, text: str) -> bool:
    """
    Add message to user's debounce buffer.
    Returns True if message should be processed immediately (commands).
    Returns False if buffered or dropped.
    """
    now = time.time()

    # Check cooldown — if user just got a reply, silently buffer/drop
    cooldown_until = user_cooldowns.get(user_id, 0)
    if now < cooldown_until:
        remaining = int(cooldown_until - now)
        log(f"[COOLDOWN SKIP] user={user_id} {remaining}s remaining, dropped: {text[:60]}")
        return False

    # Commands bypass debounce — process immediately
    clean = text.strip().lower()
    immediate_cmds = ["help", "lupa", "hubungi clt", "aktifkan bot", "stats",
                      "list clt", "reset semua"]
    if clean in immediate_cmds or clean.startswith("tambah clt") or \
       clean.startswith("hapus clt") or clean.startswith(":koreksi:"):
        return True  # process immediately

    # Add to buffer
    if user_id in user_buffers:
        user_buffers[user_id]["timer"].cancel()
        user_buffers[user_id]["buffer"].append(text)
        user_buffers[user_id]["channel"] = channel
        log(f"[DEBOUNCE BUFFER] user={user_id} buffered={len(user_buffers[user_id]['buffer'])} text={text[:60]}")
    else:
        user_buffers[user_id] = {
            "buffer": [text],
            "channel": channel,
        }
        log(f"[DEBOUNCE START] user={user_id} text={text[:60]}")

    # Start debounce timer
    t = threading.Timer(DEBOUNCE_SECONDS, flush_buffer, args=[user_id])
    user_buffers[user_id]["timer"] = t
    t.start()

    return False


# ── Memory helpers ──────────────────────────────────────────────

# Every message does a read-modify-write of the whole file from a daemon
# thread. Without this lock two concurrent mitra lose each other's updates;
# without the atomic replace below, a crash mid-write truncates the file and
# load_memory() silently returns empty -- wiping every CLT correction.
_memory_lock = threading.RLock()

MAX_HISTORY_PER_USER = 20      # prompt only ever uses the last 10
HISTORY_RETENTION_DAYS = 30    # drop users idle longer than this


def load_memory() -> dict:
    """Load persistent memory from JSON file."""
    with _memory_lock:
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data.setdefault("history", {})
                data.setdefault("corrections", [])
                data.setdefault("last_seen", {})
                return data
            except (json.JSONDecodeError, IOError) as e:
                log(f"[MEMORY] load failed ({e}) - starting empty")
        return {"history": {}, "corrections": [], "last_seen": {}}


def _prune_memory(data: dict):
    """Cap per-user history and drop users idle beyond the retention window."""
    cutoff = time.time() - HISTORY_RETENTION_DAYS * 86400
    last_seen = data.setdefault("last_seen", {})
    for uid in list(data.get("history", {})):
        msgs = data["history"][uid]
        if len(msgs) > MAX_HISTORY_PER_USER:
            data["history"][uid] = msgs[-MAX_HISTORY_PER_USER:]
        if last_seen.get(uid, cutoff + 1) < cutoff:
            data["history"].pop(uid, None)
            last_seen.pop(uid, None)


def save_memory(data: dict):
    """Atomically persist memory: temp file + os.replace, never a bare open('w')."""
    with _memory_lock:
        _prune_memory(data)
        directory = os.path.dirname(MEMORY_FILE) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".memory.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, MEMORY_FILE)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def persist_history(user_id: str):
    """Save user's conversation history to persistent memory."""
    with _memory_lock:
        memory = load_memory()
        memory["history"][user_id] = conversation_history.get(user_id, [])[-MAX_HISTORY_PER_USER:]
        memory.setdefault("last_seen", {})[user_id] = time.time()
        save_memory(memory)


def restore_history():
    """Rehydrate conversation_history at boot.

    Previously memory["history"] was written on every message but never read
    back, so context died on each restart and the stored copy was immediately
    overwritten with the fresh (empty) in-memory list.
    """
    memory = load_memory()
    restored = 0
    for uid, msgs in memory.get("history", {}).items():
        if msgs:
            conversation_history[uid] = list(msgs)[-MAX_HISTORY_PER_USER:]
            restored += 1
    if restored:
        log(f"[MEMORY] restored history for {restored} user(s)")


def add_correction(user_question: str, bot_answer: str, corrected: str):
    """Add a correction entry so bot learns from mistakes."""
    with _memory_lock:
        memory = load_memory()
        memory["corrections"].append({
            "user_question": user_question,
            "bot_answer": bot_answer,
            "corrected": corrected,
            "timestamp": time.time()
        })
        # Keep only last 50 corrections to avoid prompt bloat
        memory["corrections"] = memory["corrections"][-50:]
        save_memory(memory)


def get_corrections_list() -> list:
    """Load corrections list from memory for few-shot injection."""
    memory = load_memory()
    return memory.get("corrections", [])


def remove_last_correction() -> dict | None:
    """Pop the most recent correction (undo for a mis-paired :koreksi:)."""
    with _memory_lock:
        memory = load_memory()
        if not memory.get("corrections"):
            return None
        removed = memory["corrections"].pop()
        save_memory(memory)
        return removed


def _clean_correction(raw: str) -> str:
    """Strip the [ ] wrapper from the documented `:koreksi: [jawaban]` syntax.

    The README shows brackets as placeholder notation, so CLT types them
    literally. Only strips when the leading [ closes at the very end, so a
    reply that genuinely contains brackets is left alone.
    """
    text = raw.strip()
    while len(text) >= 2 and text[0] == "[" and text[-1] == "]":
        depth = 0
        for i, ch in enumerate(text):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0 and i != len(text) - 1:
                    return text
        text = text[1:-1].strip()
    return text


def is_clt(user_id: str) -> bool:
    """Check if user is a CLT member."""
    memory = load_memory()
    return user_id in memory.get("clt_users", [])


# ── Slack helpers ────────────────────────────────────────────────

def verify_slack_signature(req) -> bool:
    ts = req.headers.get("X-Slack-Request-Timestamp", "")
    if not ts:
        return False
    try:
        if abs(time.time() - int(ts)) > 60 * 5:
            return False
    except (ValueError, TypeError):
        return False
    sig_base = f"v0:{ts}:{req.get_data(as_text=True)}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), sig_base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, req.headers.get("X-Slack-Signature", ""))


def handoff_to_clt(user_id: str, channel: str, history: list[dict]):
    summary = "\n".join(
        f"{'Mitra' if m['role']=='user' else 'Bot'}: {m['content']}"
        for m in history[-10:]
    )
    slack.chat_postMessage(
        channel=CS_CHANNEL,
        text=f"⚠️ *Handoff dari bot ke CLT*\nMitra: <@{user_id}>\nChannel: <#{channel}>\n\n*Riwayat chat:*\n```{summary}```",
    )
    slack.chat_postMessage(
        channel=channel,
        text="Baik, saya akan menghubungkan kamu dengan CLT kami. Mohon tunggu sebentar ya! 🙏",
    )


def _do_process(user_id: str, channel: str, text: str):
    """Core message processing (was process_message). Called after debounce or immediately for commands."""
    try:
        log(f"[INCOMING] user={user_id} channel={channel} text={text[:100]}")
        # ── Parse mention from bot (strip <@BOT_ID> from text) ──
        clean = text.strip()
        if f"<@{BOT_USER_ID}>" in clean:
            clean = clean.replace(f"<@{BOT_USER_ID}>", "").strip()

        # ── Help (all users) ──
        if clean.lower() == "help":
            slack.chat_postMessage(
                channel=channel,
                text=(
                    "*🤖 Daftar Perintah Bot*\n\n"
                    "*Mitra:*\n"
                    "• Ketik pertanyaan seputar layanan Houzcall\n"
                    "• `lupa` — Reset riwayat percakapan\n"
                    "• `hubungi CLT` — Terhubung dengan CLT\n\n"
                    "*CLT Admin:*\n"
                    "• `aktifkan bot` — Aktifkan kembali bot untuk mitra\n"
                    "• `:koreksi: [jawaban]` — Koreksi jawaban bot\n"
                    "• `tambah clt @user` — Tambah member CLT\n"
                    "• `hapus clt @user` — Hapus member CLT\n"
                    "• `list clt` — Lihat daftar CLT\n"
                    "• `stats` — Statistik bot\n"
                    "• `reset semua` — Reset semua riwayat (hati-hati!)"
                ),
            )
            return

        # ── Reactivate bot (CLT only) ──
        if clean.lower() == "aktifkan bot":
            if not is_clt(user_id):
                return
            # Find which user to reactivate — check recent handoff
            if handoff_done:
                last_handoff = next(iter(handoff_done))
                handoff_done.discard(last_handoff)
                conversation_history.pop(last_handoff, None)
                persist_history(last_handoff)
                slack.chat_postMessage(
                    channel=channel,
                    text=f"✅ Bot diaktifkan kembali untuk <@{last_handoff}>. Silakan tanyakan apa saja ya! 😊",
                )
            else:
                slack.chat_postMessage(channel=channel, text="ℹ️ Tidak ada user yang sedang di-handoff.")
            return

        # ── Stats (CLT only) ──
        if clean.lower() == "stats":
            if not is_clt(user_id):
                return
            memory = load_memory()
            total_users = len(memory.get("history", {}))
            total_corrections = len(memory.get("corrections", {}))
            total_handoffs = len(handoff_done)
            active_chats = len(conversation_history)
            slack.chat_postMessage(
                channel=channel,
                text=(
                    "*📊 Statistik Bot*\n\n"
                    f"• Total user pernah chat: {total_users}\n"
                    f"• Total koreksi tersimpan: {total_corrections}\n"
                    f"• Sedang di-handoff: {total_handoffs}\n"
                    f"• Percakapan aktif: {active_chats}\n"
                    f"• CLT members: {len(memory.get('clt_users', []))}"
                ),
            )
            return

        # ── List CLT (CLT only) ──
        if clean.lower() == "list clt":
            if not is_clt(user_id):
                return
            memory = load_memory()
            clt_list = memory.get("clt_users", [])
            if not clt_list:
                slack.chat_postMessage(channel=channel, text="ℹ️ Belum ada CLT yang terdaftar.")
            else:
                members = "\n".join(f"• <@{uid}>" for uid in clt_list)
                slack.chat_postMessage(channel=channel, text=f"*👥 Daftar CLT:*\n{members}")
            return

        # ── Tambah CLT (CLT only) ──
        # Format: tambah clt @username  (Slack akan kirim <@USER_ID>)
        if clean.lower().startswith("tambah clt"):
            if not is_clt(user_id):
                return
            # Extract user ID from <@USER_ID>
            import re
            match = re.search(r"<@([A-Z0-9]+)>", clean)
            if not match:
                slack.chat_postMessage(channel=channel, text="❌ Format: `tambah clt @username`")
                return
            new_uid = match.group(1)
            with _memory_lock:
                memory = load_memory()
                already = new_uid in memory["clt_users"]
                if not already:
                    memory["clt_users"].append(new_uid)
                    save_memory(memory)
            if already:
                slack.chat_postMessage(channel=channel, text=f"ℹ️ <@{new_uid}> sudah terdaftar sebagai CLT.")
            else:
                slack.chat_postMessage(channel=channel, text=f"✅ <@{new_uid}> ditambahkan sebagai CLT.")
            return

        # ── Hapus CLT (CLT only) ──
        if clean.lower().startswith("hapus clt"):
            if not is_clt(user_id):
                return
            import re
            match = re.search(r"<@([A-Z0-9]+)>", clean)
            if not match:
                slack.chat_postMessage(channel=channel, text="❌ Format: `hapus clt @username`")
                return
            rem_uid = match.group(1)
            with _memory_lock:
                memory = load_memory()
                if rem_uid not in memory["clt_users"]:
                    outcome = "absent"
                elif len(memory["clt_users"]) <= 1:
                    outcome = "last"
                else:
                    memory["clt_users"].remove(rem_uid)
                    save_memory(memory)
                    outcome = "removed"
            if outcome == "absent":
                slack.chat_postMessage(channel=channel, text=f"ℹ️ <@{rem_uid}> tidak terdaftar.")
            elif outcome == "last":
                slack.chat_postMessage(channel=channel, text="❌ Tidak bisa hapus CLT terakhir.")
            else:
                slack.chat_postMessage(channel=channel, text=f"✅ <@{rem_uid}> dihapus dari CLT.")
            return

        # ── Reset semua (CLT only) ──
        if clean.lower() == "reset semua":
            if not is_clt(user_id):
                return
            conversation_history.clear()
            handoff_done.clear()
            with _memory_lock:
                memory = load_memory()
                memory["history"] = {}
                memory["last_seen"] = {}
                save_memory(memory)
            slack.chat_postMessage(
                channel=channel,
                text="🗑️ Semua riwayat percakapan & handoff telah di-reset.",
            )
            return

        # ── Block non-CLT from using CLT-only commands ──
        clt_only_cmds = ["aktifkan bot", "stats", "list clt", "reset semua", "hapus koreksi terakhir"]
        if clean.lower() in clt_only_cmds and not is_clt(user_id):
            return

        if user_id in handoff_done:
            return

        # ── Reset history (mitra command) ──
        if clean.lower() == "lupa":
            conversation_history.pop(user_id, None)
            persist_history(user_id)
            slack.chat_postMessage(
                channel=channel,
                text="Oke, saya akan mengobrol dari awal ya! 😊",
            )
            return

        # ── Correction (CLT reply) ──
        if clean.startswith(":koreksi:"):
            if not is_clt(user_id):
                return
            corrected = _clean_correction(clean[9:])
            if corrected:
                history = conversation_history.get(user_id, [])
                last_bot_msg = ""
                last_user_msg = ""
                for m in reversed(history):
                    if m["role"] == "assistant" and not last_bot_msg:
                        last_bot_msg = m["content"]
                    if m["role"] == "user" and not last_user_msg:
                        last_user_msg = m["content"]
                    if last_bot_msg and last_user_msg:
                        break
                if not last_user_msg or not last_bot_msg:
                    log(f"[CORRECTION] user={user_id} REJECTED (no prior Q&A pair)")
                    slack.chat_postMessage(
                        channel=channel,
                        text=(
                            "⚠️ Koreksi *tidak* tersimpan — belum ada pasangan "
                            "pertanyaan mitra + jawaban bot untuk dikoreksi.\n"
                            "Kirim pertanyaannya dulu, tunggu bot menjawab, "
                            "baru ketik `:koreksi: jawaban yang benar`."
                        ),
                    )
                    return
                add_correction(
                    user_question=last_user_msg,
                    bot_answer=last_bot_msg,
                    corrected=corrected,
                )
                log(
                    f"[CORRECTION] user={user_id} q={last_user_msg[:60]!r} "
                    f"corrected={corrected[:80]!r}"
                )
                slack.chat_postMessage(
                    channel=channel,
                    text=(
                        "✅ Koreksi tersimpan!\n"
                        f"• Pertanyaan yang dikoreksi: _{last_user_msg[:200]}_\n"
                        f"• Jawaban baru: _{corrected[:300]}_\n"
                        "Kalau pertanyaannya salah, ketik `hapus koreksi terakhir`."
                    ),
                )
            return

        # ── Typo guard: "koreksi:" tanpa titik dua di depan ──
        if clean.lower().startswith("koreksi:"):
            if not is_clt(user_id):
                return
            log(f"[CORRECTION] user={user_id} REJECTED (missing leading colon)")
            slack.chat_postMessage(
                channel=channel,
                text=(
                    "⚠️ Koreksi *tidak* tersimpan — perintahnya harus diawali "
                    "titik dua: `:koreksi: jawaban yang benar`"
                ),
            )
            return

        # ── Undo last correction ──
        if clean.lower() == "hapus koreksi terakhir":
            if not is_clt(user_id):
                return
            removed = remove_last_correction()
            if removed:
                log(f"[CORRECTION] user={user_id} REMOVED q={removed.get('user_question','')[:60]!r}")
                slack.chat_postMessage(
                    channel=channel,
                    text=(
                        "🗑️ Koreksi terakhir dihapus:\n"
                        f"• Pertanyaan: _{removed.get('user_question','')[:200]}_"
                    ),
                )
            else:
                slack.chat_postMessage(channel=channel, text="Belum ada koreksi tersimpan.")
            return

        # ── Normal flow ──
        if user_id not in conversation_history:
            conversation_history[user_id] = []

        conversation_history[user_id].append({"role": "user", "content": clean})

        # ── Handoff keywords
        clt_keywords = ["hubungi clt", "customer loyalty", "manusia", "human", "operator"]
        if any(kw in clean.lower() for kw in clt_keywords):
            handoff_done.add(user_id)
            log(f"[HANDOFF] user={user_id} channel={channel}")
            handoff_to_clt(user_id, channel, conversation_history[user_id])
            persist_history(user_id)
            return

        # ── Auto-eskalasi: sudah di lokasi tapi belum mulai / sudah lama nunggu
        eskalasi_keywords = ["sudah 15 menit", "sudah lama", "lama nunggu", "belum mulai",
                             "sudah di lokasi", "sudah tiba", "nunggu lama"]
        eskalasi_match = any(kw in clean.lower() for kw in eskalasi_keywords)
        if eskalasi_match:
            log(f"[ESKALASI] user={user_id} channel={channel} text={clean[:80]}")
            # Kirim notif ke CLT channel dulu
            slack.chat_postMessage(
                channel=CS_CHANNEL,
                text=(
                    f"⚠️ *Eskalasi Otomatis - Mitra Butuh Bantuan*\n"
                    f"Mitra: <@{user_id}>\n"
                    f"Channel: <#{channel}>\n"
                    f"Pesan: _{clean}_\n\n"
                    f"Mitra sudah di lokasi/tunggu lama tapi layanan belum mulai. Mohon segera di-follow up."
                ),
            )
            # Kirim notif ke mitra
            eskalasi_reply = (
                "Baik Mba/Mas, mohon maaf atas kendalanya. "
                "Saya sudah informasikan ke tim kami untuk segera membantu Mba/Mas. "
                "Mohon ditunggu sebentar ya.\n\n"
                "_Ketik *hubungi CLT* jika ingin langsung terhubung dengan CLT kami._"
            )
            conversation_history[user_id].append({"role": "user", "content": clean})
            conversation_history[user_id].append({"role": "assistant", "content": eskalasi_reply})
            persist_history(user_id)
            log(f"[REPLY] user={user_id} reply={eskalasi_reply[:100]}")
            slack.chat_postMessage(channel=channel, text=eskalasi_reply)
            return

        # Build messages with corrections + call LLM
        messages, rag_meta = build_messages(
            conversation_history[user_id],
            corrections=get_corrections_list(),
        )
        log(
            f"[RAG] user={user_id} mode={rag_meta['mode']} "
            f"sections={rag_meta.get('sections')} "
            f"top={rag_meta.get('top_score')} "
            f"koreksi={rag_meta.get('corrections_used')}/{rag_meta.get('corrections_total')}"
        )
        reply, usage = chat(messages)
        if usage:
            cached = usage.get("cached_tokens", 0)
            cached_note = f" cached={cached}" if cached else ""
            log(
                f"[TOKENS] user={user_id} input={usage['prompt_tokens']} "
                f"output={usage['completion_tokens']} total={usage['total_tokens']}{cached_note}"
            )
        conversation_history[user_id].append({"role": "assistant", "content": reply})
        persist_history(user_id)
        log(f"[REPLY] user={user_id} reply={reply[:100]}")
        slack.chat_postMessage(channel=channel, text=reply)

    except Exception as e:
        log(f"[ERROR] {e}")
        slack.chat_postMessage(
            channel=os.environ["CS_CHANNEL"],
            text=f"⚠️ Error di <#{channel}>:\n{str(e)[:300]}"
        )


# ── Routes ───────────────────────────────────────────────────────

@app.route("/slack/events", methods=["POST"])
def slack_events():
    if not verify_slack_signature(request):
        return jsonify({"error": "invalid signature"}), 403

    data = request.json

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    event = data.get("event", {})
    event_id = data.get("event_id", "")

    # Deduplicate retries
    if event_id in processed_events:
        return jsonify({"ok": True})
    processed_events.add(event_id)

    if event.get("type") != "message" or event.get("bot_id") or event.get("subtype"):
        return jsonify({"ok": True})

    user_id = event.get("user")
    channel = event.get("channel")
    text = event.get("text", "").strip()

    # Abaikan pesan dari bot sendiri
    if user_id == BOT_USER_ID or not text:
        return jsonify({"ok": True})

    # Skip pesan terlalu pendek (bukan command)
    clean = text.strip().lower()
    immediate_cmds = {"help", "lupa", "hubungi clt", "aktifkan bot", "stats",
                      "list clt", "reset semua"}
    is_command = clean in immediate_cmds or clean.startswith("tambah clt") or \
                 clean.startswith("hapus clt") or clean.startswith(":koreksi:")
    if not is_command and len(text.strip()) < MIN_CHARS:
        log(f"[SKIP] user={user_id} text too short: '{text}'")
        return jsonify({"ok": True})

    # Handoff users bypass everything
    if user_id in handoff_done:
        return jsonify({"ok": True})

    # Debounce: buffer message, process after timer expires
    immediate = buffer_message(user_id, channel, text)
    if immediate:
        # Commands are processed immediately in a background thread
        threading.Thread(target=_do_process, args=(user_id, channel, text), daemon=True).start()

    return jsonify({"ok": True})


def notify_shutdown(signum=None, frame=None):
    log(f"🔴 Bot shutting down (signal={signum})...")
    try:
        slack.chat_postMessage(
            channel=CS_CHANNEL,
            text="🔴 *Bot CLT Houzcall offline.* Mohon tunggu beberapa saat.",
        )
    except Exception:
        pass
    if signum:
        os._exit(0)


def _startup():
    """Boot sequence. Runs at import, not under __main__.

    Under gunicorn the __main__ block never executes, so keeping the startup
    notification and the signal handlers in there meant they silently stopped
    working the moment the bot ran behind a real WSGI server.
    """
    log("🚀 Bot starting up...")
    restore_history()
    try:
        slack.chat_postMessage(
            channel=CS_CHANNEL,
            text="🟢 *Bot CLT Houzcall aktif kembali!* Siap menerima pertanyaan mitra.",
        )
    except Exception as e:
        log(f"[WARN] Failed to send startup notification: {e}")

    atexit.register(notify_shutdown)
    try:
        signal.signal(signal.SIGTERM, notify_shutdown)
        signal.signal(signal.SIGINT, notify_shutdown)
    except ValueError:
        # Not on the main thread (some WSGI servers import from a worker).
        log("[WARN] signal handlers not installed (non-main thread)")


_startup()


if __name__ == "__main__":

    app.run(host="0.0.0.0", port=3000, debug=False)

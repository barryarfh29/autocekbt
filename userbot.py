#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Userbot - Link-only manager
- Auto-fix link input & auto-verify
- Robust /check (text, caption, entities, buttons)
- /join for invite + public
- Anti-flood, batching, cooldown, governor caps
- SESSION_STRING via ENV (ideal untuk Easypanel)
- Storage pluggable: FILES (default) atau MongoDB (STORAGE=mongo)
"""

import os
import re
import json
import time
import random
import asyncio
from typing import Optional, List, Dict, Tuple
from collections import deque

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import (
    FloodWait,
    InviteHashInvalid,
    InviteHashExpired,
    UserAlreadyParticipant,
)

# ==================== ENV & RUNTIME ====================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")
PHONE = (os.getenv("PHONE", "") or "").strip()

# Storage selector: "files" (default) atau "mongo"
STORAGE = os.getenv("STORAGE", "files").lower()

# DATA_DIR untuk mode files; default:
# - jika SESSION_STRING ada (umumnya di server) -> /data
# - jika tidak, simpan di folder lokal project (.)
DATA_DIR = os.getenv("DATA_DIR", "/data" if SESSION_STRING else ".")
os.makedirs(DATA_DIR, exist_ok=True)

def _p(*names):  # helper path
    return os.path.join(DATA_DIR, *names)

# Nama “file/kind” yang dipakai call-site (tidak perlu diubah di handlers)
CHANNEL_FILE = _p("channels.txt")
LINK_FILE    = _p("links.txt")
CACHE_FILE   = _p("channels_cache.json")

# Anti Flood (bisa diubah runtime via commands)
JOIN_DELAY = 12
BATCH_SIZE = 6
BATCH_COOLDOWN = 25 * 60
CHECK_LIMIT = 30
STATUS_INTERVAL = 5
HOURLY_JOIN_CAP = 8
DAILY_JOIN_CAP = 40
FLOOD_ABORT_SECONDS = 600
MAX_AUTOVERIFY_PER_ADD = 6

adaptive_delay = JOIN_DELAY
join_timestamps = deque(maxlen=DAILY_JOIN_CAP * 2)

# ==================== STORAGE LAYER (FILES / MONGO) ====================
def _load_lines_file(path):
    if not os.path.exists(path): return []
    with open(path, "r", encoding="utf-8") as f:
        return [x.strip() for x in f if x.strip()]

def _save_lines_file(path, lines):
    lines = sorted(set([x.strip() for x in lines if x.strip()]))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

def _load_cache_file():
    if not os.path.exists(CACHE_FILE): return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_cache_file(cache: dict):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

# --- Mongo backend (opsional) ---
_mongo_enabled = False
if STORAGE == "mongo":
    try:
        from pymongo import MongoClient, ASCENDING
        MONGO_URI = os.getenv("MONGO_URI")
        MONGO_DB  = os.getenv("MONGO_DB", "userbot")
        if not MONGO_URI:
            raise RuntimeError("STORAGE=mongo tapi MONGO_URI tidak di-set.")
        _client = MongoClient(MONGO_URI)
        _db = _client[MONGO_DB]
        col_channels = _db["channels"]
        col_links    = _db["targets"]
        col_cache    = _db["cache"]
        # index unik untuk rapi & anti-duplicate
        col_channels.create_index([("value", ASCENDING)], unique=True)
        col_links.create_index([("value", ASCENDING)], unique=True)
        col_cache.create_index([("key", ASCENDING)], unique=True)
        _mongo_enabled = True
    except Exception as e:
        # fallback ke files bila Mongo gagal init
        print(f"[WARN] Mongo init error: {e}. Fallback ke files storage.")
        STORAGE = "files"

def _load_lines_mongo(col):
    return [d["value"] for d in col.find({}, {"_id": 0, "value": 1})]

def _save_lines_mongo(col, lines):
    lines = sorted(set([x.strip() for x in lines if x.strip()]))
    current = set(_load_lines_mongo(col))
    newset = set(lines)
    remove = current - newset
    add = newset - current
    if remove:
        col.delete_many({"value": {"$in": list(remove)}})
    if add:
        col.insert_many([{"value": v} for v in add])

def _load_cache_mongo():
    out = {}
    for d in col_cache.find({}, {"_id": 0, "key": 1, "value": 1}):
        out[d["key"]] = d["value"]
    return out

def _save_cache_mongo(cache: dict):
    for k, v in cache.items():
        col_cache.update_one({"key": k}, {"$set": {"value": v}}, upsert=True)

# --- Public API dipakai seluruh kode ---
def load_lines(kind_path: str) -> List[str]:
    if STORAGE == "mongo" and _mongo_enabled:
        if kind_path == CHANNEL_FILE: return _load_lines_mongo(col_channels)
        if kind_path == LINK_FILE:    return _load_lines_mongo(col_links)
        raise ValueError("Unknown lines kind for mongo")
    # files
    if kind_path == CHANNEL_FILE: return _load_lines_file(CHANNEL_FILE)
    if kind_path == LINK_FILE:    return _load_lines_file(LINK_FILE)
    return _load_lines_file(kind_path)

def save_lines(kind_path: str, lines: List[str]):
    if STORAGE == "mongo" and _mongo_enabled:
        if kind_path == CHANNEL_FILE: return _save_lines_mongo(col_channels, lines)
        if kind_path == LINK_FILE:    return _save_lines_mongo(col_links, lines)
        raise ValueError("Unknown lines kind for mongo")
    # files
    if kind_path == CHANNEL_FILE: return _save_lines_file(CHANNEL_FILE, lines)
    if kind_path == LINK_FILE:    return _save_lines_file(LINK_FILE, lines)
    return _save_lines_file(kind_path, lines)

def load_cache() -> Dict[str, dict]:
    if STORAGE == "mongo" and _mongo_enabled: return _load_cache_mongo()
    return _load_cache_file()

def save_cache(cache: Dict[str, dict]):
    if STORAGE == "mongo" and _mongo_enabled: return _save_cache_mongo(cache)
    return _save_cache_file(cache)

# ==================== LINK & MESSAGE PARSING ====================
TME_ANY_RE   = re.compile(r"(?:https?://)?t\.me/.+", re.IGNORECASE)
INV_PLUS_RE  = re.compile(r"^(?:https?://)?t\.me/\+([A-Za-z0-9_-]+)$", re.IGNORECASE)
INV_JOIN_RE  = re.compile(r"^(?:https?://)?t\.me/joinchat/([A-Za-z0-9_-]+)$", re.IGNORECASE)
PUB_USER_RE  = re.compile(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{3,})$", re.IGNORECASE)
PUB_POST_RE  = re.compile(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{3,})/\d+$", re.IGNORECASE)
BARE_USER_RE = re.compile(r"^@?([A-Za-z0-9_]{3,})$")

URL_TME_RE = re.compile(r"(?:https?://)?t\.me/[^\s\)\]\}\,]+", re.IGNORECASE)
AT_USER_RE = re.compile(r"(?<!\w)@([A-Za-z0-9_]{3,})(?!\w)")

def normalize_tme_link(s: str) -> str:
    if not s: return ""
    s = s.strip().strip(" \t\n\r\"'<>").rstrip(".,;:)]}»\"'")
    s = re.sub(r"\s+", "", s)
    if s.lower().startswith("https://@") or s.lower().startswith("http://@"):
        s = s[s.find("@"):]
    m = BARE_USER_RE.match(s)
    if s.startswith("@") and m:              return f"https://t.me/{m.group(1)}"
    if m and not s.lower().startswith("http"): return f"https://t.me/{m.group(1)}"
    if s.lower().startswith("t.me/"):        s = "https://" + s
    if not s.lower().startswith("http"):     s = "https://" + s
    s = s.replace("t.me//", "t.me/")
    if s.endswith("/"): s = s[:-1]
    return s

def is_tme_link(s: str) -> bool:
    return bool(TME_ANY_RE.search(s))

def is_invite_link(s: str) -> bool:
    return bool(INV_PLUS_RE.match(s) or INV_JOIN_RE.match(s))

def extract_public_username(s: str) -> Optional[str]:
    s = s.strip()
    m = PUB_USER_RE.match(s) or PUB_POST_RE.match(s)
    return m.group(1) if m else None

def normalize_tme_text_link(s: str) -> str:
    s = s.strip().rstrip(".,;:)]}»\"'")
    if s.lower().startswith("t.me/"): s = "https://" + s
    s = s.replace("t.me//", "t.me/")
    if s.endswith("/"): s = s[:-1]
    return s

def links_from_text(text: Optional[str]) -> List[str]:
    if not text: return []
    raw = URL_TME_RE.findall(text)
    raw += [f"https://t.me/{u}" for u in AT_USER_RE.findall(text)]
    return [normalize_tme_text_link(x) for x in raw]

def links_from_entities(msg) -> List[str]:
    out = []
    ents = getattr(msg, "entities", None) or getattr(msg, "caption_entities", None)
    if not ents: return out
    txt = (msg.text or "") if getattr(msg, "text", None) else (msg.caption or "")
    for e in ents:
        t = getattr(e, "type", None)
        if t and t.name == "TEXT_LINK":
            url = getattr(e, "url", "")
            if "t.me/" in url: out.append(normalize_tme_text_link(url))
        elif t and t.name == "URL":
            try:
                s = txt[e.offset:e.offset+e.length]
                if "t.me/" in s or s.startswith("@"):
                    out.append(normalize_tme_text_link(s))
            except Exception:
                pass
    return out

def links_from_buttons(msg) -> List[str]:
    out = []
    rm = getattr(msg, "reply_markup", None)
    if not rm or not getattr(rm, "inline_keyboard", None): return out
    for row in rm.inline_keyboard:
        for btn in row:
            url = getattr(btn, "url", None)
            if url and "t.me/" in url: out.append(normalize_tme_text_link(url))
    return out

def get_all_tme_links(msg) -> List[str]:
    acc = []
    acc += links_from_text(getattr(msg, "text", None) or "")
    acc += links_from_text(getattr(msg, "caption", None) or "")
    acc += links_from_entities(msg)
    acc += links_from_buttons(msg)
    seen, out = set(), []
    for x in acc:
        if not x: continue
        n = normalize_tme_link(x)
        if n not in seen:
            seen.add(n); out.append(n)
    return out

# ==================== DELAY / STATUS / GOVERNOR ====================
async def human_sleep(base: Optional[float] = None):
    global adaptive_delay
    base = base or adaptive_delay
    jitter = base * random.uniform(0.2, 0.4)
    await asyncio.sleep(base + jitter)

async def update_status(msg, text: str):
    try:
        await msg.edit_text(text, disable_web_page_preview=True)
    except Exception:
        try:
            await msg.reply_text(text, disable_web_page_preview=True)
        except Exception:
            pass

def _prune_old():
    now = time.time()
    while join_timestamps and now - join_timestamps[0] > 24*3600:
        join_timestamps.popleft()

def quota_allows_join() -> Tuple[bool, str]:
    _prune_old()
    now = time.time()
    last_hour = [t for t in join_timestamps if now - t <= 3600]
    if len(last_hour) >= HOURLY_JOIN_CAP: return False, "hour"
    if len(join_timestamps) >= DAILY_JOIN_CAP: return False, "day"
    return True, ""

# ==================== CLIENT INIT (SESSION_STRING) ====================
if SESSION_STRING:
    app = Client(
        name=":memory:",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING
    )
else:
    app = Client(
        "userbot",
        api_id=API_ID,
        api_hash=API_HASH,
        phone_number=PHONE
    )

# ==================== COMMANDS ====================
@app.on_message(filters.me & filters.command("ping", prefixes="/"))
async def ping_cmd(_, msg: Message):
    await msg.reply_text("pong ✅")

@app.on_message(filters.me & filters.command("whoami", prefixes="/"))
async def whoami_cmd(client: Client, msg: Message):
    me = await client.get_me()
    await msg.reply_text(
        f"👤 **Logged in as**\n"
        f"- id: `{me.id}`\n"
        f"- username: @{me.username if me.username else '(none)'}\n"
        f"- first_name: {me.first_name}\n"
        f"- storage: `{STORAGE}`"
    )

@app.on_message(filters.me & filters.command("help", prefixes="/"))
async def help_cmd(_, msg: Message):
    text = (
        "**Perintah (Auto-Verify & Anti-Flood):**\n\n"
        "🧩 Join (undangan & publik)\n"
        "  `/join <multi-link atau @user>` — satu per baris\n\n"
        "🔗 Target Pencarian\n"
        "  `/addlist <link t.me atau keyword>`\n  `/dellist [item]`\n  `/showlist`\n\n"
        "📺 Channel (t.me)\n"
        "  `/addchan <multi-link>` — auto-fix & auto-verify (maks terbatas)\n"
        "  `/delchan [link]`\n  `/showchan`\n  `/verifychan`\n\n"
        "🔍 Cek\n"
        "  `/check [limit]` — default 30 pesan per channel\n\n"
        "⚙️ Setting runtime\n"
        "  `/setdelay <detik>`  `/setbatch <jumlah>`  `/setcooldown <menit>`\n"
        "  `/setcaps <hourly> <daily>`\n"
        "🧪 Debug\n"
        "  `/ping`  `/whoami`\n"
    )
    await msg.reply_text(text, disable_web_page_preview=True)

@app.on_message(filters.me & filters.command("setdelay", prefixes="/"))
async def setdelay_cmd(_, msg: Message):
    global JOIN_DELAY, adaptive_delay
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.reply_text("Format: `/setdelay <detik>`"); return
    JOIN_DELAY = int(parts[1]); adaptive_delay = JOIN_DELAY
    await msg.reply_text(f"✅ JOIN_DELAY diset ke **{JOIN_DELAY}s** (adaptive reset).")

@app.on_message(filters.me & filters.command("setbatch", prefixes="/"))
async def setbatch_cmd(_, msg: Message):
    global BATCH_SIZE
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.reply_text("Format: `/setbatch <jumlah>`"); return
    BATCH_SIZE = int(parts[1])
    await msg.reply_text(f"✅ BATCH_SIZE diset ke **{BATCH_SIZE}**.")

@app.on_message(filters.me & filters.command("setcooldown", prefixes="/"))
async def setcooldown_cmd(_, msg: Message):
    global BATCH_COOLDOWN
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.reply_text("Format: `/setcooldown <menit>`"); return
    minutes = int(parts[1]); BATCH_COOLDOWN = minutes * 60
    await msg.reply_text(f"✅ BATCH_COOLDOWN diset ke **{minutes} menit**.")

@app.on_message(filters.me & filters.command("setcaps", prefixes="/"))
async def setcaps_cmd(_, msg: Message):
    global HOURLY_JOIN_CAP, DAILY_JOIN_CAP, join_timestamps
    parts = msg.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await msg.reply_text("Format: `/setcaps <hourly> <daily>`"); return
    HOURLY_JOIN_CAP = int(parts[1]); DAILY_JOIN_CAP = int(parts[2])
    join_timestamps = deque(maxlen=DAILY_JOIN_CAP * 2)
    await msg.reply_text(f"✅ Caps diset ke hourly={HOURLY_JOIN_CAP}, daily={DAILY_JOIN_CAP}.")

# ----- LIST MANAGEMENT -----
@app.on_message(filters.me & filters.command("addlist", prefixes="/"))
async def addlist_cmd(_, msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Gunakan: `/addlist <link t.me atau keyword>`"); return
    item = parts[1].strip()
    lines = load_lines(LINK_FILE); lines.append(item); save_lines(LINK_FILE, lines)
    await msg.reply_text(f"✅ Ditambahkan ke target pencarian:\n{item}")

@app.on_message(filters.me & filters.command("dellist", prefixes="/"))
async def dellist_cmd(_, msg: Message):
    parts = msg.text.split(maxsplit=1)
    lines = load_lines(LINK_FILE)
    if len(parts) == 1:
        save_lines(LINK_FILE, []); await msg.reply_text("🗑️ Semua target dihapus."); return
    item = parts[1].strip()
    if item in lines:
        lines.remove(item); save_lines(LINK_FILE, lines); await msg.reply_text(f"🗑️ Dihapus:\n{item}")
    else:
        await msg.reply_text("❌ Target tidak ditemukan.")

@app.on_message(filters.me & filters.command("showlist", prefixes="/"))
async def showlist_cmd(_, msg: Message):
    lines = load_lines(LINK_FILE)
    text = "\n".join(lines) if lines else "(kosong)"
    await msg.reply_text(f"📄 **Daftar Target:**\n{text}", disable_web_page_preview=True)

# ----- CHANNEL MANAGEMENT -----
@app.on_message(filters.me & filters.command("addchan", prefixes="/"))
async def addchan_cmd(client: Client, msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Gunakan: `/addchan <multi-link>` (boleh @user / t.me/kotor)."); return

    raw = [x for x in parts[1].splitlines() if x.strip()]
    fixed = [normalize_tme_link(x) for x in raw]
    bad = [ln for ln in fixed if not is_tme_link(ln)]
    good = [ln for ln in fixed if is_tme_link(ln)]

    if bad:
        await msg.reply_text("⚠️ Dilewati (bukan link Telegram):\n" + "\n".join(bad[:20]))

    chans_existing = load_lines(CHANNEL_FILE)
    before = set(chans_existing)
    chans_existing.extend(good)
    save_lines(CHANNEL_FILE, chans_existing)

    added = sorted(set(good) - before)
    if not added:
        await msg.reply_text("ℹ️ Tidak ada link baru yang ditambahkan (mungkin sudah ada)."); return

    note = await msg.reply_text(f"✅ Ditambahkan {len(added)} link.\n🤖 Auto-verify dimulai (maks {MAX_AUTOVERIFY_PER_ADD} item)...")
    subset = added[:MAX_AUTOVERIFY_PER_ADD]
    await verify_links(client, subset, note)
    if len(added) > len(subset):
        await note.reply_text(f"ℹ️ {len(added)-len(subset)} sisanya tidak auto-diverify. Jalankan `/verifychan` jika ingin verifikasi semua.")

@app.on_message(filters.me & filters.command("delchan", prefixes="/"))
async def delchan_cmd(_, msg: Message):
    parts = msg.text.split(maxsplit=1)
    chans = load_lines(CHANNEL_FILE)
    if len(parts) == 1:
        save_lines(CHANNEL_FILE, []); await msg.reply_text("🗑️ Semua channel dihapus."); return
    item = normalize_tme_link(parts[1].strip())
    if item in chans:
        chans.remove(item); save_lines(CHANNEL_FILE, chans); await msg.reply_text(f"🗑️ Dihapus:\n{item}")
    else:
        await msg.reply_text("❌ Channel/link tidak ditemukan.")

@app.on_message(filters.me & filters.command("showchan", prefixes="/"))
async def showchan_cmd(_, msg: Message):
    chans = load_lines(CHANNEL_FILE)
    text = "\n".join(chans) if chans else "(kosong)"
    await msg.reply_text(f"📺 **Daftar Channel (t.me):**\n{text}", disable_web_page_preview=True)

# ----- VERIFY CORE -----
async def verify_links(client: Client, links: List[str], parent_msg: Message):
    global adaptive_delay
    cache = load_cache()
    status = await parent_msg.reply_text("🔍 Memverifikasi channel…")
    ok, bad = [], []
    total = len(links)

    for i, link in enumerate(links, start=1):
        link_n = normalize_tme_link(link)
        try:
            target = None
            cached = cache.get(link_n)
            if cached and "chat_id" in cached:
                target = int(cached["chat_id"])
            else:
                uname = extract_public_username(link_n)
                if uname:
                    target = uname
                elif is_invite_link(link_n):
                    ok_quota, w = quota_allows_join()
                    if not ok_quota:
                        bad.append(f"{link_n} → quota {w} reached (skipped)")
                    else:
                        try:
                            chat = await client.join_chat(link_n)
                            cache[link_n] = {"chat_id": chat.id, "title": chat.title or ""}
                            save_cache(cache)
                            target = chat.id
                            join_timestamps.append(time.time())
                        except UserAlreadyParticipant:
                            try:
                                ch = await client.get_chat(link_n)
                                cache[link_n] = {"chat_id": ch.id, "title": ch.title or ""}
                                save_cache(cache)
                                target = ch.id
                            except Exception:
                                pass

            if not target:
                bad.append(f"{link_n} → cannot resolve (skip)")
            else:
                chat = await client.get_chat(target)
                ok.append(chat.title or link_n)

        except InviteHashInvalid:
            bad.append(f"{link_n} → INVITE INVALID")
        except InviteHashExpired:
            bad.append(f"{link_n} → INVITE EXPIRED")
        except FloodWait as e:
            if e.value >= 300: adaptive_delay = max(adaptive_delay, 15)
            if e.value >= 1200: adaptive_delay = max(adaptive_delay, 20)
            bad.append(f"{link_n} → FloodWait {e.value}s (skipped)")
            await asyncio.sleep(min(e.value + 10, 60 * 60))
        except Exception as e:
            bad.append(f"{link_n} → {e}")

        if i % STATUS_INTERVAL == 0 or i == total:
            await update_status(status, f"🔍 Verifikasi… {i}/{total} selesai.\n✔️ OK: {len(ok)} | ⚠️ Bad: {len(bad)}")

        await human_sleep(1)

    text = f"✅ Verifikasi selesai.\nOK: {len(ok)} | Bad: {len(bad)}"
    if ok:  text += "\n\n**VALID (sample):**\n" + "\n".join(ok[:30])
    if bad: text += "\n\n**INVALID/ERROR:**\n" + "\n".join(bad[:30])
    await update_status(status, text)

@app.on_message(filters.me & filters.command("verifychan", prefixes="/"))
async def verifychan_cmd(client: Client, msg: Message):
    chans = load_lines(CHANNEL_FILE)
    if not chans:
        await msg.reply_text("Tidak ada channel di daftar."); return
    await verify_links(client, chans, msg)

# ----- JOIN -----
@app.on_message(filters.me & filters.command("join", prefixes="/"))
async def join_cmd(client: Client, msg: Message):
    global adaptive_delay
    raw = msg.text.split(maxsplit=1)
    if len(raw) < 2:
        await msg.reply_text("Gunakan: `/join <multi-link or @user or username>` (satu per baris)."); return

    items = [x.strip() for x in raw[1].splitlines() if x.strip()]
    normalized = [normalize_tme_link(x) for x in items]
    invite_links = [ln for ln in normalized if is_invite_link(ln)]
    public_users = [extract_public_username(ln) for ln in normalized if extract_public_username(ln)]

    work_items = [("invite", ln) for ln in invite_links] + [("public", u) for u in public_users if u]
    if not work_items:
        await msg.reply_text("❌ Tidak ada link undangan atau username publik yang valid."); return

    status = await msg.reply_text("🚪 Memulai proses join…")
    cache = load_cache()
    success, failed = [], []
    adaptive_delay = JOIN_DELAY
    count_in_batch = 0
    total = len(work_items)

    for idx, (kind, val) in enumerate(work_items, start=1):
        try:
            ok_quota, window = quota_allows_join()
            if not ok_quota:
                if window == "hour":
                    await update_status(status, "⛔ Hourly cap reached. Cooling down 60 minutes...")
                    await asyncio.sleep(max(BATCH_COOLDOWN, 60 * 60))
                else:
                    await update_status(status, "⛔ Daily cap reached. Stopping join.")
                    break

            if kind == "invite":
                chat = await client.join_chat(val)
                cache[normalize_tme_link(val)] = {"chat_id": chat.id, "title": chat.title or ""}
                save_cache(cache)
                success.append(chat.title or val)
                join_timestamps.append(time.time())
            else:
                username = val
                try:
                    chat = await client.join_chat(username)
                    cache_key = normalize_tme_link(f"https://t.me/{username}")
                    cache[cache_key] = {"chat_id": chat.id, "title": chat.title or ""}
                    save_cache(cache)
                    success.append(chat.title or "@" + username)
                    join_timestamps.append(time.time())
                except UserAlreadyParticipant:
                    success.append(f"(sudah join) @{username}")
                    join_timestamps.append(time.time())

            adaptive_delay = min(max(adaptive_delay, JOIN_DELAY) + 1, 20)

        except UserAlreadyParticipant:
            success.append(f"(sudah join) {val}")
            join_timestamps.append(time.time())
        except InviteHashInvalid:
            failed.append(f"{val} → INVALID")
        except InviteHashExpired:
            failed.append(f"{val} → EXPIRED")
        except FloodWait as e:
            if e.value >= FLOOD_ABORT_SECONDS:
                adaptive_delay = max(adaptive_delay, 20)
                await update_status(status, f"⛔ FloodWait {e.value}s (>= abort threshold). Long cooldown 60 minutes.")
                await asyncio.sleep(60 * 60)
                failed.append(f"{val} → FloodWait {e.value}s (abort)")
                break
            else:
                if e.value >= 300: adaptive_delay = max(adaptive_delay, 15)
                await update_status(status, f"⛔ FloodWait {e.value}s (pausing)...")
                await asyncio.sleep(e.value + 10)
                failed.append(f"{val} → FloodWait {e.value}s (skipped)")
        except Exception as e:
            failed.append(f"{val} → {e}")

        count_in_batch += 1
        if idx < total:
            if count_in_batch >= BATCH_SIZE:
                mins = BATCH_COOLDOWN // 60
                await update_status(status, f"🧰 Batch done ({count_in_batch}). Cooldown {mins} min...")
                await asyncio.sleep(BATCH_COOLDOWN)
                count_in_batch = 0
            else:
                await human_sleep()

        if idx % STATUS_INTERVAL == 0 or idx == total:
            await update_status(status, f"🚪 Join progres: {idx}/{total}\n✔️ Sukses: {len(success)} | ⚠️ Gagal: {len(failed)}\n⏱ Delay adaptif: ~{int(adaptive_delay)}s")

    report = "✅ **Join selesai!**\n"
    report += f"Total attempt: {total} | ✔️ {len(success)} | ⚠️ {len(failed)}\n"
    if success: report += "\n**Sukses (sample):**\n" + "\n".join(success[:30])
    if failed:  report += "\n\n**Gagal/Skip (sample):**\n" + "\n".join(failed[:30])
    await update_status(status, report)

# ----- CHECK -----
@app.on_message(filters.me & filters.command("check", prefixes="/"))
async def check_cmd(client: Client, msg: Message):
    parts = msg.text.split(maxsplit=1)
    limit = CHECK_LIMIT
    if len(parts) == 2 and parts[1].isdigit():
        limit = max(10, min(1000, int(parts[1])))

    chans = load_lines(CHANNEL_FILE)
    targets = load_lines(LINK_FILE)
    if not chans:
        await msg.reply_text("⚠️ Tidak ada channel. Tambahkan dulu pakai `/addchan`."); return
    if not targets:
        await msg.reply_text("⚠️ Tidak ada target pencarian. Tambahkan dulu pakai `/addlist`."); return

    status = await msg.reply_text(f"🔎 Memulai pengecekan (limit {limit} per channel)...")
    cache = load_cache()
    total = len(chans)
    found, processed = 0, 0
    lines = []

    for i, link in enumerate(chans, start=1):
        link_n = normalize_tme_link(link)
        try:
            if is_invite_link(link_n):
                ok_quota, win = quota_allows_join()
                if ok_quota:
                    await ensure_join_if_needed(client, link_n, cache)
                else:
                    lines.append(f"{link_n}: ⚠️ quota {win} reached (skip join invite).")

            target = None
            cached = cache.get(link_n)
            if cached and "chat_id" in cached:
                target = int(cached["chat_id"])
            else:
                uname = extract_public_username(link_n)
                if uname: target = uname

            if target is None:
                lines.append(f"{link_n}: ⚠️ Tidak bisa diakses (undangan belum ter-cache). Jalankan /verifychan.")
                processed += 1
                continue

            chat = await client.get_chat(target)

            ok = False
            async for m in client.get_chat_history(chat.id, limit=limit):
                all_tlinks = get_all_tme_links(m)
                for tgt in targets:
                    tgt_low = tgt.lower()
                    if tgt_low.startswith("http"):
                        if any(tgt_low in tl.lower() for tl in all_tlinks):
                            ok = True; break
                    else:
                        hay = (m.text or "") + "\n" + (m.caption or "")
                        if tgt_low in hay.lower() or any(tgt_low in tl.lower() for tl in all_tlinks):
                            ok = True; break
                if ok: break

            lines.append(f"{chat.title or link_n}: {'✅ YES' if ok else '❌ NO'}")
            if ok: found += 1
            processed += 1

        except FloodWait as e:
            lines.append(f"{link_n}: ⏳ FloodWait {e.value}s (skipped temporarily)")
            await asyncio.sleep(min(e.value + 5, 60 * 60))
        except Exception as e:
            lines.append(f"{link_n}: ⚠️ {e}")
            processed += 1

        if i % STATUS_INTERVAL == 0 or i == total:
            sample = "\n".join(lines[-10:])
            await update_status(status, f"🔎 Progres cek: {i}/{total}\n✔️ Ketemu: {found}\n📝 Sampel:\n{sample}")

        await human_sleep(2)

    head = f"✅ **Selesai!**\nChannel dicek: {processed}\nKetemu: {found}\n\n"
    await update_status(status, head + ("\n".join(lines[:200]) + (f"\n…({len(lines)-200} lagi)" if len(lines) > 200 else "")))

# ----- helper for /check -----
async def ensure_join_if_needed(client: Client, link: str, cache: Dict[str, dict]):
    global adaptive_delay
    link_n = normalize_tme_link(link)
    if not is_invite_link(link_n): return
    ok_quota, _ = quota_allows_join()
    if not ok_quota: return
    try:
        chat = await client.join_chat(link_n)
        cache[link_n] = {"chat_id": chat.id, "title": chat.title or ""}
        save_cache(cache)
        join_timestamps.append(time.time())
        adaptive_delay = min(max(adaptive_delay, JOIN_DELAY) + 1, 20)
    except UserAlreadyParticipant:
        try:
            ch = await client.get_chat(link_n)
            cache[link_n] = {"chat_id": ch.id, "title": ch.title or ""}
            save_cache(cache)
        except Exception:
            pass
    except FloodWait as e:
        if e.value >= 300: adaptive_delay = max(adaptive_delay, 15)
        if e.value >= 1200: adaptive_delay = max(adaptive_delay, 20)
        await asyncio.sleep(min(e.value + 10, 60 * 60))
    except Exception:
        pass

# ==================== STARTUP ====================
if __name__ == "__main__":
    print(f"🚀 Userbot siap. (SESSION_STRING mode: {'yes' if SESSION_STRING else 'no'}) | storage={STORAGE}")
    app.run()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Userbot - Link-only manager dengan:
- auto-fix link input & auto-verify on /addchan (terbatas)
- robust /check (text, caption, entities, buttons)
- /join untuk invite + public (opsional)
- anti-flood: adaptive delay, jitter, batching, cooldown
- governor: hourly/daily caps + FloodWait abort policy
- status updates during /join, /verifychan, /check
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
from pyrogram.errors import (
    FloodWait,
    InviteHashInvalid,
    InviteHashExpired,
    UserAlreadyParticipant,
)

# ==================== KONFIGURASI DASAR ====================
API_ID = int(os.getenv("API_ID", "28330391"))
API_HASH = os.getenv("API_HASH", "d53eea2d5226e5937d617683ba9d952c")
PHONE = os.getenv("PHONE", "+15308366939 ")

CHANNEL_FILE = "channels.txt"
LINK_FILE = "links.txt"
CACHE_FILE = "channels_cache.json"

# ---- Anti Flood (runtime-configurable via /setxxx) ----
JOIN_DELAY = 12                # baseline jeda antar join (detik)
BATCH_SIZE = 6                 # maks join per batch (lebih safe default)
BATCH_COOLDOWN = 25 * 60       # cooldown antar batch (detik)
CHECK_LIMIT = 30               # default pesan per channel saat /check
STATUS_INTERVAL = 5            # update status tiap N item

# ---- Governor (kuota nyata agar terhindar Flood besar) ----
HOURLY_JOIN_CAP = 8           # maximal join per 60 menit
DAILY_JOIN_CAP  = 40          # maximal join per 24 jam
FLOOD_ABORT_SECONDS = 600     # FloodWait >= ini -> hentikan batch & cooldown panjang

# Tunables
MAX_AUTOVERIFY_PER_ADD = 6    # saat /addchan auto-verify hanya proses N link pertama

# runtime adaptive
adaptive_delay = JOIN_DELAY

# storage join timestamps untuk governor
join_timestamps = deque(maxlen=DAILY_JOIN_CAP * 2)


# ---------- FILE UTILITIES ----------
def load_lines(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [x.strip() for x in f if x.strip()]


def save_lines(path: str, lines: List[str]):
    lines = sorted(set([x.strip() for x in lines if x.strip()]))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def load_cache() -> Dict[str, dict]:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache: Dict[str, dict]):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ---------- LINK UTILITIES ----------
TME_ANY_RE = re.compile(r"(?:https?://)?t\.me/.+", re.IGNORECASE)
INV_PLUS_RE = re.compile(r"^(?:https?://)?t\.me/\+([A-Za-z0-9_-]+)$", re.IGNORECASE)
INV_JOIN_RE = re.compile(r"^(?:https?://)?t\.me/joinchat/([A-Za-z0-9_-]+)$", re.IGNORECASE)
PUB_USER_RE = re.compile(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{3,})$", re.IGNORECASE)
PUB_POST_RE = re.compile(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{3,})/\d+$", re.IGNORECASE)
BARE_USER_RE = re.compile(r"^@?([A-Za-z0-9_]{3,})$")

# Patterns for extracting links from text
URL_TME_RE = re.compile(r"(?:https?://)?t\.me/[^\s\)\]\}\,]+", re.IGNORECASE)
AT_USER_RE = re.compile(r"(?<!\w)@([A-Za-z0-9_]{3,})(?!\w)")

# Normalizer - robust untuk input user yang kotor
def normalize_tme_link(s: str) -> str:
    """Normalize various raw inputs to canonical https://t.me/username or https://t.me/+invite"""
    if not s:
        return ""
    s = s.strip()
    # strip surrounding punctuation
    s = s.strip(" \t\n\r\"'<>")
    s = s.rstrip(".,;:)]}»\"'")  # common trailing punctuation
    # remove internal whitespace
    s = re.sub(r"\s+", "", s)
    # fix leading https://@username => t.me/username
    if s.lower().startswith("https://@") or s.lower().startswith("http://@"):
        s = s[s.find("@"):]
    # bare @username or username -> convert
    m = BARE_USER_RE.match(s)
    if s.startswith("@") and m:
        user = m.group(1)
        return f"https://t.me/{user}"
    if m and not s.lower().startswith("http"):
        # 'username' provided without @ -> treat as t.me/username
        return f"https://t.me/{m.group(1)}"
    # ensure schema
    if s.lower().startswith("t.me/"):
        s = "https://" + s
    if not s.lower().startswith("http"):
        s = "https://" + s
    # fix double slash
    s = s.replace("t.me//", "t.me/")
    if s.endswith("/"):
        s = s[:-1]
    return s


def is_tme_link(s: str) -> bool:
    return bool(TME_ANY_RE.search(s))


def is_invite_link(s: str) -> bool:
    s = s.strip()
    return bool(INV_PLUS_RE.match(s) or INV_JOIN_RE.match(s))


def extract_invite_code(s: str) -> Optional[str]:
    m = INV_PLUS_RE.match(s) or INV_JOIN_RE.match(s)
    return m.group(1) if m else None


def extract_public_username(s: str) -> Optional[str]:
    s = s.strip()
    m = PUB_USER_RE.match(s) or PUB_POST_RE.match(s)
    return m.group(1) if m else None


# ---------- MESSAGE LINK EXTRACTION (text, caption, entities, buttons) ----------
def normalize_tme_text_link(s: str) -> str:
    s = s.strip()
    s = s.rstrip(".,;:)]}»\"'")
    if s.lower().startswith("t.me/"):
        s = "https://" + s
    if not s.lower().startswith("http"):
        # keep as is (will be filtered later)
        pass
    s = s.replace("t.me//", "t.me/")
    if s.endswith("/"):
        s = s[:-1]
    return s


def links_from_text(text: Optional[str]) -> List[str]:
    if not text:
        return []
    raw = []
    raw += URL_TME_RE.findall(text)
    raw += [f"https://t.me/{u}" for u in AT_USER_RE.findall(text)]
    return [normalize_tme_text_link(x) for x in raw]


def links_from_entities(msg) -> List[str]:
    out = []
    ents = getattr(msg, "entities", None) or getattr(msg, "caption_entities", None)
    if not ents:
        return out
    txt = (msg.text or "") if getattr(msg, "text", None) else (msg.caption or "")
    for e in ents:
        t = getattr(e, "type", None)
        # TEXT_LINK has .url
        if t and t.name == "TEXT_LINK":
            url = getattr(e, "url", "")
            if "t.me/" in url:
                out.append(normalize_tme_text_link(url))
        elif t and t.name == "URL":
            try:
                s = txt[e.offset : e.offset + e.length]
                if "t.me/" in s or s.startswith("@"):
                    out.append(normalize_tme_text_link(s))
            except Exception:
                pass
    return out


def links_from_buttons(msg) -> List[str]:
    out = []
    rm = getattr(msg, "reply_markup", None)
    if not rm or not getattr(rm, "inline_keyboard", None):
        return out
    for row in rm.inline_keyboard:
        for btn in row:
            url = getattr(btn, "url", None)
            if url and "t.me/" in url:
                out.append(normalize_tme_text_link(url))
    return out


def get_all_tme_links(msg) -> List[str]:
    acc = []
    acc += links_from_text(getattr(msg, "text", None) or "")
    acc += links_from_text(getattr(msg, "caption", None) or "")
    acc += links_from_entities(msg)
    acc += links_from_buttons(msg)
    seen = set(); out = []
    for x in acc:
        if not x:
            continue
        # normalize again fully
        n = normalize_tme_link(x)
        if n not in seen:
            seen.add(n); out.append(n)
    return out


# ---------- DELAY & STATUS HELPERS ----------
async def human_sleep(base: Optional[float] = None):
    global adaptive_delay
    base = base if base is not None else adaptive_delay
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


# ---------- GOVERNOR HELPERS ----------
def _prune_old():
    now = time.time()
    while join_timestamps and now - join_timestamps[0] > 24 * 3600:
        join_timestamps.popleft()


def quota_allows_join() -> Tuple[bool, str]:
    _prune_old()
    now = time.time()
    last_hour = [t for t in join_timestamps if now - t <= 3600]
    if len(last_hour) >= HOURLY_JOIN_CAP:
        return False, "hour"
    if len(join_timestamps) >= DAILY_JOIN_CAP:
        return False, "day"
    return True, ""


# ---------- CLIENT INIT ----------
app = Client("userbot", api_id=API_ID, api_hash=API_HASH, phone_number=PHONE)


# ---------- COMMANDS: help & runtime settings ----------
@app.on_message(filters.me & filters.command("help", prefixes="/"))
async def help_cmd(_, msg):
    text = (
        "**Perintah (Auto-Verify & Anti-Flood):**\n\n"
        "🧩 Join Undangan & Publik:\n"
        "  /join <multi-link or @user or username>  (satu per baris)\n\n"
        "🔗 Target Pencarian:\n"
        "  /addlist <link t.me atau keyword>\n"
        "  /dellist [item]\n"
        "  /showlist\n\n"
        "📺 Manajemen Channel (link t.me):\n"
        "  /addchan <multi-link> → auto-fix & auto-verify (terbatas)\n"
        "  /delchan [link]\n"
        "  /showchan\n"
        "  /verifychan → verifikasi manual & cache chat_id\n\n"
        "🔍 Cek:\n"
        "  /check [limit] → cek semua channel (default 30)\n\n"
        "⚙️ Runtime settings:\n"
        "  /setdelay <detik>\n"
        "  /setbatch <jumlah>\n"
        "  /setcooldown <menit>\n"
        "  /setcaps <hourly> <daily>\n"
    )
    await msg.reply_text(text, disable_web_page_preview=True)


@app.on_message(filters.me & filters.command("setdelay", prefixes="/"))
async def setdelay_cmd(_, msg):
    global JOIN_DELAY, adaptive_delay
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.reply_text("Format: `/setdelay <detik>`", disable_web_page_preview=True)
        return
    JOIN_DELAY = int(parts[1]); adaptive_delay = JOIN_DELAY
    await msg.reply_text(f"✅ JOIN_DELAY diset ke **{JOIN_DELAY}s** (adaptive reset).")


@app.on_message(filters.me & filters.command("setbatch", prefixes="/"))
async def setbatch_cmd(_, msg):
    global BATCH_SIZE
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.reply_text("Format: `/setbatch <jumlah>`", disable_web_page_preview=True)
        return
    BATCH_SIZE = int(parts[1])
    await msg.reply_text(f"✅ BATCH_SIZE diset ke **{BATCH_SIZE}**.")


@app.on_message(filters.me & filters.command("setcooldown", prefixes="/"))
async def setcooldown_cmd(_, msg):
    global BATCH_COOLDOWN
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.reply_text("Format: `/setcooldown <menit>`", disable_web_page_preview=True)
        return
    minutes = int(parts[1]); BATCH_COOLDOWN = minutes * 60
    await msg.reply_text(f"✅ BATCH_COOLDOWN diset ke **{minutes} menit**.")


@app.on_message(filters.me & filters.command("setcaps", prefixes="/"))
async def setcaps_cmd(_, msg):
    global HOURLY_JOIN_CAP, DAILY_JOIN_CAP, join_timestamps
    parts = msg.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await msg.reply_text("Format: `/setcaps <hourly> <daily>`", disable_web_page_preview=True)
        return
    HOURLY_JOIN_CAP = int(parts[1]); DAILY_JOIN_CAP = int(parts[2])
    join_timestamps = deque(maxlen=DAILY_JOIN_CAP * 2)
    await msg.reply_text(f"✅ Caps diset ke hourly={HOURLY_JOIN_CAP}, daily={DAILY_JOIN_CAP}.")


# ---------- LIST MANAGEMENT ----------
@app.on_message(filters.me & filters.command("addlist", prefixes="/"))
async def addlist_cmd(_, msg):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Gunakan: `/addlist <link t.me atau keyword>`", disable_web_page_preview=True)
        return
    item = parts[1].strip()
    lines = load_lines(LINK_FILE)
    lines.append(item)
    save_lines(LINK_FILE, lines)
    await msg.reply_text(f"✅ Ditambahkan ke target pencarian:\n{item}")


@app.on_message(filters.me & filters.command("dellist", prefixes="/"))
async def dellist_cmd(_, msg):
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
async def showlist_cmd(_, msg):
    lines = load_lines(LINK_FILE)
    text = "\n".join(lines) if lines else "(kosong)"
    await msg.reply_text(f"📄 **Daftar Target:**\n{text}", disable_web_page_preview=True)


# ---------- CHANNEL MANAGEMENT (auto-fix + auto-verify subset) ----------
@app.on_message(filters.me & filters.command("addchan", prefixes="/"))
async def addchan_cmd(client, msg):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Gunakan: `/addchan <multi-link>` (boleh @user / t.me/kotor).", disable_web_page_preview=True)
        return

    raw = [x for x in parts[1].splitlines() if x.strip()]
    fixed = [normalize_tme_link(x) for x in raw]
    bad = [ln for ln in fixed if not is_tme_link(ln)]
    good = [ln for ln in fixed if is_tme_link(ln)]

    if bad:
        await msg.reply_text("⚠️ Ada item yang tidak valid setelah dibersihkan (dilewati):\n" + "\n".join(bad[:20]))

    chans_existing = load_lines(CHANNEL_FILE)
    before = set(chans_existing)
    chans_existing.extend(good)
    save_lines(CHANNEL_FILE, chans_existing)

    added = sorted(set(good) - before)
    if not added:
        await msg.reply_text("ℹ️ Tidak ada link baru yang ditambahkan (mungkin sudah ada).")
        return

    # if many added, limit auto-verify subset
    note = await msg.reply_text(f"✅ Ditambahkan {len(added)} link.\n🤖 Auto-verify dimulai (maks {MAX_AUTOVERIFY_PER_ADD} item)...")
    subset = added[:MAX_AUTOVERIFY_PER_ADD]
    await verify_links(client, subset, note)
    if len(added) > len(subset):
        await note.reply_text(f"ℹ️ {len(added)-len(subset)} sisanya tidak auto-diverify. Jalankan `/verifychan` jika ingin verifikasi semua.")


@app.on_message(filters.me & filters.command("delchan", prefixes="/"))
async def delchan_cmd(_, msg):
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
async def showchan_cmd(_, msg):
    chans = load_lines(CHANNEL_FILE)
    text = "\n".join(chans) if chans else "(kosong)"
    await msg.reply_text(f"📺 **Daftar Channel (t.me):**\n{text}", disable_web_page_preview=True)


# ---------- CORE VERIFY (used by /verifychan and /addchan auto-verify) ----------
async def verify_links(client: Client, links: List[str], parent_msg):
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
                    # try join
                    # check quota before join
                    ok_quota, w = quota_allows_join()
                    if not ok_quota:
                        bad.append(f"{link_n} → quota {w} reached (skipped)")
                        continue
                    try:
                        chat = await client.join_chat(link_n)
                        cache[link_n] = {"chat_id": chat.id, "title": chat.title or ""}
                        save_cache(cache)
                        target = chat.id
                        # record join timestamp
                        join_timestamps.append(time.time())
                    except UserAlreadyParticipant:
                        # attempt to get chat info instead
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
            # adapt
            if e.value >= 300: adaptive_delay = max(adaptive_delay, 15)
            if e.value >= 1200: adaptive_delay = max(adaptive_delay, 20)
            bad.append(f"{link_n} → FloodWait {e.value}s (skipped)")
            # Respect the wait but continue
            await asyncio.sleep(min(e.value + 10, 60 * 60))
        except Exception as e:
            bad.append(f"{link_n} → {e}")

        if i % STATUS_INTERVAL == 0 or i == total:
            await update_status(status, f"🔍 Verifikasi… {i}/{total} selesai.\n✔️ OK: {len(ok)} | ⚠️ Bad: {len(bad)}")

        await human_sleep(1)

    text = f"✅ Verifikasi selesai.\nOK: {len(ok)} | Bad: {len(bad)}"
    if ok:
        text += "\n\n**VALID (sample):**\n" + "\n".join(ok[:30])
    if bad:
        text += "\n\n**INVALID/ERROR:**\n" + "\n".join(bad[:30])
    await update_status(status, text)


@app.on_message(filters.me & filters.command("verifychan", prefixes="/"))
async def verifychan_cmd(client, msg):
    chans = load_lines(CHANNEL_FILE)
    if not chans:
        await msg.reply_text("Tidak ada channel di daftar."); return
    await verify_links(client, chans, msg)


# ---------- JOIN handler (support invite + public optionally) ----------
@app.on_message(filters.me & filters.command("join", prefixes="/"))
async def join_cmd(client, msg):
    global adaptive_delay
    raw = msg.text.split(maxsplit=1)
    if len(raw) < 2:
        await msg.reply_text("Gunakan: `/join <multi-link or @user or username>` (satu per baris).", disable_web_page_preview=True)
        return

    items = [x.strip() for x in raw[1].splitlines() if x.strip()]
    # normalize and classify
    normalized = [normalize_tme_link(x) for x in items]
    invite_links = [ln for ln in normalized if is_invite_link(ln)]
    public_users = [extract_public_username(ln) for ln in normalized if extract_public_username(ln)]

    # prepare items list with kind marker
    work_items = [("invite", ln) for ln in invite_links] + [("public", u) for u in public_users if u]
    if not work_items:
        await msg.reply_text("❌ Tidak ada link undangan atau username publik yang valid.", disable_web_page_preview=True)
        return

    status = await msg.reply_text("🚪 Memulai proses join…")
    cache = load_cache()
    success, failed = [], []
    adaptive_delay = JOIN_DELAY
    count_in_batch = 0
    total = len(work_items)

    for idx, (kind, val) in enumerate(work_items, start=1):
        try:
            # quota check before attempts
            ok_quota, window = quota_allows_join()
            if not ok_quota:
                if window == "hour":
                    await update_status(status, "⛔ Hourly cap reached. Cooling down 60 minutes...")
                    await asyncio.sleep(max(BATCH_COOLDOWN, 60 * 60))
                else:
                    await update_status(status, "⛔ Daily cap reached. Stopping join.")
                    break

            if kind == "invite":
                target = val
                # perform join
                chat = await client.join_chat(target)
                cache[normalize_tme_link(val)] = {"chat_id": chat.id, "title": chat.title or ""}
                save_cache(cache)
                success.append(chat.title or val)
                join_timestamps.append(time.time())
            else:
                username = val
                # try join public (subscribe)
                try:
                    chat = await client.join_chat(username)
                    cache_key = normalize_tme_link(f"https://t.me/{username}")
                    cache[cache_key] = {"chat_id": chat.id, "title": chat.title or ""}
                    save_cache(cache)
                    success.append(chat.title or "@" + username)
                    join_timestamps.append(time.time())
                except UserAlreadyParticipant:
                    success.append(f"(sudah join) @{username}")
                    # still record
                    join_timestamps.append(time.time())

            # adaptively slow down after success
            adaptive_delay = min(max(adaptive_delay, JOIN_DELAY) + 1, 20)

        except UserAlreadyParticipant:
            success.append(f"(sudah join) {val}")
            join_timestamps.append(time.time())
        except InviteHashInvalid:
            failed.append(f"{val} → INVALID")
        except InviteHashExpired:
            failed.append(f"{val} → EXPIRED")
        except FloodWait as e:
            # big flood → abort or long cooldown
            if e.value >= FLOOD_ABORT_SECONDS:
                adaptive_delay = max(adaptive_delay, 20)
                await update_status(status, f"⛔ FloodWait {e.value}s (>= abort threshold). Long cooldown 60 minutes.")
                await asyncio.sleep(60 * 60)
                failed.append(f"{val} → FloodWait {e.value}s (abort)")
                break
            else:
                # respect wait + buffer
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

    # final report
    report = "✅ **Join selesai!**\n"
    report += f"Total attempt: {total} | ✔️ {len(success)} | ⚠️ {len(failed)}\n"
    if success:
        report += "\n**Sukses (sample):**\n" + "\n".join(success[:30])
    if failed:
        report += "\n\n**Gagal/Skip (sample):**\n" + "\n".join(failed[:30])
    await update_status(status, report)


# ---------- CHECK handler (supports /check [limit]) ----------
@app.on_message(filters.me & filters.command("check", prefixes="/"))
async def check_cmd(client, msg):
    parts = msg.text.split(maxsplit=1)
    limit = CHECK_LIMIT
    if len(parts) == 2 and parts[1].isdigit():
        # clamp 10..1000
        limit = max(10, min(1000, int(parts[1])))

    chans = load_lines(CHANNEL_FILE)
    targets = load_lines(LINK_FILE)
    if not chans:
        await msg.reply_text("⚠️ Tidak ada channel. Tambahkan dulu pakai `/addchan`.", disable_web_page_preview=True)
        return
    if not targets:
        await msg.reply_text("⚠️ Tidak ada target pencarian. Tambahkan dulu pakai `/addlist`.", disable_web_page_preview=True)
        return

    status = await msg.reply_text(f"🔎 Memulai pengecekan (limit {limit} per channel)...")
    cache = load_cache()
    total = len(chans)
    found, processed = 0, 0
    lines = []

    for i, link in enumerate(chans, start=1):
        link_n = normalize_tme_link(link)
        try:
            # ensure join if invite
            # but don't aggressively join many: check quota
            if is_invite_link(link_n):
                ok_quota, win = quota_allows_join()
                if ok_quota:
                    await ensure_join_if_needed(client, link_n, cache)
                    # record join in ensure_join_if_needed
                else:
                    lines.append(f"{link_n}: ⚠️ quota {win} reached (will skip joining this invite).")
            target = None
            cached = cache.get(link_n)
            if cached and "chat_id" in cached:
                target = int(cached["chat_id"])
            else:
                uname = extract_public_username(link_n)
                if uname:
                    target = uname

            if target is None:
                lines.append(f"{link_n}: ⚠️ Tidak bisa diakses (undangan belum ter-cache). Jalankan /verifychan.")
                processed += 1
                continue

            chat = await client.get_chat(target)

            ok = False
            async for m in client.get_chat_history(chat.id, limit=limit):
                all_tlinks = get_all_tme_links(m)
                # check each target
                for tgt in targets:
                    tgt_low = tgt.lower()
                    # exact url-ish
                    if tgt_low.startswith("http"):
                        if any(tgt_low in tl.lower() for tl in all_tlinks):
                            ok = True; break
                    else:
                        hay = (m.text or "") + "\n" + (m.caption or "")
                        if tgt_low in hay.lower() or any(tgt_low in tl.lower() for tl in all_tlinks):
                            ok = True; break
                if ok:
                    break

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


# ---------- helper: ensure join if needed (used by /check) ----------
async def ensure_join_if_needed(client: Client, link: str, cache: Dict[str, dict]):
    """Jika link adalah undangan dan belum tercache, coba join dengan hormat."""
    global adaptive_delay
    link_n = normalize_tme_link(link)
    if not is_invite_link(link_n):
        return
    # quota check
    ok_quota, win = quota_allows_join()
    if not ok_quota:
        return
    try:
        chat = await client.join_chat(link_n)
        cache[link_n] = {"chat_id": chat.id, "title": chat.title or ""}
        save_cache(cache)
        # record join
        join_timestamps.append(time.time())
        adaptive_delay = min(max(adaptive_delay, JOIN_DELAY) + 1, 20)
    except UserAlreadyParticipant:
        # try get chat and cache it
        try:
            ch = await client.get_chat(link_n)
            cache[link_n] = {"chat_id": ch.id, "title": ch.title or ""}
            save_cache(cache)
        except Exception:
            pass
    except FloodWait as e:
        # large flood -> respect
        if e.value >= 300:
            adaptive_delay = max(adaptive_delay, 15)
        if e.value >= 1200:
            adaptive_delay = max(adaptive_delay, 20)
        await asyncio.sleep(min(e.value + 10, 60 * 60))
    except Exception:
        pass


# ---------- startup message & run ----------
if __name__ == "__main__":
    print("🚀 Userbot siap. Kirim /help di Saved Messages kamu.")
    app.run()

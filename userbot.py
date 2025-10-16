#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Userbot - Link-only manager
- Auto-fix link input & auto-verify
- Robust /check (text, caption, entities, buttons)
- /join for invite + public
- Anti-flood, batching, cooldown, governor caps
- Support SESSION_STRING via environment (for Easypanel deploy)
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
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")
PHONE = (os.getenv("PHONE", "") or "").strip()

CHANNEL_FILE = "channels.txt"
LINK_FILE = "links.txt"
CACHE_FILE = "channels_cache.json"

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

URL_TME_RE = re.compile(r"(?:https?://)?t\.me/[^\s\)\]\}\,]+", re.IGNORECASE)
AT_USER_RE = re.compile(r"(?<!\w)@([A-Za-z0-9_]{3,})(?!\w)")

def normalize_tme_link(s: str) -> str:
    if not s: return ""
    s = s.strip().strip(" \t\n\r\"'<>").rstrip(".,;:)]}»\"'")
    s = re.sub(r"\s+", "", s)
    if s.lower().startswith("https://@") or s.lower().startswith("http://@"):
        s = s[s.find("@"):]
    m = BARE_USER_RE.match(s)
    if s.startswith("@") and m:
        return f"https://t.me/{m.group(1)}"
    if m and not s.lower().startswith("http"):
        return f"https://t.me/{m.group(1)}"
    if s.lower().startswith("t.me/"):
        s = "https://" + s
    if not s.lower().startswith("http"):
        s = "https://" + s
    s = s.replace("t.me//", "t.me/")
    if s.endswith("/"): s = s[:-1]
    return s

def is_tme_link(s: str) -> bool:
    return bool(TME_ANY_RE.search(s))

def is_invite_link(s: str) -> bool:
    return bool(INV_PLUS_RE.match(s) or INV_JOIN_RE.match(s))

def extract_invite_code(s: str) -> Optional[str]:
    m = INV_PLUS_RE.match(s) or INV_JOIN_RE.match(s)
    return m.group(1) if m else None

def extract_public_username(s: str) -> Optional[str]:
    s = s.strip()
    m = PUB_USER_RE.match(s) or PUB_POST_RE.match(s)
    return m.group(1) if m else None

# ---------- MESSAGE LINK EXTRACTION ----------
def normalize_tme_text_link(s: str) -> str:
    s = s.strip().rstrip(".,;:)]}»\"'")
    if s.lower().startswith("t.me/"):
        s = "https://" + s
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
            if "t.me/" in url:
                out.append(normalize_tme_text_link(url))
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
    seen, out = set(), []
    for x in acc:
        if not x: continue
        n = normalize_tme_link(x)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out

# ---------- DELAY & STATUS ----------
async def human_sleep(base: Optional[float] = None):
    global adaptive_delay
    base = base or adaptive_delay
    jitter = base * random.uniform(0.2, 0.4)
    await asyncio.sleep(base + jitter)

async def update_status(msg, text: str):
    try:
        await msg.edit_text(text, disable_web_page_preview=True)
    except Exception:
        try: await msg.reply_text(text, disable_web_page_preview=True)
        except Exception: pass

# ---------- GOVERNOR ----------
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

# ---------- CLIENT INIT (support SESSION_STRING) ----------
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

# ---------- COMMANDS & HANDLERS ----------
@app.on_message(filters.me & filters.command("ping", prefixes="/"))
async def ping_cmd(_, msg):
    await msg.reply_text("pong ✅")

# (Semua handler /help, /join, /check, dll dari versi kamu tetap sama di bawah sini)
# --------------------------------------------------------------------------
# Tempel seluruh bagian kode lanjutan kamu mulai dari:
#   # ---------- COMMANDS: help & runtime settings ----------
# sampai akhir file tanpa mengubah apapun
# --------------------------------------------------------------------------

if __name__ == "__main__":
    print("🚀 Userbot siap. (SESSION_STRING mode)" if SESSION_STRING else "🚀 Userbot siap. (PHONE mode)")
    app.run()

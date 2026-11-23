# -*- coding: utf-8 -*-
"""
premium_emoji.py — AIR_NUMBER_BOT এর Telegram Premium (custom) emoji সাপোর্ট
=========================================================================
কীভাবে কাজ করে
--------------
bot.py এর মেসেজ Markdown এ লেখা, কিন্তু custom emoji এর জন্য HTML (<tg-emoji>) লাগে।
তাই এই মডিউল telegram.Bot.send_message / edit_message_text (এবং OTP group এর raw HTTP payload)
আটকে নিয়ে —

  1. দেশের পতাকা  (GLOBAL_EMOJI.txt)      → প্রিমিয়াম পতাকা
  2. সার্ভিসের আইকন (PREMUAM_APPS.txt)     → প্রিমিয়াম অ্যাপ আইকন (সার্ভিসের নাম মিললে)
  3. Inline বাটনের আইকন (পতাকা/সার্ভিস/অ্যাডমিন লেবেল) → icon_custom_emoji_id

বসায়। কোনো কারণে Telegram রিজেক্ট করলে সাথে সাথে আগের সাধারণ মেসেজ/বাটন দিয়েই
আবার পাঠানো হয়, আর বারবার fail করলে নিজে থেকে বন্ধ হয়ে যায় (বট কখনো আটকে যায় না)।

ফাইল: GLOBAL_EMOJI.txt   → পতাকা|emoji_id
      PREMUAM_APPS.txt    → সার্ভিস_নাম|fallback_emoji|emoji_id
(প্রথমে DATA_DIR তে খোঁজে, না পেলে এই ফাইলের পাশে)
"""

import copy
import html
import json
import logging
import os
import re

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

logger = logging.getLogger("premium_emoji")
esc = html.escape

FLAGS = {}            # "🇧🇩" -> "5911365056594973179"
APPS = {}             # "whatsapp" -> {"name", "emoji", "id"}
STATE = {"enabled": True, "ok": 0, "fallback": 0, "consec_fail": 0, "last_error": "",
         "button_icons": False, "installed": False}
_NS = {}
_HERE = os.path.dirname(os.path.abspath(__file__))

# অ্যাডমিন/ইউটিলিটি বাটনের লেবেল → AIR এর CUSTOM_EMOJIS
LABEL_ICONS = {
    "user manager": "6307777408300753473", "search user": "6206446249181189526",
    "download user list": "6203886371363364022", "edit balance": "6204162490515855272",
    "ban": "6203761490894264678", "banned users": "6203761490894264678",
    "broadcast": "6206080502651164081", "targeted broadcast": "6206080502651164081",
    "giveaway": "6282893896796082998", "analytics": "6266936886405633043",
    "back": "5267490665117275176", "back to admin panel": "5267490665117275176",
    "cancel": "6267000941547885720", "support": "6264853036993090338",
    "stock report": "6206236607532504295", "settings": "6206236607532504295",
    "manage services": "6206236607532504295", "delete numbers": "6206108815075579644",
    "add numbers": "6206375377925839184", "upload file": "6206046503690048595",
    "send now": "4956721670690702265", "overview": "6266936886405633043",
}
SERVICE_CB_PREFIXES = ("svc", "upload_svc", "fake_svc")

RI = "\U0001F1E6-\U0001F1FF"
FLAG_RE = re.compile(f"[{RI}]{{2}}")
LEAD_RE = re.compile(
    f"^([{RI}]{{2}}|[^\\w\\s<>&\"'`*_\\[\\](){{}}@#$%^+=|/\\\\:;,.?!~-][\\uFE0F\\u200d\\U0001F3FB-\\U0001F3FF]*"
    f"(?:\\u200d[^\\w\\s][\\uFE0F]*)*)\\s*")
TAG_SPLIT = re.compile(r"(<[^>]+>)")


# ═══════════════════════════════════════════════════════════════════════════
#  Loading
# ═══════════════════════════════════════════════════════════════════════════
def _data_dir():
    return _NS.get("DATA_DIR") or _HERE


def _find(fname):
    for d in (_data_dir(), _HERE):
        p = os.path.join(d, fname)
        if os.path.exists(p):
            return p
    return None


def _cfg_path():
    return os.path.join(_data_dir(), "premium_emoji.json")


def load():
    FLAGS.clear()
    APPS.clear()
    p = _find("GLOBAL_EMOJI.txt")
    if p:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) >= 2 and parts[1].strip().isdigit():
                    FLAGS[parts[0].strip()] = parts[1].strip()
    p = _find("PREMUAM_APPS.txt")
    if p:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) >= 3 and parts[2].strip().isdigit():
                    APPS[parts[0].strip().lower()] = {"name": parts[0].strip(), "emoji": parts[1].strip(),
                                                      "id": parts[2].strip()}
    try:
        if os.path.exists(_cfg_path()):
            STATE["enabled"] = bool(json.load(open(_cfg_path(), encoding="utf-8")).get("enabled", True))
    except Exception:
        pass


def save_cfg():
    try:
        with open(_cfg_path(), "w", encoding="utf-8") as f:
            json.dump({"enabled": STATE["enabled"]}, f)
    except Exception:
        logger.exception("premium_emoji cfg save failed")


def set_enabled(v):
    STATE["enabled"] = bool(v)
    STATE["consec_fail"] = 0
    save_cfg()


def add_app(name, emoji_id, fallback="📱"):
    """PREMUAM_APPS.txt এ সার্ভিস যোগ/আপডেট (DATA_DIR এ লেখে)।"""
    if not emoji_id.isdigit() or len(emoji_id) < 10:
        raise ValueError("emoji id must be a long number")
    target = os.path.join(_data_dir(), "PREMUAM_APPS.txt")
    lines = []
    src = _find("PREMUAM_APPS.txt")
    if src:
        lines = open(src, encoding="utf-8").read().splitlines()
    key = name.strip().lower()
    new = f"{name.strip()}|{fallback}|{emoji_id}"
    replaced = False
    for i, ln in enumerate(lines):
        if ln.split("|")[0].strip().lower() == key and not ln.startswith("#"):
            lines[i] = new
            replaced = True
    if not replaced:
        lines.append(new)
    with open(target, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    load()


def status_text():
    return (f"✨ <b>Premium Emoji</b>\n\nStatus: <b>{'ON' if STATE['enabled'] else 'OFF'}</b>\n"
            f"🏳️ Flags loaded: <b>{len(FLAGS)}</b>\n📱 App icons loaded: <b>{len(APPS)}</b>\n"
            f"🔘 Button icons: <b>{'supported' if STATE['button_icons'] else 'not supported by this PTB version'}</b>\n"
            f"✅ Sent with premium: <b>{STATE['ok']}</b>  |  ↩️ Fell back: <b>{STATE['fallback']}</b>\n"
            f"{('⚠️ Last error: <code>' + esc(STATE['last_error'][:120]) + '</code>') if STATE['last_error'] else ''}")


# ═══════════════════════════════════════════════════════════════════════════
#  Markdown (legacy) -> HTML
# ═══════════════════════════════════════════════════════════════════════════
def _unesc(s):
    return esc(re.sub(r"\\([_*`\[\]()\\])", r"\1", s))


def _find_close(s, start, ch):
    j = start
    while j < len(s):
        if s[j] == "\\":
            j += 2
            continue
        if s[j] == ch:
            return j
        j += 1
    return -1


def md_to_html(s):
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and s[i + 1] in "_*`[]()\\":
            out.append(esc(s[i + 1]))
            i += 2
            continue
        if s.startswith("```", i):
            j = s.find("```", i + 3)
            if j != -1:
                body = re.sub(r"^[A-Za-z0-9_+-]*\n", "", s[i + 3:j], count=1)
                out.append("<pre>" + esc(body) + "</pre>")
                i = j + 3
                continue
        if c == "`":
            j = s.find("`", i + 1)
            if j > i + 1:
                out.append("<code>" + esc(s[i + 1:j]) + "</code>")
                i = j + 1
                continue
        if c in "*_":
            j = _find_close(s, i + 1, c)
            if j > i + 1:
                tag = "b" if c == "*" else "i"
                out.append(f"<{tag}>{_unesc(s[i + 1:j])}</{tag}>")
                i = j + 1
                continue
        if c == "[":
            m = re.match(r"\[([^\]]+)\]\(([^)\s]+)\)", s[i:])
            if m:
                out.append(f'<a href="{html.escape(m.group(2), quote=True)}">{esc(m.group(1))}</a>')
                i += m.end()
                continue
        out.append(esc(c))
        i += 1
    return "".join(out)


# ═══════════════════════════════════════════════════════════════════════════
#  Emoji substitution
# ═══════════════════════════════════════════════════════════════════════════
def _tag(eid, ch):
    return f'<tg-emoji emoji-id="{eid}">{ch}</tg-emoji>'


def _service_rules():
    rules = []
    for sid, svc in (_NS.get("services") or {}).items():
        name = str(svc.get("name", ""))
        icon = str(svc.get("icon", ""))
        app = APPS.get(name.lower()) or APPS.get(str(sid).lower())
        if app and icon:
            pat = re.compile(re.escape(icon) + r"(?=[ \t]*(?:<b>)?(?:Service:[ \t]*(?:</b>)?[ \t]*)?"
                             + re.escape(esc(name)) + ")")
            rules.append((pat, icon, app["id"]))
    return rules


def apply_html(h):
    for pat, icon, eid in _service_rules():
        h = pat.sub(lambda m, e=eid, ic=icon: _tag(e, ic), h)
    out, inside = [], False
    for part in TAG_SPLIT.split(h):
        if part.startswith("<") and part.endswith(">"):
            low = part.lower()
            if low.startswith("<tg-emoji"):
                inside = True
            elif low.startswith("</tg-emoji"):
                inside = False
            out.append(part)
            continue
        if inside or not part:
            out.append(part)
            continue
        out.append(FLAG_RE.sub(lambda m: _tag(FLAGS[m.group()], m.group()) if m.group() in FLAGS else m.group(), part))
    return "".join(out)


def _might_need(text):
    if FLAG_RE.search(text):
        return True
    low = text.lower()
    return any(k in low for k in APPS)


def transform_text(text, mode):
    """(new_html_text) অথবা None (কিছু বদলানোর নেই / স্পর্শ করা যাবে না)।"""
    if not isinstance(text, str) or mode == "MarkdownV2" or not _might_need(text):
        return None
    if mode == "HTML":
        base = text
    elif mode == "Markdown":
        base = md_to_html(text)
    else:
        base = esc(text)
    new = apply_html(base)
    if new == base:
        return None
    return new


# ═══════════════════════════════════════════════════════════════════════════
#  Button icons
# ═══════════════════════════════════════════════════════════════════════════
def _icon_for(text, cb):
    """(new_text, emoji_id) অথবা None"""
    m = LEAD_RE.match(text or "")
    if not m:
        return None
    lead, rest = m.group(1), (text or "")[m.end():]
    if not rest.strip():
        return None
    if FLAG_RE.fullmatch(lead) and lead in FLAGS:
        return rest, FLAGS[lead]
    if cb and ":" in cb:
        pre, sid = cb.split(":", 1)
        if pre in SERVICE_CB_PREFIXES:
            svc = (_NS.get("services") or {}).get(sid)
            if svc:
                app = APPS.get(str(svc.get("name", "")).lower()) or APPS.get(sid.lower())
                if app:
                    return rest, app["id"]
    label = re.sub(r"\s+", " ", rest.strip().lower())
    if label in LABEL_ICONS:
        return rest, LABEL_ICONS[label]
    return None


def _clone_button(b, new_text, eid):
    try:
        d = b.to_dict()
        d["text"] = new_text
        d["icon_custom_emoji_id"] = eid
        nb = InlineKeyboardButton.de_json(d, None)
        if nb is not None and getattr(nb, "icon_custom_emoji_id", None) == eid and nb.text == new_text:
            return nb
    except Exception:
        logger.debug("button clone failed", exc_info=True)
    return None


def transform_markup(mk):
    if not STATE["button_icons"] or not isinstance(mk, InlineKeyboardMarkup):
        return mk
    changed, rows = False, []
    for row in mk.inline_keyboard:
        nr = []
        for b in row:
            r = _icon_for(getattr(b, "text", ""), getattr(b, "callback_data", None))
            nb = _clone_button(b, r[0], r[1]) if r and not getattr(b, "icon_custom_emoji_id", None) else None
            nr.append(nb or b)
            changed = changed or bool(nb)
        rows.append(nr)
    return InlineKeyboardMarkup(rows) if changed else mk


def transform_payload(payload):
    """raw HTTP sendMessage payload (dict) → নতুন dict অথবা None"""
    if not isinstance(payload, dict):
        return None
    new = copy.deepcopy(payload)
    changed = False
    t = transform_text(payload.get("text"), payload.get("parse_mode"))
    if t is not None:
        new["text"], new["parse_mode"] = t, "HTML"
        changed = True
    mk = new.get("reply_markup")
    if isinstance(mk, dict):
        for row in mk.get("inline_keyboard", []):
            for b in row:
                if isinstance(b, dict) and not b.get("icon_custom_emoji_id"):
                    r = _icon_for(b.get("text", ""), b.get("callback_data"))
                    if r:
                        b["text"], b["icon_custom_emoji_id"] = r
                        changed = True
    return new if changed else None


# ═══════════════════════════════════════════════════════════════════════════
#  Patching
# ═══════════════════════════════════════════════════════════════════════════
def _note_fail(e):
    msg = str(e)
    low = msg.lower()
    if any(k in low for k in ("emoji", "entit", "parse", "icon", "button")) and not any(
            k in low for k in ("not modified", "not found", "chat not found", "blocked")):
        STATE["consec_fail"] += 1
        STATE["last_error"] = msg
        STATE["fallback"] += 1
        if STATE["consec_fail"] >= 5:
            STATE["enabled"] = False
            logger.error("premium_emoji: 5 failures in a row — auto-disabled (%s)", msg)


def _wrap_bot_method(orig):
    async def wrapper(self, *args, **kwargs):
        if not STATE["enabled"] or args or "text" not in kwargs or kwargs.get("entities"):
            return await orig(self, *args, **kwargs)
        try:
            pm = kwargs.get("parse_mode")
            mode = pm if isinstance(pm, str) else None
            new_text = transform_text(kwargs.get("text"), mode)
            new_markup = transform_markup(kwargs.get("reply_markup"))
            if new_text is None and new_markup is kwargs.get("reply_markup"):
                try:
                    return await orig(self, *args, **kwargs)
                except BadRequest as e:
                    # অ্যাডমিনের লেখা <tg-emoji> রিজেক্ট হলে ট্যাগ সরিয়ে সাধারণ ইমোজিতে পাঠাও
                    if "emoji" in str(e).lower() and "<tg-emoji" in str(kwargs.get("text", "")):
                        k2 = dict(kwargs)
                        k2["text"] = re.sub(r"</?tg-emoji[^>]*>", "", kwargs["text"])
                        return await orig(self, *args, **k2)
                    raise
            nk = dict(kwargs)
            if new_text is not None:
                nk["text"], nk["parse_mode"] = new_text, "HTML"
            nk["reply_markup"] = new_markup
        except Exception:
            logger.debug("premium transform failed", exc_info=True)
            return await orig(self, *args, **kwargs)
        try:
            res = await orig(self, *args, **nk)
            STATE["ok"] += 1
            STATE["consec_fail"] = 0
            return res
        except BadRequest as e:
            _note_fail(e)
            return await orig(self, *args, **kwargs)      # আগের মতো সাধারণ ভাবে পাঠাও
    return wrapper


def install(ns):
    """extra_features.setup() থেকে ডাকা হয়।"""
    global _NS
    _NS = ns
    load()
    try:
        InlineKeyboardButton("t", callback_data="t", icon_custom_emoji_id="1234567890")
        STATE["button_icons"] = True
    except TypeError:
        STATE["button_icons"] = False
    if STATE["installed"]:
        return
    Bot.send_message = _wrap_bot_method(Bot.send_message)
    Bot.edit_message_text = _wrap_bot_method(Bot.edit_message_text)

    orig_group = ns.get("_send_group_payload")
    if orig_group:
        async def _send_group_payload(payload):
            if STATE["enabled"]:
                try:
                    new = transform_payload(payload)
                except Exception:
                    new = None
                if new is not None:
                    if await orig_group(new):
                        STATE["ok"] += 1
                        return True
                    STATE["fallback"] += 1          # fail হলে সাধারণ payload দিয়ে আবার
            return await orig_group(payload)
        ns["_send_group_payload"] = _send_group_payload
    STATE["installed"] = True
    logger.info("premium_emoji: installed (flags=%d, apps=%d, button_icons=%s)",
                len(FLAGS), len(APPS), STATE["button_icons"])

# -*- coding: utf-8 -*-
"""
hub_features.py — "main (14).py" বট থেকে আনা নতুন ফিচার
=====================================================
extra_features.setup() এর শেষে নিজে থেকে লোড হয় (আলাদা কিছু করতে হবে না)।

ইউজারদের জন্য
  🔍 Search Number   — ৩-৯ ডিজিট দিয়ে স্টকের নাম্বার খুঁজে সরাসরি নেওয়া
  📊 Traffic         — গত ১ ঘণ্টায় সার্ভিস/দেশ অনুযায়ী সফল OTP, Refresh/Close বাটন
Admin (🧰 New Tools)
  🎨 Menu Design     — start / get number / country / search / traffic / refer / withdrawal /
                        support মেসেজের লেখা ও ইনলাইন বাটন বটের ভেতর থেকেই এডিট (+ OTP গ্রুপের বাটন)
  🏆 Leaderboard     — Top referrers, Top OTP receivers (all-time / today), last withdrawals
  📦 Stock Tools     — Unused / Used নাম্বার TXT ডাউনলোড (সার্ভিস অনুযায়ীও), স্টক সামারি
  💳 W. Methods      — উইথড্র মেথড যোগ/মুছা (settings["withdrawMethods"])
  📡 Panel OTP Stats — যেকোনো প্রোভাইডারের যেকোনো প্যানেলে কতগুলো OTP এলো (আজ/৭/৩০ দিন/মোট)
"""

import html
import io
import logging
import re
import time
from datetime import datetime, timedelta

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (ApplicationHandlerStop, CallbackQueryHandler,
                          ContextTypes, MessageHandler, filters)

import extra_features as xf
from extra_features import back_row, btn, edit, esc, fmt_num, is_adm, reply

logger = logging.getLogger("hub_features")

MENUS = {
    "start":          ("👋 Start / Welcome", "{name}"),
    "get_number":     ("🎯 Get Number (service list)", ""),
    "select_country": ("🌍 Select Country", "{service} {icon}"),
    "search_number":  ("🔍 Search Number prompt", ""),
    "traffic":        ("📊 Traffic (header)", ""),
    "refer":          ("👥 Referral", "{ref_link} {total_ref} {ref_percent} {earned}"),
    "withdrawal":     ("💸 Withdrawal", "{balance} {min_withdraw} {user_id}"),
    "support":        ("💬 Support", ""),
    "otp_group":      ("📨 OTP Group (buttons only)", ""),
}
BUTTONS_ONLY = {"otp_group"}


def NS():
    return xf.NS


# ═══════════════════════════════════════════════════════════════════════════
#  MENU DESIGN — runtime helpers (bot.py এখান থেকে ডাকে)
# ═══════════════════════════════════════════════════════════════════════════
class _Safe(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def _cfg(key):
    return xf.DATA.setdefault("menu", {}).get(key) or {}


def _rows(key):
    return [[btn(b["text"], url=b["url"], style="primary")] for b in _cfg(key).get("buttons", [])]


def menu(key, default_text, default_pm="Markdown", **vars):
    """(text, parse_mode, extra_inline_rows, is_custom)"""
    cfg = _cfg(key)
    custom = cfg.get("text")
    rows = _rows(key)
    if custom and key not in BUTTONS_ONLY:
        safe = _Safe({k: esc(str(v)) for k, v in vars.items()})
        try:
            text = custom.format_map(safe)
        except Exception:
            text = custom
        return text, "HTML", rows, True
    return default_text, default_pm, rows, False


def with_rows(markup, rows):
    """InlineKeyboardMarkup এর নিচে extra row জোড়া লাগায়।"""
    if not rows:
        return markup
    base = [list(r) for r in markup.inline_keyboard] if markup else []
    return InlineKeyboardMarkup(base + rows)


# ═══════════════════════════════════════════════════════════════════════════
#  MENU DESIGN — admin UI
# ═══════════════════════════════════════════════════════════════════════════
def md_list_kb():
    rows, row = [], []
    for key, (title, _) in MENUS.items():
        mark = "✅ " if (_cfg(key).get("text") or _cfg(key).get("buttons")) else ""
        row.append(btn(f"{mark}{title}"[:34], f"hx:mde:{key}", style="primary"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("♻️ Reset ALL to defaults", "hx:mdra", style="danger")])
    rows.append(back_row("xf:menu"))
    return InlineKeyboardMarkup(rows)


MD_INTRO = ("🎨 <b>Menu Design</b>\n\nপ্রতিটা স্ক্রিনের লেখা ও ইনলাইন বাটন এখান থেকে বদলানো যায়।\n"
            "✅ চিহ্ন মানে কাস্টমাইজ করা আছে। লেখায় bold/italic/প্রিমিয়াম ইমোজি পাঠালে ফরম্যাট ঠিক থাকবে।")


def md_detail(key):
    title, variables = MENUS[key]
    cfg = _cfg(key)
    txt_state = "default" if not cfg.get("text") else f"custom ({len(cfg['text'])} chars)"
    btns = cfg.get("buttons", [])
    lines = [f"🎨 <b>{esc(title)}</b>", ""]
    if key not in BUTTONS_ONLY:
        lines.append(f"📝 Text: <b>{txt_state}</b>")
        if variables:
            lines.append(f"🔣 Variables: <code>{esc(variables)}</code>")
    lines.append(f"🔘 Extra buttons: <b>{len(btns)}</b>")
    rows = []
    if key not in BUTTONS_ONLY:
        rows.append([btn("📝 Edit Text", f"hx:mdt:{key}", style="primary"),
                     btn("👁 Preview", f"hx:mdp:{key}", style="success")])
    rows.append([btn("🔘 Edit Buttons", f"hx:mdb:{key}", style="success"),
                 btn("♻️ Reset", f"hx:mdr:{key}", style="danger")])
    rows.append(back_row("hx:md"))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def md_buttons_kb(key):
    rows = [[btn(f"🗑 {b['text']}"[:40], f"hx:mdd:{key}:{i}", style="danger")]
            for i, b in enumerate(_cfg(key).get("buttons", []))]
    rows.append([btn("➕ Add Inline Button", f"hx:mda:{key}", style="success")])
    rows.append(back_row(f"hx:mde:{key}"))
    return InlineKeyboardMarkup(rows)


URL_RE = re.compile(r"^(.*?)\s*[-–|]\s*((?:https?://|t\.me/)\S+)\s*$", re.S)


def parse_button(text):
    m = URL_RE.match(text.strip())
    if not m or not m.group(1).strip():
        return None
    url = m.group(2)
    if url.startswith("t.me/"):
        url = "https://" + url
    return {"text": m.group(1).strip()[:60], "url": url}


# ═══════════════════════════════════════════════════════════════════════════
#  SEARCH NUMBER
# ═══════════════════════════════════════════════════════════════════════════
SEARCH_DEFAULT = ("🔍 *Search Number*\n\nEnter *3 to 9 digits* to find a number in stock.\n\n"
                  "Example: `880` or `1712`")
CANCEL_USER_KB = lambda: InlineKeyboardMarkup([[btn("❌ Cancel", "hx:snx", style="danger")]])


async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ns = NS()
    if not await ns["ensure_verified"](update, context):
        return
    uid = str(update.effective_user.id)
    sess = ns["get_session"](uid)
    sess["state"], sess["data"] = "hx_usearch", None
    text, pm, rows, _ = menu("search_number", SEARCH_DEFAULT)
    await update.effective_message.reply_text(text, parse_mode=pm,
                                              reply_markup=with_rows(CANCEL_USER_KB(), rows))


def find_numbers(q, limit=10):
    ns = NS()
    pref, cont = [], []
    for cc, svcs in ns["numbers_by_cs"].items():
        if cc not in ns["countries"]:
            continue
        for svc, nums in svcs.items():
            for n in nums:
                if n.startswith(q):
                    pref.append((svc, cc, n))
                elif len(q) >= 4 and q in n:
                    cont.append((svc, cc, n))
        if len(pref) >= 400:
            break
    total = len(pref) + len(cont)
    return (pref + cont)[:limit], total


async def search_input(update: Update, context: ContextTypes.DEFAULT_TYPE, sess):
    ns = NS()
    msg = update.effective_message
    raw = (msg.text or "").strip()
    if raw and not raw[0].isascii():          # অন্য মেনু বাটন চাপলে সার্চ বন্ধ, বাটনটা স্বাভাবিকভাবে চলুক
        sess["state"], sess["data"] = None, None
        return "PASS"
    q = re.sub(r"[\s+\-]", "", raw)
    if not re.fullmatch(r"\d{3,9}", q):
        return await msg.reply_text("❌ Send only 3 to 9 digits (example: 880).",
                                    reply_markup=CANCEL_USER_KB())
    sess["state"], sess["data"] = None, None
    found, total = find_numbers(q)
    if not found:
        return await msg.reply_text(f"😕 No number found for <code>{esc(q)}</code>.", parse_mode=ParseMode.HTML)
    rows = []
    for i, (svc, cc, n) in enumerate(found):
        s = ns["services"].get(svc, {"icon": "📞", "name": svc})
        c = ns["countries"].get(cc, {"flag": "🌍", "name": cc})
        rows.append([btn(f"{s['icon']} {c['flag']} +{n}", f"hx:sn:{svc}:{cc}:{n}",
                         style=["primary", "success", "danger"][i % 3])])
    await msg.reply_text(f"🔍 <b>{total}</b> match(es) for <code>{esc(q)}</code> — showing {len(found)}.\nTap a number to take it:",
                         parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def claim_number(update: Update, context: ContextTypes.DEFAULT_TYPE, parts):
    from datetime import timezone
    from telegram import CopyTextButton, InlineKeyboardButton
    ns = NS()
    q = update.callback_query
    if not await ns["ensure_verified"](update, context):
        return
    _, _, svc, cc, number = parts[:5]
    uid = str(update.effective_user.id)
    sess = ns["get_session"](uid)
    now = time.time()
    cooldown = ns["settings"].get("cooldownSeconds", 5)
    if (now - sess["last_number_time"]) < cooldown and sess["current_numbers"]:
        return await q.answer(f"⏳ Wait {int(cooldown - (now - sess['last_number_time']))}s", show_alert=True)
    pool = ns["numbers_by_cs"].get(cc, {}).get(svc, [])
    if number not in pool:
        return await q.answer("❌ That number was just taken.", show_alert=True)
    pool.remove(number)
    for old in sess["current_numbers"]:
        ns["active_numbers"].pop(old, None)
    ns["active_numbers"][number] = {"userId": uid, "countryCode": cc, "service": svc,
                                    "assignedAt": datetime.now(timezone.utc).isoformat(),
                                    "lastOTP": None, "otpCount": 0}
    await ns["async_save_numbers"]()
    await ns["async_save_active"]()
    sess["current_numbers"], sess["current_service"] = [number], svc
    sess["current_country"], sess["last_number_time"] = cc, now
    await q.answer()
    s = ns["services"].get(svc, {"icon": "📞", "name": svc})
    c = ns["countries"].get(cc, {"flag": "🌍", "name": cc})
    price = ns["get_otp_price"](cc)
    text = (f"✅ *1 Number Assigned!*\n\n{s['icon']} *Service:* {s['name']}\n"
            f"{c['flag']} *Country:* {c['name']}\n💵 *Earnings per OTP:* {price:.2f} taka\n\n"
            f"📌 OTP automatically আসবে।")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📋 +{number}", copy_text=CopyTextButton(text=f"+{number}"), **_style("primary"))],
        [btn("📨 Open OTP Group", url=ns["OTP_GROUP"], style="primary")],
        [btn("🔄 Get New Numbers", f"newnum:{svc}:{cc}", style="success")],
    ])
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


def _style(s):
    """PTB ভার্সন style= না বুঝলে খালি dict।"""
    from telegram import InlineKeyboardButton
    try:
        InlineKeyboardButton("t", callback_data="t", style=s)
        return {"style": s}
    except TypeError:
        return {}


# ═══════════════════════════════════════════════════════════════════════════
#  TRAFFIC
# ═══════════════════════════════════════════════════════════════════════════
def _ts(item):
    try:
        dt = datetime.fromisoformat(str(item.get("timestamp", "")).replace("Z", "+00:00"))
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        return None


def traffic_text(window_min=60):
    ns = NS()
    cutoff = datetime.now() - timedelta(minutes=window_min)
    stats = {}
    for item in ns["otp_log"]:
        dt = _ts(item)
        if not dt or dt < cutoff:
            continue
        svc, cc = item.get("service") or "?", item.get("countryCode") or "?"
        stats.setdefault(svc, {})
        stats[svc][cc] = stats[svc].get(cc, 0) + 1
    avail = sum(len(n) for svcs in ns["numbers_by_cs"].values() for n in svcs.values())
    custom, pm, _, is_custom = menu("traffic", "", "HTML")
    out = []
    if is_custom:
        out.append(custom + "\n")
    out.append("╔═════════════════╗\n║  📈 <b>NETWORK TRAFFIC</b>  (last hour)\n╚═════════════════╝\n")
    if not stats:
        out.append("<i>No successful OTPs in the last hour.</i>\n")
    else:
        ranked = sorted(stats.items(), key=lambda kv: -sum(kv[1].values()))[:6]
        for svc, ccs in ranked:
            s = ns["services"].get(svc, {"icon": "📞", "name": svc})
            out.append(f"[ {s['icon']} <b>{esc(str(s['name']))}</b> ] — {sum(ccs.values())}\n│")
            top = sorted(ccs.items(), key=lambda kv: -kv[1])[:5]
            for i, (cc, n) in enumerate(top):
                c = ns["countries"].get(cc, {"flag": "🌍", "name": cc})
                out.append(f"├ {c['flag']} <b>{esc(str(c['name']))}</b> (+{esc(cc)}) ╰ ✅ {n}")
            out.append("")
    out.append(f"━━━━━━━━━━━━━━━━━━\n📦 Available: <b>{avail}</b>  |  🚀 Assigned: <b>{len(ns['active_numbers'])}</b>")
    return "\n".join(out)


def traffic_kb():
    _, _, rows, _ = menu("traffic", "", "HTML")
    base = [[btn("🔄 Refresh", "hx:tr", style="primary")], [btn("❌ Close", "hx:close", style="danger")]]
    return InlineKeyboardMarkup(rows + base)


async def traffic_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await NS()["ensure_verified"](update, context):
        return
    await update.effective_message.reply_text(traffic_text(), parse_mode=ParseMode.HTML,
                                              reply_markup=traffic_kb())


# ═══════════════════════════════════════════════════════════════════════════
#  LEADERBOARD / STOCK / W.METHODS  — admin screens
# ═══════════════════════════════════════════════════════════════════════════
def _name(uid):
    u = NS()["users"].get(str(uid), {})
    return f"<a href='tg://user?id={uid}'>{esc(str(u.get('first_name') or uid)[:16])}</a>"


def lb_menu_kb():
    return InlineKeyboardMarkup([
        [btn("👥 Top Referrers", "hx:lb:refs", style="primary"), btn("📨 Top OTP (all-time)", "hx:lb:otps", style="success")],
        [btn("🔥 Top OTP (today)", "hx:lb:today", style="danger"), btn("💸 Last Withdrawals", "hx:lb:wd", style="primary")],
        back_row("xf:menu"),
    ])


def lb_text(kind):
    ns = NS()
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    if kind == "refs":
        rows = sorted(((u, d.get("referralCount", 0)) for u, d in ns["users"].items()), key=lambda x: -x[1])
        title, fmt = "TOP 10 REFERRERS", lambda v: f"{v} referrals"
    elif kind == "otps":
        rows = sorted(((u, d.get("otpCount", 0)) for u, d in ns["earnings"].items()), key=lambda x: -x[1])
        title, fmt = "TOP 10 OTP RECEIVERS (ALL TIME)", lambda v: f"{v} OTP"
    elif kind == "today":
        today = datetime.now().strftime("%Y-%m-%d")
        cnt = {}
        for it in ns["otp_log"]:
            dt = _ts(it)
            if dt and dt.strftime("%Y-%m-%d") == today:
                cnt[str(it.get("userId"))] = cnt.get(str(it.get("userId")), 0) + 1
        rows = sorted(cnt.items(), key=lambda x: -x[1])
        title, fmt = "TOP 10 OTP RECEIVERS (TODAY)", lambda v: f"{v} OTP"
    else:
        ws = ns["withdrawals"][-10:][::-1]
        out = ["💸 <b>LAST 10 WITHDRAWALS</b>", "━━━━━━━━━━━━━━━━━━"]
        icon = {"approved": "✅", "rejected": "❌"}
        for i, w in enumerate(ws, 1):
            out.append(f"{i}. {_name(w.get('userId'))} → <b>{fmt_num(w.get('amount', 0))}</b> "
                       f"{esc(str(w.get('method', '')))} {icon.get(w.get('status'), '⏳')}")
        if not ws:
            out.append("No withdrawals yet.")
        return "\n".join(out)
    rows = [r for r in rows if r[1] > 0][:10]
    out = [f"🏆 <b>{title}</b>", "━━━━━━━━━━━━━━━━━━"]
    for i, (u, v) in enumerate(rows):
        out.append(f"{medals[i]} {_name(u)} — <b>{fmt(v)}</b>")
    if not rows:
        out.append("No data.")
    return "\n".join(out)


def stock_menu_kb():
    ns = NS()
    rows = [[btn("📥 Unused (all)", "hx:st:un:all", style="primary"),
             btn("📥 Used / Assigned", "hx:st:used", style="success")]]
    for svc, s in ns["services"].items():
        n = sum(len(v.get(svc, [])) for v in ns["numbers_by_cs"].values())
        if n:
            rows.append([btn(f"📥 {s['icon']} {s['name']} ({n})"[:40], f"hx:st:un:{svc}", style="primary")])
    rows.append(back_row("xf:menu"))
    return InlineKeyboardMarkup(rows)


def stock_summary():
    ns = NS()
    total = sum(len(n) for svcs in ns["numbers_by_cs"].values() for n in svcs.values())
    return (f"📦 <b>Stock Tools</b>\n\n📭 Unused (available): <b>{total}</b>\n"
            f"🚀 Used / assigned: <b>{len(ns['active_numbers'])}</b>\n\n"
            "Download করা TXT এর ফরম্যাট: <code>number|country|service</code> (আবার আপলোড করা যায়)")


def unused_txt(svc):
    ns = NS()
    lines = []
    for cc, svcs in ns["numbers_by_cs"].items():
        for s, nums in svcs.items():
            if svc in ("all", s):
                lines += [f"{n}|{cc}|{s}" for n in nums]
    return lines


def used_txt():
    ns = NS()
    return [f"{n}|{d.get('countryCode', '')}|{d.get('service', '')}|{d.get('userId', '')}|"
            f"{d.get('otpCount', 0)}|{str(d.get('assignedAt', ''))[:19]}" for n, d in ns["active_numbers"].items()]


def wm_text():
    ms = NS()["settings"].get("withdrawMethods", [])
    return ("💳 <b>Withdraw Methods</b>\n\n" + ("\n".join(f"{i + 1}. {esc(m)}" for i, m in enumerate(ms)) or "none")
            + "\n\nইউজাররা উইথড্রের সময় এই মেথডগুলো দেখবে।")


def wm_kb():
    ms = NS()["settings"].get("withdrawMethods", [])
    rows = [[btn(f"🗑 {m}"[:40], f"hx:wmd:{i}", style="danger")] for i, m in enumerate(ms)]
    rows.append([btn("➕ Add Method", "hx:wma", style="success")])
    rows.append(back_row("xf:menu"))
    return InlineKeyboardMarkup(rows)


def method_icon(m):
    return {"bkash": "🟣", "nagad": "🟠", "rocket": "🟪", "upay": "🟡", "binance": "🟨"}.get(m.lower(), "💳")


def method_rows(styles=("danger", "primary", "success")):
    """handle_withdraw এ ব্যবহারের জন্য মেথড বাটন (২টা করে এক row)।"""
    ms = NS()["settings"].get("withdrawMethods") or ["bKash", "Nagad"]
    rows, row = [], []
    for i, m in enumerate(ms):
        row.append(btn(f"{method_icon(m)} {m}", f"wm:{m}", style=styles[i % len(styles)]))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows



# ═══════════════════════════════════════════════════════════════════════════
#  📡 PANEL OTP STATS — যেকোনো প্রোভাইডারের যেকোনো প্যানেলে কতগুলো OTP এলো
# ═══════════════════════════════════════════════════════════════════════════
# সব প্রোভাইডার (1–5, ints, masdar…) _handle_scraped_sms() এর ভেতর দিয়ে যায়; সেটাকেই গুনি।
#   received = প্যানেল থেকে যতগুলো OTP এসেছে (ইউজারের নাম্বারে মিলুক বা না মিলুক)
#   credited = যেগুলো কোনো ইউজারের নাম্বারে মিলে ইউজার আয় পেয়েছে
def _ps():
    return xf.DATA.setdefault("panel_stats", {})


def _host(key):
    from urllib.parse import urlparse
    return (urlparse(key).netloc or key)[:34]


def ps_record(key, idx, received=1, credited=0, svc=None):
    d = xf.today_str()
    e = _ps().setdefault(key, {"received": {}, "credited": {}, "services": {}, "last": "", "idx": idx,
                               "first": d, "total_received": 0, "total_credited": 0})
    e["received"][d] = e["received"].get(d, 0) + received
    e["credited"][d] = e["credited"].get(d, 0) + credited
    e["total_received"] = e.get("total_received", 0) + received
    e["total_credited"] = e.get("total_credited", 0) + credited
    e["last"] = datetime.now().isoformat()
    e["idx"] = idx
    if svc:
        e["services"][svc] = e["services"].get(svc, 0) + received
    xf.mark_dirty()


def install_panel_stats(ns):
    orig = ns.get("_handle_scraped_sms")
    if not orig:
        logger.warning("hub_features: _handle_scraped_sms not found — panel stats disabled")
        return

    async def _handle_scraped_sms(sms, source_url, app, panel_idx=None):
        try:
            key = str(source_url).rstrip("/")
            number = str(sms.get("number", "")).lstrip("0")
            credited = 1 if ns["find_active_number"](number) else 0
            try:
                svc = ns["scraper_detect_service"](sms.get("message", ""), sms.get("range", ""))
            except Exception:
                svc = None
            ps_record(key, panel_idx, 1, credited, svc)
        except Exception:
            logger.debug("panel stats record failed", exc_info=True)
        return await orig(sms, source_url, app, panel_idx)

    ns["_handle_scraped_sms"] = _handle_scraped_sms


def _ps_rows():
    """সাজানো প্যানেল তালিকা: [(key, entry, panel_dict|None)]"""
    ns = NS()
    try:
        panels = ns["load_panels"]()
    except Exception:
        panels = []
    stats = _ps()
    rows = []
    used_idx = set()
    for key, e in stats.items():
        idx = e.get("idx")
        panel = panels[idx] if isinstance(idx, int) and 0 <= idx < len(panels) else None
        if isinstance(idx, int):
            used_idx.add(idx)
        rows.append((key, e, panel))
    for i, p in enumerate(panels):                 # এখনো একটাও OTP আসেনি এমন প্যানেল
        if i not in used_idx:
            k = str(p.get("url") or p.get("sms_url") or p.get("login_url") or f"panel{i}").rstrip("/")
            rows.append((k, {"received": {}, "credited": {}, "services": {}, "last": "", "idx": i,
                             "total_received": 0, "total_credited": 0}, p))
    rows.sort(key=lambda r: (-sum(r[1]["received"].get(d, 0) for d in xf._days(1)),
                             -r[1].get("total_received", 0)))
    return rows


def _status_icon(idx):
    st = NS().get("otp_panel_status", {}).get(idx) if isinstance(idx, int) else None
    return {"running": "🟢", "error": "🔴", "stopped": "⚫"}.get(st, "⚪")


def _label(key, e, panel):
    t = ""
    if panel:
        t = NS().get("PANEL_TYPE_LABEL", {}).get(panel.get("type", ""), panel.get("type", ""))
    idx = e.get("idx")
    num = f"#{idx + 1} " if isinstance(idx, int) else ""
    return f"{num}{_host(key)}" + (f" ({t})" if t else "")


def _sum(m, days):
    return sum(m.get(d, 0) for d in days)


def ps_text():
    rows = _ps_rows()
    d1, d7 = xf._days(1), xf._days(7)
    out = ["📡 <b>Panel OTP Stats</b>", "📥 received = প্যানেলে যত OTP এলো | ✅ credited = ইউজারের নাম্বারে মিলেছে",
           "━━━━━━━━━━━━━━━━━━"]
    if not rows:
        out.append("কোনো প্যানেল পাওয়া যায়নি।")
    tr = tc = 0
    for key, e, panel in rows[:15]:
        r1, c1 = _sum(e["received"], d1), _sum(e["credited"], d1)
        r7 = _sum(e["received"], d7)
        tr += r1
        tc += c1
        out.append(f"{_status_icon(e.get('idx'))} <b>{esc(_label(key, e, panel))}</b>\n"
                   f"   আজ: 📥 {r1} | ✅ {c1}   •  ৭ দিন: 📥 {r7}   •  মোট: 📥 {e.get('total_received', 0)}")
    if rows:
        out.append(f"━━━━━━━━━━━━━━━━━━\n🔢 আজ সব প্যানেল মিলিয়ে: 📥 <b>{tr}</b> | ✅ <b>{tc}</b>")
    return "\n".join(out)


def ps_kb():
    rows = _ps_rows()
    btns = [btn(f"{_status_icon(e.get('idx'))} {_label(k, e, p)}"[:34], f"hx:ps:d:{i}", style="primary")
            for i, (k, e, p) in enumerate(rows[:15])]
    kb = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    kb.append([btn("🔄 Refresh", "hx:ps", style="success")])
    kb.append(back_row("xf:menu"))
    return InlineKeyboardMarkup(kb)


def ps_detail(i):
    rows = _ps_rows()
    if i >= len(rows):
        return "⚠️ Panel not found — Refresh করো।", InlineKeyboardMarkup([back_row("hx:ps")])
    key, e, panel = rows[i]
    ns = NS()
    days = {"আজ": xf._days(1), "গতকাল": [xf._days(2)[1]], "৭ দিন": xf._days(7), "৩০ দিন": xf._days(30)}
    out = [f"📡 <b>{esc(_label(key, e, panel))}</b>", f"🔗 <code>{esc(key[:80])}</code>",
           f"স্ট্যাটাস: {_status_icon(e.get('idx'))} {esc(str(ns.get('otp_panel_status', {}).get(e.get('idx'), 'unknown')))}",
           "━━━━━━━━━━━━━━━━━━"]
    for name, ds in days.items():
        out.append(f"<b>{name}:</b> 📥 {_sum(e['received'], ds)} | ✅ {_sum(e['credited'], ds)}")
    out.append(f"<b>মোট (ট্র্যাকিং শুরু {e.get('first', '—')}):</b> 📥 {e.get('total_received', 0)} | ✅ {e.get('total_credited', 0)}")
    if e.get("last"):
        out.append(f"🕐 শেষ OTP: {ns['get_time_ago'](e['last'])}")
    top = sorted(e.get("services", {}).items(), key=lambda kv: -kv[1])[:5]
    if top:
        out.append("🔧 টপ সার্ভিস: " + ", ".join(
            f"{esc(str(ns['services'].get(s, {}).get('name', s)))} ({n})" for s, n in top))
    recent = list(ns.get("panel_recent_log", {}).get(e.get("idx"), []))[:5]
    if recent:
        out.append("\n🕘 <b>সর্বশেষ ৫টা:</b>")
        for r in recent:
            num = str(r.get("number", ""))
            out.append(f"  {esc(str(r.get('time', '')))}  +{esc(num[:5])}•••{esc(num[-3:])}")
    return "\n".join(out), InlineKeyboardMarkup([
        [btn("🔄 Refresh", f"hx:ps:d:{i}", style="success")], back_row("hx:ps")])

# ═══════════════════════════════════════════════════════════════════════════
#  CALLBACK ROUTER   (hx:*)
# ═══════════════════════════════════════════════════════════════════════════
async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ns = NS()
    q = update.callback_query
    uid = str(update.effective_user.id)
    parts = q.data.split(":")
    sec = parts[1] if len(parts) > 1 else ""

    # ── সব ইউজারের জন্য ──
    if sec == "sn":
        return await claim_number(update, context, parts)
    if sec == "snx":
        ns["get_session"](uid)["state"] = None
        await q.answer()
        return await edit(q, "❌ Search cancelled.")
    if sec == "tr":
        await q.answer("🔄")
        return await edit(q, traffic_text(), traffic_kb())
    if sec == "close":
        await q.answer()
        try:
            await q.message.delete()
        except Exception:
            pass
        return

    # ── admin only ──
    if not is_adm(uid):
        return await q.answer("❌ Admin only", show_alert=True)
    await q.answer()
    sub = parts[2] if len(parts) > 2 else ""
    arg = parts[3] if len(parts) > 3 else ""

    # ---------- Menu Design ----------
    if sec == "md":
        xf._set_state(uid, None)
        return await edit(q, MD_INTRO, md_list_kb())
    if sec == "mde" and sub in MENUS:
        xf._set_state(uid, None)
        t, kb = md_detail(sub)
        return await edit(q, t, kb)
    if sec == "mdt" and sub in MENUS:
        xf._set_state(uid, "hx_md_text", {"key": sub})
        return await edit(q, f"📝 <b>{esc(MENUS[sub][0])}</b>\n\nনতুন লেখাটা পাঠাও (bold/italic/প্রিমিয়াম ইমোজি সহ চলবে)।\n"
                             f"Variables: <code>{esc(MENUS[sub][1]) or 'none'}</code>",
                          InlineKeyboardMarkup([back_row(f"hx:mde:{sub}", "❌ Cancel")]))
    if sec == "mdp" and sub in MENUS:
        text = _cfg(sub).get("text")
        if not text:
            return await q.answer("Default text in use", show_alert=True)
        try:
            await context.bot.send_message(int(uid), "👁 <b>Preview</b>\n\n" + text, parse_mode=ParseMode.HTML)
        except Exception as e:
            await context.bot.send_message(int(uid), f"❌ Preview failed: {e}")
        return
    if sec == "mdb" and sub in MENUS:
        xf._set_state(uid, None)
        return await edit(q, f"🔘 <b>{esc(MENUS[sub][0])}</b> — buttons", md_buttons_kb(sub))
    if sec == "mda" and sub in MENUS:
        xf._set_state(uid, "hx_md_btn", {"key": sub})
        return await edit(q, "➕ বাটন যোগ করো, ফরম্যাট:\n<code>Button Text - https://link.com</code>",
                          InlineKeyboardMarkup([back_row(f"hx:mdb:{sub}", "❌ Cancel")]))
    if sec == "mdd" and sub in MENUS and arg.isdigit():
        btns = _cfg(sub).get("buttons", [])
        if int(arg) < len(btns):
            btns.pop(int(arg))
            xf.save_data()
        return await edit(q, f"🔘 <b>{esc(MENUS[sub][0])}</b> — buttons", md_buttons_kb(sub))
    if sec == "mdr" and sub in MENUS:
        xf.DATA.setdefault("menu", {}).pop(sub, None)
        xf.save_data()
        t, kb = md_detail(sub)
        return await edit(q, "♻️ Reset done.\n\n" + t, kb)
    if sec == "mdra":
        xf.DATA["menu"] = {}
        xf.save_data()
        return await edit(q, "♻️ All menus reset.\n\n" + MD_INTRO, md_list_kb())

    # ---------- Leaderboard ----------
    if sec == "lb":
        if not sub:
            return await edit(q, "🏆 <b>Leaderboard</b>", lb_menu_kb())
        return await edit(q, lb_text(sub), InlineKeyboardMarkup([
            [btn("🔄 Refresh", f"hx:lb:{sub}", style="primary")], back_row("hx:lb")]))

    # ---------- Stock tools ----------
    if sec == "st":
        if not sub:
            return await edit(q, stock_summary(), stock_menu_kb())
        if sub == "un":
            lines = unused_txt(arg or "all")
            if not lines:
                return await q.answer("❌ Nothing in stock", show_alert=True)
            f = io.BytesIO("\n".join(lines).encode("utf-8"))
            f.name = f"unused_numbers_{arg or 'all'}.txt"
            return await context.bot.send_document(int(uid), f, caption=f"📥 {len(lines)} unused numbers")
        if sub == "used":
            lines = used_txt()
            if not lines:
                return await q.answer("❌ None assigned", show_alert=True)
            f = io.BytesIO("\n".join(lines).encode("utf-8"))
            f.name = "used_numbers.txt"
            return await context.bot.send_document(int(uid), f, caption=f"📥 {len(lines)} used numbers\n"
                                                   "number|country|service|user|otp_count|assigned_at")

    # ---------- Panel OTP stats ----------
    if sec == "ps":
        if sub == "d" and arg.isdigit():
            t, kb = ps_detail(int(arg))
            return await edit(q, t, kb)
        return await edit(q, ps_text(), ps_kb())

    # ---------- Withdraw methods ----------
    if sec == "wm":
        return await edit(q, wm_text(), wm_kb())
    if sec == "wma":
        xf._set_state(uid, "hx_wm_add")
        return await edit(q, "➕ নতুন মেথডের নাম পাঠাও (যেমন: Rocket)।", InlineKeyboardMarkup([back_row("hx:wm", "❌ Cancel")]))
    if sec == "wmd" and sub.isdigit():
        ms = ns["settings"].setdefault("withdrawMethods", ["bKash", "Nagad"])
        if len(ms) <= 1:
            return await q.answer("কমপক্ষে ১টা মেথড থাকতে হবে", show_alert=True)
        if int(sub) < len(ms):
            ms.pop(int(sub))
            ns["save_settings"]()
        return await edit(q, wm_text(), wm_kb())


# ═══════════════════════════════════════════════════════════════════════════
#  STATE HANDLER  (xf.state_router থেকে ডাকা হয়)
# ═══════════════════════════════════════════════════════════════════════════
async def state_body(update: Update, context: ContextTypes.DEFAULT_TYPE, sess, state):
    ns = NS()
    msg = update.effective_message
    uid = str(update.effective_user.id)
    text = (msg.text or "").strip()

    if state == "hx_usearch":
        return await search_input(update, context, sess)

    if state == "hx_md_text":
        key = (sess.get("data") or {}).get("key")
        if not msg.text or key not in MENUS:
            return await reply(update, "❌ Send text only.")
        try:
            html_text = msg.text_html
        except Exception:
            html_text = html.escape(msg.text)
        if len(html_text) > 3500:
            return await reply(update, "❌ Too long (max 3500).")
        try:       # সেভের আগে যাচাই — Telegram এ পাঠিয়ে দেখি HTML ঠিক আছে কিনা
            sample = html_text.format_map(_Safe())
            await context.bot.send_message(int(uid), "👁 <b>Preview</b>\n\n" + sample, parse_mode=ParseMode.HTML)
        except Exception as e:
            return await reply(update, f"❌ Invalid formatting: <code>{esc(str(e)[:150])}</code>")
        xf.DATA.setdefault("menu", {}).setdefault(key, {})["text"] = html_text
        xf.save_data()
        xf._set_state(uid, None)
        t, kb = md_detail(key)
        return await reply(update, "✅ Saved.\n\n" + t, kb)

    if state == "hx_md_btn":
        key = (sess.get("data") or {}).get("key")
        b = parse_button(text)
        if not b or key not in MENUS:
            return await reply(update, "❌ ফরম্যাট: <code>Button Text - https://link.com</code>")
        xf.DATA.setdefault("menu", {}).setdefault(key, {}).setdefault("buttons", []).append(b)
        xf.save_data()
        xf._set_state(uid, None)
        return await reply(update, f"✅ Added <b>{esc(b['text'])}</b>.", md_buttons_kb(key))

    if state == "hx_wm_add":
        name = re.sub(r"[:|]", "", text).strip()[:20]
        ms = ns["settings"].setdefault("withdrawMethods", ["bKash", "Nagad"])
        if not name:
            return await reply(update, "❌ Send a method name.")
        if name.lower() in [m.lower() for m in ms]:
            return await reply(update, "ℹ️ Already exists.", wm_kb())
        ms.append(name)
        ns["save_settings"]()
        xf._set_state(uid, None)
        return await reply(update, f"✅ Added <b>{esc(name)}</b>.\n\n" + wm_text(), wm_kb())


# ═══════════════════════════════════════════════════════════════════════════
#  SETUP
# ═══════════════════════════════════════════════════════════════════════════
def install_group_buttons(ns):
    """OTP গ্রুপের মেসেজে Menu Design এর "otp_group" বাটন যোগ।"""
    orig = ns.get("_send_group_payload")
    if not orig:
        return

    async def _send_group_payload(payload):
        extra = _cfg("otp_group").get("buttons", [])
        if extra and isinstance(payload, dict):
            import copy
            payload = copy.deepcopy(payload)
            mk = payload.setdefault("reply_markup", {"inline_keyboard": []})
            for b in extra:
                mk.setdefault("inline_keyboard", []).append([{"text": b["text"], "url": b["url"], "style": "primary"}])
        return await orig(payload)

    ns["_send_group_payload"] = _send_group_payload


def setup(app, ns):
    xf.DATA.setdefault("menu", {})
    install_group_buttons(ns)
    install_panel_stats(ns)
    app.add_handler(CallbackQueryHandler(cb_router, pattern=r"^hx:"))
    logger.info("hub_features: setup complete")

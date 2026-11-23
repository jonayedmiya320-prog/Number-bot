# -*- coding: utf-8 -*-
"""
extra_features.py  —  Earning Hub Number Bot add-on
====================================================
bot.py এর পাশে এই ফাইলটা রাখো। bot.py তে এটা আগে থেকেই wired করা আছে
(main() এর শুরুতে setup(), post_init এ start_background())।

Features
--------
 1. Anti-Spam Rate Limiter (token bucket, admin panel থেকে Strict/Normal/Relaxed)
 2. Ban / Unban system (banned user বটের কিছুই ব্যবহার করতে পারবে না)
 3. User Manager  — search, detail card, balance edit (+/-/=), ban, user list CSV, overview
 4. Analytics Dashboard — Today / 7d / 30d, panel performance, growth chart,
    top services & countries, OTP log CSV export, auto daily report, low-stock alert
 5. Targeted Broadcast — All / Verified / Active 7d / VIP / Referrers / by language
    (preview + confirm, rate-limit safe, media/formatting সহ copy)
 6. Multi-language (English, বাংলা, हिन्दी, العربية) — menu, referral card, /setlang
 7. Referral card (/ref) + Top Referrers leaderboard
 8. Force-Join Manager — admin extra required channel যোগ/বাদ দিতে পারবে
 9. Giveaway — random active users কে balance দেওয়া
10. Admin management — /addadmin /removeadmin /adminlist (super admin only)

ডেটা: DATA_DIR/extra_data.json   (users.json এ শুধু "lang", "banned" ফিল্ড যোগ হয়)
"""

import asyncio
import contextvars
import csv
import html
import io
import logging
import os
import random
import re
import time
import urllib.parse
from datetime import date, datetime, timedelta

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import (
    ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, TypeHandler, filters,
)

logger = logging.getLogger("extra_features")

NS = {}                       # bot.py এর globals() — setup() এ বসে
_CUR_UID = contextvars.ContextVar("xf_uid", default=None)
esc = html.escape

# ═══════════════════════════════════════════════════════════════════════════
#  PERSISTENT DATA
# ═══════════════════════════════════════════════════════════════════════════
DEFAULT_DATA = {
    "analytics": {"daily": {}, "panels": {}, "services": {}, "countries": {}},
    "force_join": {"enabled": True, "channels": []},
    "rate": {"enabled": True, "capacity": 8, "refill": 1.0, "preset": "normal"},
    "lowstock": {"enabled": True, "threshold": 10, "last_alert": 0},
    "report": {"enabled": True, "hour": 0, "last_sent": ""},
    "vip_min": 100.0,
    "menu": {},
    "panel_stats": {},
    "hidden_buttons": [],
    "hide_for_admins": False,
}
DATA = {}
_dirty = False


def _path():
    return os.path.join(NS.get("DATA_DIR", "."), "extra_data.json")


def _merge(default, loaded):
    out = {}
    for k, v in default.items():
        if isinstance(v, dict):
            out[k] = _merge(v, loaded.get(k, {}) if isinstance(loaded.get(k), dict) else {})
        else:
            out[k] = loaded.get(k, v)
    for k, v in loaded.items():          # unknown keys (daily dates etc.) রেখে দাও
        if k not in out:
            out[k] = v
    return out


def load_data():
    global DATA
    loaded = NS["load_json"](_path(), {})
    DATA = _merge(DEFAULT_DATA, loaded if isinstance(loaded, dict) else {})


def save_data():
    global _dirty
    _prune()
    NS["save_json"](_path(), DATA)
    _dirty = False


def mark_dirty():
    global _dirty
    _dirty = True


def _prune(keep_days=120):
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    A = DATA["analytics"]
    for d in [d for d in A["daily"] if d < cutoff]:
        A["daily"].pop(d, None)
    for key in ("panels", "services", "countries"):
        for name, days in A[key].items():
            for d in [d for d in days if d < cutoff]:
                days.pop(d, None)
    for e in DATA.get("panel_stats", {}).values():
        for fld in ("received", "credited"):
            for d in [d for d in e.get(fld, {}) if d < cutoff]:
                e[fld].pop(d, None)


# ═══════════════════════════════════════════════════════════════════════════
#  SMALL HELPERS
# ═══════════════════════════════════════════════════════════════════════════
def g(name):
    return NS[name]


def today_str():
    return date.today().isoformat()


def btn(text, data=None, url=None, style=None):
    """style= সাপোর্ট না করলে নিজে থেকেই fallback।"""
    kw = {"url": url} if url else {"callback_data": data}
    if style:
        try:
            return InlineKeyboardButton(text, style=style, **kw)
        except TypeError:
            pass
    return InlineKeyboardButton(text, **kw)


def kbtn(text, style=None):
    if style:
        try:
            return KeyboardButton(text, style=style)
        except TypeError:
            pass
    return KeyboardButton(text)


def is_adm(uid):
    uid = str(uid)
    try:
        return NS["is_admin"](uid) or NS["get_session"](uid).get("is_admin", False)
    except Exception:
        return False


def super_admin_ids():
    env = os.environ.get("SUPER_ADMIN_IDS", "").replace(" ", "")
    ids = [x for x in env.split(",") if x]
    if ids:
        return ids
    return [str(NS["admins"][0])] if NS["admins"] else []


def parse_dt(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def fmt_num(x):
    try:
        return f"{float(x):,.2f}"
    except Exception:
        return str(x)


async def edit(q, text, kb=None):
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                  disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        try:
            await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                       disable_web_page_preview=True)
        except Exception:
            logger.exception("edit fallback failed")


async def reply(update, text, kb=None):
    return await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)


def back_row(to="xf:menu", label="🔙 Back"):
    return [btn(label, to, style="primary")]


# ═══════════════════════════════════════════════════════════════════════════
#  1. i18n
# ═══════════════════════════════════════════════════════════════════════════
LANGS = {"en": "🇬🇧 English", "bn": "🇧🇩 বাংলা", "hi": "🇮🇳 हिन्दी", "ar": "🇸🇦 العربية"}

T = {
    "m_num":  {"en": "☎️ Get Number", "bn": "☎️ নাম্বার নিন", "hi": "☎️ नंबर लें", "ar": "☎️ احصل على رقم"},
    "m_mail": {"en": "📧 Get Tempmail", "bn": "📧 টেম্পমেইল", "hi": "📧 टेम्पमेल", "ar": "📧 بريد مؤقت"},
    "m_2fa":  {"en": "🔐 2FA", "bn": "🔐 2FA", "hi": "🔐 2FA", "ar": "🔐 2FA"},
    "m_bal":  {"en": "💰 Balances", "bn": "💰 ব্যালেন্স", "hi": "💰 बैलेंस", "ar": "💰 الرصيد"},
    "m_wd":   {"en": "💸 Withdraw", "bn": "💸 উইথড্র", "hi": "💸 निकासी", "ar": "💸 سحب"},
    "m_ref":  {"en": "👥 Referral", "bn": "👥 রেফারেল", "hi": "👥 रेफरल", "ar": "👥 الإحالة"},
    "m_sup":  {"en": "💬 Support", "bn": "💬 সাপোর্ট", "hi": "💬 सपोर्ट", "ar": "💬 الدعم"},
    "m_search": {"en": "🔍 Search Number", "bn": "🔍 নাম্বার সার্চ", "hi": "🔍 नंबर खोजें", "ar": "🔍 بحث عن رقم"},
    "m_traffic": {"en": "📊 Traffic", "bn": "📊 ট্রাফিক", "hi": "📊 ट्रैफ़िक", "ar": "📊 الحركة"},
    "m_lang": {"en": "🌐 Language", "bn": "🌐 ভাষা", "hi": "🌐 भाषा", "ar": "🌐 اللغة"},
    "lang_prompt": {"en": "🌐 <b>Choose your language</b>", "bn": "🌐 <b>আপনার ভাষা বেছে নিন</b>",
                    "hi": "🌐 <b>अपनी भाषा चुनें</b>", "ar": "🌐 <b>اختر لغتك</b>"},
    "lang_set": {"en": "✅ Language set to English.", "bn": "✅ ভাষা বাংলা সেট হয়েছে।",
                 "hi": "✅ भाषा हिन्दी सेट हो गई।", "ar": "✅ تم ضبط اللغة على العربية."},
    "choose": {"en": "Choose an option:", "bn": "একটি অপশন বেছে নিন:", "hi": "एक विकल्प चुनें:", "ar": "اختر خياراً:"},
    "ref_title": {"en": "REFERRAL PROGRAM", "bn": "রেফারেল প্রোগ্রাম", "hi": "रेफरल प्रोग्राम", "ar": "برنامج الإحالة"},
    "ref_link": {"en": "Your link", "bn": "আপনার লিংক", "hi": "आपका लिंक", "ar": "رابطك"},
    "ref_rank": {"en": "Your rank", "bn": "আপনার র‍্যাংক", "hi": "आपकी रैंक", "ar": "ترتيبك"},
    "ref_total": {"en": "Total referrals", "bn": "মোট রেফারেল", "hi": "कुल रेफरल", "ar": "إجمالي الإحالات"},
    "ref_comm": {"en": "Commission earned", "bn": "কমিশন আয়", "hi": "कमीशन आय", "ar": "العمولة المكتسبة"},
    "ref_rate": {"en": "Commission rate", "bn": "কমিশন রেট", "hi": "कमीशन दर", "ar": "نسبة العمولة"},
    "ref_recent": {"en": "Recent referrals", "bn": "সাম্প্রতিক রেফারেল", "hi": "हाल के रेफरल", "ar": "أحدث الإحالات"},
    "ref_how": {"en": "Share your link — you earn {pct}% of every OTP your friends earn, for life!",
                "bn": "লিংক শেয়ার করুন — আপনার বন্ধুদের প্রতিটি OTP আয়ের {pct}% আজীবন পাবেন!",
                "hi": "अपना लिंक शेयर करें — आपके दोस्तों की हर OTP कमाई का {pct}% आपको जीवनभर मिलेगा!",
                "ar": "شارك رابطك — تحصل على {pct}% من أرباح كل OTP لأصدقائك مدى الحياة!"},
    "b_share": {"en": "🔗 Share Link", "bn": "🔗 লিংক শেয়ার", "hi": "🔗 लिंक शेयर", "ar": "🔗 مشاركة الرابط"},
    "b_board": {"en": "🏆 Leaderboard", "bn": "🏆 লিডারবোর্ড", "hi": "🏆 लीडरबोर्ड", "ar": "🏆 المتصدرون"},
    "b_stats": {"en": "📊 Referral Stats", "bn": "📊 রেফারেল স্ট্যাটস", "hi": "📊 रेफरल स्टैट्स", "ar": "📊 إحصائيات الإحالة"},
    "b_back": {"en": "🔙 Back", "bn": "🔙 ফিরে যান", "hi": "🔙 वापस", "ar": "🔙 رجوع"},
    "board_title": {"en": "TOP REFERRERS", "bn": "সেরা রেফারার", "hi": "टॉप रेफरर", "ar": "أفضل المحيلين"},
    "none_yet": {"en": "No referrals yet", "bn": "এখনো কোনো রেফারেল নেই", "hi": "अभी कोई रेफरल नहीं", "ar": "لا توجد إحالات بعد"},
    "share_text": {"en": "Join using my link and earn money from OTPs!",
                   "bn": "আমার লিংক দিয়ে জয়েন করুন এবং OTP থেকে আয় করুন!",
                   "hi": "मेरे लिंक से जुड़ें और OTP से कमाएँ!",
                   "ar": "انضم عبر رابطي واربح من OTP!"},
    "banned": {"en": "⛔ Your account has been suspended. Please contact support.",
               "bn": "⛔ আপনার অ্যাকাউন্ট সাসপেন্ড করা হয়েছে। সাপোর্টে যোগাযোগ করুন।",
               "hi": "⛔ आपका अकाउंट निलंबित है। कृपया सपोर्ट से संपर्क करें।",
               "ar": "⛔ تم تعليق حسابك. يرجى التواصل مع الدعم."},
    "unavailable": {"en": "🚫 This option is currently unavailable.", "bn": "🚫 এই অপশনটি এখন বন্ধ আছে।",
                    "hi": "🚫 यह विकल्प अभी उपलब्ध नहीं है।", "ar": "🚫 هذا الخيار غير متاح حالياً."},
    "menu_updated": {"en": "🔄 Menu updated.", "bn": "🔄 মেনু আপডেট হয়েছে।", "hi": "🔄 मेनू अपडेट हो गया।", "ar": "🔄 تم تحديث القائمة."},
    "slow": {"en": "⏳ Too fast! Please slow down.", "bn": "⏳ অনেক দ্রুত! একটু ধীরে চাপুন।",
             "hi": "⏳ बहुत तेज़! कृपया धीमे चलें।", "ar": "⏳ سريع جداً! يرجى التمهل."},
}


def lang_of(uid):
    if uid is None:
        return "en"
    code = NS["users"].get(str(uid), {}).get("lang", "en")
    return code if code in LANGS else "en"


def tr(lang, key, **kw):
    s = T.get(key, {}).get(lang) or T.get(key, {}).get("en", key)
    return s.format(**kw) if kw else s


MENU_BUTTONS = [("m_num", "primary"), ("m_mail", "success"), ("m_2fa", "danger"), ("m_bal", "primary"),
                ("m_wd", "success"), ("m_ref", "danger"), ("m_search", "success"), ("m_traffic", "primary"),
                ("m_sup", "primary"), ("m_lang", "success")]
LABEL2KEY = {}      # যেকোনো ভাষার বাটন লেখা -> key


def _build_label_map():
    for key, _ in MENU_BUTTONS:
        for lg in LANGS:
            LABEL2KEY[T[key][lg]] = key


def hidden_keys(uid=None):
    """এই ইউজারের জন্য লুকানো বাটনের key সেট। অ্যাডমিন (preview মোড ছাড়া) সব দেখে।"""
    if uid is not None and is_adm(uid) and not DATA.get("hide_for_admins"):
        return set()
    return set(DATA.get("hidden_buttons", []))


def build_main_keyboard(lang, uid=None):
    hid = hidden_keys(uid)
    items = [kbtn(tr(lang, k), st) for k, st in MENU_BUTTONS if k not in hid]
    rows = [items[i:i + 2] for i in range(0, len(items), 2)]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def lang_keyboard():
    rows, row = [], []
    for code, name in LANGS.items():
        row.append(btn(name, f"xf:lang:{code}", style="primary"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = lang_of(update.effective_user.id)
    if "m_lang" in hidden_keys(update.effective_user.id):
        return await update.effective_message.reply_text(tr(lang, "unavailable"))
    await reply(update, tr(lang, "lang_prompt"), lang_keyboard())


# ═══════════════════════════════════════════════════════════════════════════
#  2. Rate limiter + Ban pre-check
# ═══════════════════════════════════════════════════════════════════════════
_buckets = {}
_ban_notified = {}


def allow(uid):
    cfg = DATA["rate"]
    now = time.monotonic()
    b = _buckets.get(uid)
    if b is None:
        _buckets[uid] = [cfg["capacity"] - 1, now]
        return True
    tokens = min(cfg["capacity"], b[0] + (now - b[1]) * cfg["refill"])
    b[1] = now
    if tokens >= 1:
        b[0] = tokens - 1
        return True
    b[0] = tokens
    return False


async def pre_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (update.message or update.callback_query):
        return
    user, chat = update.effective_user, update.effective_chat
    if not user or user.is_bot or (chat and chat.type != "private"):
        return
    uid = str(user.id)
    _CUR_UID.set(uid)
    hid = hidden_keys(uid)
    if hid and update.message and update.message.text:       # লুকানো বাটনের পুরনো কিবোর্ড/হাতে লেখা
        if LABEL2KEY.get(update.message.text.strip()) in hid:
            try:
                lg = lang_of(uid)
                await update.message.reply_text(tr(lg, "unavailable"), reply_markup=build_main_keyboard(lg, uid))
            except Exception:
                pass
            raise ApplicationHandlerStop
    if is_adm(uid):
        return
    u = NS["users"].get(uid)
    if u and u.get("banned"):
        now = time.time()
        if now - _ban_notified.get(uid, 0) > 300:
            _ban_notified[uid] = now
            try:
                if update.callback_query:
                    await update.callback_query.answer(tr(lang_of(uid), "banned"), show_alert=True)
                else:
                    await update.message.reply_text(tr(lang_of(uid), "banned"))
            except Exception:
                pass
        elif update.callback_query:
            try:
                await update.callback_query.answer()
            except Exception:
                pass
        raise ApplicationHandlerStop
    if DATA["rate"]["enabled"] and not allow(uid):
        if update.callback_query:
            try:
                await update.callback_query.answer(tr(lang_of(uid), "slow"))
            except Exception:
                pass
        raise ApplicationHandlerStop


# ═══════════════════════════════════════════════════════════════════════════
#  3. Referral card + leaderboard
# ═══════════════════════════════════════════════════════════════════════════
def _ref_rank(uid):
    ranked = sorted(NS["users"].values(), key=lambda u: u.get("referralCount", 0), reverse=True)
    for i, u in enumerate(ranked, 1):
        if str(u.get("id")) == str(uid):
            return i if u.get("referralCount", 0) > 0 else None
    return None


def referral_card(uid, bot_username):
    lang = lang_of(uid)
    users, referrals = NS["users"], NS["referrals"]
    link = NS["get_referral_link"](uid, bot_username)
    pct = NS["settings"].get("referralCommission", 10)
    cnt = users.get(uid, {}).get("referralCount", 0)
    earn = NS["get_user_earnings"](uid).get("referralEarnings", 0)
    rank = _ref_rank(uid)
    lines = [f"👥 <b>{tr(lang, 'ref_title')}</b>", "━━━━━━━━━━━━━━━━━━",
             f"🔗 <b>{tr(lang, 'ref_link')}:</b>\n<code>{esc(link)}</code>", ""]
    lines.append(f"🏆 {tr(lang, 'ref_rank')}: <b>{'#' + str(rank) if rank else '—'}</b>")
    lines.append(f"👥 {tr(lang, 'ref_total')}: <b>{cnt}</b>")
    lines.append(f"💰 {tr(lang, 'ref_comm')}: <b>{fmt_num(earn)}</b> taka")
    lines.append(f"📈 {tr(lang, 'ref_rate')}: <b>{pct}%</b>")
    recent = referrals.get(uid, [])[-5:][::-1]
    if recent:
        lines += ["", f"📋 <b>{tr(lang, 'ref_recent')}:</b>"]
        for r in recent:
            ru = users.get(r, {})
            cut = round(NS["get_user_earnings"](r).get("totalEarned", 0) * pct / 100, 2)
            lines.append(f"  👤 {esc(str(ru.get('first_name', 'User'))[:16])} — {fmt_num(cut)} taka")
    lines += ["", f"💡 {tr(lang, 'ref_how', pct=pct)}"]
    try:
        import hub_features
        _t, _pm, _rows, _custom = hub_features.menu(
            "refer", "", "HTML", ref_link=link, total_ref=cnt, ref_percent=pct, earned=fmt_num(earn))
    except Exception:
        _t, _rows, _custom = "", [], False
    share = (f"https://t.me/share/url?url={urllib.parse.quote(link)}"
             f"&text={urllib.parse.quote(tr(lang, 'share_text'))}")
    kb = InlineKeyboardMarkup([
        [btn(tr(lang, "b_share"), url=share, style="success")],
        [btn(tr(lang, "b_board"), "xf:refboard", style="primary"),
         btn(tr(lang, "b_stats"), "ref_stats", style="danger")],
    ])
    if _custom:
        kb = InlineKeyboardMarkup([list(r) for r in kb.inline_keyboard] + _rows)
        return _t, kb
    if _rows:
        kb = InlineKeyboardMarkup([list(r) for r in kb.inline_keyboard] + _rows)
    return "\n".join(lines), kb


async def cmd_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "m_ref" in hidden_keys(update.effective_user.id):
        return await update.effective_message.reply_text(tr(lang_of(update.effective_user.id), "unavailable"))
    if not await NS["ensure_verified"](update, context):
        return
    uid = str(update.effective_user.id)
    text, kb = referral_card(uid, context.bot.username)
    await reply(update, text, kb)


def leaderboard_text(lang):
    ranked = [u for u in NS["users"].values() if u.get("referralCount", 0) > 0]
    ranked.sort(key=lambda u: u.get("referralCount", 0), reverse=True)
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    out = [f"🏆 <b>{tr(lang, 'board_title')}</b>", "━━━━━━━━━━━━━━━━━━"]
    if not ranked:
        out.append(tr(lang, "none_yet"))
    for i, u in enumerate(ranked[:10]):
        out.append(f"{medals[i]} {esc(str(u.get('first_name', 'User'))[:14])} — <b>{u.get('referralCount', 0)}</b>")
    return "\n".join(out)


# ═══════════════════════════════════════════════════════════════════════════
#  4. Analytics
# ═══════════════════════════════════════════════════════════════════════════
def _panel_name(src):
    if not src:
        return "Telegram Group"
    if src == "http_api":
        return "HTTP API"
    if str(src).startswith("panel_"):
        raw = str(src)[6:]
        host = urllib.parse.urlparse(raw).netloc or raw
        return host[:40]
    return str(src)[:30]


def record_otp(item):
    d = today_str()
    A = DATA["analytics"]
    row = A["daily"].setdefault(d, {"otps": 0, "earned": 0.0})
    row["otps"] += 1
    row["earned"] = round(row["earned"] + float(item.get("earned") or 0), 4)
    for key, name in (("panels", _panel_name(item.get("source"))),
                      ("services", item.get("service") or "?"),
                      ("countries", item.get("countryCode") or "?")):
        days = A[key].setdefault(str(name), {})
        days[d] = days.get(d, 0) + 1
    mark_dirty()


class TrackedList(list):
    """otp_log.append() হলেই analytics এ গুনে রাখে (bot.py এর কোড বদলাতে হয় না)।"""
    def append(self, item):
        super().append(item)
        try:
            record_otp(item)
        except Exception:
            logger.exception("record_otp failed")


def _days(n, end=None):
    end = end or date.today()
    return [(end - timedelta(days=i)).isoformat() for i in range(n)]


def _sum_days(mapping, days):
    return sum(mapping.get(d, 0) for d in days)


def _top(kind, days, n=3):
    scores = {name: _sum_days(m, days) for name, m in DATA["analytics"][kind].items()}
    return [(k, v) for k, v in sorted(scores.items(), key=lambda x: -x[1]) if v > 0][:n]


def _svc_name(sid):
    return NS["services"].get(sid, {}).get("name", sid)


def _cty_name(cc):
    c = NS["countries"].get(cc, {})
    return f"{c.get('flag', '🌍')} {c.get('name', cc)}"


def new_users_on(days):
    ds = set(days)
    return sum(1 for u in NS["users"].values() if str(u.get("joined", ""))[:10] in ds)


def overview_text(days, title):
    A = DATA["analytics"]["daily"]
    otps = sum(A.get(d, {}).get("otps", 0) for d in days)
    paid = sum(A.get(d, {}).get("earned", 0) for d in days)
    ds = set(days)
    w_all = [w for w in NS["withdrawals"] if str(w.get("requestedAt", ""))[:10] in ds]
    w_amt = sum(w.get("amount", 0) for w in w_all)
    w_ok = sum(1 for w in w_all if w.get("status") == "approved")
    users = NS["users"]
    out = [f"📈 <b>{esc(title)}</b>", "━━━━━━━━━━━━━━━━━━",
           f"📨 OTPs: <b>{otps}</b>  (avg {otps / max(len(days), 1):.1f}/day)",
           f"💵 Paid to users: <b>{fmt_num(paid)}</b> taka",
           f"👥 New users: <b>{new_users_on(days)}</b>  |  Total: <b>{len(users)}</b>",
           f"💸 Withdrawals: <b>{len(w_all)}</b> ({fmt_num(w_amt)} taka, approved {w_ok})"]
    ts, tc = _top("services", days), _top("countries", days)
    if ts:
        out.append("🔧 Top services: " + ", ".join(f"{esc(_svc_name(k))} ({v})" for k, v in ts))
    if tc:
        out.append("🌍 Top countries: " + ", ".join(f"{esc(_cty_name(k))} ({v})" for k, v in tc))
    return "\n".join(out)


def growth_text(n=14):
    days = _days(n)[::-1]
    vals = [new_users_on([d]) for d in days]
    mx = max(vals) or 1
    rows = [f"{d[5:]} {'█' * round(v / mx * 12):<12} {v}" for d, v in zip(days, vals)]
    return (f"📊 <b>New Users — last {n} days</b>\n<pre>" + "\n".join(rows) + "</pre>"
            f"\nTotal in period: <b>{sum(vals)}</b>")


def panels_text():
    P = DATA["analytics"]["panels"]
    d1, d7, d30 = _days(1), _days(7), _days(30)
    if not P:
        return "📡 <b>Panel Performance</b>\n\nNo OTP data yet."
    rows = sorted(P.items(), key=lambda kv: -_sum_days(kv[1], d30))
    out = ["📡 <b>Panel Performance</b>", "━━━━━━━━━━━━━━━━━━"]
    for name, m in rows[:15]:
        out.append(f"• <b>{esc(name)}</b>\n   today {_sum_days(m, d1)} | 7d {_sum_days(m, d7)} | 30d {_sum_days(m, d30)}")
    return "\n".join(out)


def export_otp_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "phone", "user_id", "service", "country_code", "panel/source", "otp", "earned"])
    for r in NS["otp_log"]:
        w.writerow([r.get("timestamp", ""), r.get("phoneNumber", ""), r.get("userId", ""),
                    r.get("service", ""), r.get("countryCode", ""), _panel_name(r.get("source")),
                    r.get("otpCode", ""), r.get("earned", "")])
    return io.BytesIO(buf.getvalue().encode("utf-8-sig"))


def analytics_menu_kb():
    return InlineKeyboardMarkup([
        [btn("📅 Today", "xf:an:1", style="primary"), btn("🗓 7 Days", "xf:an:7", style="success"),
         btn("📆 30 Days", "xf:an:30", style="danger")],
        [btn("📡 Panels", "hx:ps", style="primary"), btn("📊 Growth", "xf:an:growth", style="success")],
        [btn("📤 Export OTP Log", "xf:an:export", style="danger"),
         btn("⚙️ Alerts & Reports", "xf:an:settings", style="primary")],
        back_row(),
    ])


def analytics_settings(ctx_text=""):
    r, s = DATA["report"], DATA["lowstock"]
    text = (f"⚙️ <b>Alerts & Reports</b>\n\n"
            f"🗓 Daily report: <b>{'ON' if r['enabled'] else 'OFF'}</b> (every day {r['hour']:02d}:00)\n"
            f"📉 Low-stock alert: <b>{'ON' if s['enabled'] else 'OFF'}</b> (below {s['threshold']} numbers)")
    kb = InlineKeyboardMarkup([
        [btn("🗓 Toggle Daily Report", "xf:an:tgl_report", style="primary")],
        [btn("📉 Toggle Low-Stock Alert", "xf:an:tgl_stock", style="success")],
        [btn("➖ Threshold", "xf:an:th_dn", style="danger"), btn("➕ Threshold", "xf:an:th_up", style="danger")],
        [btn("📨 Send Report Now", "xf:an:report_now", style="primary")],
        back_row("xf:an"),
    ])
    return text, kb


def admin_ids():
    return [str(a) for a in NS["admins"]]


async def _send_admins(app, text):
    for aid in admin_ids():
        try:
            await app.bot.send_message(int(aid), text, parse_mode=ParseMode.HTML)
        except Exception:
            pass


def low_stock_list():
    th = DATA["lowstock"]["threshold"]
    low = []
    for cc, svcs in NS["numbers_by_cs"].items():
        for svc, nums in svcs.items():
            if len(nums) < th:
                low.append((len(nums), cc, svc))
    return sorted(low)


# ═══════════════════════════════════════════════════════════════════════════
#  5. Audiences / Broadcast / Giveaway helpers
# ═══════════════════════════════════════════════════════════════════════════
def audience(kind):
    users = NS["users"]
    now = datetime.now()
    out = []
    for uid, u in users.items():
        if u.get("banned"):
            continue
        if kind == "all":
            ok = True
        elif kind == "verified":
            ok = bool(u.get("verified"))
        elif kind == "active7":
            dt = parse_dt(u.get("last_active"))
            ok = bool(dt and now - dt <= timedelta(days=7))
        elif kind == "vip":
            ok = NS["get_user_earnings"](uid).get("totalEarned", 0) >= DATA["vip_min"]
        elif kind == "referrers":
            ok = u.get("referralCount", 0) > 0
        elif kind.startswith("lang_"):
            ok = lang_of(uid) == kind[5:]
        else:
            ok = False
        if ok:
            out.append(uid)
    return out


AUD_LABEL = {"all": "All users", "verified": "Verified users", "active7": "Active (7 days)",
             "vip": "VIP (high earners)", "referrers": "Referrers"}


def aud_label(kind):
    if kind.startswith("lang_"):
        return f"Language: {LANGS.get(kind[5:], kind[5:])}"
    return AUD_LABEL.get(kind, kind)


def bc_menu_kb():
    rows = [
        [btn(f"👥 All ({len(audience('all'))})", "xf:bc:all", style="primary"),
         btn(f"✅ Verified ({len(audience('verified'))})", "xf:bc:verified", style="success")],
        [btn(f"🔥 Active 7d ({len(audience('active7'))})", "xf:bc:active7", style="danger"),
         btn(f"💎 VIP ({len(audience('vip'))})", "xf:bc:vip", style="primary")],
        [btn(f"🤝 Referrers ({len(audience('referrers'))})", "xf:bc:referrers", style="success")],
    ]
    row = []
    for code, name in LANGS.items():
        row.append(btn(f"{name} ({len(audience('lang_' + code))})", f"xf:bc:lang_{code}", style="primary"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn(f"⚙️ VIP min earned: {fmt_num(DATA['vip_min'])}", "xf:bc_vip", style="danger")])
    rows.append(back_row())
    return InlineKeyboardMarkup(rows)


async def run_broadcast(app, admin_chat, targets, src_chat, src_msg, progress_msg):
    sent = failed = blocked = 0
    total = len(targets)
    for i, uid in enumerate(targets, 1):
        for attempt in (1, 2):
            try:
                await app.bot.copy_message(int(uid), src_chat, src_msg)
                sent += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(float(e.retry_after) + 1)
                continue
            except Forbidden:
                blocked += 1
                NS["users"].get(uid, {})["blocked_bot"] = True
                break
            except Exception:
                failed += 1
                break
        await asyncio.sleep(0.05)               # ~20 msg/sec — Telegram limit এর নিচে
        if i % 100 == 0:
            try:
                await progress_msg.edit_text(f"📡 Sending… {i}/{total}")
            except Exception:
                pass
    try:
        NS["save_users"]()
    except Exception:
        pass
    summary = (f"✅ <b>Broadcast finished</b>\n\n📤 Sent: <b>{sent}</b>\n"
               f"🚫 Blocked bot: <b>{blocked}</b>\n⚠️ Failed: <b>{failed}</b>\n👥 Targeted: <b>{total}</b>")
    try:
        await progress_msg.edit_text(summary, parse_mode=ParseMode.HTML)
    except Exception:
        await app.bot.send_message(admin_chat, summary, parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════════════════════════
#  6. User manager helpers
# ═══════════════════════════════════════════════════════════════════════════
def find_users(q):
    q = q.strip().lstrip("@").lower()
    users = NS["users"]
    if q in users:
        return [q]
    hits = []
    for uid, u in users.items():
        hay = f"{u.get('username', '')} {u.get('first_name', '')} {u.get('last_name', '')}".lower()
        if q and (q == str(u.get("username", "")).lower() or q in hay):
            hits.append(uid)
    return hits[:8]


def user_card(uid):
    u = NS["users"].get(uid, {})
    e = NS["get_user_earnings"](uid)
    wds = [w for w in NS["withdrawals"] if w.get("userId") == uid]
    paid = sum(w["amount"] for w in wds if w.get("status") == "approved")
    pending = sum(1 for w in wds if w.get("status") == "pending")
    ref_by = u.get("referredBy")
    return (
        f"👤 <b>{esc(str(u.get('first_name', 'User')))} {esc(str(u.get('last_name', '')))}</b>\n"
        f"🆔 <code>{uid}</code>  |  @{esc(str(u.get('username', 'no_username')))}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💵 Balance: <b>{fmt_num(e['balance'])}</b> taka\n"
        f"📈 Total earned: <b>{fmt_num(e['totalEarned'])}</b> taka\n"
        f"📨 OTPs: <b>{e.get('otpCount', 0)}</b>\n"
        f"💸 Withdrawn: <b>{fmt_num(paid)}</b> taka (pending {pending})\n"
        f"👥 Referrals: <b>{u.get('referralCount', 0)}</b>"
        f"{'  | referred by <code>' + str(ref_by) + '</code>' if ref_by else ''}\n"
        f"🌐 Language: {LANGS.get(lang_of(uid), 'English')}\n"
        f"✅ Verified: {'Yes' if u.get('verified') else 'No'}\n"
        f"🚫 Status: <b>{'BANNED' if u.get('banned') else 'Active'}</b>\n"
        f"📅 Joined: {str(u.get('joined', ''))[:10]}\n"
        f"🕐 Last active: {NS['get_time_ago'](u.get('last_active', ''))}"
    )


def user_card_kb(uid):
    banned = NS["users"].get(uid, {}).get("banned")
    return InlineKeyboardMarkup([
        [btn("💰 Edit Balance", f"xf:umb:{uid}", style="primary"),
         btn("✅ Unban" if banned else "🚫 Ban", f"xf:umban:{uid}", style="success" if banned else "danger")],
        [btn("🔄 Refresh", f"xf:umd:{uid}", style="primary")],
        back_row("xf:um"),
    ])


def users_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "username", "first_name", "last_name", "joined", "last_active", "verified",
                "banned", "lang", "balance", "total_earned", "otp_count", "referrals", "referred_by"])
    for uid, u in NS["users"].items():
        e = NS["get_user_earnings"](uid)
        w.writerow([uid, u.get("username", ""), u.get("first_name", ""), u.get("last_name", ""),
                    u.get("joined", ""), u.get("last_active", ""), u.get("verified", False),
                    u.get("banned", False), lang_of(uid), e["balance"], e["totalEarned"],
                    e.get("otpCount", 0), u.get("referralCount", 0), u.get("referredBy", "")])
    return io.BytesIO(buf.getvalue().encode("utf-8-sig"))


def users_overview():
    users = NS["users"]
    now = datetime.now()
    def active(h):
        return sum(1 for u in users.values()
                   if (dt := parse_dt(u.get("last_active"))) and now - dt <= timedelta(hours=h))
    bal = [NS["get_user_earnings"](uid)["balance"] for uid in users]
    return (f"📊 <b>User Overview</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total: <b>{len(users)}</b>\n"
            f"✅ Verified: <b>{sum(1 for u in users.values() if u.get('verified'))}</b>\n"
            f"🚫 Banned: <b>{sum(1 for u in users.values() if u.get('banned'))}</b>\n"
            f"🔥 Active 24h: <b>{active(24)}</b>  |  7d: <b>{active(24 * 7)}</b>\n"
            f"🆕 Joined today: <b>{new_users_on(_days(1))}</b>  |  7d: <b>{new_users_on(_days(7))}</b>\n"
            f"💰 Users with balance: <b>{sum(1 for b in bal if b > 0)}</b>\n"
            f"💵 Total owed to users: <b>{fmt_num(sum(bal))}</b> taka")


# ═══════════════════════════════════════════════════════════════════════════
#  7. Force-join extension (wrappers around bot.py functions)
# ═══════════════════════════════════════════════════════════════════════════
def install_force_join_wrappers():
    orig_check = NS["check_membership"]
    orig_vkb = NS["verify_keyboard"]

    async def check_membership(user_id, app):
        res = await orig_check(user_id, app)
        fj = DATA["force_join"]
        missing = []
        if fj["enabled"]:
            for ch in fj["channels"]:
                try:
                    m = await app.bot.get_chat_member(ch["id"], user_id)
                    if m.status not in ("member", "administrator", "creator", "restricted"):
                        missing.append(ch.get("title", str(ch["id"])))
                except Exception as e:      # bot admin না হলে সবাইকে আটকে দিও না
                    logger.warning(f"force-join check {ch.get('id')}: {e}")
        res["extra_missing"] = missing
        if missing:
            res["allJoined"] = False
        return res

    def verify_keyboard():
        mk = orig_vkb()
        rows = [list(r) for r in mk.inline_keyboard]
        extra = [[btn(f"➕ {ch.get('title', 'Channel')}", url=ch["url"], style="primary")]
                 for ch in DATA["force_join"]["channels"] if ch.get("url")] if DATA["force_join"]["enabled"] else []
        if rows:
            rows = rows[:-1] + extra + rows[-1:]
        return InlineKeyboardMarkup(rows)

    NS["check_membership"] = check_membership
    NS["verify_keyboard"] = verify_keyboard


def fj_text():
    fj = DATA["force_join"]
    lines = [f"🔐 <b>Force Join Manager</b>\n",
             f"Status: <b>{'ON' if fj['enabled'] else 'OFF'}</b>",
             "(মূল ৩টা গ্রুপ আগের মতোই থাকবে — এখানে অতিরিক্ত চ্যানেল যোগ হয়)\n"]
    if not fj["channels"]:
        lines.append("No extra channels yet.")
    for i, ch in enumerate(fj["channels"], 1):
        lines.append(f"{i}. {esc(ch.get('title', ''))} — <code>{ch['id']}</code>")
    return "\n".join(lines)


def fj_kb():
    fj = DATA["force_join"]
    rows = [[btn("➕ Add Channel", "xf:fj:add", style="success"),
             btn("⏻ Turn OFF" if fj["enabled"] else "⏻ Turn ON", "xf:fj:toggle", style="primary")]]
    for i, ch in enumerate(fj["channels"]):
        rows.append([btn(f"🗑 Remove {ch.get('title', ch['id'])}"[:40], f"xf:fj:del:{i}", style="danger")])
    rows.append(back_row())
    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════════════════════════════════════
#  8. Menus
# ═══════════════════════════════════════════════════════════════════════════
def tools_kb():
    return InlineKeyboardMarkup([
        [btn("👤 User Manager", "xf:um", style="primary"), btn("📈 Analytics", "xf:an", style="success")],
        [btn("🎯 Targeted Broadcast", "xf:bc", style="danger"), btn("🎁 Giveaway", "xf:gw", style="primary")],
        [btn("🔐 Force Join", "xf:fj", style="success"), btn("🛡 Anti-Spam", "xf:rl", style="danger")],
        [btn("🎨 Menu Design", "hx:md", style="primary"), btn("🏆 Leaderboard", "hx:lb", style="success")],
        [btn("📡 Panel OTP Stats", "hx:ps", style="danger"), btn("👁 Button Visibility", "xf:bv", style="success")],
        [btn("📦 Stock Tools", "hx:st", style="danger"), btn("💳 W. Methods", "hx:wm", style="primary")],
        [btn("👮 Admins", "xf:adm", style="primary"), btn("✨ Premium Emoji", "xf:pe", style="success")],
        [btn("🔙 Back to Admin Panel", "admin_back", style="success")],
    ])


TOOLS_TEXT = ("🧰 <b>New Tools</b>\n\nUser Manager • Analytics • Targeted Broadcast • Giveaway • "
              "Force Join • Anti-Spam • Admins • Premium Emoji • Menu Design • Leaderboard • Stock • W. Methods • Panel OTP Stats • Button Visibility")


def install_admin_keyboard_wrapper():
    orig = NS["admin_keyboard"]

    def admin_keyboard():
        mk = orig()
        rows = [list(r) for r in mk.inline_keyboard]
        new_row = [btn("🧰 New Tools", "xf:menu", style="primary")]
        rows = rows[:-1] + [new_row] + rows[-1:] if rows else [new_row]
        return InlineKeyboardMarkup(rows)

    NS["admin_keyboard"] = admin_keyboard


def bv_text():
    hid = set(DATA.get("hidden_buttons", []))
    shown = len(MENU_BUTTONS) - len(hid)
    return ("👁 <b>Button Visibility</b>\n\nইউজারদের নিচের মেনুর (reply keyboard) বাটন লুকানো/দেখানো যায়।\n"
            f"✅ = দেখা যাচ্ছে   🙈 = লুকানো\n\nদেখা যাচ্ছে: <b>{shown}</b>/{len(MENU_BUTTONS)}\n"
            f"👤 Admin নিজেও লুকানো দেখবে না (preview): <b>{'ON' if DATA.get('hide_for_admins') else 'OFF'}</b>\n\n"
            "লুকানো বাটনের কাজ (লেখা পাঠিয়ে বা পুরনো কিবোর্ড থেকে) ইউজাররা করতে পারবে না।\n"
            "বদলানোর পর ইউজারদের কিবোর্ড নতুন করতে <b>Push menu update</b> চাপো।")


def bv_kb():
    hid = set(DATA.get("hidden_buttons", []))
    rows, row = [], []
    for key, _ in MENU_BUTTONS:
        mark = "🙈" if key in hid else "✅"
        row.append(btn(f"{mark} {T[key]['en']}"[:30], f"xf:bvt:{key}", style="danger" if key in hid else "success"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn(f"👤 Preview as user: {'ON' if DATA.get('hide_for_admins') else 'OFF'}", "xf:bva", style="primary")])
    rows.append([btn("📣 Push menu update to users", "xf:bvp", style="success"),
                 btn("♻️ Show all", "xf:bvr", style="primary")])
    rows.append(back_row())
    return InlineKeyboardMarkup(rows)


async def run_menu_push(app, admin_chat, targets, progress_msg):
    sent = failed = 0
    for i, uid in enumerate(targets, 1):
        lg = lang_of(uid)
        for _ in (1, 2):
            try:
                await app.bot.send_message(int(uid), tr(lg, "menu_updated"), reply_markup=build_main_keyboard(lg, uid))
                sent += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(float(e.retry_after) + 1)
            except Exception:
                failed += 1
                break
        await asyncio.sleep(0.05)
    try:
        await progress_msg.edit_text(f"✅ Menu pushed — sent <b>{sent}</b>, failed <b>{failed}</b>.", parse_mode=ParseMode.HTML)
    except Exception:
        pass


def rl_text():
    r = DATA["rate"]
    return (f"🛡 <b>Anti-Spam (Rate Limiter)</b>\n\nStatus: <b>{'ON' if r['enabled'] else 'OFF'}</b>\n"
            f"Preset: <b>{r['preset'].title()}</b>\n"
            f"Burst: <b>{r['capacity']}</b> actions, refill <b>{r['refill']}</b>/sec\n\n"
            f"Admins are never limited. Users who flood are silently ignored.")


def rl_kb():
    r = DATA["rate"]
    return InlineKeyboardMarkup([
        [btn("⏻ Turn OFF" if r["enabled"] else "⏻ Turn ON", "xf:rl:toggle", style="primary")],
        [btn("🔒 Strict", "xf:rl:strict", style="danger"), btn("⚖️ Normal", "xf:rl:normal", style="success"),
         btn("🕊 Relaxed", "xf:rl:relaxed", style="primary")],
        back_row(),
    ])


RL_PRESETS = {"strict": (5, 0.5), "normal": (8, 1.0), "relaxed": (15, 2.0)}


# ═══════════════════════════════════════════════════════════════════════════
#  9. Callback router
# ═══════════════════════════════════════════════════════════════════════════
def _set_state(uid, state, data=None):
    sess = NS["get_session"](uid)
    sess["state"] = state
    sess["data"] = data


CANCEL_KB = InlineKeyboardMarkup([[btn("❌ Cancel", "xf:cancel", style="danger")]])


async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = str(update.effective_user.id)
    parts = q.data.split(":")            # xf:<section>:...
    sec = parts[1] if len(parts) > 1 else ""

    # ── public callbacks (সব user) ──
    if sec == "lang" and len(parts) > 2:
        code = parts[2]
        if code in LANGS:
            NS["users"].setdefault(uid, {"id": uid})["lang"] = code
            await NS["async_save_users"]()
            await q.answer()
            try:
                await q.edit_message_text(tr(code, "lang_set"))
            except Exception:
                pass
            await context.bot.send_message(int(uid), f"{tr(code, 'lang_set')}\n{tr(code, 'choose')}",
                                           reply_markup=build_main_keyboard(code, uid))
        return
    if sec == "refboard":
        await q.answer()
        lang = lang_of(uid)
        await edit(q, leaderboard_text(lang),
                   InlineKeyboardMarkup([[btn(tr(lang, "b_back"), "xf:refback", style="primary")]]))
        return
    if sec == "refback":
        await q.answer()
        text, kb = referral_card(uid, context.bot.username)
        await edit(q, text, kb)
        return

    # ── admin only ──
    if not is_adm(uid):
        return await q.answer("❌ Admin only", show_alert=True)
    await q.answer()
    sub = parts[2] if len(parts) > 2 else ""
    arg = parts[3] if len(parts) > 3 else ""

    if sec == "menu":
        _set_state(uid, None)
        return await edit(q, TOOLS_TEXT, tools_kb())
    if sec == "cancel":
        _set_state(uid, None)
        return await edit(q, TOOLS_TEXT, tools_kb())

    # ---------- User Manager ----------
    if sec == "um":
        _set_state(uid, None)
        return await edit(q, "👤 <b>User Manager</b>", InlineKeyboardMarkup([
            [btn("🔍 Search User", "xf:ums", style="primary"), btn("📊 Overview", "xf:umo", style="success")],
            [btn("📥 Download User List", "xf:umdl", style="danger"), btn("🚫 Banned Users", "xf:umbl", style="primary")],
            back_row()]))
    if sec == "ums":
        _set_state(uid, "xf_um_search")
        return await edit(q, "🔍 <b>Search User</b>\n\nSend a user ID, @username or name:", CANCEL_KB)
    if sec == "umo":
        return await edit(q, users_overview(), InlineKeyboardMarkup([back_row("xf:um")]))
    if sec == "umdl":
        f = users_csv()
        f.name = f"users_{today_str()}.csv"
        await context.bot.send_document(int(uid), f, caption=f"👥 {len(NS['users'])} users")
        return
    if sec == "umbl":
        banned = [u for u, d in NS["users"].items() if d.get("banned")]
        rows = [[btn(f"🚫 {NS['users'][u].get('first_name', 'User')} ({u})"[:48], f"xf:umd:{u}", style="danger")]
                for u in banned[:20]]
        rows.append(back_row("xf:um"))
        return await edit(q, f"🚫 <b>Banned users: {len(banned)}</b>", InlineKeyboardMarkup(rows))
    if sec == "umd" and sub in NS["users"]:
        return await edit(q, user_card(sub), user_card_kb(sub))
    if sec == "umb" and sub in NS["users"]:
        _set_state(uid, "xf_um_bal", {"uid": sub})
        return await edit(q, f"💰 <b>Edit balance</b> for <code>{sub}</code>\n\n"
                             "Send:\n<code>+50</code> add\n<code>-20</code> deduct\n<code>=100</code> set exactly",
                          CANCEL_KB)
    if sec == "umban" and sub in NS["users"]:
        if is_adm(sub):
            return await q.answer("❌ Can't ban an admin", show_alert=True)
        u = NS["users"][sub]
        u["banned"] = not u.get("banned", False)
        await NS["async_save_users"]()
        try:
            await context.bot.send_message(int(sub), tr(lang_of(sub), "banned") if u["banned"]
                                           else "✅ Your account has been restored.")
        except Exception:
            pass
        return await edit(q, user_card(sub), user_card_kb(sub))

    # ---------- Analytics ----------
    if sec == "an":
        if sub == "":
            return await edit(q, "📈 <b>Analytics Dashboard</b>", analytics_menu_kb())
        if sub in ("1", "7", "30"):
            n = int(sub)
            title = {1: "Today", 7: "Last 7 Days", 30: "Last 30 Days"}[n]
            return await edit(q, overview_text(_days(n), title),
                              InlineKeyboardMarkup([[btn("🔄 Refresh", f"xf:an:{n}", style="primary")], back_row("xf:an")]))
        if sub == "panels":
            return await edit(q, panels_text(), InlineKeyboardMarkup([[btn("🔄 Refresh", "xf:an:panels", style="primary")], back_row("xf:an")]))
        if sub == "growth":
            return await edit(q, growth_text(), InlineKeyboardMarkup([back_row("xf:an")]))
        if sub == "export":
            f = export_otp_csv()
            f.name = f"otp_log_{today_str()}.csv"
            await context.bot.send_document(int(uid), f, caption=f"📋 OTP log ({len(NS['otp_log'])} rows)")
            return
        if sub == "settings":
            t, kb = analytics_settings()
            return await edit(q, t, kb)
        if sub == "tgl_report":
            DATA["report"]["enabled"] = not DATA["report"]["enabled"]
            save_data()
        elif sub == "tgl_stock":
            DATA["lowstock"]["enabled"] = not DATA["lowstock"]["enabled"]
            save_data()
        elif sub in ("th_up", "th_dn"):
            th = DATA["lowstock"]["threshold"]
            DATA["lowstock"]["threshold"] = max(1, th + (5 if sub == "th_up" else -5))
            save_data()
        elif sub == "report_now":
            await context.bot.send_message(int(uid), overview_text(_days(1), "Today so far"), parse_mode=ParseMode.HTML)
        t, kb = analytics_settings()
        return await edit(q, t, kb)

    # ---------- Targeted broadcast ----------
    if sec == "bc":
        if sub == "":
            _set_state(uid, None)
            return await edit(q, "🎯 <b>Targeted Broadcast</b>\n\nচাই কাদের কাছে পাঠাবে সেটা বেছে নাও:", bc_menu_kb())
        kind = sub
        n = len(audience(kind))
        _set_state(uid, "xf_bc_wait", {"aud": kind})
        return await edit(q, f"🎯 Audience: <b>{esc(aud_label(kind))}</b> — <b>{n}</b> users\n\n"
                             "এখন যে মেসেজ পাঠাতে চাও সেটা পাঠাও (text / photo / video — formatting সহ):", CANCEL_KB)
    if sec == "bc_vip":
        _set_state(uid, "xf_vip")
        return await edit(q, f"💎 VIP minimum total earned (now {fmt_num(DATA['vip_min'])} taka).\nSend a new number:", CANCEL_KB)
    if sec == "bcgo":                       # confirm
        sess = NS["get_session"](uid)
        d = sess.get("data") or {}
        if not d.get("msg_id"):
            return await edit(q, "⚠️ Session expired.", InlineKeyboardMarkup([back_row("xf:bc")]))
        targets = audience(d["aud"])
        _set_state(uid, None)
        prog = await context.bot.send_message(int(uid), f"📡 Sending to {len(targets)} users…")
        await edit(q, "🚀 Broadcast started.", InlineKeyboardMarkup([back_row("xf:bc")]))
        asyncio.create_task(run_broadcast(context.application, int(uid), targets, d["chat_id"], d["msg_id"], prog))
        return

    # ---------- Giveaway ----------
    if sec == "gw":
        _set_state(uid, "xf_gw_wait")
        return await edit(q, "🎁 <b>Giveaway</b>\n\nFormat: <code>amount winners</code>\nExample: <code>10 5</code> "
                             "(৫ জন random active user কে ১০ taka করে)\nPool: verified, non-banned, active in last 7 days.",
                          CANCEL_KB)
    if sec == "gwgo":
        sess = NS["get_session"](uid)
        d = sess.get("data") or {}
        winners = d.get("winners") or []
        if not winners:
            return await edit(q, "⚠️ Session expired.", InlineKeyboardMarkup([back_row()]))
        amount = d["amount"]
        for w in winners:
            e = NS["get_user_earnings"](w)
            e["balance"] = round(e["balance"] + amount, 2)
        await NS["async_save_earnings"]()
        _set_state(uid, None)
        for w in winners:
            try:
                await context.bot.send_message(int(w), f"🎉 <b>Giveaway winner!</b>\n\nYou won <b>{fmt_num(amount)}</b> taka. 💰",
                                               parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return await edit(q, f"✅ Giveaway done — <b>{len(winners)}</b> winners × {fmt_num(amount)} taka.",
                          InlineKeyboardMarkup([back_row()]))

    # ---------- Force join ----------
    if sec == "fj":
        if sub == "add":
            _set_state(uid, "xf_fj_add")
            return await edit(q, "➕ Send the channel <code>@username</code> or numeric id (<code>-100…</code>).\n"
                                 "⚠️ বটকে ওই চ্যানেলে admin করতে হবে।", CANCEL_KB)
        if sub == "toggle":
            DATA["force_join"]["enabled"] = not DATA["force_join"]["enabled"]
            save_data()
        if sub == "del" and arg.isdigit() and int(arg) < len(DATA["force_join"]["channels"]):
            DATA["force_join"]["channels"].pop(int(arg))
            save_data()
        return await edit(q, fj_text(), fj_kb())

    # ---------- Button visibility ----------
    if sec == "bv":
        return await edit(q, bv_text(), bv_kb())
    if sec == "bvt" and sub in dict(MENU_BUTTONS):
        hid = DATA.setdefault("hidden_buttons", [])
        if sub in hid:
            hid.remove(sub)
        else:
            if len(hid) >= len(MENU_BUTTONS) - 1:
                return await q.answer("কমপক্ষে ১টা বাটন দেখা যেতে হবে", show_alert=True)
            hid.append(sub)
        save_data()
        return await edit(q, bv_text(), bv_kb())
    if sec == "bva":
        DATA["hide_for_admins"] = not DATA.get("hide_for_admins")
        save_data()
        return await edit(q, bv_text(), bv_kb())
    if sec == "bvr":
        DATA["hidden_buttons"] = []
        save_data()
        return await edit(q, bv_text(), bv_kb())
    if sec == "bvp":
        n = len(audience("all"))
        return await edit(q, f"📣 সব <b>{n}</b> জন ইউজারকে একটা ছোট মেসেজ ('Menu updated') পাঠানো হবে, যাতে তাদের কিবোর্ড নতুন হয়ে যায়। চালাবে?",
                          InlineKeyboardMarkup([[btn("✅ Yes, push", "xf:bvgo", style="success"),
                                                 btn("❌ Cancel", "xf:bv", style="danger")]]))
    if sec == "bvgo":
        targets = [u for u in audience("all") if not NS["users"][u].get("blocked_bot")]
        prog = await context.bot.send_message(int(uid), f"📡 Pushing to {len(targets)} users…")
        await edit(q, "🚀 Started.", InlineKeyboardMarkup([back_row("xf:bv")]))
        asyncio.create_task(run_menu_push(context.application, int(uid), targets, prog))
        return

    # ---------- Anti-spam ----------
    if sec == "rl":
        if sub == "toggle":
            DATA["rate"]["enabled"] = not DATA["rate"]["enabled"]
        elif sub in RL_PRESETS:
            DATA["rate"]["capacity"], DATA["rate"]["refill"] = RL_PRESETS[sub]
            DATA["rate"]["preset"] = sub
            _buckets.clear()
        save_data()
        return await edit(q, rl_text(), rl_kb())

    # ---------- Premium emoji ----------
    if sec == "pe":
        import premium_emoji as pe
        if sub == "toggle":
            pe.set_enabled(not pe.STATE["enabled"])
        elif sub == "add":
            _set_state(uid, "xf_pe_add")
            return await edit(q, "➕ <b>Service premium emoji</b>\n\nSend:\n<code>ServiceName|emoji_id</code>\n"
                                 "অথবা <code>ServiceName|emoji_id|🙂</code> (fallback emoji সহ)\n"
                                 "Example: <code>WhatsApp|5429612632430654504</code>\n\n"
                                 "ServiceName অবশ্যই বটের সার্ভিসের নামের সাথে হুবহু মিলতে হবে।", CANCEL_KB)
        elif sub == "reload":
            pe.load()
        return await edit(q, pe.status_text(), InlineKeyboardMarkup([
            [btn("⏻ Turn OFF" if pe.STATE["enabled"] else "⏻ Turn ON", "xf:pe:toggle", style="primary"),
             btn("🔄 Reload files", "xf:pe:reload", style="success")],
            [btn("➕ Set Service Emoji", "xf:pe:add", style="danger")],
            back_row()]))

    # ---------- Admins ----------
    if sec == "adm":
        sup = super_admin_ids()
        lines = ["👮 <b>Admins</b>\n"]
        for a in NS["admins"]:
            u = NS["users"].get(str(a), {})
            lines.append(f"• <code>{a}</code> {esc(str(u.get('first_name', '')))}"
                         f"{' ⭐ super' if str(a) in sup else ''}")
        lines.append("\nCommands (super admin): /addadmin &lt;id&gt;  /removeadmin &lt;id&gt;  /adminlist")
        return await edit(q, "\n".join(lines), InlineKeyboardMarkup([back_row()]))


# ═══════════════════════════════════════════════════════════════════════════
#  10. Text/message state router (group -1, admin states only)
# ═══════════════════════════════════════════════════════════════════════════
async def state_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    uid = str(update.effective_user.id)
    sess0 = NS["get_session"](uid)
    state = (sess0.get("state") or "")
    if not state.startswith(("xf_", "hx_")):
        return
    if state != "hx_usearch" and not is_adm(uid):      # সার্চ ছাড়া সব স্টেট শুধু অ্যাডমিনের
        return
    try:
        if state.startswith("hx_"):
            import hub_features
            res = await hub_features.state_body(update, context, sess0, state)
            if res == "PASS":
                return
            raise ApplicationHandlerStop
        await _state_body(update, context)
    except ApplicationHandlerStop:
        raise
    except Exception:
        logger.exception("state_router error")
        try:
            await msg.reply_text("⚠️ Error — try again or press Cancel.")
        except Exception:
            pass
    raise ApplicationHandlerStop     # bot.py এর handle_text এ যেন না যায়


async def _state_body(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    uid = str(update.effective_user.id)
    sess = NS["get_session"](uid)
    state = sess.get("state") or ""
    if not state.startswith("xf_") or not is_adm(uid):
        return
    text = (msg.text or "").strip()

    if state == "xf_um_search":
        hits = find_users(text)
        _set_state(uid, None)
        if not hits:
            await reply(update, "❌ No user found.", InlineKeyboardMarkup([back_row("xf:um")]))
        elif len(hits) == 1:
            await reply(update, user_card(hits[0]), user_card_kb(hits[0]))
        else:
            rows = [[btn(f"{NS['users'][h].get('first_name', 'User')} | @{NS['users'][h].get('username', '')} | {h}"[:55],
                         f"xf:umd:{h}", style="primary")] for h in hits]
            rows.append(back_row("xf:um"))
            await reply(update, f"🔍 {len(hits)} matches:", InlineKeyboardMarkup(rows))

    elif state == "xf_um_bal":
        target = (sess.get("data") or {}).get("uid")
        m = re.match(r"^([+\-=])\s*(\d+(?:\.\d+)?)$", text.replace(" ", ""))
        if not m or target not in NS["users"]:
            return await reply(update, "❌ Format: <code>+50</code>, <code>-20</code> or <code>=100</code>", CANCEL_KB)
        op, val = m.group(1), float(m.group(2))
        e = NS["get_user_earnings"](target)
        old = e["balance"]
        e["balance"] = round(max(0.0, old + val if op == "+" else old - val if op == "-" else val), 2)
        await NS["async_save_earnings"]()
        _set_state(uid, None)
        await reply(update, f"✅ Balance: {fmt_num(old)} → <b>{fmt_num(e['balance'])}</b> taka\n\n" + user_card(target),
                    user_card_kb(target))

    elif state == "xf_bc_wait":
        aud = (sess.get("data") or {}).get("aud", "all")
        n = len(audience(aud))
        sess["data"] = {"aud": aud, "chat_id": msg.chat_id, "msg_id": msg.message_id}
        await reply(update, f"📋 Preview above ☝️\n\nSend to <b>{esc(aud_label(aud))}</b> — <b>{n}</b> users?",
                    InlineKeyboardMarkup([[btn("✅ Send Now", "xf:bcgo", style="success"),
                                           btn("❌ Cancel", "xf:cancel", style="danger")]]))

    elif state == "xf_vip":
        try:
            DATA["vip_min"] = max(0.0, float(text))
            save_data()
            _set_state(uid, None)
            await reply(update, "✅ VIP threshold updated.", bc_menu_kb())
        except ValueError:
            await reply(update, "❌ Send a number.", CANCEL_KB)

    elif state == "xf_gw_wait":
        m = re.match(r"^(\d+(?:\.\d+)?)\s+(\d+)$", text)
        if not m:
            return await reply(update, "❌ Format: <code>amount winners</code>", CANCEL_KB)
        amount, count = float(m.group(1)), int(m.group(2))
        pool = [u for u in audience("active7")
                if NS["users"][u].get("verified") and not is_adm(u)]
        if not pool:
            _set_state(uid, None)
            return await reply(update, "⚠️ Pool is empty (no active verified users).", InlineKeyboardMarkup([back_row()]))
        winners = random.sample(pool, min(count, len(pool)))
        sess["data"] = {"amount": amount, "winners": winners}
        names = "\n".join(f"• {esc(str(NS['users'][w].get('first_name', 'User')))} (<code>{w}</code>)" for w in winners[:15])
        await reply(update, f"🎁 <b>Winners ({len(winners)} / pool {len(pool)})</b>\n{names}\n\n"
                            f"Each gets <b>{fmt_num(amount)}</b> taka — total {fmt_num(amount * len(winners))}. Confirm?",
                    InlineKeyboardMarkup([[btn("✅ Pay Out", "xf:gwgo", style="success"),
                                           btn("❌ Cancel", "xf:cancel", style="danger")]]))

    elif state == "xf_pe_add":
        import premium_emoji as pe
        parts = [x.strip() for x in text.split("|")]
        if len(parts) < 2 or not parts[0]:
            return await reply(update, "❌ Format: <code>ServiceName|emoji_id</code>", CANCEL_KB)
        try:
            pe.add_app(parts[0], parts[1], parts[2] if len(parts) > 2 and parts[2] else "📱")
        except ValueError as e:
            return await reply(update, f"❌ {esc(str(e))}", CANCEL_KB)
        _set_state(uid, None)
        await reply(update, f"✅ Saved premium emoji for <b>{esc(parts[0])}</b>.\n\n" + pe.status_text(),
                    InlineKeyboardMarkup([back_row("xf:pe")]))

    elif state == "xf_fj_add":
        ref = text if text.startswith("@") or text.lstrip("-").isdigit() else "@" + text
        ref = int(ref) if ref.lstrip("-").isdigit() else ref
        try:
            chat = await context.bot.get_chat(ref)
            me = await context.bot.get_chat_member(chat.id, context.bot.id)
            warn = "" if me.status in ("administrator", "creator") else "\n⚠️ Bot is NOT admin there — membership check কাজ নাও করতে পারে।"
            url = f"https://t.me/{chat.username}" if chat.username else getattr(chat, "invite_link", None)
            if not url:
                try:
                    url = await context.bot.export_chat_invite_link(chat.id)
                except Exception:
                    url = None
            if any(c["id"] == chat.id for c in DATA["force_join"]["channels"]):
                await reply(update, "ℹ️ Already added.", fj_kb())
            else:
                DATA["force_join"]["channels"].append({"id": chat.id, "title": chat.title or str(chat.id), "url": url})
                save_data()
                await reply(update, f"✅ Added <b>{esc(chat.title or '')}</b>{warn}\n\n" + fj_text(), fj_kb())
            _set_state(uid, None)
        except Exception as e:
            await reply(update, f"❌ Couldn't access that chat: <code>{esc(str(e)[:150])}</code>", CANCEL_KB)



# ═══════════════════════════════════════════════════════════════════════════
#  11. Admin commands
# ═══════════════════════════════════════════════════════════════════════════
def _is_super(uid):
    return str(uid) in super_admin_ids()


async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_super(update.effective_user.id):
        return await reply(update, "❌ Super admin only.")
    if not context.args or not context.args[0].isdigit():
        return await reply(update, "Usage: <code>/addadmin 123456789</code>")
    t = context.args[0]
    if t not in NS["admins"]:
        NS["admins"].append(t)
        NS["save_admins"]()
    await reply(update, f"✅ <code>{t}</code> is now an admin.")


async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_super(update.effective_user.id):
        return await reply(update, "❌ Super admin only.")
    if not context.args:
        return await reply(update, "Usage: <code>/removeadmin 123456789</code>")
    t = context.args[0]
    if t in super_admin_ids():
        return await reply(update, "❌ Can't remove a super admin.")
    if t in NS["admins"]:
        NS["admins"].remove(t)
        NS["save_admins"]()
        NS["get_session"](t)["is_admin"] = False
        return await reply(update, f"✅ <code>{t}</code> removed.")
    await reply(update, "ℹ️ Not an admin.")


async def cmd_adminlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_adm(update.effective_user.id):
        return await reply(update, "❌ Admin only.")
    sup = super_admin_ids()
    lines = [f"• <code>{a}</code>{' ⭐' if str(a) in sup else ''}" for a in NS["admins"]]
    await reply(update, "👮 <b>Admins</b>\n" + ("\n".join(lines) or "none"))


# ═══════════════════════════════════════════════════════════════════════════
#  12. Background workers
# ═══════════════════════════════════════════════════════════════════════════
async def _flush_worker():
    while True:
        await asyncio.sleep(30)
        if _dirty:
            try:
                await asyncio.get_event_loop().run_in_executor(None, save_data)
            except Exception:
                logger.exception("extra_data save failed")


async def _report_worker(app):
    while True:
        await asyncio.sleep(60)
        try:
            r = DATA["report"]
            now = datetime.now()
            if r["enabled"] and now.hour == r["hour"] and r["last_sent"] != today_str():
                r["last_sent"] = today_str()
                yday = (date.today() - timedelta(days=1)).isoformat()
                await _send_admins(app, overview_text([yday], f"Daily Report — {yday}"))
                save_data()
        except Exception:
            logger.exception("daily report failed")


async def _stock_worker(app):
    while True:
        await asyncio.sleep(300)
        try:
            s = DATA["lowstock"]
            if not s["enabled"] or time.time() - s["last_alert"] < 6 * 3600:
                continue
            low = low_stock_list()
            if low:
                s["last_alert"] = time.time()
                lines = [f"{n:>4}  {NS['countries'].get(cc, {}).get('flag', '')} {cc} / {svc}" for n, cc, svc in low[:15]]
                await _send_admins(app, f"📉 <b>Low stock alert</b> (below {s['threshold']})\n<pre>"
                                        + "\n".join(lines) + "</pre>")
                save_data()
        except Exception:
            logger.exception("stock alert failed")


def start_background(application):
    asyncio.create_task(_flush_worker())
    asyncio.create_task(_report_worker(application))
    asyncio.create_task(_stock_worker(application))
    logger.info("extra_features: background workers started")


# ═══════════════════════════════════════════════════════════════════════════
#  SETUP
# ═══════════════════════════════════════════════════════════════════════════
def setup(app, bot_globals):
    """bot.py এর main() এ app বানানোর ঠিক পরে (অন্য handler এর আগে) ডাকতে হবে।"""
    global NS
    NS = bot_globals      # bot.py এর globals dict — wrapper বসালে সরাসরি bot.py এ কার্যকর হয়

    load_data()
    _build_label_map()
    bot_globals["otp_log"] = TrackedList(bot_globals["otp_log"])      # analytics hook

    install_force_join_wrappers()
    install_admin_keyboard_wrapper()
    orig_main_kb = bot_globals["main_keyboard"]

    def main_keyboard():
        uid = _CUR_UID.get()
        return build_main_keyboard(lang_of(uid), uid) if uid else orig_main_kb()

    bot_globals["main_keyboard"] = main_keyboard

    app.add_handler(TypeHandler(Update, pre_check), group=-2)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, state_router), group=-1)

    app.add_handler(CommandHandler(["ref", "referral"], cmd_ref))
    app.add_handler(CommandHandler(["setlang", "language", "lang"], cmd_lang))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("adminlist", cmd_adminlist))

    # মেনু বাটন — সব ভাষায় (referral/language আমাদের handler, বাকিগুলো bot.py এর)
    def exact(key):
        return "^(" + "|".join(re.escape(T[key][l]) for l in LANGS) + ")$"

    priv = filters.ChatType.PRIVATE
    app.add_handler(MessageHandler(filters.Regex(exact("m_ref")) & priv, cmd_ref))
    app.add_handler(MessageHandler(filters.Regex(exact("m_lang")) & priv, cmd_lang))
    import hub_features
    app.add_handler(MessageHandler(filters.Regex(exact("m_search")) & priv, hub_features.search_start))
    app.add_handler(MessageHandler(filters.Regex(exact("m_traffic")) & priv, hub_features.traffic_show))
    for key, fn in (("m_num", "handle_get_numbers"), ("m_bal", "handle_balance"),
                    ("m_wd", "handle_withdraw"), ("m_sup", "handle_support")):
        app.add_handler(MessageHandler(filters.Regex(exact(key)) & priv, bot_globals[fn]))

    app.add_handler(CallbackQueryHandler(cb_router, pattern=r"^xf:"))

    # ✨ AIR premium emoji (flags, service icons, button icons) — fail হলে নিজে থেকেই বন্ধ/fallback
    try:
        import premium_emoji
        premium_emoji.install(bot_globals)
    except Exception:
        logger.exception("premium_emoji install failed — continuing without it")
    try:
        hub_features.setup(app, bot_globals)      # Menu Design, Search, Traffic, Leaderboard, Stock, W.Methods
    except Exception:
        logger.exception("hub_features setup failed — continuing without it")
    logger.info("extra_features: setup complete")

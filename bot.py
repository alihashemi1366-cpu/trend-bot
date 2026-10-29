#!/usr/bin/env python3
"""ربات تلگرام: یافتن ویدیوهای ترند هم‌موضوع با کانال شما (منوی دکمه‌ای)."""
import asyncio
import hashlib
import html
import io
import logging
import os
import re
import time
import urllib.parse
from functools import wraps

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PersistenceInput,
    PicklePersistence,
    filters,
)

import ideas
import trend_core as tc

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # آدرس‌های httpx شامل توکن بات هستند
log = logging.getLogger("trendbot")

# ------------------------------------------------------------------ تنظیمات
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
YT_KEY = os.environ.get("YOUTUBE_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
IDEAS_ENABLED = bool(ANTHROPIC_KEY)
ALLOWED = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_USER_IDS", "")) if x.strip().isdigit()}
MAX_KEYWORDS = int(os.environ.get("MAX_KEYWORDS", "6"))
TOP_N = int(os.environ.get("TOP_N", "10"))
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", str(6 * 3600)))
PAGE_SIZE = 5

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
CACHE = {}  # (keywords, channel, days, lang) -> (time, result)

LANGS = [("fa", "🇮🇷 فارسی"), ("en", "🇬🇧 English"), ("any", "🌍 همه")]
LANG_LABEL = dict(LANGS)
DAYS = (7, 14, 30, 90)
DEFAULTS = {"keywords": [], "channel": None, "days": 30, "lang": "fa", "awaiting": None}


# ------------------------------------------------------------------ کمکی‌ها
def settings(context):
    s = context.user_data.setdefault("s", {})
    for k, v in DEFAULTS.items():
        s.setdefault(k, list(v) if isinstance(v, list) else v)
    return s


def esc(text):
    return html.escape(str(text), quote=False)


def fmt_num(n):
    if n is None:
        return "—"
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= limit:
            return f"{n / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def fmt_duration(sec):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_channel(text):
    text = text.strip()
    m = re.search(r"youtube\.com/(@[\w.\-%]+)", text)
    if m:
        return urllib.parse.unquote(m.group(1))
    m = re.search(r"youtube\.com/channel/(UC[\w-]{20,})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"UC[\w-]{20,}", text):
        return text
    if re.fullmatch(r"@?[\w.\-]{3,30}", text):
        return text if text.startswith("@") else "@" + text
    return None


def authorized(fn):
    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in ALLOWED:
            uid = user.id if user else "؟"
            if update.callback_query:
                await update.callback_query.answer("⛔ دسترسی ندارید", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_html(
                    f"⛔ دسترسی ندارید.\nشناسهٔ عددی شما: <code>{uid}</code>\n"
                    "مالک ربات باید آن را در <code>ALLOWED_USER_IDS</code> قرار دهد."
                )
            return None
        return await fn(update, context)
    return wrapper


# ------------------------------------------------------------------ منو
def menu_text(s):
    kws = "، ".join(s["keywords"]) if s["keywords"] else "—"
    return (
        "<b>🎯 یاب ویدیوهای ترند</b>\n\n"
        f"🔑 کلمات کلیدی: {esc(kws)}\n"
        f"📺 کانال من: {esc(s['channel'] or '—')}\n"
        f"📅 بازه: {s['days']} روز اخیر\n"
        f"🌐 زبان: {LANG_LABEL[s['lang']]}\n"
        "🎬 Shorts: حذف می‌شود\n\n"
        "تنظیمات را انتخاب کنید و «🚀 جستجو» را بزنید:"
    )


def menu_kb(s):
    def mark(cond, label):
        return ("✅ " if cond else "") + label

    return InlineKeyboardMarkup([
        [Btn("🔑 کلمات کلیدی", callback_data="kw"), Btn("📺 کانال من", callback_data="ch")],
        [Btn(mark(s["days"] == d, f"{d} روز"), callback_data=f"days:{d}") for d in DAYS],
        [Btn(mark(s["lang"] == code, label), callback_data=f"lang:{code}") for code, label in LANGS],
        [Btn("🚀 جستجو", callback_data="run")],
    ])


async def send_menu(message, context):
    s = settings(context)
    s["awaiting"] = None
    await message.reply_html(menu_text(s), reply_markup=menu_kb(s))


async def edit_menu(query, context):
    s = settings(context)
    s["awaiting"] = None
    try:
        await query.edit_message_text(menu_text(s), reply_markup=menu_kb(s), parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ------------------------------------------------------------------ دستورها
@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_menu(update.effective_message, context)


@authorized
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = settings(context)
    text = (update.effective_message.text or "").strip()
    mode = s.get("awaiting")

    if mode == "channel":
        ch = parse_channel(text)
        if not ch:
            await update.effective_message.reply_html(
                "نشناختم 🤔 هندل (<code>@mychannel</code>) یا لینک کانال را بفرستید.")
            return
        s["channel"] = ch
        await update.effective_message.reply_html(f"✅ کانال ثبت شد: <code>{esc(ch)}</code>")
    else:
        # هم در حالت «کلمات کلیدی» و هم متن آزاد، آن را کلمات کلیدی می‌گیریم
        parts = [p.strip() for p in re.split(r"[,\n،;]+", text) if p.strip()]
        if not parts:
            return
        s["keywords"] = list(dict.fromkeys(parts))[:MAX_KEYWORDS]
        extra = ""
        if len(set(parts)) > MAX_KEYWORDS:
            extra = f" (فقط {MAX_KEYWORDS} کلمهٔ اول نگه داشته شد)"
        await update.effective_message.reply_html(f"✅ کلمات ثبت شد{extra}.")
    await send_menu(update.effective_message, context)


# ------------------------------------------------------------------ دکمه‌ها
@authorized
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    s = settings(context)
    data = q.data

    if data == "run":
        await run_search(update, context)
        return
    if data in ("ideas", "ideas_new"):
        await run_ideas(update, context, fresh=(data == "ideas_new"))
        return
    if data == "csv":
        rows = (context.user_data.get("last_result") or {}).get("rows")
        if not rows:
            await q.answer("نتیجه‌ای برای ذخیره نیست؛ دوباره جستجو کنید.", show_alert=True)
            return
        await q.answer()
        await q.message.reply_document(
            document=io.BytesIO(tc.rows_to_csv(rows)),
            filename="trending_videos.csv",
            caption=f"📄 {len(rows)} ویدیو",
        )
        return

    await q.answer()
    if data == "kw":
        s["awaiting"] = "keywords"
        await q.edit_message_text(
            "🔑 کلمات کلیدی را بفرستید.\n"
            "چند کلمه را با ویرگول یا خط جدید جدا کنید.\n"
            "مثال: <code>آموزش پایتون، هوش مصنوعی، ربات تلگرام</code>\n\n"
            f"حداکثر {MAX_KEYWORDS} کلمه (هر کلمه ۱۰۰ واحد از سهمیهٔ روزانهٔ یوتیوب مصرف می‌کند).",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                Btn("🗑 پاک کردن کلمات", callback_data="kw_clear"),
                Btn("↩️ بازگشت", callback_data="back"),
            ]]),
        )
    elif data == "ch":
        s["awaiting"] = "channel"
        await q.edit_message_text(
            "📺 هندل یا لینک کانال خودتان را بفرستید.\n"
            "مثال: <code>@mychannel</code>\n\n"
            "کلمات کلیدی از ۳۰ ویدیوی آخر کانال استخراج و به کلمات شما اضافه می‌شود.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                Btn("🗑 حذف کانال", callback_data="ch_clear"),
                Btn("↩️ بازگشت", callback_data="back"),
            ]]),
        )
    elif data == "kw_clear":
        s["keywords"] = []
        await edit_menu(q, context)
    elif data == "ch_clear":
        s["channel"] = None
        await edit_menu(q, context)
    elif data == "back":
        await edit_menu(q, context)
    elif data == "menu":
        await send_menu(q.message, context)
    elif data.startswith("days:"):
        s["days"] = int(data.split(":")[1])
        await edit_menu(q, context)
    elif data.startswith("lang:"):
        code = data.split(":")[1]
        if code in LANG_LABEL:
            s["lang"] = code
        await edit_menu(q, context)


# ------------------------------------------------------------------ جستجو و نمایش نتایج
def render_entries(rows, start=1):
    out = []
    for i, r in enumerate(rows, start):
        o = r["outlier_score"]
        o_txt = "—" if o is None else f"{o:.1f}×"
        icon = "🔥" if (o or 0) >= 5 else "📈"
        title = r["title"] if len(r["title"]) <= 90 else r["title"][:87] + "…"
        channel = r["channel"] if len(r["channel"]) <= 40 else r["channel"][:37] + "…"
        out.append(
            f'<b>{i}.</b> <a href="{r["url"]}">{esc(title)}</a>\n'
            f"📺 {esc(channel)} · 👥 {fmt_num(r['subs'])}\n"
            f"👁 {fmt_num(r['views'])} · ⚡ {fmt_num(r['views_per_day'])}/روز · "
            f"{icon} {o_txt} · ⏱ {fmt_duration(r['duration_sec'])}"
        )
    return "\n\n".join(out)


def build_messages(result, s, cached_age=None):
    rows = result["rows"][:TOP_N]
    header = (
        f"<b>🎯 {len(rows)} ویدیوی ترند</b> · {s['days']} روز اخیر\n"
        f"🔑 {esc('، '.join(result['keywords']))}\n"
        f"🎬 {result['shorts_dropped']} شورت حذف شد\n"
    )
    if cached_age is not None:
        header += f"♻️ نتیجهٔ ذخیره‌شده ({max(1, cached_age // 60)} دقیقه پیش)\n"
    header += "\n<i>🔥/📈 = بازدید ÷ مشترکین کانال؛ هرچه بزرگ‌تر، خودِ موضوع جذاب‌تر بوده.</i>\n\n"

    pages = [rows[i:i + PAGE_SIZE] for i in range(0, len(rows), PAGE_SIZE)]
    messages = []
    for idx, page in enumerate(pages):
        text = (header if idx == 0 else "") + render_entries(page, start=idx * PAGE_SIZE + 1)
        if idx == len(pages) - 1 and result["title_words"]:
            text += "\n\n💡 <b>کلمات پرتکرار در عنوان‌های برتر:</b>\n" + esc("، ".join(result["title_words"]))
        messages.append(text)
    return messages


async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    s = settings(context)
    if not s["keywords"] and not s["channel"]:
        await q.answer("اول کلمهٔ کلیدی یا کانال را تنظیم کنید", show_alert=True)
        return
    await q.answer()
    status = await q.message.reply_text("⏳ در حال جستجو… (چند ثانیه)")

    key = (tuple(s["keywords"]), s["channel"], s["days"], s["lang"])
    cached = CACHE.get(key)
    cached_age = None
    if cached and time.time() - cached[0] < CACHE_TTL:
        result = cached[1]
        cached_age = int(time.time() - cached[0])
    else:
        try:
            result = await asyncio.to_thread(
                tc.find_trends, YT_KEY,
                keywords=s["keywords"], channel=s["channel"], days=s["days"],
                lang=s["lang"], max_keywords=MAX_KEYWORDS,
            )
        except tc.TrendError as e:
            await status.edit_text(f"❌ {e}")
            return
        except Exception:
            log.exception("search failed")
            await status.edit_text("❌ خطای غیرمنتظره رخ داد؛ دوباره تلاش کنید.")
            return
        if len(CACHE) > 100:
            CACHE.clear()
        CACHE[key] = (time.time(), result)

    context.user_data["last_result"] = result
    await status.delete()

    if not result["rows"]:
        await q.message.reply_html(
            f"نتیجه‌ای پیدا نشد 😕 ({result['shorts_dropped']} شورت حذف شد).\n"
            "بازهٔ زمانی را بیشتر کنید یا کلمات دیگری امتحان کنید.",
            reply_markup=InlineKeyboardMarkup([[Btn("⚙️ منو", callback_data="menu")]]),
        )
        return

    messages = build_messages(result, s, cached_age)
    button_rows = []
    if IDEAS_ENABLED:
        button_rows.append([Btn("💡 ایده‌های ویدیو", callback_data="ideas")])
    button_rows.append([
        Btn("📄 فایل CSV", callback_data="csv"),
        Btn("⚙️ منو", callback_data="menu"),
    ])
    buttons = InlineKeyboardMarkup(button_rows)
    for i, text in enumerate(messages):
        last = i == len(messages) - 1
        await q.message.reply_html(
            text, link_preview_options=NO_PREVIEW, reply_markup=buttons if last else None)


def split_text(text, limit=4000):
    """تقسیم متن بلند به پیام‌های کوچک‌تر از حد تلگرام، روی خط خالی."""
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > limit:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
        while len(cur) > limit:  # یک بند خیلی بلند
            parts.append(cur[:limit])
            cur = cur[limit:]
    if cur:
        parts.append(cur)
    return parts


async def run_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE, fresh=False):
    q = update.callback_query
    if not IDEAS_ENABLED:
        await q.answer("ANTHROPIC_API_KEY تنظیم نشده است.", show_alert=True)
        return
    result = context.user_data.get("last_result")
    if not result or not result.get("rows"):
        await q.answer("اول یک جستجو انجام دهید.", show_alert=True)
        return
    await q.answer()

    cached = result.get("ideas")
    if cached and not fresh and time.time() - cached[0] < CACHE_TTL:
        text = cached[1]
    else:
        status = await q.message.reply_text("💡 در حال ساختن ایده‌ها… (۱۰ تا ۳۰ ثانیه)")
        try:
            text = await asyncio.to_thread(
                ideas.generate_ideas, ANTHROPIC_KEY, result["rows"][:TOP_N],
                result["keywords"], result.get("own_titles", []), ANTHROPIC_MODEL,
            )
        except ideas.IdeaError as e:
            await status.edit_text(f"❌ {e}")
            return
        except Exception:
            log.exception("ideas failed")
            await status.edit_text("❌ خطای غیرمنتظره در ساخت ایده‌ها؛ دوباره تلاش کنید.")
            return
        result["ideas"] = (time.time(), text)
        await status.delete()

    kb = InlineKeyboardMarkup([[
        Btn("🔄 ایده‌های جدید", callback_data="ideas_new"),
        Btn("⚙️ منو", callback_data="menu"),
    ]])
    chunks = split_text("💡 ایده‌های ویدیو بر اساس ترندها\n\n" + text)
    for i, chunk in enumerate(chunks):
        await q.message.reply_text(chunk, reply_markup=kb if i == len(chunks) - 1 else None)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)


# ------------------------------------------------------------------ راه‌اندازی
def build_app(token):
    persistence = PicklePersistence(
        filepath=os.environ.get("PERSIST_PATH", "bot_state.pickle"),
        store_data=PersistenceInput(bot_data=False, chat_data=False, callback_data=False),
    )
    app = Application.builder().token(token).persistence(persistence).build()
    app.add_handler(CommandHandler(["start", "menu"], cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app


def main():
    if not TOKEN or not YT_KEY:
        raise SystemExit("متغیرهای TELEGRAM_BOT_TOKEN و YOUTUBE_API_KEY باید تنظیم شوند.")
    if not ALLOWED:
        log.warning("ALLOWED_USER_IDS خالی است؛ ربات به همه «دسترسی ندارید» + شناسهٔ عددی‌شان را نشان می‌دهد.")

    app = build_app(TOKEN)
    base = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_BASE_URL")
    if base:
        raw_secret = os.environ.get("WEBHOOK_SECRET")
        if not raw_secret:
            raise SystemExit("WEBHOOK_SECRET باید تنظیم شود.")
        # تلگرام فقط A-Z a-z 0-9 _ - را در secret_token قبول می‌کند؛ هر مقداری را به هش امن تبدیل می‌کنیم.
        secret = hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()
        log.info("Starting in webhook mode")
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", "10000")),
            url_path=secret,
            webhook_url=f"{base.rstrip('/')}/{secret}",
            secret_token=secret,
            drop_pending_updates=True,
        )
    else:
        log.info("Starting in polling mode (local)")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

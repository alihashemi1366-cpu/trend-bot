"""
trend_core.py - منطق اصلی: پیدا کردن ویدیوهای ترند (outlier) هم‌موضوع در یوتیوب.

فقط از کتابخانهٔ استاندارد پایتون استفاده می‌کند.
هزینهٔ سهمیه: هر جستجو (search.list) ۱۰۰ واحد از ۱۰٬۰۰۰ واحد روزانه؛ بقیه ۱ واحد.
"""
import csv
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

API = "https://www.googleapis.com/youtube/v3"

# Shorts تا ۳ دقیقه هم می‌شوند، پس هر ویدیوی ≤ ۱۸۰ ثانیه «شورت» حساب می‌شود.
SHORTS_MAX_SECONDS = 180

STOPWORDS = {
    "که", "از", "در", "به", "را", "با", "این", "آن", "برای", "های", "ها", "یک", "تا", "هم",
    "the", "and", "for", "with", "you", "your", "how", "what", "this", "that", "from",
    "are", "was", "not", "all", "new", "can", "will", "why", "who",
}

CSV_FIELDS = [
    "title", "channel", "subs", "views", "views_per_day", "outlier_score",
    "age_days", "duration_sec", "likes", "comments", "keyword", "url",
]


class TrendError(Exception):
    """خطایی که پیام آن برای نمایش به کاربر مناسب است."""


# ---------------------------------------------------------------- API helpers
def _call(api_key, endpoint, **params):
    params["key"] = api_key
    url = f"{API}/{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:  # باید قبل از URLError بیاید
        body = e.read().decode("utf-8", "ignore")
        if "quotaExceeded" in body:
            raise TrendError("سهمیهٔ روزانهٔ YouTube API تمام شده؛ فردا دوباره تلاش کنید.") from None
        if "keyInvalid" in body or "API key not valid" in body:
            raise TrendError("کلید YouTube API معتبر نیست.") from None
        raise TrendError(f"خطای YouTube API (کد {e.code}).") from None
    except urllib.error.URLError:
        raise TrendError("اتصال به YouTube برقرار نشد.") from None


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _parse_duration(iso):
    """PT1H2M3S -> ثانیه"""
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def _words(text):
    return [w for w in re.findall(r"\w{3,}", text.lower()) if w not in STOPWORDS and not w.isdigit()]


def _is_short(duration, title, tags):
    if duration <= SHORTS_MAX_SECONDS:
        return True
    blob = (title + " " + " ".join(tags or [])).lower()
    return re.search(r"#shorts?\b", blob) is not None


# ------------------------------------------------- استخراج کلمات از کانال خودتان
def resolve_channel(api_key, channel):
    if channel.startswith("@"):
        data = _call(api_key, "channels", part="id,contentDetails", forHandle=channel)
    else:
        data = _call(api_key, "channels", part="id,contentDetails", id=channel)
    items = data.get("items", [])
    if not items:
        raise TrendError("کانال پیدا نشد. هندل (@name) یا شناسهٔ UC... را بررسی کنید.")
    it = items[0]
    return it["id"], it["contentDetails"]["relatedPlaylists"]["uploads"]


def keywords_from_channel(api_key, uploads_playlist, max_videos=30, top_n=3):
    data = _call(api_key, "playlistItems", part="contentDetails",
                 playlistId=uploads_playlist, maxResults=min(50, max_videos))
    ids = [i["contentDetails"]["videoId"] for i in data.get("items", [])]
    if not ids:
        return [], []

    tags, words, titles = Counter(), Counter(), []
    vids = _call(api_key, "videos", part="snippet", id=",".join(ids))
    for v in vids.get("items", []):
        titles.append(v["snippet"]["title"])
        for t in v["snippet"].get("tags", []):
            tags[t.strip().lower()] += 1
        words.update(_words(v["snippet"]["title"]))

    kws = [t for t, _ in tags.most_common(top_n)]
    if len(kws) < top_n:
        for w, _ in words.most_common():
            if w not in kws:
                kws.append(w)
            if len(kws) >= top_n:
                break
    return kws, titles


# ------------------------------------------------------------------ جستجو
def _search_videos(api_key, keyword, since, lang, region, per_keyword):
    params = dict(part="snippet", q=keyword, type="video", order="viewCount",
                  publishedAfter=since, maxResults=min(per_keyword, 50))
    if lang:
        params["relevanceLanguage"] = lang
    if region:
        params["regionCode"] = region
    data = _call(api_key, "search", **params)
    return [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]


def _fetch_videos(api_key, ids):
    out = {}
    for batch in _chunks(list(ids), 50):
        data = _call(api_key, "videos", part="snippet,statistics,contentDetails", id=",".join(batch))
        for v in data.get("items", []):
            out[v["id"]] = v
    return out


def _fetch_channel_subs(api_key, channel_ids):
    out = {}
    for batch in _chunks(list(channel_ids), 50):
        data = _call(api_key, "channels", part="statistics", id=",".join(batch))
        for c in data.get("items", []):
            st = c["statistics"]
            out[c["id"]] = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))
    return out


def find_trends(api_key, keywords=None, channel=None, days=30, lang=None, region=None,
                min_views=1000, include_shorts=False, per_keyword=50,
                channel_keywords=3, max_keywords=6):
    """
    خروجی: dict با کلیدهای
      keywords      کلمات استفاده‌شده
      rows          لیست ویدیوها (مرتب‌شده بر اساس outlier_score و بعد views_per_day)
      title_words   کلمات پرتکرار عنوان‌های برتر
      shorts_dropped تعداد شورت‌های حذف‌شده
    """
    kws = [k.strip() for k in (keywords or []) if k and k.strip()]
    own_channel_id = None
    own_titles = []
    if channel:
        own_channel_id, uploads = resolve_channel(api_key, channel)
        channel_kws, own_titles = keywords_from_channel(api_key, uploads, top_n=channel_keywords)
        kws += channel_kws
    kws = list(dict.fromkeys(kws))[:max_keywords]
    if not kws:
        raise TrendError("هیچ کلمهٔ کلیدی‌ای پیدا نشد؛ کلمه یا کانال را تنظیم کنید.")

    lang = None if lang in (None, "", "any") else lang
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    found_by = {}
    for kw in kws:
        for vid in _search_videos(api_key, kw, since, lang, region, per_keyword):
            found_by.setdefault(vid, kw)

    videos = _fetch_videos(api_key, found_by.keys())
    subs = _fetch_channel_subs(api_key, {v["snippet"]["channelId"] for v in videos.values()})

    now = datetime.now(timezone.utc)
    rows, shorts_dropped = [], 0
    for vid, v in videos.items():
        sn, st = v["snippet"], v.get("statistics", {})
        if own_channel_id and sn["channelId"] == own_channel_id:
            continue
        duration = _parse_duration(v["contentDetails"].get("duration"))
        if not include_shorts and _is_short(duration, sn["title"], sn.get("tags")):
            shorts_dropped += 1
            continue
        views = int(st.get("viewCount", 0))
        if views < min_views:
            continue
        published = datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
        age_days = max((now - published).total_seconds() / 86400, 0.5)
        ch_subs = subs.get(sn["channelId"])
        # بازدید نسبت به اندازهٔ کانال؛ کف ۱۰۰۰ تا کانال‌های خیلی کوچک عدد بی‌معنی نسازند
        outlier = round(views / max(ch_subs, 1000), 2) if ch_subs is not None else None
        rows.append({
            "title": sn["title"],
            "channel": sn["channelTitle"],
            "subs": ch_subs,
            "views": views,
            "views_per_day": round(views / age_days),
            "outlier_score": outlier,
            "age_days": round(age_days, 1),
            "duration_sec": duration,
            "likes": int(st.get("likeCount", 0)),
            "comments": int(st.get("commentCount", 0)),
            "keyword": found_by.get(vid, ""),
            "url": f"https://www.youtube.com/watch?v={vid}",
        })

    rows.sort(key=lambda r: (r["outlier_score"] if r["outlier_score"] is not None else -1,
                             r["views_per_day"]), reverse=True)

    counter = Counter()
    for r in rows[:30]:
        counter.update(_words(r["title"]))
    return {
        "keywords": kws,
        "rows": rows,
        "title_words": [w for w, _ in counter.most_common(10)],
        "shorts_dropped": shorts_dropped,
        "own_titles": own_titles,
    }


def rows_to_csv(rows):
    """CSV با BOM تا فارسی در Excel درست باز شود."""
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in CSV_FIELDS})
    return buf.getvalue().encode("utf-8-sig")

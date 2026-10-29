#!/usr/bin/env python3
"""
نسخهٔ خط فرمان (بدون تلگرام) - همان منطق ربات.

    export YOUTUBE_API_KEY="کلید-شما"
    python trend_finder.py --channel @mychannel --days 30 --lang fa
    python trend_finder.py --keywords "آموزش پایتون" "هوش مصنوعی" --days 14

Shorts (ویدیوهای ≤ ۳ دقیقه) پیش‌فرض حذف می‌شوند؛ برای نگه داشتن: --include-shorts
"""
import argparse
import os
import sys

import trend_core as tc


def main():
    ap = argparse.ArgumentParser(description="پیدا کردن ویدیوهای ترند هم‌موضوع")
    ap.add_argument("--channel", help="@handle یا UC... برای استخراج خودکار کلمات")
    ap.add_argument("--keywords", nargs="*", help="کلمات کلیدی دستی")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--lang", help="مثلاً fa یا en")
    ap.add_argument("--region", help="مثلاً IR یا US")
    ap.add_argument("--min-views", type=int, default=1000)
    ap.add_argument("--include-shorts", action="store_true")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--out", default="trending_videos.csv")
    args = ap.parse_args()

    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        sys.exit("خطا: YOUTUBE_API_KEY تنظیم نشده است.")
    try:
        res = tc.find_trends(key, keywords=args.keywords, channel=args.channel, days=args.days,
                             lang=args.lang, region=args.region, min_views=args.min_views,
                             include_shorts=args.include_shorts)
    except tc.TrendError as e:
        sys.exit(f"خطا: {e}")

    rows = res["rows"]
    print(f"کلمات: {res['keywords']} | شورت حذف‌شده: {res['shorts_dropped']} | نتیجه: {len(rows)}")
    if not rows:
        sys.exit("نتیجه‌ای پیدا نشد.")
    with open(args.out, "wb") as f:
        f.write(tc.rows_to_csv(rows))
    for r in rows[:args.top]:
        o = "—" if r["outlier_score"] is None else f"{r['outlier_score']}x"
        print(f"[{o}] {r['title'][:70]}\n      {r['channel']} | {r['views']:,} بازدید | {r['url']}")
    print("\nکلمات پرتکرار:", res["title_words"])
    print(f"خروجی کامل: {args.out}")


if __name__ == "__main__":
    main()

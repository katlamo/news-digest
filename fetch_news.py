#!/usr/bin/env python3
"""News aggregator: collects RSS/Atom entries from the last 24 hours,
removes duplicates and sends the list to Telegram (or just prints it).
Times and dates are shown in the Europe/Prague timezone."""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from calendar import timegm
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser

HOURS = int(os.getenv("HOURS", "24"))                    # look-back window
FEEDS_FILE = os.getenv("FEEDS_FILE", "feeds.txt")
TZ = ZoneInfo(os.getenv("DIGEST_TZ", "Europe/Prague"))   # display timezone (handles CET/CEST)
UA = "Mozilla/5.0 (compatible; news-digest/1.0)"
TRACKING = ("utm_", "fbclid", "gclid", "ref", "ref_src")  # query params ignored when comparing URLs


def read_feeds(path):
    """Each line is either 'URL' or 'Name | URL'. Everything after '#' is a comment."""
    feeds = []
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, url = line.rpartition("|")
        feeds.append((name.strip(), url.strip()))
    return feeds


def norm_url(url):
    """Normalize a URL (host case, www, trailing slash, tracking params) to detect duplicates."""
    p = urllib.parse.urlsplit(url.strip())
    host = p.netloc.lower().removeprefix("www.")
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
             if not k.lower().startswith(TRACKING)]
    path = p.path.rstrip("/") or "/"
    return f"{host}{path}?{urllib.parse.urlencode(query)}" if query else f"{host}{path}"


def norm_title(title):
    """Lowercase title without punctuation/spaces, used to catch the same story under different URLs."""
    return re.sub(r"[\W_]+", "", title.lower())


def entry_time(e):
    """Return the entry's timestamp in UTC, or None if the feed gives no date."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = e.get(key)
        if t:
            return datetime.fromtimestamp(timegm(t), tz=timezone.utc)
    return None


def collect(feeds):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS)
    seen_urls, seen_titles = set(), set()
    items, failed, no_date = [], [], 0
    for name, url in feeds:
        d = feedparser.parse(url, agent=UA)
        if not d.entries:
            failed.append(url)  # unreachable, broken, or empty feed
            continue
        source = name or d.feed.get("title") or urllib.parse.urlsplit(url).netloc
        for e in d.entries:
            link, title = e.get("link"), (e.get("title") or "").strip()
            if not link or not title:
                continue
            ts = entry_time(e)
            if ts is None:
                no_date += 1  # cannot tell whether it is fresh, so skip it
                continue
            if ts < cutoff:
                continue
            # Deduplicate by normalized URL and by normalized title (first feed in feeds.txt wins)
            nu, nt = norm_url(link), norm_title(title)
            if nu in seen_urls or nt in seen_titles:
                continue
            seen_urls.add(nu)
            seen_titles.add(nt)
            items.append({"source": source, "title": title, "link": link,
                          "ts": ts.astimezone(TZ)})
    items.sort(key=lambda x: x["ts"], reverse=True)  # newest first
    return items, failed, no_date


def build_blocks(items, failed, no_date):
    """Return (plain_text, [html_blocks]); plain text is for logs, HTML blocks are for Telegram."""
    today = datetime.now(TZ).strftime("%d.%m.%Y")
    head = f"News digest, last {HOURS}h - {today} ({len(items)} items)"
    plain = [head, ""]
    blocks = [f"<b>{html.escape(head)}</b>"]
    by_source = {}
    for it in items:
        by_source.setdefault(it["source"], []).append(it)
    for source, group in by_source.items():
        plain.append(f"## {source}")
        lines = [f"\n<b>{html.escape(source)}</b>"]
        for it in group:
            hhmm = it["ts"].strftime("%H:%M")
            plain.append(f"- [{hhmm}] {it['title']} - {it['link']}")
            lines.append(f'- <a href="{html.escape(it["link"], quote=True)}">'
                         f'{html.escape(it["title"])}</a>')
        plain.append("")
        blocks.append("\n".join(lines))
    if not items:
        blocks.append("\nNo new entries in this period.")
        plain.append("No new entries.")
    notes = []
    if failed:
        notes.append("Feeds that failed to load: " + ", ".join(failed))
    if no_date:
        notes.append(f"Entries skipped (no date): {no_date}")
    if notes:
        plain.append("\n".join(notes))
        blocks.append("\n[!] " + html.escape("\n".join(notes)))
    return "\n".join(plain), blocks


def chunk(blocks, limit=3800):
    """Pack blocks into messages that stay under Telegram's 4096-character limit."""
    messages, cur = [], ""
    for b in blocks:
        # An oversized block is split between lines (never inside an HTML tag);
        # only a single line longer than the limit is cut as a last resort
        parts = [b] if len(b) <= limit else [
            piece for line in b.split("\n")
            for piece in ([line] if len(line) <= limit else
                          [line[i:i + limit] for i in range(0, len(line), limit)])]
        for part in parts:
            if cur and len(cur) + len(part) + 1 > limit:
                messages.append(cur)
                cur = ""
            cur = f"{cur}\n{part}" if cur else part
    if cur:
        messages.append(cur)
    return messages


def send_telegram(token, chat_id, text):
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        # Telegram explains the problem in the response body (e.g. "can't parse entities")
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Telegram HTTP {e.code}: {body}") from None
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram returned an error: {resp}")


def main():
    items, failed, no_date = collect(read_feeds(FEEDS_FILE))
    plain, blocks = build_blocks(items, failed, no_date)
    print(plain)
    open("digest.md", "w", encoding="utf-8").write(plain)  # also shown in the GitHub run summary
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        messages, errors = chunk(blocks), []
        for n, msg in enumerate(messages, 1):
            try:
                send_telegram(token, chat, msg)
            except Exception as e:  # keep sending the rest, report at the end
                errors.append(f"message {n}/{len(messages)}: {e}")
        if errors:
            print("\nTelegram send failures:\n" + "\n".join(errors), file=sys.stderr)
            sys.exit(1)
    else:
        print("\n(TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set - sending skipped)",
              file=sys.stderr)


if __name__ == "__main__":
    main()

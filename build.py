#!/usr/bin/env python3
"""Builds TL;DR Today: fetches RSS from major news sites and renders
a wizarding-style newspaper as a single self-contained HTML file."""

import base64
import datetime as dt
import io
import json
import math
import email.utils
import html
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "tldr-today.html")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) TLDRToday/1.0"

# (section key, source name, feed url)
FEEDS = [
    ("world", "BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("world", "The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ("world", "The Guardian", "https://www.theguardian.com/world/rss"),
    ("world", "Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("world", "NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("business", "BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("tech", "The Verge", "https://www.theverge.com/rss/index.xml"),
    ("science", "ScienceDaily", "https://www.sciencedaily.com/rss/top/science.xml"),
    ("sport", "ESPN", "https://www.espn.com/espn/rss/news"),
    ("sport", "BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml"),
    ("india", "The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss"),
    ("india", "Times of India", "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"),
    ("india", "Hindustan Times", "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml"),
    ("india", "NDTV", "https://feeds.feedburner.com/ndtvnews-india-news"),
    ("business", "The Economic Times", "https://economictimes.indiatimes.com/rssfeedstopstories.cms"),
    ("business", "Mint", "https://www.livemint.com/rss/news"),
    ("business", "The Hindu BusinessLine", "https://www.thehindu.com/business/feeder/default.rss"),
    ("tech", "The Hindu", "https://www.thehindu.com/sci-tech/feeder/default.rss"),
    ("tech", "Times of India", "https://timesofindia.indiatimes.com/rssfeeds/66949542.cms"),
    ("tech", "Mint", "https://www.livemint.com/rss/technology"),
    ("sport", "ESPNcricinfo", "https://www.espncricinfo.com/rss/content/story/feeds/0.xml"),
    ("sport", "The Hindu", "https://www.thehindu.com/sport/feeder/default.rss"),
    ("sport", "Times of India", "https://timesofindia.indiatimes.com/rssfeeds/4719148.cms"),
]

# One paper: global and Indian sources share each section.
# key: (wizarding title, plain label, story count)
SECTIONS = {
    "world": ("Owl Post from Distant Lands", "World", 9),
    "india": ("Tidings from the Realm", "India", 9),
    "business": ("The Gringotts Ledger", "Business & Markets", 8),
    "tech": ("Muggle Contraptions & Curiosities", "Technology", 7),
    "science": ("Dispatches from the Department of Mysteries", "Science", 4),
    "sport": ("The Quaffle Report", "Sport & Cricket", 9),
}
PER_SECTION = {k: v[2] for k, v in SECTIONS.items()}
NS = {
    "media": "http://search.yahoo.com/mrss/",
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(text, limit=None):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"
    return text


def parse_date(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def find_image(item):
    for tag in ("media:content", "media:thumbnail"):
        best = None
        for el in item.findall(tag, NS):
            u, w = el.get("url"), int(el.get("width") or 0)
            if u and (best is None or w > best[1]):
                best = (u, w)
        if best:
            return best[0]
    enc = item.find("enclosure")
    if enc is not None and (enc.get("type") or "").startswith("image"):
        return enc.get("url")
    body = (item.findtext("content:encoded", "", NS) or "") + (item.findtext("description") or "")
    m = re.search(r'<img[^>]+src="([^"]+)"', body)
    return m.group(1) if m else None


def parse_feed(section, source, url):
    try:
        root = ET.fromstring(fetch(url))
    except Exception as e:  # one dead feed must not stop the presses
        print(f"  ! {source}: {e}")
        return []
    items = root.findall(".//item") or root.findall(".//atom:entry", NS)
    out = []
    for it in items:
        title = clean(it.findtext("title") or it.findtext("atom:title", "", NS))
        link = it.findtext("link") or ""
        if not link:
            el = it.find("atom:link", NS)
            link = el.get("href") if el is not None else ""
        summary = clean(
            it.findtext("description") or it.findtext("atom:summary", "", NS)
            or it.findtext("atom:content", "", NS), 260)
        date = parse_date(it.findtext("pubDate") or it.findtext("atom:updated", "", NS)
                          or it.findtext("atom:published", "", NS))
        if title and link.strip():
            out.append({"section": section, "source": source, "title": title,
                        "link": link.strip(), "summary": summary, "date": date,
                        "image": find_image(it)})
    print(f"  {source}: {len(out)} stories")
    return out


def og_image(link):
    try:
        page = fetch(link, timeout=12)[:300000].decode("utf-8", "ignore")
    except Exception:
        return None
    m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', page))
    return html.unescape(m.group(1)) if m else None


def embed_image(url):
    """Download, shrink and sepia-ready an image; return a data: URI (the
    artifact sandbox only allows inline images)."""
    try:
        raw = fetch(url, timeout=15)
        try:
            from PIL import Image  # on GitHub Actions
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            im.thumbnail((560, 560))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=55, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except ImportError:
            pass
        with tempfile.TemporaryDirectory() as d:  # macOS fallback
            src, dst = os.path.join(d, "in"), os.path.join(d, "out.jpg")
            open(src, "wb").write(raw)
            subprocess.run(["sips", "-Z", "560", "-s", "format", "jpeg",
                            "-s", "formatOptions", "55", src, "--out", dst],
                           check=True, capture_output=True)
            data = open(dst, "rb").read()
        return "data:image/jpeg;base64," + base64.b64encode(data).decode()
    except Exception as e:
        print(f"  ! image {url[:60]}: {e}")
        return None


def pick(stories):
    seen, by_sec = set(), {k: [] for k in SECTIONS}
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    # interleave sources so no single paper dominates a section
    for sec in SECTIONS:
        pool = [s for s in stories if s["section"] == sec]
        sources = {}
        for s in sorted(pool, key=lambda s: s["date"] or epoch, reverse=True):
            sources.setdefault(s["source"], []).append(s)
        queues = list(sources.values())
        while any(queues) and len(by_sec[sec]) < PER_SECTION[sec]:
            for q in queues:
                if q and len(by_sec[sec]) < PER_SECTION[sec]:
                    s = q.pop(0)
                    key = re.sub(r"\W+", "", s["title"].lower())[:50]
                    if key not in seen:
                        seen.add(key)
                        by_sec[sec].append(s)
    return by_sec


def moon(now):
    """Tonight's moon, from the mean synodic month (good to within a day)."""
    period = 29.530588853
    age = ((now - dt.datetime(2000, 1, 6, 18, 14, tzinfo=dt.timezone.utc)).total_seconds()
           / 86400) % period
    lit = round((1 - math.cos(2 * math.pi * age / period)) / 2 * 100)
    names = ["New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
             "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent"]
    name = names[int((age / period) * 8 + 0.5) % 8]
    return name, lit, age / period


def moon_shadow(f):
    """x offset of the shadow disc: covers the moon at new, slides off
    to the left while waxing, returns from the right while waning."""
    return -64 * f if f < 0.5 else 64 * (1 - f)


def ago(d, now):
    if not d:
        return ""
    h = int((now - d).total_seconds() // 3600)
    if h < 1:
        return "within the hour"
    if h < 24:
        return f"{h} hour{'s' if h != 1 else ''} ago"
    return d.strftime("%-d %b")


def article(s, now, cls="", img=None, caption=None):
    e = html.escape
    meta = " · ".join(x for x in ("Owled in by " + e(s["source"]), ago(s["date"], now)) if x)
    pic = ""
    if img:
        pic = (f'<figure class="photo"><div class="frame"><img src="{img}" alt=""></div>'
               f'<figcaption>{e(caption or s["title"])}</figcaption></figure>')
    summary = f'<p class="dek">{e(s["summary"])}</p>' if s["summary"] else ""
    return (f'<article class="story {cls}">{pic}<h3><a href="{e(s["link"])}" target="_blank" '
            f'rel="noopener">{e(s["title"])}</a></h3>{summary}'
            f'<p class="byline">{meta} — <a href="{e(s["link"])}" target="_blank" '
            f'rel="noopener">follow the owl to {e(s["source"])} →</a></p></article>')


# (endpoint, model) pairs, tried in order
REWRITE_MODELS = [
    ("https://models.github.ai/inference/chat/completions", "openai/gpt-4.1-mini"),
    ("https://models.inference.ai.azure.com/chat/completions", "gpt-4o-mini"),
    ("https://models.github.ai/inference/chat/completions", "openai/gpt-4o-mini"),
]
REWRITE_PROMPT = """You rewrite news headlines and summaries for TL;DR Today, a daily paper read by people in their late teens and twenties.

For each item, write:
- "title": a headline of at most 12 words, in plain everyday English.
- "summary": one or two short sentences, at most 40 words total, that say what happened and why it matters.

Rules:
- Keep every fact exact: names, places, numbers, dates and who said what. Never add anything that isn't in the original. If the original summary is empty or vague, summarise only what the headline says.
- Use simple words and short sentences. Explain jargon in a few words.
- Add a light touch of Gen Z slang where it fits naturally, at most one slang phrase per item: e.g. "lowkey", "highkey", "no cap", "it's giving", "a big W", "a major L", "the tea", "main character energy", "rent-free", "touch grass", "ate", "vibe check", "plot twist". Don't force it; many items need none.
- No slang, jokes or playful tone at all on serious or sad stories: deaths, violence, war, crime victims, disasters, illness, abuse. Write those plainly and respectfully.
- No emojis, hashtags or exclamation marks.

Reply with JSON only: {"items": [{"id": <same id>, "title": "...", "summary": "..."}]} with one entry for every input item."""


def rewrite(stories):
    """Rewrites titles and summaries in plain, lightly Gen Z English via
    GitHub Models. Any failure keeps the original wording."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token or not stories:
        print("  rewrite: skipped (no GITHUB_TOKEN)")
        return 0
    batch = [{"id": i, "title": s["title"], "summary": s["summary"]} for i, s in enumerate(stories)]
    for endpoint, model in REWRITE_MODELS:
        body = json.dumps({
            "model": model,
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": REWRITE_PROMPT},
                         {"role": "user", "content": json.dumps({"items": batch}, ensure_ascii=False)}],
        }).encode()
        req = urllib.request.Request(
            endpoint, data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/json"})
        raw = b""
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
            reply = json.loads(raw)
            items = json.loads(reply["choices"][0]["message"]["content"])["items"]
        except urllib.error.HTTPError as e:
            print(f"  rewrite: {model} @ {endpoint} HTTP {e.code}: {e.read()[:200]!r}")
            continue
        except Exception as e:
            print(f"  rewrite: {model} @ {endpoint} failed: {str(e)[:120]} | reply: {raw[:200]!r}")
            continue
        done = 0
        for it in items:
            try:
                s = stories[int(it["id"])]
                title, summary = clean(it["title"]), clean(it.get("summary", ""))
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            if 3 <= len(title) <= 140 and len(summary) <= 360:
                s["title"] = title
                if summary:
                    s["summary"] = summary
                done += 1
        print(f"  rewrite: {done}/{len(stories)} via {model}")
        return done
    return 0


def lead_html(lead, img, kicker, now):
    e = html.escape
    return ('<section class="front">'
            + (f'<figure class="photo lead-photo"><div class="frame"><img src="{img}" alt=""></div>'
               f'<figcaption>Captured by enchanted camera, courtesy of {e(lead["source"])}</figcaption></figure>'
               if img else "")
            + f'<div class="lead-text"><p class="kicker">{e(kicker)}</p>'
            f'<h2><a href="{e(lead["link"])}" target="_blank" rel="noopener">{e(lead["title"])}</a></h2>'
            f'<p class="lead-dek">{e(lead["summary"])}</p>'
            f'<p class="byline">{e(lead["source"])} · {ago(lead["date"], now)} — '
            f'<a href="{e(lead["link"])}" target="_blank" rel="noopener">follow the owl for the full account →</a></p></div>'
            '</section>')


def build():
    now = dt.datetime.now(dt.timezone.utc)
    print("Summoning the owls…")
    with ThreadPoolExecutor(8) as ex:
        results = ex.map(lambda f: parse_feed(*f), FEEDS)
    stories = [s for r in results for s in r]
    by_sec = pick(stories)

    # Lead story: the freshest world story that carries a photograph
    world = by_sec["world"]
    lead = next((s for s in world if s["image"]), world[0] if world else None)
    if lead:
        world.remove(lead)

    # Plain-English rewrite, one request per section to stay within limits
    print("Translating from Newspeak…")
    for sec in SECTIONS:
        batch = ([lead] if lead and sec == "world" else []) + by_sec[sec]
        rewrite(batch)

    # Each section opens with a photograph: prefer a story whose feed has
    # one, otherwise look up the first story's og:image.
    wanted = [lead] if lead else []
    for sec in SECTIONS:
        items = by_sec[sec]
        if not items:
            continue
        pictured = next((s for s in items[:4] if s["image"]), None)
        if pictured:
            items.remove(pictured)
            items.insert(0, pictured)
        wanted.append(items[0])
    with ThreadPoolExecutor(6) as ex:
        for s, url in zip(wanted, ex.map(lambda s: s["image"] or og_image(s["link"]), wanted)):
            s["image"] = url
    images = {}
    with ThreadPoolExecutor(6) as ex:
        for s, uri in zip(wanted, ex.map(lambda s: embed_image(s["image"]) if s["image"] else None, wanted)):
            if uri:
                images[id(s)] = uri

    local = dt.datetime.now()
    issue_no = (local.date() - dt.date(2026, 1, 1)).days + 1
    sources = sorted({s["source"] for s in stories})
    e = html.escape

    parts = []
    if lead:
        parts.append(lead_html(lead, images.get(id(lead)), "Hot off the Enchanted Press", now))
    for sec, (title, plain, _) in SECTIONS.items():
        if not by_sec[sec]:
            continue
        body = [article(s, now, "first" if i == 0 else "", images.get(id(s)) if i == 0 else None)
                for i, s in enumerate(by_sec[sec])]
        parts.append(
            f'<section class="sec sec-{sec}" id="{sec}"><header class="sec-head">'
            f'<h2>{e(title)}</h2><span>{plain}</span></header>'
            f'<div class="cols">{"".join(body)}</div></section>')

    page = (TEMPLATE
            .replace("{{DATE}}", local.strftime("%A, %-d %B %Y"))
            .replace("{{ISSUE}}", str(issue_no))
            .replace("{{UPDATED}}", local.strftime("%-I:%M %p").lower())
            .replace("{{SOURCES}}", e(", ".join(sources)))
            .replace("{{COUNT}}", str(sum(len(v) for v in by_sec.values()) + (1 if lead else 0)))
            .replace("{{MOON}}", "{} · {}% lit".format(*moon(now)[:2]))
            .replace("{{MOONX}}", "{:.1f}".format(moon_shadow(moon(now)[2])))
            .replace("{{BODY}}", "\n".join(parts)))
    with open(OUT, "w") as f:
        f.write(page)
    # Standalone copy for GitHub Pages
    os.makedirs(os.path.join(HERE, "site"), exist_ok=True)
    with open(os.path.join(HERE, "site", "index.html"), "w") as f:
        f.write('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                '<meta property="og:title" content="TL;DR Today">\n'
                '<meta property="og:description" content="Today\'s global and India headlines, read by owls so you don\'t have to.">\n'
                '</head>\n<body>\n' + page + '\n</body>\n</html>\n')
    print(f"Printed {OUT} ({len(page)//1024} KB)")


TEMPLATE = open(os.path.join(HERE, "template.html")).read()

if __name__ == "__main__":
    build()

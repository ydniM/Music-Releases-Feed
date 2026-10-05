#!/usr/bin/env python3
"""
Builds an RSS feed of recent releases by the artists listed in Artists.txt.

Main source: Deezer's free public API (no account or key needed).
Backup source: MusicBrainz, used automatically if Deezer isn't working.
When the backup is used, a warning item is added to the top of the feed.

Each line of Artists.txt is a name, optionally followed by links, separated by |
    Artist Name
    Artist Name | https://www.deezer.com/artist/12345
    Artist Name | https://www.deezer.com/artist/12345 | https://www.deezer.com/artist/67890
    Artist Name | https://www.deezer.com/artist/12345 | https://musicbrainz.org/artist/<id>
Deezer links are used with Deezer. A line with a MusicBrainz link is ALSO checked on
MusicBrainz every day (for releases Deezer is missing), and MusicBrainz links are used
by the backup if Deezer stops working.
Lines starting with # are ignored.

Output:
    site/feed.xml    - the RSS feed
    site/index.html  - a small page linking to the feed
    A match report in the GitHub Actions run summary (and the log).
"""

import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from email.utils import format_datetime
from xml.sax.saxutils import escape

# ---- Settings you can change ----------------------------------------------
DAYS_BACK = 90                     # how far back to include releases
FEED_TITLE = "Music Releases"
ARTISTS_FILE = "Artists.txt"
OUT_DIR = "site"
# ---------------------------------------------------------------------------

REPO = os.environ.get("GITHUB_REPOSITORY", "")
REPO_URL = f"https://github.com/{REPO}" if REPO else "https://github.com"
USER_AGENT = f"personal-release-feed/1.1 ( {REPO_URL} )"   # MusicBrainz asks for contact info here

DEEZER = "https://api.deezer.com"
MUSICBRAINZ = "https://musicbrainz.org/ws/2/"
DEEZER_LINK_RE = re.compile(r"deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?artist/(\d+)", re.I)
MB_LINK_RE = re.compile(r"musicbrainz\.org/artist/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
DEEZER_TYPES = {"album": "Album", "ep": "EP", "single": "Single", "compile": "Compilation"}


class NotFound(Exception):
    pass


# ---- Shared helpers -------------------------------------------------------

def http_json(url, delay=0.0):
    """Download JSON from a URL. Retries temporary problems; raises NotFound on 404."""
    last_error = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
            time.sleep(delay)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url)
            last_error = e
        except Exception as e:
            last_error = e
        time.sleep(2 * (attempt + 1))
    host = urllib.parse.urlsplit(url).netloc
    raise RuntimeError(f"{host} did not respond after several tries ({last_error})")


def norm(text):
    """Normalize text for comparing names/titles (case, spacing, Unicode forms)."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(text.split())


def parse_date(value):
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None  # unknown or partial date (like "0000-00-00" or "2026")


def md(text):
    """Make text safe inside a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def parse_line(line):
    """Split a line into name, Deezer ids, MusicBrainz ids, and unreadable parts."""
    parts = [p.strip() for p in line.split("|")]
    name, deezer_ids, mb_ids, unreadable = "", [], [], []
    for i, part in enumerate(parts):
        if not part:
            continue
        dz, mb = DEEZER_LINK_RE.search(part), MB_LINK_RE.search(part)
        if dz:
            deezer_ids.append(dz.group(1))
        elif mb:
            mb_ids.append(mb.group(1).lower())
        elif i > 0 and part.isdigit():
            deezer_ids.append(part)
        elif i == 0:
            name = part
        else:
            unreadable.append(part)
    return {"name": name, "deezer": list(dict.fromkeys(deezer_ids)),
            "mb": list(dict.fromkeys(mb_ids)), "unreadable": unreadable}


# ---- Deezer (main source) -------------------------------------------------

def deezer_get(path, params=None):
    url = DEEZER + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(3):
        data = http_json(url, delay=0.15)
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            if err.get("code") == 4 and attempt < 2:   # "too many requests": wait and retry
                time.sleep(5)
                continue
            if err.get("code") == 800:                 # "no data"
                raise NotFound(err.get("message", "no data"))
            raise RuntimeError(f"Deezer error: {err.get('message', err)}")
        return data
    raise RuntimeError("Deezer kept saying it was too busy")


class DeezerSource:
    label = "Deezer"

    def check(self):
        data = deezer_get("/search/artist", {"q": "radiohead", "limit": 1})
        if not isinstance(data, dict) or not data.get("data"):
            raise RuntimeError("Deezer gave an unexpected answer to a test search")

    def lookup(self, line, notes):
        name, artists = line["name"], []
        if line["deezer"]:
            for artist_id in line["deezer"]:
                try:
                    info = deezer_get(f"/artist/{artist_id}")
                    artists.append((artist_id, info.get("name") or name or artist_id,
                                    f"https://www.deezer.com/artist/{artist_id}"))
                except NotFound:
                    notes.append(f"❌ No Deezer artist found with number {artist_id}.")
            return artists
        if not name:
            notes.append("❌ This line has no name or Deezer link.")
            return []
        results = deezer_get("/search/artist", {"q": name, "limit": 25}).get("data", [])
        if not results:
            notes.append("❌ No match on Deezer. Try adding the artist's Deezer link to this line.")
            return []
        top = results[0]
        if norm(top.get("name")) != norm(name):
            notes.append(f"⚠️ Closest match has a different name: **{md(top.get('name'))}**. "
                         "If that's wrong, add the right Deezer link to this line.")
        others = [r for r in results[1:] if norm(r.get("name")) == norm(name)]
        if others:
            links = ", ".join(r.get("link") or f"https://www.deezer.com/artist/{r['id']}" for r in others)
            notes.append(f"⚠️ Other Deezer artists with this exact name: {links}. "
                         "If any are the same artist, add all their links to this line.")
        return [(str(top["id"]), top.get("name") or name, f"https://www.deezer.com/artist/{top['id']}")]

    def releases(self, artist_id):
        index = 0
        while True:
            page = deezer_get(f"/artist/{artist_id}/albums", {"limit": 100, "index": index})
            batch = page.get("data", [])
            for album in batch:
                yield {
                    "source_id": f"deezer-{album.get('id')}",
                    "title": album.get("title") or "Untitled",
                    "type": DEEZER_TYPES.get(album.get("record_type"), "Release"),
                    "date": parse_date(album.get("release_date")),
                    "link": album.get("link") or f"https://www.deezer.com/album/{album.get('id')}",
                    "cover": album.get("cover_xl") or album.get("cover_big") or album.get("cover"),
                }
            index += len(batch)
            if not batch or index >= page.get("total", 0):
                return

    def cover(self, release):
        return release.get("cover")


# ---- MusicBrainz (backup source) ------------------------------------------

def mb_get(path, params):
    url = MUSICBRAINZ + path + "?" + urllib.parse.urlencode({**params, "fmt": "json"})
    return http_json(url, delay=1.1)   # MusicBrainz allows about one request per second


class MusicBrainzSource:
    label = "MusicBrainz"

    def lookup(self, line, notes):
        name, artists = line["name"], []
        if line["mb"]:
            for mbid in line["mb"]:
                try:
                    info = mb_get(f"artist/{mbid}", {})
                    artists.append((mbid, info.get("name") or name or mbid,
                                    f"https://musicbrainz.org/artist/{mbid}"))
                except NotFound:
                    notes.append(f"❌ No MusicBrainz artist found with ID {mbid}.")
            return artists
        if not name:
            notes.append("❌ The backup can't search for this line because it has no name. "
                         "Add the artist's name or MusicBrainz link.")
            return []
        q = name.replace("\\", "\\\\").replace('"', '\\"')
        results = mb_get("artist", {"query": f'artist:"{q}" OR alias:"{q}"', "limit": 25}).get("artists", [])
        if not results:
            notes.append("❌ No match on MusicBrainz. Try adding the artist's MusicBrainz link to this line.")
            return []
        top = results[0]
        known_names = {norm(top.get("name"))} | {norm(a.get("name")) for a in top.get("aliases", [])}
        if norm(name) not in known_names:
            notes.append(f"⚠️ Closest MusicBrainz match has a different name: **{md(top.get('name'))}**. "
                         "If that's wrong, add the right MusicBrainz link to this line.")
        others = [r for r in results[1:] if norm(r.get("name")) == norm(name)]
        if others:
            links = ", ".join(f"https://musicbrainz.org/artist/{r['id']}"
                              + (f" ({r['disambiguation']})" if r.get("disambiguation") else "")
                              for r in others)
            notes.append(f"⚠️ Other MusicBrainz artists with this exact name: {md(links)}. "
                         "If the match is wrong, add the right MusicBrainz link to this line.")
        return [(top["id"], top.get("name") or name, f"https://musicbrainz.org/artist/{top['id']}")]

    def releases(self, mbid):
        offset = 0
        while True:
            page = mb_get("release-group", {"artist": mbid, "limit": 100, "offset": offset})
            batch = page.get("release-groups", [])
            for rg in batch:
                kind = rg.get("primary-type") or "Release"
                if "Compilation" in (rg.get("secondary-types") or []):
                    kind = "Compilation"
                yield {
                    "source_id": f"mb-{rg.get('id')}",
                    "mbid": rg.get("id"),
                    "title": rg.get("title") or "Untitled",
                    "type": kind,
                    "date": parse_date(rg.get("first-release-date")),
                    "link": f"https://musicbrainz.org/release-group/{rg.get('id')}",
                }
            offset += len(batch)
            if not batch or offset >= page.get("release-group-count", 0):
                return

    def cover(self, release):
        try:
            data = http_json(f"https://coverartarchive.org/release-group/{release['mbid']}")
        except Exception:
            return None   # no cover art on file (or the cover service is down)
        for image in data.get("images", []):
            if image.get("front"):
                thumbs = image.get("thumbnails", {})
                url = thumbs.get("500") or thumbs.get("large") or image.get("image")
                return url.replace("http://", "https://", 1) if url else None
        return None


# ---- Building the feed ----------------------------------------------------

def collect(source, line, notes, matched, cutoff, today, seen, line_keys, skip_titles=()):
    """Gather in-range releases for one line from one source."""
    found = []
    for artist_id, artist_name, artist_link in source.lookup(line, notes):
        matched.append(f"[{md(artist_name)}]({artist_link})")
        for rel in source.releases(artist_id):
            if rel["date"] is None or not (cutoff <= rel["date"] <= today):
                continue
            key = (norm(rel["title"]), norm(rel["type"]), rel["date"])
            if rel["source_id"] in seen or key in line_keys or norm(rel["title"]) in skip_titles:
                continue
            seen.add(rel["source_id"])
            line_keys.add(key)
            rel["artist"] = line["name"] or artist_name
            found.append(rel)
    return found


def build(source, lines, cutoff, today, extra=None):
    """Check every artist with the main source. If `extra` is given, lines that have
    a MusicBrainz link are also checked there, and releases Deezer didn't have are added.
    Returns (items, report, lines checked, lines failed)."""
    items, report, seen = [], [], set()
    checked = failed = 0
    for raw in lines:
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        checked += 1
        line = parse_line(text)
        notes, matched, line_keys = [], [], set()
        for bad in line["unreadable"]:
            notes.append(f"⚠️ Couldn't read `{md(bad)}` as a Deezer or MusicBrainz artist link. "
                         "Use the link from your browser's address bar on the artist's page.")
        main_items = []
        try:
            main_items = collect(source, line, notes, matched, cutoff, today, seen, line_keys)
        except Exception as e:
            failed += 1
            notes.append(f"❌ Error while checking this artist: {md(e)}")
        items += main_items
        if extra and line["mb"]:
            try:
                titles = {norm(r["title"]) for r in main_items}
                items += collect(extra, line, notes, matched, cutoff, today, seen, line_keys, titles)
            except Exception as e:
                notes.append(f"⚠️ The extra MusicBrainz check failed this time: {md(e)}")
        report.append((text, ", ".join(matched) or "—", " ".join(notes) or "✅"))
    return items, report, checked, failed


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    cutoff = today - dt.timedelta(days=DAYS_BACK)

    try:
        with open(ARTISTS_FILE, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        sys.exit(f"Could not find {ARTISTS_FILE}. It must be in the same folder as this script.")

    source, backup_reason = DeezerSource(), None
    try:
        source.check()
        items, report, checked, failed = build(source, lines, cutoff, today, extra=MusicBrainzSource())
        if checked and failed == checked:
            raise RuntimeError("every artist failed on Deezer")
    except Exception as e:
        backup_reason = str(e)
        print(f"::warning::Deezer isn't working ({backup_reason}). Using the MusicBrainz backup.")
        source = MusicBrainzSource()
        items, report, checked, failed = build(source, lines, cutoff, today)
        if checked and failed == checked:
            print_report(report, 0, source, backup_reason)
            sys.exit("Deezer and the MusicBrainz backup both failed. The previous feed was left unchanged.")

    mb = MusicBrainzSource()
    for rel in items:
        rel["cover"] = mb.cover(rel) if rel["source_id"].startswith("mb-") else rel.get("cover")
    items.sort(key=lambda x: (x["date"], x["artist"].casefold()), reverse=True)

    os.makedirs(OUT_DIR, exist_ok=True)
    write_feed(items, today, backup_reason)
    write_index()
    print_report(report, len(items), source, backup_reason)


def release_guid(rel):
    """Same release = same ID, whichever source found it, so switching sources doesn't repeat items."""
    key = f'{norm(rel["artist"])}|{norm(rel["title"])}|{rel["date"].isoformat()}'
    return "release-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def backup_notice(today, reason):
    year, week, _ = today.isocalendar()
    actions = escape(f"{REPO_URL}/actions", {'"': "&quot;"})
    body = ("<p>Deezer wasn't working when this feed last updated, so the feed is temporarily "
            "using the MusicBrainz backup. New releases may show up a little later than usual, "
            "and a few may appear twice.</p>"
            f"<p>Reason: {escape(reason)}</p>"
            "<p>This notice repeats once a week until Deezer works again. "
            f'<a href="{actions}">See the latest runs</a>.</p>')
    return f"""    <item>
      <title>⚠️ Release feed is using the MusicBrainz backup</title>
      <link>{escape(REPO_URL)}/actions</link>
      <guid isPermaLink="false">backup-notice-{year}-W{week:02d}</guid>
      <pubDate>{format_datetime(dt.datetime.now(dt.timezone.utc))}</pubDate>
      <description>{escape(body)}</description>
    </item>"""


def write_feed(items, today, backup_reason):
    entries = [backup_notice(today, backup_reason)] if backup_reason else []
    for it in items:
        published = format_datetime(dt.datetime.combine(it["date"], dt.time(12, 0), dt.timezone.utc))
        title = f'{it["artist"]} – {it["title"]} ({it["type"]})'
        cover = escape(it["cover"] or "", {'"': "&quot;"})
        link = escape(it["link"], {'"': "&quot;"})
        site = "MusicBrainz" if it["source_id"].startswith("mb-") else "Deezer"
        body = f'<p><img src="{cover}" alt="Cover art" width="500"/></p>' if cover else ""
        body += (f'<p><b>{escape(it["artist"])}</b><br/>{escape(it["title"])}<br/>'
                 f'{escape(it["type"])} · released {it["date"].isoformat()}</p>'
                 f'<p><a href="{link}">Open on {site}</a></p>')
        cover_tags = ""
        if cover:
            cover_tags = (f'\n      <enclosure url="{cover}" type="image/jpeg" length="0"/>'
                          f'\n      <media:thumbnail url="{cover}"/>')
        entries.append(f"""    <item>
      <title>{escape(title)}</title>
      <link>{link}</link>
      <guid isPermaLink="false">{release_guid(it)}</guid>
      <pubDate>{published}</pubDate>
      <description>{escape(body)}</description>{cover_tags}
    </item>""")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>{escape(FEED_TITLE)}</title>
    <link>{escape(REPO_URL)}</link>
    <description>New releases from the artists I follow (last {DAYS_BACK} days)</description>
    <lastBuildDate>{format_datetime(dt.datetime.now(dt.timezone.utc))}</lastBuildDate>
{chr(10).join(entries)}
  </channel>
</rss>
"""
    with open(os.path.join(OUT_DIR, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(feed)


def write_index():
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{escape(FEED_TITLE)}</title></head>
<body><p>The release feed is here: <a href="feed.xml">feed.xml</a>. Add that link to your RSS reader.</p></body>
</html>
"""
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def print_report(report, item_count, source, backup_reason):
    text = ["## Release feed report\n"]
    if backup_reason:
        text.append(f"⚠️ **Deezer wasn't working ({md(backup_reason)}), so this run used the MusicBrainz backup.**\n")
    text += [f"Source: **{source.label}**{'' if backup_reason else ' (plus MusicBrainz for lines with a MusicBrainz link)'}. **{item_count}** releases in the feed (last {DAYS_BACK} days).\n",
             f"| Line in {ARTISTS_FILE} | Matched artist(s) | Notes |",
             "|---|---|---|"]
    text += [f"| {md(line)} | {m} | {n} |" for line, m, n in report]
    output = "\n".join(text) + "\n"
    print(output)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(output)


if __name__ == "__main__":
    main()

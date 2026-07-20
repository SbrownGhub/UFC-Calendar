"""
ATF UFC Calendar builder.

Key design decisions (learned the hard way):
  1. The FULL schedule lives in ESPN's leagues[0].calendar[] array, each entry
     carrying a UTC startDate. The top-level events[] array only holds the
     current/most-recent card, so reading it gets you one stale event and an
     empty calendar. We read calendar[].  <-- this was THE bug.
  2. calendar[] startDate is already UTC ISO ("...Z"). No timezone guessing.
  3. All ICS events are written in UTC ("Z" timestamps). Every calendar client
     (Google, Apple, Outlook) converts UTC to the viewer's local time itself,
     DST included. No TZID, no VTIMEZONE.
  4. If we can't get a sane schedule from any source, we KEEP the old files but
     exit non-zero so the GitHub Actions run fails loudly and emails you.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from uuid import uuid5, NAMESPACE_URL
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ICS = ROOT / "ufc_espn_schedule.ics"
OUTPUT_JSON = ROOT / "events_cache.json"
HEARTBEAT = ROOT / "last_run.txt"

# Bare scoreboard endpoint returns the current season's full calendar[].
ESPN_API_URL = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
ESPN_UFC_SCHEDULE_URL = "https://www.espn.com/mma/schedule/_/league/ufc"
ATF_URL = "https://www.youtube.com/c/AgainstTheFence"

ET = ZoneInfo("America/New_York")
UK = ZoneInfo("Europe/London")
UTC = timezone.utc

ATF_BLURBS = [
    "Skip the corporate waffle. Watch the fights, come argue with us live - ATF",
    "The card starts in the cage, but the real chaos starts in the comments - ATF",
    "Watch the event, and head to ATF for the fan verdict they won't give you on broadcast",
    "We do not do polite, sterile analysis. We do fan energy, sharp takes, and proper watch-alongs - ATF",
    "If the judges ruin your night, we will be there to say it plainly - ATF",
]

CALENDAR_NAME = "UFC Event Schedule"
CALENDAR_DESC = "Free UFC event calendar by Against The Fence"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "application/json, text/plain, */*",
}


# --------------------------------------------------------------------------
# Data model. All datetimes are stored timezone-aware in UTC.
# --------------------------------------------------------------------------

@dataclass
class FightEvent:
    title: str
    main_utc: datetime            # main card start, UTC
    location: str
    broadcaster: str
    source_url: str
    prelims_utc: Optional[datetime] = None

    @property
    def uid(self) -> str:
        stable_key = f"{normalise_title(self.title)}|{self.main_utc.date().isoformat()}"
        return f"{uuid5(NAMESPACE_URL, stable_key)}@againstthefence.com"

    @property
    def description(self) -> str:
        where = self.broadcaster if self.broadcaster not in ("", "TBC", "TBA") else "Paramount+"
        main_uk = self.main_utc.astimezone(UK)
        main_et = self.main_utc.astimezone(ET)
        lines = ["Where to watch"]
        lines.append(
            f"{self.title} main card: {fmt(main_uk)} UK time "
            f"({fmt(main_et)} ET) on {where}."
        )
        if self.prelims_utc:
            pre_uk = self.prelims_utc.astimezone(UK)
            pre_et = self.prelims_utc.astimezone(ET)
            lines.append(f"Prelims: {fmt(pre_uk)} UK time ({fmt(pre_et)} ET) on {where}.")
        blurb = ATF_BLURBS[self.main_utc.isocalendar().week % len(ATF_BLURBS)]
        lines += ["", blurb, "", "Watch along for free on Against The Fence (click the link)", ""]
        return "\n".join(lines)


def fmt(dt: datetime) -> str:
    """e.g. '3:00 AM Sun' -- the day marker matters for UK viewers of US cards."""
    return dt.strftime("%-I:%M %p %a")


def normalise_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.lower()).strip()


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text or "")).strip()


def fetch(url: str) -> requests.Response:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp


def is_ufc_card(label: str) -> bool:
    l = label.strip()
    lower = l.lower()
    if any(b in lower for b in ("travel deals", "collectibles", "how to watch", "ticket")):
        return False
    return l.startswith("UFC") or "Fight Night" in l or l.startswith("Noche")


# --------------------------------------------------------------------------
# Source 1 (primary): ESPN scoreboard JSON -> leagues[0].calendar[]
# This holds the FULL announced schedule with UTC start times. The top-level
# events[] array only holds the current card, which is why the old code that
# read events[] produced an empty calendar.
# --------------------------------------------------------------------------

def _overlay_details(data: dict) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """Pull venue + broadcaster from events[] for any card that appears there.
    Free (no extra requests); enriches whatever ESPN happens to expose today."""
    out: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for ev in (data.get("events") or []):
        eid = str(ev.get("id", ""))
        venue_str = None
        comps = ev.get("competitions") or []
        if comps:
            v = comps[0].get("venue") or {}
            addr = v.get("address") or {}
            parts = [v.get("fullName", ""), addr.get("city", ""),
                     addr.get("state", ""), addr.get("country", "")]
            venue_str = ", ".join(p for p in parts if p) or None
        bcast = ev.get("broadcast") or None
        if not bcast:
            for b in (comps[0].get("broadcasts") if comps else []) or []:
                names = b.get("names") or []
                if names:
                    bcast = names[0]
                    break
        out[eid] = (venue_str, bcast)
    return out


def fetch_api_events() -> List[FightEvent]:
    data = fetch(ESPN_API_URL).json()
    leagues = data.get("leagues") or []
    if not leagues:
        return []
    calendar = leagues[0].get("calendar") or []
    overlay = _overlay_details(data)

    events: List[FightEvent] = []
    for entry in calendar:
        label = clean_text(entry.get("label", ""))
        start = entry.get("startDate")
        if not label or not start or not is_ufc_card(label):
            continue
        try:
            main_utc = dateparser.isoparse(start)
            if main_utc.tzinfo is None:
                main_utc = main_utc.replace(tzinfo=UTC)
            main_utc = main_utc.astimezone(UTC)
        except (ValueError, TypeError):
            continue

        ref = ((entry.get("event") or {}).get("$ref")) or ""
        m = re.search(r"/events/(\d+)", ref)
        eid = m.group(1) if m else ""
        venue_str, bcast = overlay.get(eid, (None, None))

        events.append(
            FightEvent(
                title=label,
                main_utc=main_utc,
                location=venue_str or "Location TBA",
                broadcaster=bcast or "Paramount+",
                source_url=ESPN_UFC_SCHEDULE_URL,
                prelims_utc=None,  # calendar[] gives main-card start only
            )
        )
    return finalise(events)


# --------------------------------------------------------------------------
# Source 2 (fallback): scrape the ESPN schedule page text.
# CRITICAL: ESPN lists these times in US Eastern. Parse as ET, convert to UTC.
# --------------------------------------------------------------------------

def looks_like_date(line: str) -> bool:
    return bool(re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}$", line))


def looks_like_time(line: str) -> bool:
    return bool(re.match(r"^\d{1,2}:\d{2}\s*[AP]M$", line, flags=re.IGNORECASE))


def looks_like_event_title(line: str) -> bool:
    return is_ufc_card(line)


def looks_like_location(line: str) -> bool:
    venue_words = ["Arena", "Center", "Centre", "APEX", "Apex", "Stadium",
                   "Garden", "Hall", "Coliseum", "Bank", "Forum", "Place"]
    return any(word in line for word in venue_words)


def resolve_year(month_day: str, now: datetime) -> int:
    """Handle the Dec->Jan rollover: a date landing >45 days in the past is next year."""
    year = now.year
    try:
        candidate = dateparser.parse(f"{month_day} {year}")
    except (ValueError, OverflowError):
        return year
    if candidate and candidate.replace(tzinfo=UTC) < now - timedelta(days=45):
        return year + 1
    return year


def parse_espn_schedule_lines(lines: List[str]) -> List[FightEvent]:
    events: List[FightEvent] = []
    now = datetime.now(UTC)
    i = 0
    while i < len(lines):
        line = lines[i]
        if not looks_like_date(line):
            i += 1
            continue

        date_str = line
        window: List[str] = []
        j = i + 1
        while j < len(lines) and not looks_like_date(lines[j]) and len(window) < 12:
            window.append(lines[j])
            j += 1

        times = [x for x in window if looks_like_time(x)]
        titles = [x for x in window if looks_like_event_title(x)]
        locations = [x for x in window if looks_like_location(x)]
        broadcasters = [x for x in window
                        if x in {"Paramount+", "ESPN+", "ESPN", "TNT Sports", "discovery+", "TBA"}]

        if not titles or not times:
            i = j
            continue

        title = titles[0]
        location = locations[0] if locations else "Location TBA"
        broadcaster = broadcasters[0] if broadcasters else "Paramount+"
        year = resolve_year(date_str, now)

        parsed: List[datetime] = []
        for t in times[:2]:
            try:
                dt = dateparser.parse(f"{date_str} {year} {t}")
            except (ValueError, OverflowError):
                dt = None
            if dt is not None:
                parsed.append(dt.replace(tzinfo=ET).astimezone(UTC))  # ESPN times are ET

        if not parsed:
            i = j
            continue

        parsed.sort()
        if len(parsed) >= 2 and (parsed[-1] - parsed[0]) <= timedelta(hours=6):
            prelims_utc, main_utc = parsed[0], parsed[-1]
        else:
            main_utc, prelims_utc = parsed[-1], None

        events.append(
            FightEvent(title=title, main_utc=main_utc, location=location,
                       broadcaster=broadcaster, source_url=ESPN_UFC_SCHEDULE_URL,
                       prelims_utc=prelims_utc)
        )
        i = j
    return finalise(events)


def fetch_html_events() -> List[FightEvent]:
    html = fetch(ESPN_UFC_SCHEDULE_URL).text
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = [clean_text(l) for l in soup.get_text("\n").splitlines()]
    return parse_espn_schedule_lines([l for l in lines if l])


# --------------------------------------------------------------------------
# Validation, dedupe, output
# --------------------------------------------------------------------------

def finalise(events: List[FightEvent]) -> List[FightEvent]:
    deduped: List[FightEvent] = []
    seen = set()
    cutoff = datetime.now(UTC) - timedelta(days=1)
    for ev in sorted(events, key=lambda x: x.main_utc):
        if ev.main_utc < cutoff:
            continue
        if ev.prelims_utc is not None:
            gap = ev.main_utc - ev.prelims_utc
            if gap <= timedelta(0) or gap > timedelta(hours=6):
                ev.prelims_utc = None
        key = (normalise_title(ev.title), ev.main_utc.date().isoformat())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)
    return deduped


def ics_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace("\n", "\\n")
                .replace(",", "\\,").replace(";", "\\;"))


def build_calendar(events: List[FightEvent]) -> str:
    lines: List[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Against The Fence//ATF UFC Main Events//EN",
        f"X-WR-CALNAME:{CALENDAR_NAME}",
        f"X-WR-CALDESC:{CALENDAR_DESC}",
        "X-PUBLISHED-TTL:PT12H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
    ]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for item in events:
        block_start = item.prelims_utc or item.main_utc
        block_end = item.main_utc + timedelta(hours=3)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{item.uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{block_start.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{block_end.strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{ics_escape(item.title)}",
            f"DESCRIPTION:{ics_escape(item.description)}",
            f"LOCATION:{ics_escape(item.location)}",
            f"URL:{ATF_URL}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\n".join(lines) + "\n"


def write_outputs(events: List[FightEvent]) -> None:
    OUTPUT_ICS.write_text(build_calendar(events), encoding="utf-8")
    payload = [
        {
            "title": e.title,
            "main_utc": e.main_utc.isoformat(),
            "prelims_utc": e.prelims_utc.isoformat() if e.prelims_utc else None,
            "location": e.location,
            "broadcaster": e.broadcaster,
            "source_url": e.source_url,
            "uid": e.uid,
        }
        for e in events
    ]
    OUTPUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> None:
    HEARTBEAT.write_text(f"Last run: {datetime.now(UTC).isoformat()}\n", encoding="utf-8")

    events: List[FightEvent] = []
    print("Trying ESPN scoreboard API (calendar[])...")
    try:
        events = fetch_api_events()
        print(f"API returned {len(events)} upcoming events")
    except Exception as exc:
        print(f"WARNING: ESPN API failed: {exc}")

    if len(events) < 2:
        print("Falling back to ESPN schedule page scrape...")
        try:
            events = fetch_html_events()
            print(f"Scrape returned {len(events)} upcoming events")
        except Exception as exc:
            print(f"WARNING: ESPN scrape failed: {exc}")

    if len(events) < 2:
        print("ERROR: Could not build a trustworthy schedule from any source.")
        print("Existing calendar files left untouched. Failing loudly on purpose.")
        sys.exit(1)

    write_outputs(events)
    print(f"Wrote {len(events)} events to {OUTPUT_ICS.name}")
    for e in events[:8]:
        print(f"  {e.main_utc.astimezone(UK).strftime('%a %d %b %H:%M')} UK | {e.title}")


if __name__ == "__main__":
    main()

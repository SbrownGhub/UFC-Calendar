"""
ATF UFC Calendar builder.

Key design decisions (learned the hard way):
  1. ESPN lists schedule times in US Eastern (ET). We parse them as ET,
     never as the venue's timezone.
  2. All ICS events are written in UTC ("Z" timestamps). Every calendar
     client (Google, Apple, Outlook) converts UTC to the viewer's local
     time automatically, DST included. No TZID, no VTIMEZONE, no guessing.
  3. Primary data source is ESPN's JSON scoreboard API (returns UTC ISO
     dates directly). The HTML text-scrape is only a fallback.
  4. If we can't get a sane schedule, we KEEP the old calendar files but
     exit non-zero so the GitHub Actions run fails loudly and you get an
     email. No more silent staleness.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import List, Optional

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from uuid import uuid5, NAMESPACE_URL
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ICS = ROOT / "ufc_espn_schedule.ics"
OUTPUT_JSON = ROOT / "events_cache.json"
HEARTBEAT = ROOT / "last_run.txt"

ESPN_API_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
    "?dates={start}-{end}&limit=100"
)
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
            lines.append(
                f"Prelims: {fmt(pre_uk)} UK time ({fmt(pre_et)} ET) on {where}."
            )
        blurb = ATF_BLURBS[self.main_utc.isocalendar().week % len(ATF_BLURBS)]
        lines += ["", blurb, "", "Watch along for free on Against The Fence (click the link)", ""]
        return "\n".join(lines)


def fmt(dt: datetime) -> str:
    """e.g. '3:00 AM Sun' -- day marker matters for UK viewers of US cards."""
    return dt.strftime("%-I:%M %p %a")


def normalise_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.lower()).strip()


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text or "")).strip()


def fetch(url: str) -> requests.Response:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp


# --------------------------------------------------------------------------
# Source 1 (primary): ESPN JSON scoreboard API. Dates come back as ISO UTC,
# so there is zero timezone guesswork.
# --------------------------------------------------------------------------

def fetch_api_events() -> List[FightEvent]:
    now = datetime.now(UTC)
    start = now.strftime("%Y%m%d")
    end = (now + timedelta(days=240)).strftime("%Y%m%d")
    url = ESPN_API_URL.format(start=start, end=end)

    data = fetch(url).json()
    raw_events = data.get("events") or []
    events: List[FightEvent] = []

    for ev in raw_events:
        name = clean_text(ev.get("name") or ev.get("shortName") or "")
        if not name:
            continue
        # Keep it to real UFC cards, skip stray non-UFC listings.
        if not (name.startswith("UFC") or "Fight Night" in name or name.startswith("Noche")):
            continue

        date_str = ev.get("date")
        if not date_str:
            continue
        try:
            main_utc = dateparser.isoparse(date_str)
            if main_utc.tzinfo is None:
                main_utc = main_utc.replace(tzinfo=UTC)
            main_utc = main_utc.astimezone(UTC)
        except (ValueError, TypeError):
            continue

        location = "Location TBA"
        broadcaster = "TBC"
        comps = ev.get("competitions") or []
        if comps:
            venue = comps[0].get("venue") or {}
            parts = [venue.get("fullName", "")]
            addr = venue.get("address") or {}
            parts += [addr.get("city", ""), addr.get("state", ""), addr.get("country", "")]
            loc = ", ".join(p for p in parts if p)
            if loc:
                location = loc
            casts = comps[0].get("broadcasts") or []
            for b in casts:
                names = b.get("names") or []
                if names:
                    broadcaster = names[0]
                    break

        events.append(
            FightEvent(
                title=name,
                main_utc=main_utc,
                location=location,
                broadcaster=broadcaster,
                source_url=ESPN_UFC_SCHEDULE_URL,
                prelims_utc=None,  # API gives card start; prelims sanity-checked in finalise()
            )
        )

    return finalise(events)


# --------------------------------------------------------------------------
# Source 2 (fallback): scrape the ESPN schedule page text.
# CRITICAL: ESPN lists these times in US Eastern. Parse as ET, then convert
# to UTC. Never attach the venue's timezone to a listed time.
# --------------------------------------------------------------------------

def looks_like_date(line: str) -> bool:
    return bool(re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}$", line))


def looks_like_time(line: str) -> bool:
    return bool(re.match(r"^\d{1,2}:\d{2}\s*[AP]M$", line, flags=re.IGNORECASE))


def looks_like_event_title(line: str) -> bool:
    lower = line.lower()
    banned = ["travel deals", "collectibles", "shop", "store", "ticket", "how to watch"]
    if any(term in lower for term in banned):
        return False
    return line.startswith("UFC ") or "Fight Night" in line or line.startswith("Noche UFC")


def looks_like_location(line: str) -> bool:
    venue_words = ["Arena", "Center", "Centre", "APEX", "Apex", "Stadium",
                   "Garden", "Hall", "Coliseum", "Bank", "Forum", "Place"]
    return any(word in line for word in venue_words)


def resolve_year(month_day: str, now: datetime) -> int:
    """Fix the December->January rollover: if the parsed date would land
    more than 45 days in the past, it belongs to next year."""
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
        # Window ends at the next date line so times can't bleed between events.
        window: List[str] = []
        j = i + 1
        while j < len(lines) and not looks_like_date(lines[j]) and len(window) < 12:
            window.append(lines[j])
            j += 1

        times = [item for item in window if looks_like_time(item)]
        titles = [item for item in window if looks_like_event_title(item)]
        locations = [item for item in window if looks_like_location(item)]
        broadcasters = [item for item in window
                        if item in {"Paramount+", "ESPN+", "ESPN", "TNT Sports", "discovery+", "TBA"}]

        if not titles or not times:
            i = j
            continue

        title = titles[0]
        location = locations[0] if locations else "Location TBA"
        broadcaster = broadcasters[0] if broadcasters else "TBC"
        year = resolve_year(date_str, now)

        parsed: List[datetime] = []
        for t in times[:2]:
            try:
                dt = dateparser.parse(f"{date_str} {year} {t}")
            except (ValueError, OverflowError):
                dt = None
            if dt is not None:
                # ESPN times are Eastern. Full stop.
                parsed.append(dt.replace(tzinfo=ET).astimezone(UTC))

        if not parsed:
            i = j
            continue

        parsed.sort()  # earliest = prelims, latest = main card
        if len(parsed) >= 2 and (parsed[-1] - parsed[0]) <= timedelta(hours=6):
            prelims_utc, main_utc = parsed[0], parsed[-1]
        else:
            # One time, or two times too far apart to trust (old scramble bug).
            main_utc = parsed[-1]
            prelims_utc = None

        events.append(
            FightEvent(
                title=title,
                main_utc=main_utc,
                location=location,
                broadcaster=broadcaster,
                source_url=ESPN_UFC_SCHEDULE_URL,
                prelims_utc=prelims_utc,
            )
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
    """Dedupe, drop past events, enforce sanity."""
    deduped: List[FightEvent] = []
    seen = set()
    cutoff = datetime.now(UTC) - timedelta(days=1)

    for ev in sorted(events, key=lambda x: x.main_utc):
        if ev.main_utc < cutoff:
            continue
        if ev.prelims_utc is not None:
            gap = ev.main_utc - ev.prelims_utc
            if gap <= timedelta(0) or gap > timedelta(hours=6):
                ev.prelims_utc = None  # never publish prelims after mains
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
        # Event block spans prelims (if known) through main card + 3h.
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
    HEARTBEAT.write_text(
        f"Last run: {datetime.now(UTC).isoformat()}\n", encoding="utf-8"
    )

    events: List[FightEvent] = []

    print("Trying ESPN scoreboard API...")
    try:
        events = fetch_api_events()
        print(f"API returned {len(events)} usable events")
    except Exception as exc:
        print(f"WARNING: ESPN API failed: {exc}")

    if len(events) < 2:
        print("Falling back to ESPN schedule page scrape...")
        try:
            events = fetch_html_events()
            print(f"Scrape returned {len(events)} usable events")
        except Exception as exc:
            print(f"WARNING: ESPN scrape failed: {exc}")

    if len(events) < 2:
        # Keep the existing calendar on disk, but FAIL the run so GitHub
        # emails you. Silence is how this broke last time.
        print("ERROR: Could not build a trustworthy schedule from any source.")
        print("Existing calendar files left untouched. Failing loudly on purpose.")
        sys.exit(1)

    write_outputs(events)
    print(f"Wrote {len(events)} events to {OUTPUT_ICS.name}")
    for e in events[:6]:
        print(f"  {e.main_utc.astimezone(UK).strftime('%a %d %b %H:%M')} UK | {e.title}")


if __name__ == "__main__":
    main()

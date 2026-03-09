from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import List
from uuid import uuid5, NAMESPACE_URL

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ICS = ROOT / "ufc_espn_schedule.ics"
OUTPUT_JSON = ROOT / "events_cache.json"

ESPN_UFC_SCHEDULE_URL = "https://www.espn.com/mma/schedule/_/league/ufc"
ATF_URL = "https://www.youtube.com/c/AgainstTheFence"

CALENDAR_NAME = "UFC Event Schedule"
CALENDAR_DESC = "Free UFC event calendar by Against The Fence"

WATCH_LINES = [
    "Where to watch",
    "Broadcast availability varies by region. Check the official UFC watch page first.",
    "Official UFC: https://www.ufc.com/watch",
    "Paramount+: https://www.paramountplus.com/shows/ufc/",
    "TNT Sports UFC: https://www.tntsports.co.uk/mixed-martial-arts/ufc/",
    "TNT Sports Box Office: https://www.tntsports.co.uk/boxoffice/",
    f"ATF Watch Along (free): {ATF_URL}",
]

ATF_BLURBS = [
    "ATF angle: Skip the corporate waffle. Watch the fights, then come argue with us live.",
    "ATF angle: The card starts in the cage, but the real chaos starts in the comments.",
    "ATF angle: Watch the event, and head to ATF for the fan verdict they won't give you on broadcast.",
    "ATF angle: We do not do polite, sterile analysis. We do fan energy, sharp takes, and proper watch-alongs.",
    "ATF angle: If the judges ruin your night, ATF will be there to say it plainly.",
]


@dataclass
class FightEvent:
    title: str
    start_utc: datetime
    location: str
    broadcaster: str
    source_url: str

    @property
    def uid(self) -> str:
        stable_key = f"{self.title}|{self.start_utc.isoformat()}|{self.location}"
        return f"{uuid5(NAMESPACE_URL, stable_key)}@againstthefence.com"

    @property
    def description(self) -> str:
        week_index = self.start_utc.isocalendar().week % len(ATF_BLURBS)
        atf_blurb = ATF_BLURBS[week_index]

        lines = [
            f"Main event: {self.title}",
            "",
            *WATCH_LINES,
            f"Listed broadcaster: {self.broadcaster or 'TBC'}",
            "",
            atf_blurb,
            "",
            f"Venue: {self.location}",
            f"Source: {self.source_url}",
        ]
        return "\n".join(lines)


def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ATF-UFC-Calendar/1.0)"
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def clean_text(text: str) -> str:
    text = unescape(text)
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def extract_visible_lines(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    raw_text = soup.get_text("\n")
    lines = [clean_text(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    return lines


def parse_date_time(date_str: str, time_str: str) -> datetime:
    """
    ESPN schedule shows month/day plus time like:
    Mar 21 / 1:00 PM (in rendered view)
    or split lines like:
    Mar 21
    1:00 PM
    We assume the current year from the schedule page context.
    """
    current_year = datetime.now(timezone.utc).year
    candidate = f"{date_str} {current_year} {time_str}"
    dt = dateparser.parse(candidate)

    if dt is None:
        raise ValueError(f"Could not parse date/time: {candidate}")

    # ESPN schedule times are listed in US Eastern time on the schedule page.
    # If no timezone info is present, interpret them as America/New_York,
    # then convert to UTC for storage and calendar output.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))

    return dt.astimezone(timezone.utc)


def looks_like_date(line: str) -> bool:
    return bool(re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}$", line))


def looks_like_time(line: str) -> bool:
    return bool(re.match(r"^\d{1,2}:\d{2}\s*[AP]M$", line, flags=re.IGNORECASE))


def looks_like_event_title(line: str) -> bool:
    return (
        line.startswith("UFC ")
        or "Fight Night" in line
        or line.startswith("Noche UFC")
    )


def looks_like_location(line: str) -> bool:
    venue_words = [
        "Arena", "Center", "Centre", "APEX", "Apex", "Stadium",
        "Garden", "Hall", "Coliseum", "Bank", "Life Centre",
        "White House", "Place"
    ]
    return any(word in line for word in venue_words)


def parse_espn_schedule_lines(lines: List[str]) -> List[FightEvent]:
    """
    Parse only:
    - This Week's Events
    - Scheduled Events

    Stop at:
    - Past Results
    """
    events: List[FightEvent] = []

    in_upcoming_section = False
    i = 0

    while i < len(lines):
        line = lines[i]

        if line == "This Week's Events" or line == "Scheduled Events":
            in_upcoming_section = True
            i += 1
            continue

        if line == "Past Results":
            break

        if not in_upcoming_section:
            i += 1
            continue

        if looks_like_date(line):
            date_str = line

            # find next time, broadcaster, title, location in a short window
            window = lines[i + 1:i + 8]

            time_str = ""
            broadcaster = ""
            title = ""
            location = ""

            for item in window:
                if not time_str and looks_like_time(item):
                    time_str = item
                elif not title and looks_like_event_title(item):
                    title = item
                elif not location and looks_like_location(item):
                    location = item
                elif item in {"Paramount+", "ESPN+", "ESPN", "TNT Sports", "discovery+", "TBA"}:
                    broadcaster = item

            if title and time_str:
                try:
                    start_utc = parse_date_time(date_str, time_str)
                except Exception:
                    i += 1
                    continue

                if not location:
                    location = "Location TBA"

                events.append(
                    FightEvent(
                        title=title,
                        start_utc=start_utc,
                        location=location,
                        broadcaster=broadcaster or "TBC",
                        source_url=ESPN_UFC_SCHEDULE_URL,
                    )
                )

        i += 1

    # Deduplicate by title + date
    deduped: List[FightEvent] = []
    seen = set()
    for event in events:
        key = (event.title, event.start_utc.date().isoformat())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)

    # Keep only future events
    now = datetime.now(timezone.utc) - timedelta(days=1)
    deduped = [e for e in deduped if e.start_utc >= now]
    deduped.sort(key=lambda x: x.start_utc)

    return deduped


def build_calendar(events: List[FightEvent]) -> str:
    lines: List[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Against The Fence//ATF UFC Main Events//EN",
        f"X-WR-CALNAME:{CALENDAR_NAME}",
        f"X-WR-CALDESC:{CALENDAR_DESC}",
        "X-WR-TIMEZONE:UTC",
    ]

    for item in events:
        dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dtstart = item.start_utc.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dtend = (item.start_utc + timedelta(hours=5)).astimezone(timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )

        vevent = [
            "BEGIN:VEVENT",
            f"UID:{item.uid}",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART:{dtstart}",
            f"DTEND:{dtend}",
            f"SUMMARY:{item.title}",
            f"DESCRIPTION:{item.description}",
            f"LOCATION:{item.location}",
            f"URL:{ATF_URL}",
            "END:VEVENT",
        ]
        lines.extend(vevent)

    lines.append("END:VCALENDAR")
    return "\n".join(lines) + "\n"


def write_outputs(events: List[FightEvent]) -> None:
    ics_text = build_calendar(events)
    OUTPUT_ICS.write_text(ics_text, encoding="utf-8")

    payload = [
        {
            "title": e.title,
            "start_utc": e.start_utc.isoformat(),
            "location": e.location,
            "broadcaster": e.broadcaster,
            "source_url": e.source_url,
            "uid": e.uid,
        }
        for e in events
    ]
    OUTPUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> None:
    html = fetch_html(ESPN_UFC_SCHEDULE_URL)
    lines = extract_visible_lines(html)
    events = parse_espn_schedule_lines(lines)

    if len(events) < 2:
        debug_sample = "\n".join(lines[:120])
        raise RuntimeError(
            "Parsed too few UFC events. Refusing to overwrite calendar.\n\n"
            f"First visible lines for debugging:\n{debug_sample}"
        )

    write_outputs(events)
    print(f"Wrote {len(events)} events to {OUTPUT_ICS}")


if __name__ == "__main__":
    main()
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import List, Optional
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
    prelims_utc: Optional[datetime] = None

    @property
    def uid(self) -> str:
        stable_key = f"{self.title}|{self.start_utc.isoformat()}|{self.location}"
        return f"{uuid5(NAMESPACE_URL, stable_key)}@againstthefence.com"

    @property
    def description(self) -> str:
        week_index = self.start_utc.isocalendar().week % len(ATF_BLURBS)
        atf_blurb = ATF_BLURBS[week_index]

        # Show times exactly as on the ESPN schedule page,
        # which is presented in US Eastern time.
        tz = ZoneInfo("America/New_York")
        main_local = self.start_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M (%Z)")
        if self.prelims_utc is not None:
            prelim_local = self.prelims_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M (%Z)")
            prelim_line = f"Prelims start (local): {prelim_local}"
        else:
            prelim_line = "Prelims start (local): TBA"

        lines = [
            f"Main event: {self.title}",
            f"Main card start (local): {main_local}",
            prelim_line,
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


def guess_timezone_from_location(location: str) -> ZoneInfo:
    """
    Best-effort mapping from venue/location text to a timezone.
    Prioritises US venues, with a sensible default for everything else.
    """
    loc = location.lower()

    # West Coast / Pacific
    if any(
        token in loc
        for token in [
            "las vegas",
            "nevada",
            "nv",
            "apex",
            "t-mobile arena",
            "anaheim",
            "los angeles",
            "inglewood",
            "san diego",
            "sacramento",
            "san jose",
            "california",
            "ca",
        ]
    ):
        return ZoneInfo("America/Los_Angeles")

    # US Central
    if any(
        token in loc
        for token in [
            "chicago",
            "illinois",
            "il",
            "houston",
            "dallas",
            "san antonio",
            "texas",
            "tx",
            "kansas city",
            "missouri",
            "mo",
            "minneapolis",
            "minnesota",
            "mn",
            "milwaukee",
            "wisconsin",
            "wi",
        ]
    ):
        return ZoneInfo("America/Chicago")

    # US Mountain
    if any(
        token in loc
        for token in [
            "denver",
            "colorado",
            "co",
            "salt lake city",
            "utah",
            "ut",
        ]
    ):
        return ZoneInfo("America/Denver")

    # US Eastern (explicit)
    if any(
        token in loc
        for token in [
            "new york",
            "ny",
            "boston",
            "massachusetts",
            "ma",
            "miami",
            "florida",
            "fl",
            "atlantic city",
            "newark",
            "new jersey",
            "nj",
            "orlando",
            "philadelphia",
            "pennsylvania",
            "pa",
            "charlotte",
            "north carolina",
            "nc",
            "washington, dc",
            "washington dc",
        ]
    ):
        return ZoneInfo("America/New_York")

    # A few common non-US venues (for better accuracy)
    if "abu dhabi" in loc or "united arab emirates" in loc or "yas island" in loc:
        return ZoneInfo("Asia/Dubai")
    if "london" in loc or "england" in loc or "o2 arena" in loc:
        return ZoneInfo("Europe/London")
    if "paris" in loc or "france" in loc:
        return ZoneInfo("Europe/Paris")
    if "rio de janeiro" in loc or "brazil" in loc:
        return ZoneInfo("America/Sao_Paulo")
    if "perth" in loc or "australia" in loc or "sydney" in loc or "melbourne" in loc:
        return ZoneInfo("Australia/Sydney")

    # Sensible global default: ESPN schedule is US-facing, so Eastern.
    return ZoneInfo("America/New_York")


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

    # ESPN schedule shows times in US Eastern on the page.
    # Treat the scraped time as America/New_York, then convert to UTC
    # so that each subscriber's calendar can render in their own local time.
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

            # find next times, broadcaster, title, location in a short window
            window = lines[i + 1:i + 8]

            time_main_str = ""
            time_prelims_str = ""
            broadcaster = ""
            title = ""
            location = ""

            for item in window:
                if looks_like_time(item):
                    if not time_prelims_str:
                        time_prelims_str = item
                    elif not time_main_str:
                        time_main_str = item
                elif not title and looks_like_event_title(item):
                    title = item
                elif not location and looks_like_location(item):
                    location = item
                elif item in {"Paramount+", "ESPN+", "ESPN", "TNT Sports", "discovery+", "TBA"}:
                    broadcaster = item

            # If we only saw one time, treat it as the main card start and
            # leave prelims unknown. If two times, first is prelims, second main.
            if title and (time_main_str or time_prelims_str):
                try:
                    main_time_str = time_main_str or time_prelims_str
                    start_utc = parse_date_time(date_str, main_time_str)

                    prelims_utc: Optional[datetime] = None
                    if time_main_str and time_prelims_str:
                        prelims_utc = parse_date_time(date_str, time_prelims_str)
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
                        prelims_utc=prelims_utc,
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
        dtend = (item.start_utc + timedelta(hours=3)).astimezone(timezone.utc).strftime(
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
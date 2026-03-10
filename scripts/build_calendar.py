from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Dict, List, Optional
#
from uuid import uuid5, NAMESPACE_URL

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ICS = ROOT / "ufc_espn_schedule.ics"
OUTPUT_JSON = ROOT / "events_cache.json"

#
ESPN_UFC_SCHEDULE_URL = "https://www.espn.com/mma/schedule/_/league/ufc"
ATF_URL = "https://www.youtube.com/c/AgainstTheFence"

ATF_BLURBS = [
    "Skip the corporate waffle. Watch the fights, come argue with us live - ATF",
    "The card starts in the cage, but the real chaos starts in the comments - ATF",
    "Watch the event, and head to ATF for the fan verdict they won't give you on broadcast",
    "We do not do polite, sterile analysis. We do fan energy, sharp takes, and proper watch-alongs - ATF",
    "If the judges ruin your night, we will be there to say it plainly - ATF",
]

CALENDAR_NAME = "UFC Event Schedule"
CALENDAR_DESC = "Free UFC event calendar by Against The Fence"

#

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}


@dataclass
class FightEvent:
    title: str
    start_local: datetime
    location: str
    broadcaster: str
    source_url: str
    prelims_local: Optional[datetime] = None

    @property
    def uid(self) -> str:
        stable_key = f"{normalise_title(self.title)}|{self.start_local.date().isoformat()}"
        return f"{uuid5(NAMESPACE_URL, stable_key)}@againstthefence.com"

    @property
    def description(self) -> str:
        prelims_time, main_time, tz_label = format_event_local_times(
            self.start_local,
            self.location,
            self.prelims_local,
        )
        blurb_index = self.start_local.isocalendar().week % len(ATF_BLURBS)
        atf_blurb = ATF_BLURBS[blurb_index]

        lines = [
            "Where to watch",
            f"{self.title} main card streams on Paramount+ at {main_time}, {tz_label}.",
            f"The prelims stream on Paramount+ at {prelims_time}, {tz_label}.",
            "",
            atf_blurb,
            "",
        f"Watch along for free on Against The Fence",
            "",
        ]
        return "\n".join(lines)


def _should_disable_ssl_verification() -> bool:
    """
    Returns True if SSL certificate verification should be disabled.

    Controlled via the UFC_CALENDAR_INSECURE environment variable:
      - "1", "true", "yes", "on" (case-insensitive) => disable verification
      - anything else (or unset) => keep verification enabled
    """
    value = os.getenv("UFC_CALENDAR_INSECURE", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def fetch_html(url: str) -> str:
    """
    Fetch HTML with robust SSL handling.

    By default this verifies SSL certificates. If an SSL error occurs,
    it will log a warning and automatically retry once without
    certificate verification. To force insecure mode from the start,
    set UFC_CALENDAR_INSECURE=1 in the environment.
    """
    verify = not _should_disable_ssl_verification()

    try:
        response = requests.get(url, headers=HEADERS, timeout=30, verify=verify)
        response.raise_for_status()
        return response.text
    except requests.exceptions.SSLError as exc:
        if not verify:
            # Already in insecure mode; propagate the error.
            raise

        print(f"WARNING: SSL verification failed for {url}: {exc}")
        print("Retrying once without certificate verification...")

        response = requests.get(url, headers=HEADERS, timeout=30, verify=False)
        response.raise_for_status()
        return response.text


def clean_text(text: str) -> str:
    text = unescape(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def guess_timezone_from_location(location: str) -> ZoneInfo:
    loc = location.lower()

    if any(token in loc for token in ["las vegas", "nevada", "nv", "apex", "t-mobile arena", "california", "ca", "los angeles", "anaheim", "inglewood"]):
        return ZoneInfo("America/Los_Angeles")

    if any(term in loc for term in ["seattle", "washington state", " wa ", ", wa"]):
        return ZoneInfo("America/Los_Angeles")

    if any(token in loc for token in ["chicago", "illinois", "il", "houston", "dallas", "san antonio", "texas", "tx", "kansas city", "minneapolis", "wisconsin", "milwaukee"]):
        return ZoneInfo("America/Chicago")

    if any(term in loc for term in ["abu dhabi", "yas island", "united arab emirates", "uae"]):
        return ZoneInfo("Asia/Dubai")

    if any(term in loc for term in ["macau", "macao"]):
        return ZoneInfo("Asia/Macau")

    if any(token in loc for token in ["denver", "colorado", "co", "salt lake city", "utah", "ut"]):
        return ZoneInfo("America/Denver")

    if any(token in loc for token in ["new york", "ny", "newark", "new jersey", "nj", "miami", "florida", "fl", "orlando", "philadelphia", "charlotte", "north carolina", "nc", "atlanta", "boston", "massachusetts", "ma", "washington dc"]):
        return ZoneInfo("America/New_York")

    if "abu dhabi" in loc or "united arab emirates" in loc or "yas island" in loc:
        return ZoneInfo("Asia/Dubai")
    if "london" in loc or "england" in loc or "o2 arena" in loc:
        return ZoneInfo("Europe/London")
    if "paris" in loc or "france" in loc:
        return ZoneInfo("Europe/Paris")
    if "rio de janeiro" in loc or "brazil" in loc:
        return ZoneInfo("America/Sao_Paulo")
    if "perth" in loc:
        return ZoneInfo("Australia/Perth")
    if "sydney" in loc or "melbourne" in loc or "australia" in loc:
        return ZoneInfo("Australia/Sydney")

    return ZoneInfo("America/New_York")


def format_event_local_times(
    start_local: datetime,
    location: str,
    prelims_local: Optional[datetime] = None,
) -> tuple[str, str, str]:
    tz = guess_timezone_from_location(location)

    if prelims_local is not None:
        prelims_display = prelims_local.astimezone(tz)
        main_display = start_local.astimezone(tz)
    else:
        prelims_display = start_local.astimezone(tz)
        main_display = (start_local + timedelta(hours=3)).astimezone(tz)

    prelims_time = prelims_display.strftime("%-I:%M %p")
    main_time = main_display.strftime("%-I:%M %p")
    tz_label = main_display.strftime("%Z")
    return prelims_time, main_time, tz_label


#


def looks_like_date(line: str) -> bool:
    return bool(re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}$", line))


def looks_like_time(line: str) -> bool:
    return bool(re.match(r"^\d{1,2}:\d{2}\s*[AP]M$", line, flags=re.IGNORECASE))


def looks_like_event_title(line: str) -> bool:
    lower = line.lower()
    banned = [
        "travel deals",
        "collectibles",
        "shop",
        "store",
        "ticket",
        "how to watch",
    ]
    if any(term in lower for term in banned):
        return False

    return (
        line.startswith("UFC ")
        or "Fight Night" in line
        or line.startswith("Noche UFC")
    )


def looks_like_location(line: str) -> bool:
    venue_words = [
        "Arena", "Center", "Centre", "APEX", "Apex", "Stadium",
        "Garden", "Hall", "Coliseum", "Bank", "Forum", "Place"
    ]
    return any(word in line for word in venue_words)


def parse_espn_schedule_lines(lines: List[str]) -> List[FightEvent]:
    events: List[FightEvent] = []
    i = 0
    current_year = datetime.now(timezone.utc).year

    while i < len(lines):
        line = lines[i]

        if not looks_like_date(line):
            i += 1
            continue

        date_str = line
        window = lines[i + 1:i + 12]

        times = [item for item in window if looks_like_time(item)]
        titles = [item for item in window if looks_like_event_title(item)]
        locations = [item for item in window if looks_like_location(item)]
        broadcasters = [
            item for item in window
            if item in {"Paramount+", "ESPN+", "ESPN", "TNT Sports", "discovery+", "TBA"}
        ]

        if not titles or not times:
            i += 1
            continue

        title = titles[0]
        location = locations[0] if locations else "Location TBA"
        broadcaster = broadcasters[0] if broadcasters else "TBC"

        try:
            event_tz = guess_timezone_from_location(location)

            if len(times) == 1:
                prelims_local = None
                start_local = dateparser.parse(f"{date_str} {current_year} {times[0]}")
                if start_local is None:
                    i += 1
                    continue
                start_local = start_local.replace(tzinfo=event_tz)
            else:
                prelims_local = dateparser.parse(f"{date_str} {current_year} {times[0]}")
                start_local = dateparser.parse(f"{date_str} {current_year} {times[1]}")
                if prelims_local is None or start_local is None:
                    i += 1
                    continue
                prelims_local = prelims_local.replace(tzinfo=event_tz)
                start_local = start_local.replace(tzinfo=event_tz)
        except Exception:
            i += 1
            continue

        events.append(
            FightEvent(
                title=title,
                start_local=start_local,
                location=location,
                broadcaster=broadcaster,
                source_url=ESPN_UFC_SCHEDULE_URL,
                prelims_local=prelims_local,
            )
        )

        i += 1

    return dedupe_future_events(events)


def fetch_espn_fallback_events() -> List[FightEvent]:
    html = fetch_html(ESPN_UFC_SCHEDULE_URL)
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    raw_text = soup.get_text("\n")
    lines = [clean_text(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    return parse_espn_schedule_lines(lines)


#


def normalise_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.lower()).strip()


def dedupe_future_events(events: List[FightEvent]) -> List[FightEvent]:
    deduped: List[FightEvent] = []
    seen = set()
    now = datetime.now(timezone.utc) - timedelta(days=1)

    for event in sorted(events, key=lambda x: x.start_local):
        if event.start_local.astimezone(timezone.utc) < now:
            continue
        key = (normalise_title(event.title), event.start_local.date().isoformat())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)

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
        tzid = item.start_local.tzinfo.key if hasattr(item.start_local.tzinfo, "key") else "UTC"
        dtstart = item.start_local.strftime("%Y%m%dT%H%M%S")
        dtend = (item.start_local + timedelta(hours=3)).strftime("%Y%m%dT%H%M%S")

        description = (
            item.description
            .replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace(",", "\\,")
            .replace(";", "\\;")
        )
        location = item.location.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")
        title = item.title.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")

        vevent = [
            "BEGIN:VEVENT",
            f"UID:{item.uid}",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;TZID={tzid}:{dtstart}",
            f"DTEND;TZID={tzid}:{dtend}",
            f"SUMMARY:{title}",
            f"DESCRIPTION:{description}",
            f"LOCATION:{location}",
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
            "start_local": e.start_local.isoformat(),
            "prelims_local": e.prelims_local.isoformat() if e.prelims_local else None,
            "location": e.location,
            "broadcaster": e.broadcaster,
            "source_url": e.source_url,
            "uid": e.uid,
        }
        for e in events
    ]
    OUTPUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_existing_events() -> List[FightEvent]:
    if not OUTPUT_JSON.exists():
        return []

    try:
        payload = json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
        events: List[FightEvent] = []
        for item in payload:
            start_local = dateparser.isoparse(item["start_local"])
            prelims_local = dateparser.isoparse(item["prelims_local"]) if item.get("prelims_local") else None
            events.append(
                FightEvent(
                    title=item["title"],
                    start_local=start_local,
                    location=item.get("location", "Location TBA"),
                    broadcaster=item.get("broadcaster", "TBC"),
                    source_url=item.get("source_url", ESPN_UFC_SCHEDULE_URL),
                    prelims_local=prelims_local,
                )
            )
        return dedupe_future_events(events)
    except Exception:
        return []


def main() -> None:
    print("Fetching ESPN UFC schedule...")

    events: List[FightEvent] = []
    try:
        events = fetch_espn_fallback_events()
        print(f"Parsed {len(events)} ESPN events")
    except Exception as exc:
        print(f"WARNING: ESPN schedule fetch failed: {exc}")

    events = dedupe_future_events(events)

    if len(events) < 2:
        existing = load_existing_events()
        if existing:
            print("WARNING: Parsed too few events. Keeping existing calendar output.")
            print(f"Existing cached events retained: {len(existing)}")
            return

        raise RuntimeError(
            "Parsed too few UFC events and no existing cache is available. "
            "Refusing to overwrite calendar."
        )

    write_outputs(events)
    print(f"Wrote {len(events)} events to {OUTPUT_ICS}")


if __name__ == "__main__":
    main()
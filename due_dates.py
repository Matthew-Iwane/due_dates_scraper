"""Build the due-date dashboard: one table of upcoming deadlines from every source.

Flow: fetch unsubmitted Gradescope assignments (via the gradescope package)
and Blackboard calendar events (via ICS feed URLs), normalize everything to
UTC DueItems, drop Blackboard's copies of Gradescope assignments and anything
already submitted, keep only those inside the DAYS_BEHIND/DAYS_AHEAD window,
then render two sorted HTML tables (things to submit, everything else) showing
each due time in Eastern and Tokyo time.

Output goes to due_dates/: due_dates.html is always the latest dashboard, and
each run also saves a dated copy as both HTML and PDF
(due_dates_YYYY-MM-DD.html / .pdf) so past days stay around.

Usage:
    python due_dates.py              # write due_dates/ html + pdf
    python due_dates.py --inspect-bb # print raw sample events from each feed
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html import escape
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar
from tqdm import tqdm

EASTERN = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")

HERE = Path(__file__).parent
OUT_DIR = HERE / "due_dates"

# Silence tqdm's progress bars when output isn't a terminal (e.g. cron
# redirected to a log file), so the log doesn't fill up with carriage returns.
_TTY = sys.stderr.isatty()


# ---- tiny .env loader, so we don't need an extra dependency for this ----
def _load_dotenv(path: Path = HERE / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# All config comes from .env (see .env.example).
GS_EMAIL = os.environ.get("GS_EMAIL")
GS_PASSWORD = os.environ.get("GS_PASSWORD")
BB_ICS_URL = os.environ.get("BB_ICS_URL")
BB_ICS_URLS_RAW = os.environ.get("BB_ICS_URLS", "").strip()
GS_TERM = os.environ.get("GS_TERM", "").strip()
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "10"))
DAYS_BEHIND = int(os.environ.get("DAYS_BEHIND", "1"))


@dataclass
class DueItem:
    """One deadline, from either source, in a common shape."""

    source: str  # "Gradescope" or "Blackboard"
    course: str
    title: str
    due_utc: datetime  # timezone-aware, UTC
    gradable: bool = True  # something to submit; False for live sessions, reminders, breaks
    submitted: bool = False


def _parse_gradescope_dt(raw: str) -> datetime | None:
    """Gradescope's student assignment table gives ISO 8601 strings.

    They usually include a UTC offset already (e.g. '2026-04-07T23:59:00-04:00').
    If yours don't for some reason, this falls back to assuming Eastern time,
    since that's what most US Gradescope courses run on -- check one date
    against the Gradescope site itself if your numbers look off.
    """
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=EASTERN)
    return dt.astimezone(timezone.utc)


def _to_utc(dt: datetime | date) -> datetime | None:
    """Normalize an icalendar dt value (datetime or plain date) to UTC."""
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(dt, date):
        # All-day event, no specific time attached.
        return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
    return None


_MEETING_ID_RE = re.compile(r"Meeting ID:\s*([\d ]+\d)")


def _extract_bb_title(component) -> str:
    """Blackboard titles nearly-identical events (e.g. every course's weekly
    call is just "Live Session") with nothing else in CATEGORIES/SUMMARY to
    tell them apart. The Zoom meeting ID buried in DESCRIPTION is the only
    per-event identifier available, so fold it into the title.
    """
    title = str(component.get("summary", "Untitled"))
    description = component.get("description")
    if description:
        match = _MEETING_ID_RE.search(str(description))
        if match:
            title = f"{title} (Meeting ID: {match.group(1)})"
    return title


def _extract_bb_course(component) -> str:
    """Course name from an event's CATEGORIES field, if Blackboard set one."""
    cats = component.get("categories")
    if cats is not None:
        try:
            names = [str(c) for c in cats.cats]
            if names:
                return ", ".join(names)
        except AttributeError:
            return str(cats)
    return "Blackboard"


def fetch_gradescope() -> list[DueItem]:
    """Every dated assignment across your Gradescope courses.

    Submitted ones are kept (flagged) so their Blackboard gradebook copies can
    be matched and dropped too; main() filters them out afterwards.
    GS_TERM, if set, limits this to courses whose term contains that text.
    """
    if not GS_EMAIL or not GS_PASSWORD:
        print("Skipping Gradescope: set GS_EMAIL and GS_PASSWORD.", file=sys.stderr)
        return []

    from gradescope import Gradescope, Role

    items: list[DueItem] = []
    gs = Gradescope(GS_EMAIL, GS_PASSWORD)
    courses = list(gs.get_courses(role=Role.STUDENT))
    for course in tqdm(courses, desc="Gradescope courses", unit="course", disable=not _TTY):
        if GS_TERM and GS_TERM.lower() not in (course.term or "").lower():
            continue
        for a in gs.get_assignments_as_student(course):
            due = _parse_gradescope_dt(a.due_date)
            if due is None:
                continue
            items.append(
                DueItem(
                    source="Gradescope",
                    course=course.short_name or course.full_name,
                    title=a.title,
                    due_utc=due,
                    submitted=bool(a.submitted),
                )
            )
    return items


def _bb_feeds() -> list[tuple[str | None, str]]:
    """Which Blackboard ICS feeds to fetch, as (course_label, url) pairs.

    Blackboard's combined "all courses" feed (BB_ICS_URL) doesn't expose a
    course name in any field. Per-course feeds (BB_ICS_URLS, a JSON object of
    {"Course Name": "url"}, one per course's own Calendar > Share Calendar
    link) don't either, but since each URL is course-specific we can label
    its events with the course name ourselves.
    """
    feeds: list[tuple[str | None, str]] = []
    if BB_ICS_URLS_RAW:
        try:
            parsed = json.loads(BB_ICS_URLS_RAW)
        except json.JSONDecodeError as exc:
            print(f"Warning: BB_ICS_URLS is not valid JSON ({exc}); ignoring it.", file=sys.stderr)
        else:
            feeds.extend(parsed.items())
    if BB_ICS_URL:
        feeds.append((None, BB_ICS_URL))
    return feeds


def fetch_blackboard(inspect_only: bool = False) -> list[DueItem]:
    """Download each Blackboard ICS feed and turn its events into DueItems.

    With inspect_only, prints a few raw events per feed instead, which is
    handy for seeing what fields Blackboard actually fills in.
    """
    feeds = _bb_feeds()
    if not feeds:
        print("Skipping Blackboard: set BB_ICS_URL or BB_ICS_URLS.", file=sys.stderr)
        return []

    def download(url: str) -> Calendar:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return Calendar.from_ical(resp.content)

    # Feeds are independent, so download them all at once rather than in turn.
    with tqdm(total=len(feeds), desc="Blackboard calendars", unit="feed", disable=not _TTY) as pbar:
        with ThreadPoolExecutor() as pool:
            futures = [pool.submit(download, url) for _, url in feeds]
            for _ in as_completed(futures):
                pbar.update(1)
    calendars = [f.result() for f in futures]

    items: list[DueItem] = []
    for (course_label, _), cal in zip(feeds, calendars):
        events = list(cal.walk("VEVENT"))

        if inspect_only:
            print(f"=== {course_label or 'combined'} ===")
            for e in events[:5]:
                print("---")
                for key in ("summary", "uid", "dtstart", "dtend", "categories", "description"):
                    print(f"{key}: {e.get(key)}")
            continue

        for component in tqdm(events, desc=f"Blackboard events ({course_label or 'combined'})", unit="event", disable=not _TTY):
            dt = component.get("dtstart")
            if dt is None:
                continue
            due = _to_utc(dt.dt)
            if due is None:
                continue
            items.append(
                DueItem(
                    source="Blackboard",
                    course=course_label or _extract_bb_course(component),
                    title=_extract_bb_title(component),
                    due_utc=due,
                    # Blackboard builds each event's UID from the record behind it:
                    # gradebook items (real submissions) are
                    # "_blackboard.platform.gradebook9.GradableItem-...", while
                    # instructor-typed calendar entries (live sessions, "DUE: ..."
                    # reminders, breaks) are "_blackboard.data.calendar.CalendarEntry-...".
                    # If Blackboard ever changes this, items land in "Everything else"
                    # rather than disappearing.
                    gradable="GradableItem" in str(component.get("uid", "")),
                )
            )
    return items


def drop_gradescope_copies(items: list[DueItem]) -> list[DueItem]:
    """Remove Blackboard gradebook items that mirror a Gradescope assignment.

    Gradescope's Blackboard integration copies each assignment into the
    Blackboard gradebook under the same title and due date, so it arrives in the
    calendar feed a second time. The times differ by seconds (Gradescope says
    23:59:00, Blackboard 23:59:59), hence the tolerance. The Gradescope copy is
    kept since it's where you submit and it knows whether you have. Titles that
    don't match exactly just show twice; nothing is lost.
    """
    gradescope_dues: dict[str, list[datetime]] = {}
    for i in items:
        if i.source == "Gradescope":
            gradescope_dues.setdefault(i.title.strip().lower(), []).append(i.due_utc)

    def is_copy(i: DueItem) -> bool:
        dues = gradescope_dues.get(i.title.strip().lower(), [])
        return i.source == "Blackboard" and any(abs(i.due_utc - d) <= timedelta(hours=1) for d in dues)

    return [i for i in items if not is_copy(i)]


def filter_window(items: list[DueItem], now: datetime) -> list[DueItem]:
    """Keep only items due within [now - DAYS_BEHIND, now + DAYS_AHEAD].

    This is what actually gets rid of old-semester clutter: anything from a
    finished course has a due date months in the past, well outside this
    window, whether or not we can tell which "term" it belongs to.
    """
    lower = now - timedelta(days=DAYS_BEHIND)
    upper = now + timedelta(days=DAYS_AHEAD)
    return [i for i in items if lower <= i.due_utc <= upper]


# Item type -> CSS class used for its row colour and badge in style.css.
TYPE_SLUGS = {
    "Quiz": "type-quiz",
    "Assignment": "type-assignment",
    "Live Session": "type-live-session",
    "Event": "type-event",
}


_DUE_WORD_RE = re.compile(r"\bdue\b", re.IGNORECASE)


def _is_reminder(item: DueItem) -> bool:
    """An instructor's calendar note about a deadline ("DUE: ASSIGNMENT 5").

    These aren't the item you submit, and their titles don't line up with it
    ("DUE: WEEKLY QUIZ" vs "Week 5 Activity: Quiz (graded)"), so they can't be
    matched up and dropped. They're kept in their own section because they're
    often the only heads-up for work the instructor hasn't posted yet.
    """
    return not item.gradable and bool(_DUE_WORD_RE.search(item.title))


def _classify_type(item: DueItem) -> str:
    """Quiz vs Assignment for deadlines; Live Session vs Event otherwise."""
    t = item.title.lower()
    if item.gradable or _is_reminder(item):
        return "Quiz" if "quiz" in t else "Assignment"
    return "Live Session" if "live session" in t else "Event"


def _render_table(items: list[DueItem], now: datetime, source_header: str) -> str:
    """One table of items, soonest first.

    Each row gets an urgency class (overdue / due within 24h / within 3 days)
    and a type class, which style.css uses to colour it.
    """
    rows = []
    for i in sorted(items, key=lambda i: i.due_utc):
        est = i.due_utc.astimezone(EASTERN).strftime("%a %b %d, %I:%M %p")
        jst = i.due_utc.astimezone(TOKYO).strftime("%a %b %d, %I:%M %p")

        delta = i.due_utc - now
        if delta.total_seconds() < 0:
            urgency_class = "overdue"
        elif delta <= timedelta(hours=24):
            urgency_class = "due-soon"
        elif delta <= timedelta(days=3):
            urgency_class = "due-upcoming"
        else:
            urgency_class = ""

        item_type = _classify_type(i)
        type_slug = TYPE_SLUGS[item_type]

        rows.append(
            f'<tr class="{urgency_class} {type_slug}">'
            f'<td class="source source-{i.source.lower()}">{i.source}</td><td>{escape(i.course)}</td>'
            f'<td><span class="badge {type_slug}">{item_type}</span>{escape(i.title)}</td>'
            f'<td class="due">{est} EST</td><td class="due">{jst} JST</td></tr>'
        )

    body_rows = "".join(rows) if rows else '<tr><td colspan="5" class="empty">Nothing here.</td></tr>'
    return f"""<table>
    <tr><th>{source_header}</th><th>Course</th><th>Item</th><th>Due (EST)</th><th>Due (JST)</th></tr>
    {body_rows}
  </table>"""


def render_html(items: list[DueItem], now: datetime) -> str:
    """The full dashboard page: things to submit, deadline reminders, everything else.

    style.css is inlined rather than linked so the page renders the same from
    due_dates/ and when printed to PDF.
    """
    todo = _render_table([i for i in items if i.gradable], now, "Submit on")
    reminders = _render_table([i for i in items if _is_reminder(i)], now, "Source")
    other = _render_table(
        [i for i in items if not i.gradable and not _is_reminder(i)], now, "Source"
    )
    generated = now.astimezone(EASTERN).strftime("%b %d, %Y %I:%M %p")

    legend = "".join(
        f'<span class="legend-item"><span class="dot {slug}"></span>{label}</span>'
        for label, slug in TYPE_SLUGS.items()
        if label != "Event"
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Due dates</title>
<style>
{(HERE / "style.css").read_text(encoding="utf-8")}
</style>
</head>
<body>
  <h1>Upcoming due dates</h1>
  <div class="updated">Generated {generated} EST</div>
  <div class="legend">{legend}</div>
  <h2>To do</h2>
  {todo}
  <h2>Deadline reminders</h2>
  <p class="note">Notes your instructors put on the Blackboard calendar. The item you
  actually submit shows under To do once it's posted; if it isn't there yet, it
  hasn't been posted.</p>
  {reminders}
  <h2>Everything else</h2>
  {other}
</body>
</html>"""


def write_pdf(html: str, pdf_path: Path) -> None:
    """Print the dashboard to a landscape PDF with headless Chromium.

    Landscape because the five fixed-width columns leave the Item column
    almost no room on a portrait page. No page margins, so the dark background
    runs to the edge; body's own margin in style.css provides the padding.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(path=str(pdf_path), format="Letter", landscape=True, print_background=True)
        browser.close()


def _safe_fetch(name: str, fetch) -> list[DueItem]:
    """Run a fetch_* function, but don't let one source's failure wipe out the other's."""
    try:
        return fetch()
    except Exception as exc:
        print(f"Warning: {name} fetch failed ({exc}); continuing without it.", file=sys.stderr)
        return []


def main() -> None:
    if "--inspect-bb" in sys.argv:
        fetch_blackboard(inspect_only=True)
        return

    # The two sources don't depend on each other, so fetch them side by side.
    with ThreadPoolExecutor(max_workers=2) as pool:
        gradescope = pool.submit(_safe_fetch, "Gradescope", fetch_gradescope)
        blackboard = pool.submit(_safe_fetch, "Blackboard", fetch_blackboard)
        items = gradescope.result() + blackboard.result()

    # One timestamp for the whole run, so the window, urgency colours and
    # "Generated" line all agree.
    now = datetime.now(timezone.utc)
    items = [i for i in drop_gradescope_copies(items) if not i.submitted]
    items = filter_window(items, now)
    html = render_html(items, now)
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "due_dates.html").write_text(html, encoding="utf-8")

    # One dated HTML + PDF per day; a second run the same day overwrites them.
    dated = OUT_DIR / f"due_dates_{now.astimezone(EASTERN):%Y-%m-%d}"
    html_path = dated.with_suffix(".html")
    html_path.write_text(html, encoding="utf-8")
    print(
        f"Wrote {html_path} ({len(items)} due dates found, "
        f"-{DAYS_BEHIND}d to +{DAYS_AHEAD}d from now)"
    )

    pdf_path = dated.with_suffix(".pdf")
    try:
        write_pdf(html, pdf_path)
    except Exception as exc:
        print(f"Warning: PDF not written ({exc}); the HTML is still up to date.", file=sys.stderr)
    else:
        print(f"Wrote {pdf_path}")


if __name__ == "__main__":
    main()
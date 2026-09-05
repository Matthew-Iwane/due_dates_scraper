from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

EASTERN = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")

HERE = Path(__file__).parent


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

GS_EMAIL = os.environ.get("GS_EMAIL")
GS_PASSWORD = os.environ.get("GS_PASSWORD")
BB_ICS_URL = os.environ.get("BB_ICS_URL")
GS_TERM = os.environ.get("GS_TERM", "").strip()
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "7"))
DAYS_BEHIND = int(os.environ.get("DAYS_BEHIND", "1"))


@dataclass
class DueItem:
    source: str  # "Gradescope" or "Blackboard"
    course: str
    title: str
    due_utc: datetime | None  # always timezone-aware (UTC) once set


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


def _extract_bb_course(component) -> str:
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
    if not GS_EMAIL or not GS_PASSWORD:
        print("Skipping Gradescope: set GS_EMAIL and GS_PASSWORD.", file=sys.stderr)
        return []

    from gradescope import Gradescope, Role

    items: list[DueItem] = []
    gs = Gradescope(GS_EMAIL, GS_PASSWORD)
    for course in gs.get_courses(role=Role.STUDENT):
        if GS_TERM and GS_TERM.lower() not in (course.term or "").lower():
            continue
        for a in gs.get_assignments_as_student(course):
            if a.submitted:
                continue
            due = _parse_gradescope_dt(a.due_date)
            if due is None:
                continue
            items.append(
                DueItem(
                    source="Gradescope",
                    course=course.short_name or course.full_name,
                    title=a.title,
                    due_utc=due,
                )
            )
    return items


def fetch_blackboard(inspect_only: bool = False) -> list[DueItem]:
    if not BB_ICS_URL:
        print("Skipping Blackboard: set BB_ICS_URL.", file=sys.stderr)
        return []

    resp = requests.get(BB_ICS_URL, timeout=30)
    resp.raise_for_status()
    cal = Calendar.from_ical(resp.content)

    events = list(cal.walk("VEVENT"))

    if inspect_only:
        for e in events[:5]:
            print("---")
            for key in ("summary", "dtstart", "dtend", "categories", "description"):
                print(f"{key}: {e.get(key)}")
        return []

    items: list[DueItem] = []
    for component in events:
        dt = component.get("dtstart")
        if dt is None:
            continue
        due = _to_utc(dt.dt)
        if due is None:
            continue
        items.append(
            DueItem(
                source="Blackboard",
                course=_extract_bb_course(component),
                title=str(component.get("summary", "Untitled")),
                due_utc=due,
            )
        )
    return items


def filter_window(items: list[DueItem], now: datetime) -> list[DueItem]:
    """Keep only items due within [now - DAYS_BEHIND, now + DAYS_AHEAD].

    This is what actually gets rid of old-semester clutter: anything from a
    finished course has a due date months in the past, well outside this
    window, whether or not we can tell which "term" it belongs to.
    """
    lower = now - timedelta(days=DAYS_BEHIND)
    upper = now + timedelta(days=DAYS_AHEAD)
    return [i for i in items if i.due_utc and lower <= i.due_utc <= upper]


def render_html(items: list[DueItem]) -> str:
    items = [i for i in items if i.due_utc is not None]
    items.sort(key=lambda i: i.due_utc)
    now = datetime.now(timezone.utc)

    rows = []
    for i in items:
        est = i.due_utc.astimezone(EASTERN).strftime("%a %b %d, %I:%M %p")
        jst = i.due_utc.astimezone(TOKYO).strftime("%a %b %d, %I:%M %p")
        row_class = "overdue" if i.due_utc < now else ""
        rows.append(
            f'<tr class="{row_class}">'
            f"<td>{i.source}</td><td>{i.course}</td><td>{i.title}</td>"
            f"<td>{est} EST</td><td>{jst} JST</td></tr>"
        )

    body_rows = "".join(rows) if rows else '<tr><td colspan="5">No due dates found.</td></tr>'
    generated = datetime.now(EASTERN).strftime("%b %d, %Y %I:%M %p")

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Due dates</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #0b0d12; color: #e6e8ee; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 0.6rem 0.8rem; text-align: left; border-bottom: 1px solid #2a2e3a; }}
  th {{ color: #9aa4b2; font-weight: 600; text-transform: uppercase; font-size: 0.75rem; }}
  tr.overdue td {{ color: #ff7b7b; }}
  h1 {{ font-size: 1.3rem; margin-bottom: 0.25rem; }}
  .updated {{ color: #7d8698; font-size: 0.85rem; margin-bottom: 1.5rem; }}
</style>
</head>
<body>
  <h1>Upcoming due dates</h1>
  <div class="updated">Generated {generated} EST</div>
  <table>
    <tr><th>Source</th><th>Course</th><th>Assignment</th><th>Due (EST)</th><th>Due (JST)</th></tr>
    {body_rows}
  </table>
</body>
</html>"""


def main() -> None:
    if "--inspect-bb" in sys.argv:
        fetch_blackboard(inspect_only=True)
        return

    items = fetch_gradescope() + fetch_blackboard()
    items = filter_window(items, datetime.now(timezone.utc))
    html = render_html(items)
    out_path = HERE / "due_dates.html"
    out_path.write_text(html, encoding="utf-8")
    print(
        f"Wrote {out_path} ({len(items)} due dates found, "
        f"-{DAYS_BEHIND}d to +{DAYS_AHEAD}d from now)"
    )


if __name__ == "__main__":
    main()
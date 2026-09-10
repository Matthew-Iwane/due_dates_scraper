# Due dates dashboard

Pulls live assignment due dates from Gradescope and Blackboard into one
sorted HTML page, with every date shown in US Eastern and Japan time.

## 1. Install dependencies

```
pip install -r requirements.txt
```

`gradescope-tool` is an unofficial, community-maintained library (not made
by Gradescope). It logs in with your real credentials the same way the
website does. It can break if Gradescope changes their site, and it's
automated access outside the normal web UI, so treat it as a personal,
best-effort tool rather than something to depend on for anything critical.

## 2. Get your Blackboard calendar feed URL

This is a real, sanctioned Blackboard feature, not scraping:

1. Log into Blackboard, go to **Calendar**.
2. Open **Calendar Settings** (gear icon) and choose **Share Calendar**
   (wording varies slightly by school).
3. Copy the link it gives you. Treat it like a password: anyone with that
   URL can see your due dates.

Set it as `BB_ICS_URL` in `.env`. This combined feed covers every course,
but Blackboard doesn't include a course name in any field of it, so every
Blackboard row in the output just says "Blackboard" for its course.

### Getting real course names instead of "Blackboard"

If your Blackboard also lets you share a calendar from *inside* an
individual course (Course > Calendar > Calendar Settings > Share Calendar,
same flow as above but from within the course rather than your combined
calendar), grab one URL per course and set `BB_ICS_URLS` in `.env` as a JSON
object instead:

```
BB_ICS_URLS={"CS 651": "https://...", "DX 601": "https://..."}
```

Each feed's events get labeled with that course name directly, since the
URL itself is now course-specific. If your school's Blackboard doesn't
offer a per-course share link, this won't be available — `--inspect-bb`
(below) will show whether a course-level feed looks any different from the
combined one. If you set both `BB_ICS_URLS` and `BB_ICS_URL`, drop the
combined one once the per-course feeds cover everything you need, or you'll
get every event twice.


### Only current stuff, not every course you've ever taken

- `DAYS_AHEAD` (default `10`) and `DAYS_BEHIND` (default `1`) keep only items
  due within that window of "now". This is what actually clears out old
  semesters: a finished course's due dates are months in the past, well
  outside the window, so they drop out regardless of which course they're
  from. Add `DAYS_AHEAD=14` to your `.env` for a two-week look-ahead instead.
- `GS_TERM` (optional, e.g. `GS_TERM=Fall 2026`) skips Gradescope courses
  whose term doesn't match, before even fetching their assignments. Use the
  exact text Gradescope shows next to your current courses. This only
  applies to Gradescope; Blackboard's feed doesn't expose a clean per-course
  term to filter on, so the date window is what handles it there.

## 4. Run it

```
python due_dates.py
```

## If the Blackboard course name looks wrong

Blackboard's feed format varies a bit by school. Run:

```
python due_dates.py --inspect-bb
```

This prints the raw fields of your first few calendar events so you can see
exactly what's in `summary`, `categories`, and `description`, then adjust
`_extract_bb_course()` in `due_dates.py` to pull the course name from
wherever it actually lives in your feed.

## Notes

- Gradescope due dates come from the student assignment table's own
  timestamps. If a date ever looks off by a few hours, it likely means your
  Gradescope courses use a timezone other than US Eastern; check one date
  against the Gradescope site directly and adjust `EASTERN` at the top of
  `due_dates.py` if needed.
- Submitted Gradescope assignments are skipped, so the list stays focused
  on what's actually still due.
- All-day Blackboard events (no specific time) show as midnight in both
  columns since no time is attached.
- Progress bars (via `tqdm`) print to the terminal while fetching Gradescope
  courses and the Blackboard calendar, so a slow run doesn't look hung.
- Styling for `due_dates.html` lives in `style.css`, generated alongside it;
  `due_dates.py` only emits class names, so tweak colors/layout there.


  NOTE:
  HTML is generated per run, different data every week. 
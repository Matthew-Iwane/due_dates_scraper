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

## Pulling lesson material (bb_materials.py)

Grabs a week's Blackboard lesson content and turns it into one PDF per week,
sized for dropping into NotebookLM.

```
python bb_materials.py --list                               # see courses + sections
python bb_materials.py --course CX651 CX698 DX601 --week 3  # all three, one login
python bb_materials.py --course CX651 --all-weeks
python bb_materials.py --course CX651 --week 3 --upload-drive
```

The linked **Required Reading** pages (open-access textbook chapters, man
pages and so on) are fetched and appended by default — they're assigned
material, so they belong in the week's document. Pass `--no-readings` to skip
them.

Links are found by looking under a "Required Reading" heading rather than by
domain, which is what keeps Zoom links and BU nav pages out without needing a
per-course allowlist. Readings behind a login (the library proxy, ILLiad) are
skipped — they'd just 401. Linked PDFs are saved next to the week's PDF as
their own files, so NotebookLM can take them directly.

Pass several courses to `--course` so one Duo login covers all of them.
Week matching looks for titles starting `Week N`, which CX651/CX698/DX601 use.
CX500 numbers its sections `Module N` instead and won't match.

A browser window opens and drives the BU login for you if you add your
credentials to `.env`:

```
BU_USERNAME=your_bu_login_name
BU_PASSWORD=your_bu_password
```

Without those it waits for you to type them in yourself. Either way the first
run needs you to approve a Duo push on your phone — but the script ticks Duo's
"trust this browser" box, and that cookie *does* persist in `.bb_session/`, so
later runs should go straight through.

Shibboleth's own session cookie is memory-only and dies with the browser
process, which is why login can't be skipped entirely and this can't run
unattended as a cron job the way `due_dates.py` can.

The lessons in these courses aren't uploaded files. They're Blackboard "Ultra
Documents" authored inline as HTML, so the script walks the course content
tree via Blackboard's REST API, pulls each lesson body, inlines its images as
data URIs, drops source files (`.c`, `.py`) in as code blocks, and prints the
whole week to a single self-contained PDF. Anything that isn't an image or
text (PDF slide decks, for instance) is saved next to the week's PDF as its
own file, so it can be uploaded to NotebookLM separately.

Occasional `[could not fetch: ... (HTTP 404)]` markers in the output are dead
image references in the course itself, not a failure on this end.

For `--upload-drive`, see the setup steps at the top of `drive_upload.py`
(Google Cloud project, Drive API, OAuth desktop credentials) and set
`GDRIVE_FOLDER_ID` in `.env`. Skip it and just drag the PDF into NotebookLM
by hand — for one file a week that's honestly less work than the OAuth setup.

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
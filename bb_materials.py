"""Pull a week's Blackboard lesson material into a single PDF per week.

The lessons in these courses aren't uploaded files -- they're Blackboard
"Ultra Documents" authored inline as HTML, with images attached as signed
bbcswebdav links. So this walks the course content tree via Blackboard's
REST API, pulls each lesson's body HTML, inlines its images, stitches the
week together into one self-contained HTML doc, and prints that to PDF.

BU's SSO session cookies don't survive a browser restart, so login happens
interactively at the start of every run, in the same browser process that
then does the scraping.

Required readings linked from each lesson are fetched and appended too,
since they're part of the assigned material; --no-readings skips them.

Usage:
    python bb_materials.py --course CX651 --week 3
    python bb_materials.py --course CX651 CX698 DX601 --week 3
    python bb_materials.py --course CX651 --all-weeks --no-readings
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from tqdm import tqdm

from due_dates import _load_dotenv

_load_dotenv()

HERE = Path(__file__).parent
SESSION_DIR = HERE / ".bb_session"
OUT_DIR = HERE / "bb_materials"
# Set by --dump-raw; keeps raw lesson HTML around so parsing can be debugged
# without burning another interactive login.
RAW_CACHE_DIR: Path | None = None

BB_ORIGIN = "https://learn.bu.edu"
COURSE_LIST_URL = f"{BB_ORIGIN}/ultra/course"
API = f"{BB_ORIGIN}/learn/api/public/v1"

# Content types that hold lesson prose we actually want.
DOC_HANDLERS = {"resource/x-bb-document"}
# Standalone uploaded files (syllabus PDFs and the like).
FILE_HANDLER = "resource/x-bb-file"


@dataclass
class Node:
    id: str
    title: str
    handler: str
    body: str = ""
    file_name: str = ""
    children: list["Node"] = field(default_factory=list)


def _slug(text: str) -> str:
    # \s+ rather than " " so non-breaking spaces (Blackboard course names
    # have them) don't survive into filenames.
    cleaned = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "_", cleaned.strip())[:80] or "untitled"


class Blackboard:
    def __init__(self, page):
        self.page = page

    def api_get(self, url: str):
        res = self.page.evaluate(
            """async (url) => {
                const r = await fetch(url, {credentials: 'include'});
                return {status: r.status, body: await r.text()};
            }""",
            url,
        )
        if res["status"] != 200:
            return None
        try:
            return json.loads(res["body"])
        except json.JSONDecodeError:
            return None

    def courses(self) -> list[dict]:
        data = self.api_get(f"{API}/users/me/courses?expand=course&limit=100")
        out = []
        for m in (data or {}).get("results", []):
            course = m.get("course") or {}
            if course.get("id"):
                out.append({"id": course["id"], "name": course.get("name", "")})
        return out

    def children(self, course_id: str, content_id: str) -> list[dict]:
        data = self.api_get(f"{API}/courses/{course_id}/contents/{content_id}/children")
        return (data or {}).get("results", [])

    def top_level(self, course_id: str) -> list[dict]:
        data = self.api_get(f"{API}/courses/{course_id}/contents")
        return (data or {}).get("results", [])

    def content(self, course_id: str, content_id: str) -> dict:
        return self.api_get(f"{API}/courses/{course_id}/contents/{content_id}") or {}

    def walk(self, course_id: str, items: list[dict], depth: int = 0, progress=None) -> list[Node]:
        """Recursively turn raw API items into Nodes, pulling document bodies."""
        nodes: list[Node] = []
        for item in items:
            handler = item.get("contentHandler", {}).get("id", "")
            node = Node(id=item["id"], title=item.get("title", ""), handler=handler)

            if progress is not None:
                progress.update(1)
                progress.set_postfix_str(node.title[:40])

            if handler in DOC_HANDLERS:
                node.body = self.content(course_id, item["id"]).get("body", "")
                if RAW_CACHE_DIR:
                    RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    (RAW_CACHE_DIR / f"{item['id']}.html").write_text(
                        node.body, encoding="utf-8"
                    )
            elif handler == FILE_HANDLER:
                node.file_name = (
                    item.get("contentHandler", {}).get("file", {}).get("fileName", "")
                )

            if item.get("hasChildren") and depth < 6:
                node.children = self.walk(
                    course_id, self.children(course_id, item["id"]), depth + 1, progress
                )
            nodes.append(node)
        return nodes


LOGGED_IN_SELECTOR = ".course-title"
# Duo's universal prompt offers to remember the browser. That cookie is
# long-lived, unlike Shibboleth's session cookie, so clicking it means later
# runs skip the phone push entirely.
DUO_TRUST_SELECTORS = ("#trust-browser-button", "button:has-text('Yes, trust browser')")


def login(page, timeout_ms: int = 5 * 60 * 1000) -> bool:
    """Get to the Blackboard course list, filling the BU form if we can.

    Duo's push still needs a tap on your phone the first time; after that the
    trust-browser cookie in the saved profile should carry it.
    """
    user, password = os.environ.get("BU_USERNAME"), os.environ.get("BU_PASSWORD")

    if user and password:
        try:
            page.wait_for_selector("#j_username", timeout=15_000)
            page.fill("#j_username", user)
            page.fill("#j_password", password)
            page.click('button[name="_eventId_proceed"]')
            print("Submitted your BU login. Approve the Duo push if it asks.", file=sys.stderr)
        except PlaywrightError:
            # No login form, or the page moved under us. Either way the poll
            # below is what actually decides whether we got in.
            pass
    else:
        print(
            "\n>>> YOUR TURN: log in in the Chrome window (BU login + Duo).\n"
            ">>> Set BU_USERNAME and BU_PASSWORD in .env to skip this.\n",
            file=sys.stderr,
        )

    deadline = time.monotonic() + timeout_ms / 1000
    trusted = False
    while time.monotonic() < deadline:
        # The SSO hop (BU login -> Duo -> Blackboard) navigates underneath
        # this loop, which destroys the execution context mid-query. That's
        # normal here, so treat any such failure as "not ready yet".
        try:
            if page.query_selector(LOGGED_IN_SELECTOR):
                return True
            if not trusted:
                for selector in DUO_TRUST_SELECTORS:
                    button = page.query_selector(selector)
                    if button and button.is_visible():
                        button.click()
                        trusted = True
                        print("Told Duo to trust this browser.", file=sys.stderr)
                        break
        except PlaywrightError:
            pass

        try:
            page.wait_for_timeout(1000)
        except PlaywrightError:
            time.sleep(1)
    return False


ASSET_TAG_RE = re.compile(r'<a([^>]*data-bbfile="[^"]*"[^>]*)>.*?</a>', re.DOTALL)
HREF_RE = re.compile(r'href="([^"]*)"')
BBFILE_RE = re.compile(r'data-bbfile="([^"]*)"')


ERROR_LOG = HERE / "bb_materials_errors.log"
# Problems found during this run, written out at the end. Rewritten from
# scratch each run so the file always describes the latest one.
ERRORS: list[tuple[str, str]] = []
CURRENT_CONTEXT = ""
# Set when the run dies rather than finishing, so the log can't claim a clean
# run just because nothing called log_error() before the crash.
OUTCOME = "did not finish"
CRASH_DETAIL = ""


def log_error(message: str) -> None:
    ERRORS.append((CURRENT_CONTEXT, message))


def write_error_log(argv: list[str]) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"Run: {stamp}",
        f"Command: {' '.join(argv)}",
        f"Outcome: {OUTCOME}",
        "",
    ]

    if not ERRORS:
        lines.append("No problems logged.")
    else:
        lines.append(f"{len(ERRORS)} problem(s):")
        last_context = None
        for context, message in ERRORS:
            if context != last_context:
                lines.append(f"\n[{context or 'general'}]")
                last_context = context
            lines.append(f"  {message}")

    if CRASH_DETAIL:
        lines.append("\n--- traceback ---")
        lines.append(CRASH_DETAIL.rstrip())

    ERROR_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")


SOURCE_SUFFIXES = (".c", ".h", ".py", ".txt", ".md")
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.I)
TAG_RE = re.compile(r"<[^>]+>")


def _unescape(text: str) -> str:
    return text.replace("&quot;", '"').replace("&amp;", "&")


HEADING_RE = re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>", re.DOTALL | re.I)
LINK_RE = re.compile(r'<a[^>]*href="\s*([^"]+?)\s*"[^>]*>(.*?)</a>', re.DOTALL | re.I)
READING_HEADING_RE = re.compile(r"required\s+reading", re.I)
# Behind a login, or not a reading at all.
READING_SKIP_HOSTS = (
    "zoom.us", "illiad.bu.edu", "primo.exlibrisgroup.com", "library.bu.edu",
    "salesforce.com", "help.blackboard.com", "help.anthology.com",
    "knowledge.kaltura.com", "learn.bu.edu",
)


def find_reading_links(body: str) -> list[tuple[str, str]]:
    """Links sitting under a 'Required Reading' heading, as (url, label).

    Scoping by heading rather than by domain is what keeps Zoom links, help
    pages and site furniture out without needing a per-course allowlist.
    """
    found: list[tuple[str, str]] = []
    for heading in HEADING_RE.finditer(body):
        if not READING_HEADING_RE.search(TAG_RE.sub(" ", heading.group(1))):
            continue
        next_heading = HEADING_RE.search(body, heading.end())
        section = body[heading.end(): next_heading.start() if next_heading else len(body)]

        for url, label in LINK_RE.findall(section):
            url = _unescape(url).strip()
            host = urlparse(url).netloc.lower()
            if not url.startswith("http") or any(s in host for s in READING_SKIP_HOSTS):
                continue
            text = re.sub(r"\s+", " ", TAG_RE.sub(" ", label)).strip()
            found.append((url, text or url))
    return found


DROP_BLOCKS_RE = re.compile(
    r"<(script|style|nav|header|footer|aside)\b[^>]*>.*?</\1>", re.DOTALL | re.I
)
BLOCK_END_RE = re.compile(r"</(p|div|li|h[1-6]|tr|pre|blockquote)>|<br\s*/?>", re.I)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def readable_paragraphs(raw: str) -> list[str]:
    """Turn a web page into plain paragraphs, minus nav/script furniture."""
    stripped = DROP_BLOCKS_RE.sub(" ", raw)
    stripped = BLOCK_END_RE.sub("\n", stripped)
    # Full entity decoding, not just &amp;/&quot;: these pages are full of
    # &#8217; and friends, which would otherwise be re-escaped into literal
    # "&#8217;" text in the PDF.
    text = html_unescape(TAG_RE.sub(" ", stripped)).replace("\xa0", " ")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    return [line for line in lines if len(line) > 2]


def fetch_reading(url: str, label: str, asset_dir: Path) -> str:
    """Pull one required reading into the document, or save it if it's a PDF."""
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as exc:
        log_error(f"reading fetch failed: {label} <{url}> ({exc})")
        return f"<h3>{_escape(label)}</h3><p><em>[could not retrieve: {_escape(url)}]</em></p>"

    mime = (resp.headers.get("content-type") or "").split(";")[0].strip()

    if mime == "application/pdf":
        asset_dir.mkdir(parents=True, exist_ok=True)
        name = _slug(Path(urlparse(url).path).stem) + ".pdf"
        (asset_dir / name).write_bytes(resp.content)
        return (
            f"<h3>{_escape(label)}</h3>"
            f"<p><em>[PDF reading saved separately: {name}]</em></p>"
        )

    paragraphs = readable_paragraphs(resp.text)
    if not paragraphs:
        return f"<h3>{_escape(label)}</h3><p><em>[no readable text at {_escape(url)}]</em></p>"

    body = "".join(f"<p>{_escape(p)}</p>" for p in paragraphs)
    return f"<h3>{_escape(label)}</h3><p class='src'>{_escape(url)}</p>{body}"


def collect_reading_links(node: Node) -> list[tuple[str, str]]:
    links = find_reading_links(node.body)
    for child in node.children:
        links.extend(collect_reading_links(child))
    return links


def html_to_text(raw: str) -> str:
    """Visible text of an embedded HTML file, or nothing if it's just chrome."""
    stripped = SCRIPT_STYLE_RE.sub(" ", raw)
    text = html_unescape(TAG_RE.sub(" ", stripped)).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 40:
        return ""
    return f"<p>{text}</p>"


def inline_assets(context, html: str, asset_dir: Path, progress=None) -> str:
    """Resolve bbcswebdav attachments into the document.

    Blackboard emits these two ways: an <a data-bbtype="attachment"> whose
    data-bbfile JSON carries a signed resourceUrl, and an
    <a data-bbtype="image"> whose URL is only in href (sometimes with stray
    leading whitespace) and whose data-bbfile has just alt text. Both land
    here, so fetch whatever URL we can find and branch on the content-type
    the server actually returns rather than trusting the metadata.
    """

    def replace(match: re.Match) -> str:
        if progress is not None:
            progress.update(1)
        attrs = match.group(1)
        meta = {}
        bbfile = BBFILE_RE.search(attrs)
        if bbfile:
            try:
                meta = json.loads(_unescape(bbfile.group(1)))
            except json.JSONDecodeError:
                meta = {}

        href = HREF_RE.search(attrs)
        # href and resourceUrl can point at *different* file ids -- when an
        # instructor replaces an image, the metadata's resourceUrl keeps the
        # old (now 404) id while href gets the live one. Try both, signed
        # first, then bare: the signature carries an expiry, but the bare path
        # is served fine to a logged-in session.
        signed = [
            _unescape(u).strip()
            for u in (meta.get("resourceUrl", ""), href.group(1) if href else "")
            if _unescape(u).strip()
        ]
        candidates: list[str] = []
        for candidate in signed + [u.split("?")[0] for u in signed]:
            if candidate not in candidates:
                candidates.append(candidate)
        if not candidates:
            return ""

        url = candidates[0]
        name = meta.get("displayName") or meta.get("fileName") or meta.get("alt") or ""

        data = mime = None
        last_status = None
        for candidate in candidates:
            try:
                resp = context.request.get(urljoin(BB_ORIGIN, candidate), timeout=60_000)
            except Exception as exc:
                last_status = f"error {exc}"
                continue
            if resp.ok:
                data = resp.body()
                mime = (resp.headers.get("content-type") or "").split(";")[0].strip()
                break
            last_status = resp.status

        if data is None:
            log_error(f"could not fetch: {name or url} (HTTP {last_status})")
            # The filename is usually descriptive ("A Cache Hierarchy with L1,
            # L2, and L3 caches.png"), so keep it in the document as context
            # rather than an error; the HTTP detail goes to the log instead.
            label = Path(name).stem if name else "image"
            return f"<p><em>[image not available: {label}]</em></p>"

        if not name:
            name = Path(url.split("?")[0]).name or "attachment"

        if mime.startswith("image/"):
            b64 = base64.b64encode(data).decode()
            return f'<img src="data:{mime};base64,{b64}" alt="{name}" />'

        # Embedded HTML widgets (BU page furniture, mostly). Printing the
        # source would dump stylesheets and scripts into the PDF, so keep only
        # whatever text a reader would actually have seen.
        if mime.startswith("text/html"):
            return html_to_text(data.decode("utf-8", errors="replace"))

        if mime.startswith("text/") or name.endswith(SOURCE_SUFFIXES):
            code = data.decode("utf-8", errors="replace")
            escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return f"<h5>{name}</h5><pre><code>{escaped}</code></pre>"

        # Anything else (PDFs, slide decks) gets saved beside the week's PDF
        # so it can be uploaded to NotebookLM as its own source.
        asset_dir.mkdir(parents=True, exist_ok=True)
        safe = _slug(Path(name).stem) + (Path(name).suffix or _ext_for(mime))
        (asset_dir / safe).write_bytes(data)
        return f"<p><em>[attached file saved separately: {safe}]</em></p>"

    return ASSET_TAG_RE.sub(replace, html)


def _ext_for(mime: str) -> str:
    return {
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    }.get(mime, ".bin")


def count_assets(node: Node) -> int:
    return len(ASSET_TAG_RE.findall(node.body)) + sum(count_assets(c) for c in node.children)


def render_week_html(context, week: Node, asset_dir: Path, with_readings: bool = True) -> str:
    """Flatten a week's node tree into one printable HTML document."""
    parts: list[str] = []
    bar = tqdm(
        total=count_assets(week), desc="    images/files", unit="asset", leave=False
    )

    def emit(node: Node, level: int = 2) -> None:
        # "ultraDocumentBody" is Blackboard's internal name for the body of the
        # folder above it, so the parent's title is the meaningful heading.
        if node.title and node.title != "ultraDocumentBody":
            parts.append(f"<h{min(level, 6)}>{node.title}</h{min(level, 6)}>")
        if node.body:
            parts.append(inline_assets(context, node.body, asset_dir, bar))
        if node.file_name:
            parts.append(f"<p><em>[file attached in Blackboard: {node.file_name}]</em></p>")
        for child in node.children:
            emit(child, level + 1)

    emit(week, level=1)
    bar.close()

    if with_readings:
        seen: set[str] = set()
        links = [
            (url, label)
            for url, label in collect_reading_links(week)
            if not (url in seen or seen.add(url))
        ]
        if links:
            parts.append(
                "<h2>Required Readings</h2>"
                "<p><em>The sections below are not course-authored. Each is an "
                "external reading assigned by this week's lessons, reproduced "
                "from the source URL shown under its title.</em></p>"
            )
            for url, label in tqdm(
                links, desc="    readings", unit="doc", leave=False
            ):
                parts.append(fetch_reading(url, label, asset_dir))

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{week.title}</title>
<style>
  body {{ font-family: Georgia, 'Times New Roman', serif; line-height: 1.6;
         max-width: 50em; margin: 2em auto; color: #111; }}
  h1 {{ border-bottom: 3px solid #333; padding-bottom: .3em; }}
  h2, h3, h4 {{ margin-top: 1.4em; color: #222; }}
  img {{ max-width: 100%; height: auto; }}
  .src {{ color: #666; font-size: .85em; word-break: break-all; }}
  pre, code {{ background: #f4f4f4; font-family: Menlo, monospace; font-size: .9em; }}
  pre {{ padding: .8em; overflow-x: auto; }}
</style></head>
<body>{"".join(parts)}</body></html>"""


def _run() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--course",
        nargs="+",
        default=["CX651"],
        help="one or more course-name substrings, e.g. --course CX651 CX698 DX601",
    )
    ap.add_argument("--week", help="week number, e.g. 3")
    ap.add_argument("--all-weeks", action="store_true")
    ap.add_argument("--dump-raw", action="store_true", help="save raw lesson HTML for debugging")
    ap.add_argument("--upload-drive", action="store_true", help="push PDFs to Google Drive")
    ap.add_argument("--list", action="store_true", help="list courses and their sections, then exit")
    ap.add_argument("--no-readings", action="store_true",
                    help="skip the linked Required Reading pages")
    args = ap.parse_args()

    if args.dump_raw:
        global RAW_CACHE_DIR
        RAW_CACHE_DIR = HERE / ".bb_raw"

    if not args.list and not args.week and not args.all_weeks:
        ap.error("pass --week N, --all-weeks, or --list")

    OUT_DIR.mkdir(exist_ok=True)
    written: list[Path] = []

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                str(SESSION_DIR), headless=False, viewport={"width": 1280, "height": 900}
            )
        except Exception as exc:
            if "existing browser session" in str(exc):
                sys.exit(
                    "A Chrome window from a previous run is still open and is holding "
                    "the session profile.\nClose that window, then run this again."
                )
            raise
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(COURSE_LIST_URL)
        if not login(page):
            print(f"Never reached the course list; last URL: {page.url}", file=sys.stderr)
            log_error(f"login never completed; stuck at {page.url}")
            context.close()
            sys.exit(1)
        print("Logged in. Beginning course information scrape.\n", file=sys.stderr)

        bb = Blackboard(page)

        if args.list:
            for course in bb.courses():
                print(f"\n{course['name']}  [{course['id']}]")
                for item in bb.top_level(course["id"]):
                    marker = "week" if re.match(r"\s*week\s*\d+", item["title"], re.I) else "    "
                    print(f"  {marker}  {item['title']}")
            context.close()
            return

        wanted = [c.lower() for c in args.course]
        matches = [
            c for c in bb.courses() if any(w in c["name"].lower() for w in wanted)
        ]
        if not matches:
            print(f"No course matching {args.course!r}.", file=sys.stderr)
            log_error(f"no course matched {args.course!r}")
            context.close()
            sys.exit(1)

        for course in matches:
            print(f"Course: {course['name']}", file=sys.stderr)

            # Match the wanted weeks on the shallow top-level listing first;
            # walking the whole tree up front would pull every lesson body in
            # the course just to throw most of them away.
            pattern = r"\s*week\s*\d+" if args.all_weeks else rf"\s*week\s*{args.week}\b"
            week_items = [
                i for i in bb.top_level(course["id"]) if re.match(pattern, i["title"], re.I)
            ]
            if not week_items:
                target = "any week" if args.all_weeks else f"week {args.week}"
                print("  No matching weeks found.\n", file=sys.stderr)
                log_error(f"{course['name']}: no section matching {target}")
                continue

            bar = tqdm(desc="    reading lessons", unit="item", leave=False)
            weeks = bb.walk(course["id"], week_items, progress=bar)
            bar.close()

            course_dir = OUT_DIR / _slug(course["name"])
            course_dir.mkdir(parents=True, exist_ok=True)

            for week in weeks:
                print(f"  Rendering: {week.title}", file=sys.stderr)
                global CURRENT_CONTEXT
                CURRENT_CONTEXT = f"{course['name']} / {week.title}"
                base = course_dir / _slug(week.title)

                # One bad week shouldn't cost you the courses after it.
                try:
                    html = render_week_html(
                        context, week, base.with_suffix("") / "files", not args.no_readings
                    )
                    base.with_suffix(".html").write_text(html, encoding="utf-8")

                    pdf_page = context.new_page()
                    pdf_page.set_content(html, wait_until="load")
                    pdf_page.pdf(
                        path=str(base.with_suffix(".pdf")),
                        format="Letter",
                        margin={"top": "0.7in", "bottom": "0.7in", "left": "0.8in", "right": "0.8in"},
                        print_background=True,
                    )
                    pdf_page.close()
                except Exception as exc:
                    print(f"    FAILED: {exc}", file=sys.stderr)
                    log_error(f"failed to build PDF: {exc.__class__.__name__}: {exc}")
                    continue

                written.append(base.with_suffix(".pdf"))

                size_mb = base.with_suffix(".pdf").stat().st_size / 1_000_000
                imgs = html.count("data:image/")
                dead = html.count("[image not available")
                note = f", {dead} dead links" if dead else ""
                print(
                    f"    saved {base.with_suffix('.pdf').name} "
                    f"({size_mb:.1f} MB, {imgs} images{note})",
                    file=sys.stderr,
                )

        context.close()

    print(f"\nDone. {len(written)} PDF(s) in {OUT_DIR}", file=sys.stderr)
    if ERRORS:
        print(f"{len(ERRORS)} problem(s) -- see {ERROR_LOG.name}", file=sys.stderr)
    else:
        print("No problems.", file=sys.stderr)

    if args.upload_drive and written:
        folder_id = os.environ.get("GDRIVE_FOLDER_ID")
        if not folder_id:
            print("Set GDRIVE_FOLDER_ID in .env to upload.", file=sys.stderr)
            log_error("GDRIVE_FOLDER_ID not set; skipped Drive upload")
            return
        import drive_upload

        print("\nUploading to Google Drive...", file=sys.stderr)
        try:
            drive_upload.upload(written, folder_id)
        except Exception as exc:
            print(f"Drive upload failed: {exc}", file=sys.stderr)
            log_error(f"Drive upload failed: {exc}")



def main() -> None:
    global OUTCOME, CRASH_DETAIL
    try:
        _run()
        OUTCOME = "finished"
    except KeyboardInterrupt:
        OUTCOME = "interrupted with Ctrl+C"
        log_error("run was interrupted before it finished")
        raise
    except SystemExit as exc:
        OUTCOME = "finished" if not exc.code else f"exited early (code {exc.code})"
        raise
    except Exception as exc:
        OUTCOME = f"crashed: {exc.__class__.__name__}"
        CRASH_DETAIL = traceback.format_exc()
        log_error(f"crashed: {exc.__class__.__name__}: {exc}")
        raise
    finally:
        write_error_log(sys.argv)


if __name__ == "__main__":
    main()
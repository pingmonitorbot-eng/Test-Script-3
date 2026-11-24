"""
TempNumbers worker — runs inside GitHub Actions.

Logs in with a real browser, then polls the panel's own report API (/api/gen/sms) from inside the
logged-in page and pushes NEW SMS to the bot over signed HTTPS.

PUBLIC REPO RULE: the log of a public repository is public. This script therefore never prints
numbers, SMS text, cookies or the password — only counts and status words.
"""
import hashlib
import hmac
import json
import os
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

# ───────────────────────── configuration (all from environment) ─────────────────────────
BASE = os.getenv("PANEL_BASE", "https://tempnumbers.net").rstrip("/")
LOGIN_URL = f"{BASE}/login"
TARGET_URL = f"{BASE}/gen-stats"
BOT_URL = os.getenv("BOT_URL", "").rstrip("/")
SECRET = os.getenv("WORKER_SECRET", "")
PANEL_NAME = os.getenv("PANEL_NAME", "TempNumbers")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1" or not (BOT_URL and SECRET)
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "18000"))            # 5 hours
HANDOVER_SECONDS = int(os.getenv("HANDOVER_SECONDS", "300"))    # start the next run 5 min before the end
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "4"))
WINDOW_MINUTES = int(os.getenv("WINDOW_MINUTES", "20"))         # every poll asks for the last N minutes
PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", "100"))
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "30"))
RELOGIN_MARGIN = timedelta(minutes=20)                          # log in again 20 min before the session ends
RUN_ID = os.getenv("GITHUB_RUN_ID", "local")
OUTBOX_MAX = 2000


def log(message):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {message}", flush=True)


class SessionExpired(Exception):
    pass


class PollError(Exception):
    pass


# ───────────────────────── pure helpers (unit-tested) ─────────────────────────
def row_key(raw):
    """Identity of one SMS (the report has no id): time + number + text."""
    basis = f"{raw.get('receivedAt')}|{raw.get('number')}|{raw.get('content')}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def normalize_row(raw):
    number = re.sub(r"\D", "", str(raw.get("number") or ""))
    if not number:
        return None
    return {
        "number": number,
        "sender": str(raw.get("sender") or "").strip(),
        "content": str(raw.get("content") or ""),
        "received_at": str(raw.get("receivedAt") or ""),
        "range": str(raw.get("rangeName") or ""),
    }


def sign(secret, timestamp, body):
    """HMAC-SHA256 over '<timestamp>.<raw body>' — the bot verifies the same way."""
    return hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + body, hashlib.sha256).hexdigest()


def time_param(moment, style):
    if style == "iso":
        return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def parse_expiry(value, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return now + timedelta(hours=3)


class Poller:
    """Asks the report API for the last N minutes. Learns which date format the API accepts."""

    def __init__(self, api_get):
        self.api_get = api_get            # (page, path, params) -> (status, json|None)
        self.style = None
        self.limit = PAGE_LIMIT

    def _fetch_all(self, page, start, end, style):
        rows, offset = [], 0
        for _ in range(10):                                   # at most 10 pages per poll
            status, body = self.api_get(page, "/api/gen/sms", {
                "from": time_param(start, style), "to": time_param(end, style),
                "limit": self.limit, "offset": offset})
            if status in (401, 403):
                raise SessionExpired(f"HTTP {status}")
            if status != 200 or not isinstance(body, dict) or "rows" not in body:
                return rows, False
            batch = body["rows"]
            rows.extend(batch)
            offset += len(batch)
            if len(batch) < self.limit or offset >= int(body.get("total") or 0):
                break
        return rows, True

    def fetch(self, page, start, end):
        for style in ([self.style] if self.style else ["iso", "space"]):
            rows, ok = self._fetch_all(page, start, end, style)
            if not ok and self.limit > 20:                    # maybe the limit is too high
                self.limit = 20
                rows, ok = self._fetch_all(page, start, end, style)
            if ok:
                self.style = style
                return rows
        raise PollError("the report API rejected every date format")


class Outbox:
    """Rows waiting for the bot (kept if the bot is briefly unreachable)."""

    def __init__(self, limit=OUTBOX_MAX):
        self.rows, self.limit, self.dropped = [], limit, 0

    def add(self, rows):
        self.rows.extend(rows)
        overflow = len(self.rows) - self.limit
        if overflow > 0:
            self.dropped += overflow
            self.rows = self.rows[overflow:]                  # drop the oldest

    def take(self, n=200):
        return self.rows[:n]

    def done(self, n):
        self.rows = self.rows[n:]


# ───────────────────────── bot communication ─────────────────────────
def post_to_bot(path, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    response = requests.post(
        f"{BOT_URL}{path}", data=body, timeout=20,
        headers={"Content-Type": "application/json", "X-Timestamp": timestamp,
                 "X-Signature": sign(SECRET, timestamp, body)})
    return response


def dispatch_next_run():
    """Start the next 5-hour run (it waits in the queue until this one ends)."""
    repo = os.getenv("GITHUB_REPOSITORY")
    token = os.getenv("NEXT_RUN_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not repo or not token:
        log("handover: no repository/token — the 5-hourly schedule will start the next run")
        return False
    response = requests.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/{os.getenv('WORKFLOW_FILE', 'worker.yml')}/dispatches",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        json={"ref": os.getenv("GITHUB_REF_NAME", "main"),
              "inputs": {"dry_run": "1" if os.getenv("DRY_RUN", "0") == "1" else "0"}},
        timeout=20)
    log(f"handover: next run requested → HTTP {response.status_code}")
    return response.status_code == 204


# ───────────────────────── browser part ─────────────────────────
FETCH_JS = """async (url) => {
  const r = await fetch(url, {credentials: 'include', headers: {'Accept': 'application/json'}});
  return {status: r.status, text: await r.text()};
}"""


def browser_api_get(page, path, params):
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    result = page.evaluate(FETCH_JS, f"{path}?{query}" if query else path)
    try:
        body = json.loads(result["text"])
    except ValueError:
        body = None
    return result["status"], body


def login(page):
    """Real browser login (the form is protected, so a plain request cannot do it)."""
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_selector("#nl-password, input[type=password]", timeout=30000)
    page.locator("#nl-username, input[autocomplete=username], input[name=username], input[type=text]:visible"
                 ).first.fill(os.environ["PANEL_USER"])
    page.locator("#nl-password, input[type=password]").first.fill(os.environ["PANEL_PASS"])
    submit = page.locator("#nl-submit, button[type=submit]")
    if not submit.count():
        submit = page.get_by_role("button", name=re.compile(r"sign\s*in|log\s*in|login", re.I))
    with page.expect_response(lambda r: r.url.split("?")[0].endswith("/api/auth/login"), timeout=30000) as info:
        submit.first.click()
    response = info.value
    if response.status != 200:
        raise RuntimeError(f"login rejected (HTTP {response.status})")
    try:
        expires = parse_expiry(response.json().get("expiresAt"))
    except Exception:
        expires = parse_expiry(None)
    for _ in range(20):
        if "/login" not in page.url:
            break
        time.sleep(0.5)
    page.goto(TARGET_URL, wait_until="domcontentloaded")
    return expires


def login_with_retries(page, attempts=3):
    for n in range(1, attempts + 1):
        try:
            expires = login(page)
            log(f"login OK — session valid for about {int((expires - datetime.now(timezone.utc)).total_seconds() // 60)} min")
            return expires
        except Exception as exc:
            log(f"login attempt {n}/{attempts} failed: {type(exc).__name__}")
            time.sleep(15)
    raise SystemExit("login failed 3 times — stopping (check the panel password / security)")


def main():
    from playwright.sync_api import sync_playwright

    started = time.time()
    deadline = started + RUN_SECONDS
    handover_at = deadline - HANDOVER_SECONDS
    log(f"worker start — run={RUN_ID} mode={'DRY-RUN (nothing is sent)' if DRY_RUN else 'LIVE'} "
        f"poll={POLL_SECONDS}s window={WINDOW_MINUTES}min runtime={RUN_SECONDS // 60}min")
    seen, outbox, poller = {}, Outbox(), Poller(browser_api_get)
    stats = Counter()
    stop_requested = handed_over = False
    last_heartbeat = 0.0
    errors_in_a_row = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1366, "height": 900})
        page = context.new_page()
        expires = login_with_retries(page)

        while time.time() < deadline and not stop_requested:
            now_utc = datetime.now(timezone.utc)
            try:
                if expires - now_utc < RELOGIN_MARGIN:
                    log("session ends soon — logging in again")
                    expires = login_with_retries(page)

                rows = poller.fetch(page, now_utc - timedelta(minutes=WINDOW_MINUTES), now_utc + timedelta(minutes=2))
                fresh, skipped = [], 0
                for raw in rows:
                    key = row_key(raw)
                    if key in seen:
                        continue
                    seen[key] = time.time()
                    if str(raw.get("status", "ok")).lower() != "ok":
                        skipped += 1                       # only successfully received SMS are forwarded
                        continue
                    row = normalize_row(raw)
                    if row:
                        fresh.append(row)
                stats["polls"] += 1
                stats["new"] += len(fresh)
                stats["skipped_status"] += skipped
                if stats["polls"] == 1:
                    log(f"first poll: format={poller.style} limit={poller.limit} rows_in_window={len(rows)} "
                        f"fields={sorted(rows[0].keys()) if rows else '-'}")
                if fresh or stats["polls"] % 30 == 0:
                    senders = Counter(r["sender"] for r in fresh).most_common(4)
                    log(f"poll #{stats['polls']}: in_window={len(rows)} new={len(fresh)} "
                        f"non_ok_skipped={skipped} senders={senders}")
                outbox.add(fresh)
                errors_in_a_row = 0
            except SessionExpired:
                log("session rejected (401/403) — logging in again")
                expires = login_with_retries(page)
            except Exception as exc:
                errors_in_a_row += 1
                log(f"poll error ({errors_in_a_row}): {type(exc).__name__}")
                if errors_in_a_row >= 20:
                    raise SystemExit("too many poll errors in a row — stopping")
                time.sleep(min(30, 2 * errors_in_a_row))

            # forget keys older than the window (+margin)
            horizon = time.time() - (WINDOW_MINUTES + 15) * 60
            for key in [k for k, t in seen.items() if t < horizon]:
                del seen[key]

            # deliver to the bot
            if not DRY_RUN and outbox.rows:
                batch = outbox.take()
                try:
                    reply = post_to_bot("/worker/push", {"panel": PANEL_NAME, "run_id": RUN_ID, "rows": batch})
                    if reply.status_code == 200:
                        outbox.done(len(batch))
                        stats["pushed"] += len(batch)
                    else:
                        log(f"bot answered HTTP {reply.status_code} — will retry")
                except requests.RequestException as exc:
                    log(f"bot unreachable ({type(exc).__name__}) — {len(outbox.rows)} row(s) waiting")
            elif DRY_RUN:
                outbox.rows.clear()

            # heartbeat (also how the bot can force-stop this worker)
            if not DRY_RUN and time.time() - last_heartbeat >= HEARTBEAT_SECONDS:
                last_heartbeat = time.time()
                try:
                    reply = post_to_bot("/worker/heartbeat", {
                        "panel": PANEL_NAME, "run_id": RUN_ID, "polls": stats["polls"], "pushed": stats["pushed"],
                        "waiting": len(outbox.rows),
                        "session_minutes_left": int((expires - datetime.now(timezone.utc)).total_seconds() // 60)})
                    if reply.status_code == 200 and reply.json().get("stop"):
                        log("the bot asked this worker to stop")
                        stop_requested = True
                except (requests.RequestException, ValueError):
                    pass

            # start the next 5-hour run shortly before this one ends
            if not handed_over and not stop_requested and time.time() >= handover_at:
                handed_over = True
                dispatch_next_run()

            time.sleep(POLL_SECONDS * random.uniform(0.8, 1.2))

        browser.close()
    log(f"worker finished — polls={stats['polls']} new={stats['new']} pushed={stats['pushed']} "
        f"non_ok_skipped={stats['skipped_status']} dropped={outbox.dropped}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        log(str(exc))
        sys.exit(1)

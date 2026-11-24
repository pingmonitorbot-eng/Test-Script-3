#!/usr/bin/env python3
"""
TempNumbers worker — runs inside GitHub Actions.

Logs in with a real browser, then polls /api/gen/sms from inside the
logged-in page and pushes NEW SMS to the bot over signed HTTPS.

PUBLIC REPO: never print numbers, SMS text, cookies or password.
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

# ───────────────────────── configuration ─────────────────────────
BASE = os.getenv("PANEL_BASE", "https://tempnumbers.net").rstrip("/")
LOGIN_URL = f"{BASE}/login"
TARGET_URL = f"{BASE}/gen-stats"

# Test account (hardcoded for this public test repo)
PANEL_USER = os.getenv("PANEL_USER", "test_panel")
PANEL_PASS = os.getenv("PANEL_PASS", "test_panel")

BOT_URL = os.getenv("BOT_URL", "").rstrip("/")
SECRET = os.getenv("WORKER_SECRET", "")
PANEL_NAME = os.getenv("PANEL_NAME", "TempNumbers")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1" or not (BOT_URL and SECRET)

RUN_SECONDS = int(os.getenv("RUN_SECONDS", "18000"))  # 5 hours
HANDOVER_SECONDS = int(os.getenv("HANDOVER_SECONDS", "300"))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "4"))
WINDOW_MINUTES = int(os.getenv("WINDOW_MINUTES", "20"))
PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", "100"))
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "30"))
RELOGIN_MARGIN = timedelta(minutes=20)
RUN_ID = os.getenv("GITHUB_RUN_ID", "local")
OUTBOX_MAX = 2000


def log(message):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {message}", flush=True)


class SessionExpired(Exception):
    pass


class PollError(Exception):
    pass


def row_key(raw):
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
    return hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()


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
    def __init__(self, api_get):
        self.api_get = api_get
        self.style = None
        self.limit = PAGE_LIMIT

    def _fetch_all(self, page, start, end, style):
        rows, offset = [], 0
        for _ in range(10):
            status, body = self.api_get(
                page,
                "/api/gen/sms",
                {
                    "from": time_param(start, style),
                    "to": time_param(end, style),
                    "limit": self.limit,
                    "offset": offset,
                },
            )
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
            if not ok and self.limit > 20:
                self.limit = 20
                rows, ok = self._fetch_all(page, start, end, style)
            if ok:
                self.style = style
                return rows
        raise PollError("the report API rejected every date format")


class Outbox:
    def __init__(self, limit=OUTBOX_MAX):
        self.rows, self.limit, self.dropped = [], limit, 0

    def add(self, rows):
        self.rows.extend(rows)
        overflow = len(self.rows) - self.limit
        if overflow > 0:
            self.dropped += overflow
            self.rows = self.rows[overflow:]

    def take(self, n=200):
        return self.rows[:n]

    def done(self, n):
        self.rows = self.rows[n:]


def post_to_bot(path, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    return requests.post(
        f"{BOT_URL}{path}",
        data=body,
        timeout=20,
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": timestamp,
            "X-Signature": sign(SECRET, timestamp, body),
        },
    )


def dispatch_next_run():
    repo = os.getenv("GITHUB_REPOSITORY")
    token = os.getenv("NEXT_RUN_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not repo or not token:
        log("handover: no repository/token — schedule will start next run")
        return False
    response = requests.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{os.getenv('WORKFLOW_FILE', 'worker.yml')}/dispatches",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "ref": os.getenv("GITHUB_REF_NAME", "main"),
            "inputs": {"dry_run": "1" if os.getenv("DRY_RUN", "0") == "1" else "0"},
        },
        timeout=20,
    )
    log(f"handover: next run requested → HTTP {response.status_code}")
    return response.status_code == 204


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


def set_input(page, selector: str, value: str) -> None:
    page.wait_for_selector(selector, state="visible", timeout=30000)
    page.click(selector)
    page.fill(selector, "")
    page.type(selector, value, delay=20)
    page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) return false;
            const desc = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            );
            if (desc && desc.set) desc.set.call(el, val);
            else el.value = val;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return el.value;
        }""",
        [selector, value],
    )


def is_logged_in(page) -> bool:
    url = (page.url or "").lower()
    if any(
        x in url
        for x in ("/dashboard", "/gen-stats", "/cli-search", "/numbers", "/cdrs")
    ):
        return True
    try:
        if PANEL_USER and page.locator(f"text={PANEL_USER}").count() > 0:
            return True
        body = page.inner_text("body")
        if "Assigned numbers" in body or "Estimated earnings" in body:
            return True
    except Exception:
        pass
    return False


def login(page):
    """Real browser login (form is JS-protected)."""
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_selector("#nl-username, #nl-password, input[type=password]", timeout=30000)
    time.sleep(0.5)

    if page.locator("#nl-username").count():
        set_input(page, "#nl-username", PANEL_USER)
        set_input(page, "#nl-password", PANEL_PASS)
    else:
        page.locator(
            "input[autocomplete=username], input[name=username], input[type=text]:visible"
        ).first.fill(PANEL_USER)
        page.locator("input[type=password]").first.fill(PANEL_PASS)

    expires = datetime.now(timezone.utc) + timedelta(hours=3)
    submit = page.locator("button.nl-submit, #nl-submit, button[type=submit]")
    if not submit.count():
        submit = page.get_by_role("button", name=re.compile(r"sign\s*in|log\s*in|login", re.I))

    try:
        with page.expect_response(
            lambda r: "/api/auth/login" in r.url.split("?")[0], timeout=30000
        ) as info:
            submit.first.click()
        response = info.value
        log(f"login API HTTP {response.status}")
        if response.status != 200:
            raise RuntimeError(f"login rejected (HTTP {response.status})")
        try:
            data = response.json()
            if data.get("expiresAt"):
                expires = parse_expiry(data.get("expiresAt"))
        except Exception:
            pass
    except Exception as e:
        log(f"login response wait: {type(e).__name__}: {e}")
        # fallback click / Enter
        try:
            submit.first.click()
        except Exception:
            pass
        try:
            page.locator("#nl-password, input[type=password]").first.press("Enter")
        except Exception:
            pass

    for _ in range(40):
        if is_logged_in(page):
            break
        time.sleep(0.5)
    else:
        if not is_logged_in(page):
            raise RuntimeError(f"login did not leave login page (url={page.url})")

    page.goto(TARGET_URL, wait_until="domcontentloaded")
    return expires


def login_with_retries(page, attempts=3):
    for n in range(1, attempts + 1):
        try:
            expires = login(page)
            mins = int((expires - datetime.now(timezone.utc)).total_seconds() // 60)
            log(f"login OK — session ~{mins} min")
            return expires
        except Exception as exc:
            log(f"login attempt {n}/{attempts} failed: {type(exc).__name__}: {exc}")
            time.sleep(15)
    raise SystemExit("login failed 3 times — stopping")


def main():
    from playwright.sync_api import sync_playwright

    started = time.time()
    deadline = started + RUN_SECONDS
    handover_at = deadline - HANDOVER_SECONDS
    log(
        f"worker start — run={RUN_ID} mode={'DRY-RUN' if DRY_RUN else 'LIVE'} "
        f"user={PANEL_USER} poll={POLL_SECONDS}s window={WINDOW_MINUTES}min "
        f"runtime={RUN_SECONDS // 60}min"
    )
    seen, outbox, poller = {}, Outbox(), Poller(browser_api_get)
    stats = Counter()
    stop_requested = handed_over = False
    last_heartbeat = 0.0
    errors_in_a_row = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()
        page.set_default_timeout(60000)
        expires = login_with_retries(page)

        while time.time() < deadline and not stop_requested:
            now_utc = datetime.now(timezone.utc)
            try:
                if expires - now_utc < RELOGIN_MARGIN:
                    log("session ends soon — logging in again")
                    expires = login_with_retries(page)

                rows = poller.fetch(
                    page,
                    now_utc - timedelta(minutes=WINDOW_MINUTES),
                    now_utc + timedelta(minutes=2),
                )
                fresh, skipped = [], 0
                for raw in rows:
                    key = row_key(raw)
                    if key in seen:
                        continue
                    seen[key] = time.time()
                    if str(raw.get("status", "ok")).lower() != "ok":
                        skipped += 1
                        continue
                    row = normalize_row(raw)
                    if row:
                        fresh.append(row)
                stats["polls"] += 1
                stats["new"] += len(fresh)
                stats["skipped_status"] += skipped
                if stats["polls"] == 1:
                    log(
                        f"first poll: format={poller.style} limit={poller.limit} "
                        f"rows_in_window={len(rows)} "
                        f"fields={sorted(rows[0].keys()) if rows else '-'}"
                    )
                if fresh or stats["polls"] % 30 == 0:
                    senders = Counter(r["sender"] for r in fresh).most_common(4)
                    log(
                        f"poll #{stats['polls']}: in_window={len(rows)} new={len(fresh)} "
                        f"non_ok_skipped={skipped} senders={senders}"
                    )
                outbox.add(fresh)
                errors_in_a_row = 0
            except SessionExpired:
                log("session rejected (401/403) — logging in again")
                expires = login_with_retries(page)
            except Exception as exc:
                errors_in_a_row += 1
                log(f"poll error ({errors_in_a_row}): {type(exc).__name__}: {exc}")
                if errors_in_a_row >= 20:
                    raise SystemExit("too many poll errors in a row — stopping")
                time.sleep(min(30, 2 * errors_in_a_row))

            horizon = time.time() - (WINDOW_MINUTES + 15) * 60
            for key in [k for k, t in seen.items() if t < horizon]:
                del seen[key]

            if not DRY_RUN and outbox.rows:
                batch = outbox.take()
                try:
                    reply = post_to_bot(
                        "/worker/push",
                        {"panel": PANEL_NAME, "run_id": RUN_ID, "rows": batch},
                    )
                    if reply.status_code == 200:
                        outbox.done(len(batch))
                        stats["pushed"] += len(batch)
                    else:
                        log(f"bot answered HTTP {reply.status_code} — will retry")
                except requests.RequestException as exc:
                    log(
                        f"bot unreachable ({type(exc).__name__}) — "
                        f"{len(outbox.rows)} row(s) waiting"
                    )
            elif DRY_RUN:
                outbox.rows.clear()

            if not DRY_RUN and time.time() - last_heartbeat >= HEARTBEAT_SECONDS:
                last_heartbeat = time.time()
                try:
                    reply = post_to_bot(
                        "/worker/heartbeat",
                        {
                            "panel": PANEL_NAME,
                            "run_id": RUN_ID,
                            "polls": stats["polls"],
                            "pushed": stats["pushed"],
                            "waiting": len(outbox.rows),
                            "session_minutes_left": int(
                                (expires - datetime.now(timezone.utc)).total_seconds()
                                // 60
                            ),
                        },
                    )
                    if reply.status_code == 200 and reply.json().get("stop"):
                        log("the bot asked this worker to stop")
                        stop_requested = True
                except (requests.RequestException, ValueError):
                    pass

            if not handed_over and not stop_requested and time.time() >= handover_at:
                handed_over = True
                dispatch_next_run()

            time.sleep(POLL_SECONDS * random.uniform(0.8, 1.2))

        browser.close()
    log(
        f"worker finished — polls={stats['polls']} new={stats['new']} "
        f"pushed={stats['pushed']} non_ok_skipped={stats['skipped_status']} "
        f"dropped={outbox.dropped}"
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        log(str(exc))
        sys.exit(1)

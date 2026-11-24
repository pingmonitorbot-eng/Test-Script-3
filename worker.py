#!/usr/bin/env python3
"""
TempNumbers worker — GitHub Actions.

Login with Playwright, poll gen-stats by driving the SPA (Show Report) so the
panel's own X-CSRF-Token is applied. Never print numbers/SMS/password.
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

import requests

BASE = os.getenv("PANEL_BASE", "https://tempnumbers.net").rstrip("/")
LOGIN_URL = f"{BASE}/login"
TARGET_URL = f"{BASE}/gen-stats"

PANEL_USER = os.getenv("PANEL_USER", "test_panel")
PANEL_PASS = os.getenv("PANEL_PASS", "test_panel")

BOT_URL = os.getenv("BOT_URL", "").rstrip("/")
SECRET = os.getenv("WORKER_SECRET", "")
PANEL_NAME = os.getenv("PANEL_NAME", "TempNumbers")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1" or not (BOT_URL and SECRET)

RUN_SECONDS = int(os.getenv("RUN_SECONDS", "18000"))
HANDOVER_SECONDS = int(os.getenv("HANDOVER_SECONDS", "300"))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "4"))
WINDOW_MINUTES = int(os.getenv("WINDOW_MINUTES", "20"))
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


def parse_expiry(value, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return now + timedelta(hours=3)


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
        log("handover: no repository/token")
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
    log(f"handover: next run → HTTP {response.status_code}")
    return response.status_code == 204


def set_input(page, selector: str, value: str) -> None:
    page.wait_for_selector(selector, state="visible", timeout=30000)
    page.click(selector)
    page.fill(selector, "")
    page.type(selector, value, delay=15)
    page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) return;
            const desc = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value');
            if (desc && desc.set) desc.set.call(el, val);
            else el.value = val;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
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
        if "Gen. SMS" in body or "Generated SMS" in body:
            return True
    except Exception:
        pass
    return False


def login(page):
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(0.5)

    # already logged in (redirect)
    if is_logged_in(page):
        log("already logged in")
        page.goto(TARGET_URL, wait_until="domcontentloaded")
        return datetime.now(timezone.utc) + timedelta(hours=3)

    page.wait_for_selector("#nl-username, input[type=password]", timeout=30000)

    if page.locator("#nl-username").count():
        set_input(page, "#nl-username", PANEL_USER)
        set_input(page, "#nl-password", PANEL_PASS)
    else:
        page.locator("input[type=text]:visible, input[name=username]").first.fill(
            PANEL_USER
        )
        page.locator("input[type=password]").first.fill(PANEL_PASS)

    expires = datetime.now(timezone.utc) + timedelta(hours=3)
    submit = page.locator("button.nl-submit, button[type=submit]")
    if not submit.count():
        submit = page.get_by_role(
            "button", name=re.compile(r"sign\s*in|log\s*in", re.I)
        )

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
        log(f"login response: {type(e).__name__}: {e}")
        try:
            submit.first.click()
        except Exception:
            pass

    for _ in range(40):
        if is_logged_in(page):
            break
        time.sleep(0.5)
    else:
        if not is_logged_in(page):
            raise RuntimeError(f"login stuck url={page.url}")

    page.goto(TARGET_URL, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
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
            time.sleep(10)
    raise SystemExit("login failed 3 times — stopping")


def ensure_gen_stats(page):
    if "gen-stats" not in (page.url or ""):
        page.goto(TARGET_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(1)
    if not is_logged_in(page):
        raise SessionExpired("not on gen-stats / logged out")


def poll_via_ui(page, start: datetime, end: datetime):
    """
    Drive the SPA: set From/To if possible, click Show Report, capture /api/gen/sms.
    Uses the panel's own CSRF signing.
    """
    ensure_gen_stats(page)

    # Best-effort: fill datetime fields (placeholders vary)
    from_s = start.strftime("%Y-%m-%d %H:%M:%S")
    to_s = end.strftime("%Y-%m-%d %H:%M:%S")
    try:
        inputs = page.locator("input").all()
        # First two visible text/datetime-like inputs often are From / To
        filled = 0
        for inp in inputs:
            try:
                if not inp.is_visible():
                    continue
                t = (inp.get_attribute("type") or "").lower()
                ph = (inp.get_attribute("placeholder") or "").lower()
                if t in ("text", "datetime-local", "") or "date" in ph or not ph:
                    # skip number/sender search boxes
                    if "start" in ph or "sender" in ph or "exact" in ph:
                        continue
                    if filled == 0:
                        inp.fill(from_s)
                        filled = 1
                    elif filled == 1:
                        inp.fill(to_s)
                        filled = 2
                        break
            except Exception:
                continue
    except Exception as e:
        log(f"date fill soft-fail: {type(e).__name__}")

    show = page.get_by_role("button", name=re.compile(r"show\s*report", re.I))
    if not show.count():
        raise PollError("Show Report button not found")

    try:
        with page.expect_response(
            lambda r: "/api/gen/sms" in r.url and r.request.resource_type in (
                "xhr",
                "fetch",
            ),
            timeout=30000,
        ) as info:
            show.first.click()
        resp = info.value
        status = resp.status
        if status in (401, 403):
            # distinguish CSRF/auth
            try:
                body = resp.json()
                err = str(body.get("error", ""))
            except Exception:
                err = ""
            if err in ("bad_signature",):
                raise PollError(f"gen/sms bad_signature HTTP {status}")
            raise SessionExpired(f"gen/sms HTTP {status} {err}")
        if status != 200:
            raise PollError(f"gen/sms HTTP {status}")
        data = resp.json()
        if not isinstance(data, dict) or "rows" not in data:
            raise PollError("gen/sms unexpected body shape")
        return data.get("rows") or []
    except SessionExpired:
        raise
    except PollError:
        raise
    except Exception as e:
        raise PollError(f"poll ui: {type(e).__name__}: {e}") from e


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
    seen, outbox = {}, Outbox()
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

                start = now_utc - timedelta(minutes=WINDOW_MINUTES)
                end = now_utc + timedelta(minutes=2)
                rows = poll_via_ui(page, start, end)

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
                        f"first poll: rows_in_window={len(rows)} "
                        f"fields={sorted(rows[0].keys()) if rows else '-'}"
                    )
                if fresh or stats["polls"] % 15 == 0:
                    senders = Counter(r["sender"] for r in fresh).most_common(4)
                    log(
                        f"poll #{stats['polls']}: in_window={len(rows)} new={len(fresh)} "
                        f"non_ok_skipped={skipped} senders={senders}"
                    )
                outbox.add(fresh)
                errors_in_a_row = 0

            except SessionExpired as e:
                log(f"session issue ({e}) — re-login")
                expires = login_with_retries(page)
            except Exception as exc:
                errors_in_a_row += 1
                log(f"poll error ({errors_in_a_row}): {type(exc).__name__}: {exc}")
                if errors_in_a_row >= 20:
                    raise SystemExit("too many poll errors — stopping")
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
                        log(f"bot HTTP {reply.status_code} — retry later")
                except requests.RequestException as exc:
                    log(f"bot unreachable ({type(exc).__name__})")
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
                        log("bot requested stop")
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

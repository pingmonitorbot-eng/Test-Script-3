#!/usr/bin/env python3
"""
TempNumbers worker (GitHub Actions)

- Playwright login, poll gen-stats every ~15s (same idea as big-bot API panels)
- Writes NEW SMS to Mongo tempnumbers_inbox as status=pending
- First poll: if any rows → save only the LATEST; if empty → inject one TEST SMS
  so the big bot can forward and prove the pipeline works
- Runs ~5 hours, then hands over to the next workflow run

Public logs never print phone numbers or SMS body.
"""
import hashlib
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError

BASE = os.getenv("PANEL_BASE", "https://tempnumbers.net").rstrip("/")
LOGIN_URL = f"{BASE}/login"
TARGET_URL = f"{BASE}/gen-stats"

PANEL_USER = (os.getenv("PANEL_USER") or "test_panel").strip()
PANEL_PASS = (os.getenv("PANEL_PASS") or "test_panel").strip()

MONGO_URI = (os.getenv("MONGO_URI") or "").strip()
# Empty secret becomes "" and must not override default
MONGO_DB = (os.getenv("MONGO_DB") or "tech_og_bot").strip() or "tech_og_bot"
INBOX_COL = (os.getenv("INBOX_COL") or "tempnumbers_inbox").strip() or "tempnumbers_inbox"

RUN_SECONDS = int(os.getenv("RUN_SECONDS") or "18000")
HANDOVER_SECONDS = int(os.getenv("HANDOVER_SECONDS") or "300")
POLL_SECONDS = float(os.getenv("POLL_SECONDS") or "15")
RUN_ID = os.getenv("GITHUB_RUN_ID", "local")


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def event_hash(number: str, sender: str, sms: str, received_at: str) -> str:
    basis = f"{received_at}|{number}|{sender}|{sms}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def connect_inbox():
    if not MONGO_URI:
        raise SystemExit("MONGO_URI is not set — add repo Secret MONGO_URI")
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=15000)
    client.admin.command("ping")
    log(f"mongo ping OK → db={MONGO_DB!r} col={INBOX_COL!r}")
    col = client[MONGO_DB][INBOX_COL]
    col.create_index([("event_hash", ASCENDING)], unique=True)
    col.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
    return client, col


def set_input(page, selector: str, value: str) -> None:
    page.wait_for_selector(selector, state="visible", timeout=30000)
    page.click(selector)
    page.fill(selector, "")
    page.type(selector, value, delay=20)
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
            el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
        }""",
        [selector, value],
    )


def is_logged_in(page) -> bool:
    url = (page.url or "").lower()
    if any(x in url for x in ("/dashboard", "/gen-stats", "/cli-search", "/numbers")):
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
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(0.5)
    if is_logged_in(page):
        return
    page.wait_for_selector("#nl-username", timeout=30000)
    set_input(page, "#nl-username", PANEL_USER)
    set_input(page, "#nl-password", PANEL_PASS)
    try:
        with page.expect_response(
            lambda r: "/api/auth/login" in r.url.split("?")[0], timeout=30000
        ):
            page.locator("button.nl-submit, button[type=submit]").first.click()
    except Exception as e:
        log(f"login response wait: {type(e).__name__}")
        try:
            page.locator("#nl-password").press("Enter")
        except Exception:
            pass
    for _ in range(40):
        if is_logged_in(page):
            return
        time.sleep(0.5)
    raise RuntimeError(f"login stuck url={page.url}")


def login_with_retries(page, attempts=3):
    for n in range(1, attempts + 1):
        try:
            login(page)
            log(f"login OK (attempt {n})")
            return
        except Exception as e:
            log(f"login attempt {n}/{attempts} failed: {type(e).__name__}: {e}")
            time.sleep(10)
    raise SystemExit("login failed 3 times")


def poll_rows(page):
    def match(r):
        return "/api/gen/sms" in r.url and r.request.resource_type in ("xhr", "fetch")

    try:
        with page.expect_response(match, timeout=45000) as info:
            page.goto(TARGET_URL, wait_until="domcontentloaded")
        resp = info.value
    except Exception:
        with page.expect_response(match, timeout=45000) as info:
            page.reload(wait_until="domcontentloaded")
        resp = info.value

    if resp.status in (401, 403):
        raise RuntimeError(f"session HTTP {resp.status}")
    if resp.status != 200:
        raise RuntimeError(f"gen/sms HTTP {resp.status}")
    data = resp.json()
    if not isinstance(data, dict) or "rows" not in data:
        raise RuntimeError("unexpected gen/sms shape")
    return data.get("rows") or []


def normalize(raw):
    number = re.sub(r"\D", "", str(raw.get("number") or ""))
    if len(number) < 7:
        return None
    if str(raw.get("status", "ok")).lower() not in ("ok", "success", ""):
        return None
    sms = str(raw.get("content") or "").strip()
    if not sms:
        return None
    sender = str(raw.get("sender") or "").strip()
    received_at = str(raw.get("receivedAt") or "").strip()
    return {
        "number": number,
        "sender": sender,
        "sms": sms,
        "received_at": received_at,
        "range_name": str(raw.get("rangeName") or "").strip(),
        "event_hash": event_hash(number, sender, sms, received_at),
    }


def make_test_sms_row():
    now = datetime.now(timezone.utc)
    received_at = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    code = f"{int(now.timestamp()) % 1000000:06d}"
    number = "8801700000001"
    sender = "SYSTEM"
    sms = f"TempNumbers pipeline TEST. Your code is {code}. Do not share."
    return {
        "number": number,
        "sender": sender,
        "sms": sms,
        "received_at": received_at,
        "range_name": "TEST",
        "event_hash": event_hash(number, sender, sms, received_at),
        "is_test": True,
    }


def insert_pending(col, row):
    doc = {
        "number": row["number"],
        "sender": row["sender"],
        "sms": row["sms"],
        "received_at": row["received_at"],
        "range_name": row.get("range_name") or "",
        "event_hash": row["event_hash"],
        "status": "pending",
        "source": "tempnumbers",
        "run_id": RUN_ID,
        "created_at": datetime.now(timezone.utc),
    }
    if row.get("is_test"):
        doc["is_test"] = True
    try:
        col.insert_one(doc)
        return True
    except DuplicateKeyError:
        return False


def dispatch_next_run():
    import requests

    repo = os.getenv("GITHUB_REPOSITORY")
    token = os.getenv("NEXT_RUN_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not repo or not token:
        log("handover skipped (no token/repo)")
        return False
    r = requests.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{os.getenv('WORKFLOW_FILE', 'worker.yml')}/dispatches",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"ref": os.getenv("GITHUB_REF_NAME", "main")},
        timeout=20,
    )
    log(f"handover → HTTP {r.status_code}")
    return r.status_code == 204


def main():
    from playwright.sync_api import sync_playwright

    started = time.time()
    deadline = started + RUN_SECONDS
    handover_at = deadline - HANDOVER_SECONDS
    handed_over = False
    first_poll_done = False

    client, inbox = connect_inbox()
    log(
        f"worker start run={RUN_ID} user={PANEL_USER} "
        f"db={MONGO_DB}.{INBOX_COL} poll={POLL_SECONDS}s "
        f"runtime={RUN_SECONDS // 60}min"
    )

    inserted = polls = errors = 0

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
        login_with_retries(page)

        while time.time() < deadline:
            try:
                rows = poll_rows(page)
                polls += 1
                new_n = 0

                if not first_poll_done:
                    first_poll_done = True
                    normalized = [n for n in (normalize(r) for r in rows) if n]
                    if normalized:
                        normalized.sort(
                            key=lambda r: r.get("received_at") or "", reverse=True
                        )
                        latest = normalized[0]
                        if insert_pending(inbox, latest):
                            new_n = 1
                            inserted += 1
                            log(f"first poll: api_rows={len(rows)} → saved LATEST only (1)")
                        else:
                            log(f"first poll: api_rows={len(rows)} → latest already in inbox")
                    else:
                        test_row = make_test_sms_row()
                        if insert_pending(inbox, test_row):
                            new_n = 1
                            inserted += 1
                            log("first poll: no panel data → injected TEST SMS")
                        else:
                            log("first poll: test SMS insert skipped (dup)")
                else:
                    for raw in rows:
                        row = normalize(raw)
                        if not row:
                            continue
                        if insert_pending(inbox, row):
                            new_n += 1
                            inserted += 1
                    if new_n or polls % 10 == 0:
                        log(
                            f"poll #{polls}: api_rows={len(rows)} "
                            f"inserted_new={new_n} total_inserted={inserted}"
                        )

                errors = 0
            except Exception as e:
                errors += 1
                log(f"poll error ({errors}): {type(e).__name__}: {e}")
                if errors >= 3:
                    try:
                        login_with_retries(page)
                        errors = 0
                    except SystemExit:
                        raise
                    except Exception as le:
                        log(f"re-login failed: {le}")
                if errors >= 25:
                    raise SystemExit("too many poll errors")
                time.sleep(min(45, 3 * errors))

            if not handed_over and time.time() >= handover_at:
                handed_over = True
                dispatch_next_run()

            time.sleep(POLL_SECONDS * random.uniform(0.9, 1.1))

        browser.close()

    client.close()
    log(f"worker finished polls={polls} inserted={inserted}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        log(str(e))
        sys.exit(1)

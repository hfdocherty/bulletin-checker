#!/usr/bin/env python3
"""
Triumph of the Holy Cross Parish — Bulletin Checker
Constructs the bulletin PDF URL directly from the current Sunday's date.
Bulletins are hosted at container.parishesonline.com — no scraping needed.
If names are found, sends an Apple Push Notification (APNs) to all devices.
"""

import os
import sys
import io
import json
import time
import hashlib
import requests
import jwt
from datetime import datetime, timezone, timedelta
from pypdf import PdfReader

# ── Configuration ────────────────────────────────────────────────────────────

# Parish bulletin base URL — pattern: YYYYMMDD + B.pdf
BULLETIN_BASE_URL = "https://container.parishesonline.com/bulletins/14/1225/"

SEARCH_NAMES = ["Hugh Docherty", "Docherty", "Teschke"]
STATE_FILE   = "last_bulletin.json"

# APNs settings — all come from GitHub Secrets
APNS_KEY_ID    = os.environ["APNS_KEY_ID"]
APNS_TEAM_ID   = os.environ["APNS_TEAM_ID"]
APNS_BUNDLE_ID = os.environ["APNS_BUNDLE_ID"]
APNS_AUTH_KEY  = os.environ["APNS_AUTH_KEY"]

# Supports one token or multiple tokens separated by newlines in the secret
DEVICE_TOKENS = [
    t.strip()
    for t in os.environ["APNS_DEVICE_TOKEN"].split("\n")
    if t.strip()
]

APNS_PRODUCTION = os.environ.get("APNS_PRODUCTION", "false").lower() == "true"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BulletinChecker/1.0)"
}

# ── Date helpers ──────────────────────────────────────────────────────────────

def get_this_sunday() -> datetime:
    """Return today if Sunday, otherwise the most recent past Sunday."""
    today = datetime.now(timezone.utc)
    days_since_sunday = (today.weekday() + 1) % 7
    return today - timedelta(days=days_since_sunday)


def build_bulletin_url(sunday: datetime) -> str:
    date_str = sunday.strftime("%Y%m%d")
    return f"{BULLETIN_BASE_URL}{date_str}B.pdf"


# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── PDF helpers ───────────────────────────────────────────────────────────────

def download_pdf(url: str) -> bytes | None:
    """Download the bulletin PDF. Returns None if not posted yet (404)."""
    print(f"Trying bulletin URL: {url}")
    resp = requests.get(url, headers=HEADERS, timeout=30)

    if resp.status_code == 404:
        print("Bulletin not posted yet (404). Will retry next run.")
        return None

    resp.raise_for_status()
    print(f"Downloaded {len(resp.content):,} bytes.")
    return resp.content


def extract_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "".join(page.extract_text() or "" for page in reader.pages)


def search_names(text: str) -> list[str]:
    text_lower = text.lower()
    return [name for name in SEARCH_NAMES if name.lower() in text_lower]


# ── APNs ──────────────────────────────────────────────────────────────────────

def build_apns_jwt() -> str:
    return jwt.encode(
        {"iss": APNS_TEAM_ID, "iat": int(time.time())},
        APNS_AUTH_KEY,
        algorithm="ES256",
        headers={"kid": APNS_KEY_ID},
    )


def send_push(matched_names: list[str], bulletin_url: str, device_token: str):
    """Send a push notification to a single device token."""
    import httpx

    host = "api.push.apple.com" if APNS_PRODUCTION else "api.sandbox.push.apple.com"
    names_str = " & ".join(matched_names)

    payload = {
        "aps": {
            "alert": {
                "title": "Mass Intention Found 🙏",
                "body": f"{names_str} listed in this week's bulletin.",
            },
            "sound": "default",
            "badge": 1,
        },
        "bulletin_url": bulletin_url,
    }

    headers = {
        "authorization":  f"bearer {build_apns_jwt()}",
        "apns-topic":     APNS_BUNDLE_ID,
        "apns-push-type": "alert",
        "apns-priority":  "10",
    }

    print(f"  Sending to token: {device_token[:12]}…")
    with httpx.Client(http2=True) as client:
        resp = client.post(
            f"https://{host}/3/device/{device_token}",
            json=payload,
            headers=headers,
            timeout=30,
        )

    if resp.status_code == 200:
        print("  ✅ Sent!")
    else:
        print(f"  ❌ APNs error {resp.status_code}: {resp.text}")


def send_push_to_all(matched_names: list[str], bulletin_url: str):
    """Send push notifications to every registered device."""
    print(f"\nSending push to {len(DEVICE_TOKENS)} device(s)…")
    for token in DEVICE_TOKENS:
        send_push(matched_names, bulletin_url, token)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"Bulletin Checker — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    # 1. Work out this Sunday's bulletin URL
    sunday       = get_this_sunday()
    bulletin_url = build_bulletin_url(sunday)
    print(f"This Sunday: {sunday.strftime('%Y-%m-%d')}")
    print(f"Bulletin URL: {bulletin_url}\n")

    # 2. Skip if we already processed this exact bulletin
    state = load_state()
    if state.get("last_checked_url") == bulletin_url:
        print("Already checked this bulletin. Nothing to do.")
        return

    # 3. Download the PDF (may not be posted yet)
    pdf_bytes = download_pdf(bulletin_url)
    if pdf_bytes is None:
        return

    # 4. Skip if content is identical to last run
    bulletin_hash = hashlib.md5(pdf_bytes).hexdigest()
    if state.get("last_checked_hash") == bulletin_hash:
        print("Bulletin content unchanged. Nothing to do.")
        return

    # 5. Extract text and search
    text = extract_text(pdf_bytes)
    print(f"Extracted {len(text):,} characters of text.\n")

    found = search_names(text)

    if found:
        print(f"🎯 Match found: {found}")
        send_push_to_all(found, bulletin_url)
    else:
        print(f"No matches for: {SEARCH_NAMES}")

    # 6. Save state
    save_state({
        "last_checked_url":  bulletin_url,
        "last_checked_hash": bulletin_hash,
        "last_checked_at":   datetime.now(timezone.utc).isoformat(),
        "names_found":       found,
    })

    print("\nDone.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Triumph of the Holy Cross Parish — Bulletin Checker
Checks the latest Sunday bulletin PDF for specific names.
If found, sends an Apple Push Notification (APNs).
"""

import os
import sys
import json
import time
import hashlib
import requests
import jwt
from datetime import datetime, timezone
from bs4 import BeautifulSoup

# ── Configuration ────────────────────────────────────────────────────────────

BULLETIN_URL   = "https://triumphoftheholycrosspgh.org/bulletins"
SEARCH_NAMES   = ["Hugh Docherty", "Docherty", "Teschke"]
STATE_FILE     = "last_bulletin.json"   # tracks the last bulletin we checked

# APNs settings — all come from GitHub Secrets
APNS_KEY_ID    = os.environ["APNS_KEY_ID"]        # 10-char key ID from Apple
APNS_TEAM_ID   = os.environ["APNS_TEAM_ID"]       # 10-char team ID from Apple
APNS_BUNDLE_ID = os.environ["APNS_BUNDLE_ID"]     # e.g. com.yourname.masschedule
APNS_AUTH_KEY  = os.environ["APNS_AUTH_KEY"]      # full .p8 key contents
DEVICE_TOKEN   = os.environ["APNS_DEVICE_TOKEN"]  # your iPhone's device token

# Set to True for production (App Store / TestFlight), False for development
APNS_PRODUCTION = os.environ.get("APNS_PRODUCTION", "false").lower() == "true"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load the saved state (last bulletin URL checked)."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    """Persist state so we don't re-alert on the same bulletin."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_latest_bulletin_url() -> str | None:
    """
    Fetch the bulletins page and return the URL of the most recent PDF.
    Returns None if no PDF link is found.
    """
    print(f"Fetching bulletins page: {BULLETIN_URL}")
    resp = requests.get(BULLETIN_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Look for all <a> tags whose href ends in .pdf
    pdf_links = [
        a["href"] for a in soup.find_all("a", href=True)
        if a["href"].lower().endswith(".pdf")
    ]

    if not pdf_links:
        print("No PDF links found on the bulletins page.")
        return None

    # The first PDF link is typically the most recent one
    latest = pdf_links[0]

    # Make absolute if relative
    if latest.startswith("/"):
        latest = "https://triumphoftheholycrosspgh.org" + latest
    elif not latest.startswith("http"):
        latest = "https://triumphoftheholycrosspgh.org/" + latest

    print(f"Latest bulletin PDF: {latest}")
    return latest


def download_pdf(url: str) -> bytes:
    """Download a PDF and return its raw bytes."""
    print(f"Downloading PDF...")
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.content


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract all text from a PDF using pypdf."""
    import io
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    return text


def search_for_names(text: str) -> list[str]:
    """Return a list of names that were found in the text."""
    found = []
    text_lower = text.lower()
    for name in SEARCH_NAMES:
        if name.lower() in text_lower:
            found.append(name)
    return found


def build_apns_jwt() -> str:
    """Build a signed JWT token for APNs authentication."""
    payload = {
        "iss": APNS_TEAM_ID,
        "iat": int(time.time()),
    }
    token = jwt.encode(
        payload,
        APNS_AUTH_KEY,
        algorithm="ES256",
        headers={"kid": APNS_KEY_ID},
    )
    return token


def send_push_notification(matched_names: list[str], bulletin_url: str):
    """
    Send an APNs push notification listing the matched names.
    Uses HTTP/2 via the requests library with the httpx backend.
    """
    import httpx

    host = (
        "api.push.apple.com"
        if APNS_PRODUCTION
        else "api.sandbox.push.apple.com"
    )

    names_str = " & ".join(matched_names)
    notification_payload = {
        "aps": {
            "alert": {
                "title": "Mass Intention Found 🙏",
                "body": f"{names_str} found in this week's bulletin.",
            },
            "sound": "default",
            "badge": 1,
        },
        "bulletin_url": bulletin_url,
    }

    jwt_token = build_apns_jwt()

    headers = {
        "authorization":  f"bearer {jwt_token}",
        "apns-topic":     APNS_BUNDLE_ID,
        "apns-push-type": "alert",
        "apns-priority":  "10",
    }

    url = f"https://{host}/3/device/{DEVICE_TOKEN}"

    print(f"Sending push notification to APNs ({host})...")
    with httpx.Client(http2=True) as client:
        resp = client.post(
            url,
            json=notification_payload,
            headers=headers,
            timeout=30,
        )

    if resp.status_code == 200:
        print("✅ Push notification sent successfully!")
    else:
        print(f"❌ APNs error {resp.status_code}: {resp.text}")
        sys.exit(1)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"Bulletin Checker — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    # 1. Get the latest bulletin URL
    bulletin_url = get_latest_bulletin_url()
    if not bulletin_url:
        print("Nothing to check. Exiting.")
        return

    # 2. Check if we've already processed this bulletin
    state = load_state()
    if state.get("last_checked_url") == bulletin_url:
        print("This bulletin has already been checked. No action needed.")
        return

    # 3. Download and extract text
    pdf_bytes = download_pdf(bulletin_url)
    bulletin_hash = hashlib.md5(pdf_bytes).hexdigest()

    if state.get("last_checked_hash") == bulletin_hash:
        print("Bulletin content unchanged since last check. No action needed.")
        return

    text = extract_text_from_pdf(pdf_bytes)
    print(f"Extracted {len(text)} characters of text from PDF.\n")

    # 4. Search for names
    found = search_for_names(text)

    if found:
        print(f"🎯 Match found! Names: {found}")
        send_push_notification(found, bulletin_url)
    else:
        print(f"No matches found for: {SEARCH_NAMES}")

    # 5. Save state regardless of match (so we don't recheck same bulletin)
    save_state({
        "last_checked_url":  bulletin_url,
        "last_checked_hash": bulletin_hash,
        "last_checked_at":   datetime.now(timezone.utc).isoformat(),
        "names_found":       found,
    })

    print("\nDone.")


if __name__ == "__main__":
    main()

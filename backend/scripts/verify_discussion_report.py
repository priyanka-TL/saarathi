#!/usr/bin/env python
"""Prove a Capture Discussion report PDF is actually populated.

WHY THIS EXISTS
===============
The empty-PDF bug was invisible to every check the application can make. Mitra
returned a story id, created a StoryMedia row, answered GET /api/get-story/
with 200, and served a downloadable file at a valid public_url -- and the file
was blank, because get_html_from_template returned "" and Gotenberg rendered
that as an empty page with a 200. No exception, no log line, no failing status.

The only way to catch it is to download the PDF and look inside. That is this
script. Run it after any change to capture_discussion's finalisation settings
(`finalize_path`, `finalize_as_guest`) or after a Mitra-side PDF template
change, against a real completed session.

USAGE
=====
    python scripts/verify_discussion_report.py --session <mitra_session_id>

Reads MITRA_BASE_URL / MITRA_ORIGIN_URL / MITRA_USER_AGENT from the
environment (or .env) exactly as the app does, so it talks to whatever
environment the app is pointed at. Exits 0 when the report is populated,
1 when it is not, 2 when the story or PDF is missing entirely.

Requires pypdf (test/dev dependency -- deliberately not imported by the app).
"""
from __future__ import annotations

import argparse
import io
import os
import sys

import requests

# The chaupal keys save_chaupal_report writes. 'location' is the tell: the v2
# generic path treats location as a Story column and never puts it in
# other_params, so its absence means the discussion went through the wrong
# Mitra pipeline and the PDF will be blank no matter what it contains.
CHAUPAL_KEYS = (
    "challenges_faced", "solutions_discussed", "user_name", "location",
    "organization", "participants_count", "discussion_date", "pri_member",
    "school_representative", "remarks", "flow",
)


def _settings() -> tuple[str, dict[str, str]]:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    base_url = os.getenv("MITRA_BASE_URL", "").rstrip("/")
    if not base_url:
        sys.exit("MITRA_BASE_URL is not set")

    # Origin is a credential (Mitra gates admission on it) -- never printed.
    headers = {
        "Origin": os.getenv("MITRA_ORIGIN_URL", ""),
        "User-Agent": os.getenv("MITRA_USER_AGENT", "Mozilla/5.0"),
        "Accept": "application/json, text/plain, */*",
    }
    return base_url, headers


def _fetch_story(base_url: str, headers: dict[str, str], session_id: str) -> dict:
    resp = requests.get(
        f"{base_url}/api/get-story/", params={"session": session_id},
        headers=headers, timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        sys.exit(f"[FAIL] no story exists for session {session_id}")
    return results[0]


def _pdf_url(story: dict) -> str:
    for entry in story.get("story_media") or []:
        if entry.get("media_type") == "application/pdf" and entry.get("public_url"):
            return entry["public_url"]
    sys.exit("[FAIL] the story has no application/pdf entry in story_media")


def _extract_text(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("pypdf is required: pip install pypdf")

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _normalise(text: str) -> str:
    """Gotenberg's line wrapping inserts newlines mid-sentence, so compare on
    whitespace-collapsed, case-folded text rather than raw substrings."""
    return " ".join(text.split()).casefold()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="Mitra session id")
    args = parser.parse_args()

    base_url, headers = _settings()
    story = _fetch_story(base_url, headers, args.session)
    other = story.get("other_params") or {}

    print(f"story id      : {story.get('id')}")
    print(f"stage         : {story.get('stage')}")
    print(f"title         : {story.get('title')}")

    failures: list[str] = []

    missing_keys = [k for k in CHAUPAL_KEYS if k not in other]
    if missing_keys:
        failures.append(
            f"other_params is missing {missing_keys} -- this is not the chaupal "
            f"shape, so the discussion finalised through the wrong Mitra "
            f"pipeline (expected v1 /api/end-story/ with finalize_as_guest)"
        )

    pdf_url = _pdf_url(story)
    print(f"pdf           : {pdf_url.split('?', 1)[0]}")

    pdf_resp = requests.get(pdf_url, headers={"User-Agent": headers["User-Agent"]}, timeout=60)
    pdf_resp.raise_for_status()
    if not pdf_resp.content.startswith(b"%PDF"):
        sys.exit("[FAIL] the downloaded file is not a PDF")

    text = _normalise(_extract_text(pdf_resp.content))
    print(f"pdf bytes     : {len(pdf_resp.content)}")
    print(f"extracted text: {len(text)} chars")

    if not text:
        failures.append(
            "the PDF contains NO extractable text -- this is the blank-report "
            "symptom (empty HTML rendered by Gotenberg as an empty page)"
        )

    # Every field the MOM report is supposed to render.
    expected: list[tuple[str, str]] = [("title", story.get("title") or "")]
    for key in ("location", "discussion_date", "organization", "user_name"):
        if other.get(key):
            expected.append((key, str(other[key])))
    for i, challenge in enumerate(other.get("challenges_faced") or []):
        expected.append((f"challenges_faced[{i}]", str(challenge)))
    for i, solution in enumerate(other.get("solutions_discussed") or []):
        expected.append((f"solutions_discussed[{i}]", str(solution)))

    for label, value in expected:
        # Dates are reformatted to dd/mm/yyyy in the report, and long
        # challenge text may be chunked across pages, so match on a distinctive
        # leading slice rather than the whole string.
        needle = _normalise(value)[:60]
        if not needle:
            continue
        if needle in text:
            print(f"  ok      {label}")
        elif label == "discussion_date":
            print(f"  skipped {label} (reformatted by the report; check by eye)")
        else:
            failures.append(f"{label} is not present in the PDF: {value[:80]!r}")

    if failures:
        print("\n[FAIL] this report is not fully populated:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\n[OK] the report contains the title, metadata, every challenge and every solution")
    return 0


if __name__ == "__main__":
    sys.exit(main())

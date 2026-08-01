#!/usr/bin/env python
"""GATE C -- drives the Flask app and the FastAPI app through identical
requests and diffs (status code, body, X-Request-ID presence).

This is the strongest single check in the migration: it exercises every mapped
error branch, both error envelopes, the bare-array responses and the 202
variants, and compares what a client would actually receive.

Run both apps first:
    cd saarathi-poc      && .venv/bin/python app.py                   # :5000
    cd saarathi-poc-new/backend && make run                           # :8000

Then:
    python scripts/parity_check.py [--flask URL] [--fastapi URL]

The two apps read DIFFERENT databases by design, so list endpoints are
compared by SHAPE (key sets and value types) rather than by content. Exact
equality is required everywhere else.

Exit code 0 = parity holds (allowing for the documented divergences), 1 = drift.
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

ABSENT_UUID = "11111111-1111-1111-1111-111111111111"

CASES = [
    # (method, path, json body or None, compare-by-shape?)
    ("GET", "/api/agents", None, False),
    ("GET", "/api/conversations?limit=20", None, True),
    ("GET", "/api/conversations?limit=abc", None, False),
    ("GET", "/api/conversations?limit=999", None, True),   # clamps to 20
    ("GET", "/api/conversations?limit=0", None, True),     # clamps to 1
    ("GET", f"/api/conversations/{ABSENT_UUID}/messages", None, False),
    ("GET", f"/api/sessions/{ABSENT_UUID}", None, False),
    ("GET", f"/api/sessions/{ABSENT_UUID}/report", None, False),
    ("POST", f"/api/sessions/{ABSENT_UUID}/resume", None, False),
    ("POST", f"/api/sessions/{ABSENT_UUID}/abandon", None, False),
    ("POST", f"/api/sessions/{ABSENT_UUID}/finalize", None, False),
    ("POST", "/api/chat", {}, False),
    ("POST", "/api/chat", {"not_a_message": 1}, False),
    ("POST", "/api/chat", {"message": "hi", "conversation_id": "not-a-uuid"}, False),
    ("POST", "/api/chat", {"message": "hi", "agent_key": "does_not_exist"}, False),
    # Admin surface. With SAARTHI_ADMIN_ENABLED=0 every one of these is
    # 404 {"error": "Not Found"} -- the bare envelope, NOT the standard one.
    ("GET", "/api/agents/record_stories", None, False),
    ("PATCH", "/api/agents/record_stories", {"status": "enabled"}, False),
    ("GET", "/api/agents/record_stories/config/versions", None, False),
    ("POST", "/api/agents/record_stories/config", {"x": 1}, False),
    ("POST", "/api/agents/record_stories/config/1/activate", None, False),
    ("POST", "/api/agents/reload", None, False),
    ("GET", "/api/tools", None, False),
]

# Malformed UUID path segments. Flask's <uuid:...> converter failed to MATCH,
# so Werkzeug answered with an HTML 404 page; FastAPI answers 404 with the
# app's own JSON envelope. Same status, better body -- compared on status only.
STATUS_ONLY = [
    ("GET", "/api/sessions/not-a-uuid", None),
    ("GET", "/api/conversations/xyz/messages", None),
]

VOLATILE = {
    "request_id", "conversation_id", "id", "last_message_at", "created_at",
    "started_at", "last_activity_at", "finalized_at", "ended_at", "updated_at",
}


def scrub(obj):
    if isinstance(obj, dict):
        return {k: ("<volatile>" if k in VOLATILE else scrub(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(i) for i in obj]
    return obj


def shape(obj):
    """Key sets and value types, ignoring content."""
    if isinstance(obj, dict):
        return {k: shape(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [shape(obj[0])] if obj else []
    return type(obj).__name__


def call(base, method, path, body):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw, status, headers = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw, status, headers = e.read(), e.code, dict(e.headers)
    try:
        parsed = scrub(json.loads(raw))
    except Exception:
        parsed = f"<non-json {len(raw)}b>"
    has_rid = any(k.lower() == "x-request-id" for k in headers)
    return status, parsed, has_rid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flask", default="http://127.0.0.1:5000")
    ap.add_argument("--fastapi", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    same = diff = 0

    for method, path, body, by_shape in CASES:
        fs, fb, fh = call(args.flask, method, path, body)
        ns, nb, nh = call(args.fastapi, method, path, body)
        matched = fs == ns and (fb == nb or (by_shape and shape(fb) == shape(nb)))
        label = f"{method:6}{path:56}"
        if matched and fh == nh:
            print(f"  OK   {label} [{fs}]{'  (shape)' if by_shape else ''}")
            same += 1
        else:
            diff += 1
            print(f"  DIFF {label}")
            print(f"       flask   [{fs}] rid={fh} {json.dumps(fb)[:200]}")
            print(f"       fastapi [{ns}] rid={nh} {json.dumps(nb)[:200]}")

    for method, path, body in STATUS_ONLY:
        fs, _fb, _fh = call(args.flask, method, path, body)
        ns, nb, _nh = call(args.fastapi, method, path, body)
        if fs == ns:
            print(f"  OK   {method:6}{path:56} [{fs}]  (status only -- HTML 404 -> JSON 404)")
            same += 1
        else:
            diff += 1
            print(f"  DIFF {method:6}{path:56} flask=[{fs}] fastapi=[{ns}] {json.dumps(nb)[:120]}")

    total = len(CASES) + len(STATUS_ONLY)
    print(f"\n{same} identical, {diff} different, {total} cases")
    return 1 if diff else 0


if __name__ == "__main__":
    raise SystemExit(main())

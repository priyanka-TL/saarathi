"""MITRA_ENABLED must never run under the Flask/Werkzeug reloader."""

from __future__ import annotations

from app.core.runtime import resolve_reloader


def test_reloader_defaults_on_when_mitra_disabled():
    assert resolve_reloader(0) is True


def test_reloader_is_forced_off_when_mitra_enabled():
    assert resolve_reloader(1) is False


def test_reloader_defaults_on_when_mitra_falsy_string_absent():
    assert resolve_reloader(None) is True

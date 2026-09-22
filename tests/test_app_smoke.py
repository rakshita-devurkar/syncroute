"""Smoke tests that actually execute the Streamlit script.

These run the app through Streamlit's own test harness, so a render-time
exception fails the suite rather than surfacing only in a browser. Forced into
fixture mode so the suite never spends money or needs a key.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest


@pytest.fixture
def app(tmp_path, monkeypatch):
    # Empty key -> Settings.has_api_key is False -> the app defaults to fixture
    # mode and never offers live. Keeps the suite offline and free.
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("SYNCROUTE_DB_PATH", str(tmp_path / "smoke.db"))
    monkeypatch.chdir(ROOT)
    instance = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    return instance


def test_app_renders_without_exceptions(app):
    app.run()
    assert not app.exception, f"the app raised: {app.exception}"
    assert any("SyncRoute" in str(t.value) for t in app.title)


def test_fixture_mode_is_announced(app):
    app.run()
    warnings = " ".join(str(w.value) for w in app.warning)
    assert "FIXTURE MODE" in warnings
    errors = " ".join(str(e.value) for e in app.error)
    assert "synthetic" in errors.lower(), "the synthetic-data banner should be visible"


def test_routing_all_demo_scenarios_renders_the_inbox(app):
    app.run()
    button = next(b for b in app.button if "Route all demo scenarios" in b.label)
    button.click().run()

    assert not app.exception, f"routing raised: {app.exception}"
    assert len(app.dataframe) >= 1, "the inbox table should render after routing"


def test_evaluation_view_renders(app):
    app.run()
    assert not app.exception
    # Either real results render, or the honest placeholder appears.
    text = " ".join(
        [str(w.value) for w in app.warning]
        + [str(s.value) for s in app.success]
        + [str(c.value) for c in app.caption]
    )
    assert text, "the evaluation view produced no output"

"""0.4.105: Calendars is its own House tab (peer of Connections), not nested under Connections."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "custom_components" / "patrimony_collection" / "www" / "index.html").read_text(encoding="utf-8")


def test_calendars_is_top_level_tab_after_connections():
    assert 'data-house-sub="conn"' in HTML
    assert 'data-house-sub="calendars"' in HTML
    assert 'data-house-sub="pushlog"' in HTML
    assert HTML.find('id="sub_conn"') < HTML.find('id="sub_calendars"') < HTML.find('id="sub_pushlog"')
    assert ">Calendars<" in HTML
    assert 'id="soon_calendars" hidden' in HTML
    assert HTML.find('id="soon_conn"') < HTML.find('id="soon_calendars"') < HTML.find('id="soon_pushlog"')


def test_calendars_tab_state_and_load_wired():
    assert 'name === "calendars"' in HTML
    assert 'which === "calendars"' in HTML
    assert 'v === "conn" || v === "calendars"' in HTML
    assert "loadCalendars()" in HTML
    assert 'sub_calendars' in HTML and 'soon_calendars' in HTML


def test_calendars_content_unchanged_from_0104():
    assert 'box("Show in app", "expose")' in HTML
    assert "Show in app as" in HTML
    assert "No role" in HTML and "Guest stays" in HTML and "Birthdays" in HTML
    assert (
        "Show in app is what the phone lists. Guest stays get priority rows in Week and Month; "
        "Birthdays start hidden on the phone."
    ) in HTML
    assert "/api/patrimony_collection/editor/calendars" in HTML
    conn = HTML.find('id="soon_conn"')
    conn_end = HTML.find("</section>", conn)
    cal = HTML.find('id="soon_calendars"')
    assert conn < conn_end < cal

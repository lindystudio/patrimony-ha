"""0.4.123: HA panel Soon tab notes list (1.2.5 copy, owner list path).

Source-contract tests, same style as test_panel_calendars_tab / test_contacts.
Fixture titles are neutral (Heating, Parking). Never Alarm codes.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL = (ROOT / "custom_components" / "patrimony_collection" / "www" / "index.html").read_text(
    encoding="utf-8"
)
ADDON = (ROOT / "addons" / "patrimony_collection" / "static" / "index.html").read_text(
    encoding="utf-8"
)

# 1.2.5 copy, verbatim.
EMPTY_TITLE = "No notes yet"
EMPTY_BODY = "Notes added here are shared with everyone in this home."
ADD_NOTE = "Add Note"
UNTITLED = "Untitled"
SAVE_FAILED = "Couldn't save this note. Try again in a moment."
TOO_MANY = "This home has 50 notes, the most it can hold. Remove one to add another."
TITLE_LIMIT = "Titles can be up to 80 characters."
BODY_LIMIT = "Notes can be up to 8,000 characters."
DELETE_TITLE = "Delete this note?"
DELETE_BODY = "It will be removed for everyone in this home."
CONFLICT = "Someone else changed this note first. Your edit wasn't saved, and the latest version is shown."


def test_copy_125_strings_verbatim_in_panel():
    for s in (
        EMPTY_TITLE,
        EMPTY_BODY,
        ADD_NOTE,
        UNTITLED,
        SAVE_FAILED,
        TOO_MANY,
        TITLE_LIMIT,
        BODY_LIMIT,
        DELETE_TITLE,
        DELETE_BODY,
        CONFLICT,
        "Move Up",
        "Move Down",
        "Reorder Notes",
        'placeholder="Title"',
        'placeholder="Note"',
        'aria-label="Add Note"',
        'aria-label="Title"',
        'aria-label="Note"',
    ):
        assert s in PANEL, s


def test_no_alarm_codes_in_panel_or_addon():
    assert "Alarm codes" not in PANEL
    assert "Alarm codes" not in ADDON
    assert "alarm codes" not in PANEL.lower()
    assert "alarm codes" not in ADDON.lower()


def test_panel_is_a_list_not_a_legacy_textarea():
    assert 'id="notes_text"' not in PANEL
    assert 'id="notes_list"' in PANEL
    assert 'id="notes_empty"' in PANEL
    assert 'id="notes_edit_view"' in PANEL
    assert 'id="notes_edit_title"' in PANEL
    assert 'id="notes_edit_body"' in PANEL
    assert 'id="notes_add"' in PANEL and 'id="notes_add_empty"' in PANEL
    assert "maxlength=\"80\"" in PANEL
    assert 'id="notes_edit_body"' in PANEL and "maxlength=\"8000\"" in PANEL


def test_panel_writes_items_with_ifmatch_and_never_legacy_text():
    assert "JSON.stringify({ items: notesPayloadItems(), ifMatch: notesEtag })" in PANEL
    assert "function applyNotesDoc(doc)" in PANEL
    assert "function saveNotesList()" in PANEL
    # No legacy text PUT on the component panel.
    assert "schemaVersion: 1, text" not in PANEL
    assert "JSON.stringify({ schemaVersion: 1, text" not in PANEL
    assert "NOTES_PATH" in PANEL
    assert "/api/patrimony_collection/notes" in PANEL
    # mutate() is used so 409 bodies can be read (api() throws on !ok).
    assert "mutate(NOTES_PATH" in PANEL
    assert "notes_if_match_required" not in PANEL or "saveFailed" in PANEL


def test_apply_notes_doc_is_atomic_for_qa_69():
    # etag and items are assigned together from one document; 409 uses current.
    assert "Never pair a fresh etag with stale items (stale list PUT)." in PANEL
    start = PANEL.find("function applyNotesDoc(doc)")
    end = PANEL.find("function notesPayloadItems()")
    body = PANEL[start:end]
    assert "notesEtag = doc.etag" in body
    assert "notesItems = items" in body
    # 409 path applies current, not a new etag alone.
    assert "applyNotesDoc(doc.current || doc)" in PANEL


def test_legacy_single_note_shown_as_one_item():
    start = PANEL.find("function applyNotesDoc(doc)")
    end = PANEL.find("function notesPayloadItems()")
    body = PANEL[start:end]
    assert "Array.isArray(doc.items)" in body
    assert "show the legacy text as one item" in body
    assert 'items = text ? [{ id: null, title: "", body: text }] : []' in body


def test_panel_does_not_log_note_content():
    # No console logging of titles or bodies; render uses textContent, not innerHTML.
    js_from_notes = PANEL[PANEL.find("const NOTES_COPY"): PANEL.find("function clearPairing")]
    assert "console.log" not in js_from_notes
    assert "console.debug" not in js_from_notes
    assert "console.info" not in js_from_notes
    assert "innerHTML" not in js_from_notes
    assert "title.textContent" in js_from_notes
    assert "preview.textContent" in js_from_notes


def test_empty_new_note_is_not_saved():
    assert "if (!title && !body)" in PANEL
    assert "showNotesListView()" in PANEL


def test_conflict_reloads_editor_from_current_items():
    assert "function syncEditorFromItems()" in PANEL
    assert "if (notesEditingId && !notesEditingIsNew) syncEditorFromItems()" in PANEL
    assert CONFLICT in PANEL


def test_addon_ingress_refuses_text_put_once_list_edited():
    assert "textWritable === false" in ADDON or "doc.textWritable === false" in ADDON
    assert "if (!notesTextWritable) return;" in ADDON
    assert "area.readOnly || area.hidden" in ADDON
    # Ingress may keep a textarea for the pre-list (textWritable) path only.
    assert 'id="notes_text"' in ADDON
    assert "No notes yet" in ADDON
    assert EMPTY_BODY in ADDON
    assert "Alarm codes" not in ADDON
    # Read-only list uses textContent.
    notes_js = ADDON[ADDON.find("function renderIngressNotes"): ADDON.find("async function saveNotesNow")]
    assert "innerHTML" not in notes_js
    assert "title.textContent" in notes_js


def _addon_web(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "patrimony_addon_web_notes_04123",
        ROOT / "addons" / "patrimony_collection" / "web.py",
    )
    web = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(web)
    web.NOTES_PATH = tmp_path / "notes.json"
    web.OPTIONS_PATH = tmp_path / "options.json"
    return web


def test_addon_notes_have_items_includes_empty_listedited(tmp_path):
    web = _addon_web(tmp_path)
    assert web.notes_have_items() is False
    web.NOTES_PATH.write_text(json.dumps({
        "schemaVersion": 1,
        "text": "",
        "items": [],
        "listEdited": True,
    }))
    assert web.notes_have_items() is True
    web.NOTES_PATH.write_text(json.dumps({
        "schemaVersion": 1,
        "text": "Heating is on the left",
        "items": [{"id": "x", "title": "Heating", "body": "left"}],
        "listEdited": False,
    }))
    assert web.notes_have_items() is True
    doc = web.load_notes_doc()
    assert doc["textWritable"] is True
    assert doc["items"][0]["title"] == "Heating"
    web.NOTES_PATH.write_text(json.dumps({
        "schemaVersion": 1,
        "text": "",
        "items": [],
        "listEdited": True,
    }))
    doc = web.load_notes_doc()
    assert doc["textWritable"] is False
    assert doc["items"] == []


def test_manifest_version_is_04123():
    man = json.loads(
        (ROOT / "custom_components" / "patrimony_collection" / "manifest.json").read_text()
    )
    assert man["version"] == "0.4.123"
    cfg = (ROOT / "addons" / "patrimony_collection" / "config.yaml").read_text()
    assert 'version: "0.4.123"' in cfg


def test_hidden_notes_actions_are_not_shown_by_flex():
    """`.actions { display: flex }` must not un-hide the empty-state Add button pair."""
    assert "#notes_list_actions[hidden]" in PANEL
    assert "display: none !important" in PANEL

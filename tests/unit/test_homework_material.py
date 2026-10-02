"""Offline tests for the homework-material tools (get_homework_material,
download_attachment).

These run with NO network and NO credentials. `edupage-api` has no
homework/material reader (AGENTS.md rule-1 exception), so the wrapper parses
the material-player payload itself; these tests pin that parsing, the
`content-disposition` filename handling and the path-traversal guard. Every
HTTP call is served by a fake client that returns canned responses.

Run with:  python -m pytest tests/unit -q
"""

import json
import tempfile
from pathlib import Path

import pytest

import edupage_mcp as m

SUB = "testschool"
BASE = f"https://{SUB}.edupage.org"

WIDGET_TREE = {
    "widgetClass": "MaterialContainerWidget",
    "props": {},
    "widgets": [
        {"widgetClass": "TitleETestWidget",
         "props": {"text": "<h2>Počítačová gramotnosť</h2>"}},
        {"widgetClass": "TextETestWidget",
         "props": {"htmlText": "<p>Prečítaj <b>stran</b> 12 &ndash; 15.</p>"
                                "<p></p><p>Vypracuj úlohy.</p>"}},
        {"widgetClass": "FileETestWidget",
         "props": {"files": [
             {"name": "priloha.pdf", "src": "/data/sck123/priloha.pdf"},
             {"name": "bez-src.pdf"},
         ]}},
        {"widgetClass": "ElaborationETestWidget", "props": {"enableUpload": "enabled"}},
        {"widgetClass": "SomeUnknownWidget", "props": {}, "widgets": [
            {"widgetClass": "TitleETestWidget", "props": {"text": "Doplň"}},
            {"widgetClass": "FileETestWidget",
             "props": {"files": [{"name": "", "src": "https://cdn.example.org/ok.png"}]}},
            {"widgetClass": "ElaborationETestWidget",
             "props": {"enableUpload": "disabled"}},
        ]},
    ],
}

PLAYER_DATA = {
    "materialData": {
        "name": "Fallback title",
        "cardsData": {
            "card-1": {"content": json.dumps(WIDGET_TREE)},
            "card-2": {"content": "{not json"},
        },
    },
    "superRow": {"hwRow": {
        "name": "Počítačová gramotnosť",
        "details": "<p>Do <b>20.9.</b></p>",
        "datefrom": "2026-09-15",
        "dateto": "2026-09-22",
    }},
}


def _page(player=PLAYER_DATA):
    return ("<html><body><script>var data = .etestPlayer("
            + json.dumps(player) + ", {});</script></body></html>")


class FakeResponse:
    def __init__(self, text="", content=b"", headers=None, status_code=200):
        self.text = text
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code


class FakeClient:
    """Serves canned responses through upstream's custom_request escape hatch."""

    is_logged_in = True
    subdomain = SUB

    def __init__(self, responses=None, default=None):
        self.responses = responses or {}
        self.default = default or FakeResponse(text=_page())
        self.calls = []

    def custom_request(self, url, method, data="", headers={}):
        self.calls.append((url, method))
        return self.responses.get(url, self.default)


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    """Isolate module-global session state and pin the allowlist to this file's
    test school so the run is independent of the developer's real env var."""
    monkeypatch.setattr(m, "EDUPAGE_SUBDOMAINS", SUB)
    saved = dict(m._clients), dict(m._roles)
    m._clients.clear()
    m._roles.clear()
    m._active_subdomain = None
    yield
    m._clients, m._roles = saved[0], saved[1]
    m._active_subdomain = None


@pytest.fixture()
def client():
    c = FakeClient()
    m._clients[SUB] = c
    m._active_subdomain = SUB
    return c


# --------------------------------------------------------------------------
# html -> text
# --------------------------------------------------------------------------
def test_html_to_text_strips_tags_and_block_breaks():
    html = "<div>A</div><p>B<br/>C</p><ul><li>D</li></ul><table><tr><td>E</td></tr></table>"
    assert m._html_to_text(html) == "A\nB\nC\nD\nE"


def test_html_to_text_unescapes_entities():
    assert m._html_to_text("<p>a &amp; b &ndash; &quot;c&quot;</p>") == 'a & b – "c"'


def test_html_to_text_collapses_blank_lines_and_trims():
    assert m._html_to_text("<p>  A  </p><p></p>\n\n<p> B </p><p>   </p>") == "A\nB"


def test_html_to_text_empty_and_none():
    assert m._html_to_text(None) == ""
    assert m._html_to_text("") == ""
    assert m._html_to_text("<p>  </p>") == ""


# --------------------------------------------------------------------------
# the .etestPlayer( marker + payload parse
# --------------------------------------------------------------------------
def test_marker_parse_reads_embedded_object():
    player = m._hw_parse_material_player(_page(), "42")
    assert player["materialData"]["cardsData"].keys() == {"card-1", "card-2"}


def test_marker_missing_raises_actionable_error():
    with pytest.raises(RuntimeError) as exc:
        m._hw_parse_material_player("<html><body>no data here</body></html>", "42")
    msg = str(exc.value)
    assert "No homework material data" in msg
    assert "get_timeline(category='homework')" in msg


def test_marker_with_broken_json_raises():
    with pytest.raises(RuntimeError, match="malformed player data"):
        m._hw_parse_material_player("x.etestPlayer({oops", "42")


def test_build_material_flattens_widgets_and_resolves_relative_src():
    material = m._hw_build_material(_page(), BASE, "42")
    assert material["superid"] == "42"
    assert material["title"] == "Počítačová gramotnosť"
    assert material["details"] == "Do 20.9."
    assert material["date_from"] == "2026-09-15"
    assert material["date_to"] == "2026-09-22"
    # Title, body text, upload marker and the nested unknown-parent widget.
    assert material["content"] == (
        "Počítačová gramotnosť\n\n"
        "Prečítaj stran 12 – 15.\nVypracuj úlohy.\n\n"
        "[Student answer / file upload area]\n\n"
        "Doplň"
    )
    # Relative src resolved to an absolute URL, src-less entry skipped, and an
    # already-absolute src left untouched; a nameless file falls back to the
    # URL's last path segment.
    assert material["attachments"] == [
        {"name": "priloha.pdf", "url": f"{BASE}/data/sck123/priloha.pdf"},
        {"name": "ok.png", "url": "https://cdn.example.org/ok.png"},
    ]


def test_build_material_tolerates_missing_rows():
    player = {"materialData": {"cardsData": {"c": {"content": json.dumps(
        {"widgetClass": "X", "props": None, "widgets": None})}}},
        "superRow": {}}
    material = m._hw_build_material(_page(player), BASE, "7")
    assert material["title"] is None
    assert material["details"] is None
    assert material["content"] == ""
    assert material["attachments"] == []


# --------------------------------------------------------------------------
# get_homework_material tool
# --------------------------------------------------------------------------
def test_tool_uses_upstream_custom_request_and_returns_payload(client):
    res = m.get_homework_material(superid="42", subdomain=SUB)
    assert not res.get("isError")
    assert client.calls == [
        (f"{BASE}/elearning/?cmd=MaterialPlayer&superid=42", "GET")]
    assert res["subdomain"] == SUB
    assert res["title"] == "Počítačová gramotnosť"
    assert res["attachments"][0]["url"].startswith(BASE)
    json.dumps(res)  # must stay JSON-serializable


def test_tool_surfaces_invalid_material_id_as_error(client):
    client.default = FakeResponse(text="<html>nope</html>")
    res = m.get_homework_material(superid="0", subdomain=SUB)
    assert res["isError"] is True
    text = res["content"][0]["text"]
    assert "No homework material data" in text
    assert "Traceback" not in text


def test_tool_surfaces_http_error(client):
    client.default = FakeResponse(text="", status_code=404)
    res = m.get_homework_material(superid="42", subdomain=SUB)
    assert res["isError"] is True
    assert "HTTP 404" in res["content"][0]["text"]


def test_tool_requires_a_logged_in_session():
    res = m.get_homework_material(superid="42", subdomain=SUB)
    assert res["isError"] is True
    assert "Not logged in" in res["content"][0]["text"]


# --------------------------------------------------------------------------
# filename handling
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("priloha.pdf", "priloha.pdf"),
    ("../../evil.pdf", "evil.pdf"),          # traversal attempt is neutralised
    ("..", "download"),                      # no name component at all
    ("", "download"),
    ("a/b/c.txt", "c.txt"),                  # directory components are dropped
    ('re:port*1?.pdf', "re_port_1_.pdf"),     # characters illegal on Windows
    ("  spaced.pdf  ", "spaced.pdf"),         # leading/trailing dots+spaces
    (".hidden.", "hidden"),
])
def test_sanitize_filename(raw, expected):
    assert m._hw_sanitize_filename(raw) == expected


@pytest.mark.parametrize("headers,url,expected", [
    ({}, "https://x.edupage.org/data/sck1/priloha.pdf", "priloha.pdf"),
    ({}, "https://x.edupage.org/data/sck1/priloha.pdf?token=1", "priloha.pdf"),
    ({}, "https://x.edupage.org/", "download"),
    ({"content-disposition": 'attachment; filename="moja priloha.pdf"'},
     "https://x/edupage/d", "moja priloha.pdf"),
    ({"content-disposition": "attachment; filename*=UTF-8''%C4%8Cesk%C3%BD.pdf"},
     "https://x/edupage/d", "Český.pdf"),
    ({"content-disposition": "attachment; filename=plain.pdf"},
     "https://x/edupage/d", "plain.pdf"),
])
def test_filename_from_response(headers, url, expected):
    assert m._hw_filename_from_response(FakeResponse(headers=headers), url) == expected


def test_dedupe_path_appends_incrementing_suffix(tmp_path):
    target = tmp_path / "priloha.pdf"
    assert m._hw_dedupe_path(target) == target  # free name is used as-is
    target.write_bytes(b"a")
    first = m._hw_dedupe_path(target)
    assert first.name == "priloha (1).pdf"
    first.write_bytes(b"b")
    assert m._hw_dedupe_path(target).name == "priloha (2).pdf"
    # The original is never overwritten.
    assert target.read_bytes() == b"a"


# --------------------------------------------------------------------------
# download_attachment tool
# --------------------------------------------------------------------------
def _download_client(content=b"PDF", headers=None, status_code=200):
    return FakeClient(default=FakeResponse(content=content, headers=headers,
                                           status_code=status_code))


def test_download_writes_file_into_dest_dir(client, tmp_path):
    client.responses = {"https://x.edupage.org/f/priloha.pdf": FakeResponse(
        content=b"%PDF-1.4", headers={"content-disposition": 'attachment; filename="učebnice.pdf"'})}
    res = m.download_attachment(url="https://x.edupage.org/f/priloha.pdf",
                               dest_dir=str(tmp_path / "domac"),
                               subdomain=SUB)
    assert not res.get("isError")
    saved = Path(res["saved_to"])
    assert saved == tmp_path / "domac" / "učebnice.pdf"  # directory was created
    assert saved.read_bytes() == b"%PDF-1.4"
    assert res["bytes"] == 8
    assert res["name"] == "učebnice.pdf"
    assert res["source_url"] == "https://x.edupage.org/f/priloha.pdf"


def test_download_never_overwrites(client, tmp_path):
    client.default = FakeResponse(content=b"new", headers={"content-disposition": "attachment; filename=a.pdf"})
    first = m.download_attachment(url="https://x.edupage.org/f/a.pdf",
                                 dest_dir=str(tmp_path), subdomain=SUB)
    second = m.download_attachment(url="https://x.edupage.org/f/a.pdf",
                                  dest_dir=str(tmp_path), subdomain=SUB)
    assert Path(first["saved_to"]).name == "a.pdf"
    assert Path(second["saved_to"]).name == "a (1).pdf"
    assert Path(first["saved_to"]).read_bytes() == b"new"
    assert Path(second["saved_to"]).read_bytes() == b"new"


def test_download_filename_cannot_escape_dest_dir(client, tmp_path):
    res = m.download_attachment(url="https://x.edupage.org/f/a.pdf",
                               dest_dir=str(tmp_path), filename="../../evil.pdf",
                               subdomain=SUB)
    saved = Path(res["saved_to"])
    assert saved.parent == tmp_path
    assert saved.name == "evil.pdf"
    assert not (tmp_path.parent / "evil.pdf").exists()


def test_download_defaults_to_homework_folder_in_tempdir(monkeypatch, client):
    fake_temp = Path(tempfile.gettempdir()) / "edupage-mcp-test"
    monkeypatch.setattr(m.tempfile, "gettempdir", lambda: str(fake_temp))
    client.default = FakeResponse(content=b"x", headers={"content-disposition": "attachment; filename=hw.txt"})
    try:
        res = m.download_attachment(url="https://x.edupage.org/f/hw.txt",
                                   subdomain=SUB)
        assert not res.get("isError")
        assert Path(res["saved_to"]) == fake_temp / "homework" / "hw.txt"
    finally:
        (fake_temp / "homework" / "hw.txt").unlink(missing_ok=True)
        (fake_temp / "homework").rmdir()
        fake_temp.rmdir()


def test_download_surfaces_http_error(client, tmp_path):
    client.default = FakeResponse(status_code=403, content=b"")
    res = m.download_attachment(url="https://x.edupage.org/f/a.pdf",
                               dest_dir=str(tmp_path), subdomain=SUB)
    assert res["isError"] is True
    assert "HTTP 403" in res["content"][0]["text"]
    assert list(tmp_path.iterdir()) == []


def test_download_requires_a_logged_in_session(tmp_path):
    res = m.download_attachment(url="https://x.edupage.org/f/a.pdf",
                               dest_dir=str(tmp_path), subdomain=SUB)
    assert res["isError"] is True
    assert "Not logged in" in res["content"][0]["text"]

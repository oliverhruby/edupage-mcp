"""Offline tests for the binary-body / truncation handling of the raw
passthrough (`custom_request`) and the download tool's URL + error guards.

Same no-network, no-credentials approach as `test_homework_material.py`: every
HTTP call is served by a `FakeClient` returning a canned response. The cases
here pin the behaviour measured against a live EduPage school on 2026-10-02:
EduPage serves real MIME types for attachments (`…wordprocessingml.document`,
`image/jpeg`, `application/pdf`), a bad attachment token is a plain HTTP 404,
and an unauthenticated school *page* answers 200 with the `/login/` HTML.

Run with:  python -m pytest tests/unit -q
"""

import json
from pathlib import Path

import pytest

import edupage_mcp as m

SUB = "testschool"
BASE = f"https://{SUB}.edupage.org"

DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class FakeResponse:
    def __init__(self, text="", content=b"", headers=None, status_code=200, url=""):
        self.text = text
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code
        self.url = url


class FakeClient:
    is_logged_in = True
    subdomain = SUB

    def __init__(self, default=None):
        self.default = default or FakeResponse(text="ok", content=b"ok",
                                               headers={"content-type": "text/plain"})
        self.calls = []

    def custom_request(self, url, method, data="", headers={}):
        self.calls.append((url, method, data, headers))
        return self.default


class NoHeadersResponse:
    """A response object with no usable `headers` mapping at all."""

    status_code = 200
    url = ""
    content = b"x"
    text = "x"


def mojibake(name):
    """`name` as a header value arrives when the server sends raw UTF-8 bytes and
    `requests` decodes the header as latin-1 — the live EduPage shape."""
    return name.encode("utf-8").decode("latin-1")


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
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
# _hw_media_type
# --------------------------------------------------------------------------
@pytest.mark.parametrize("headers,expected", [
    ({"content-type": "text/html; charset=UTF-8"}, "text/html"),
    ({"content-type": 'application/json; charset="utf-8"'}, "application/json"),
    ({"content-type": "application/pdf"}, "application/pdf"),
    ({}, ""),
])
def test_media_type_strips_parameters(headers, expected):
    assert m._hw_media_type(FakeResponse(headers=headers)) == expected


def test_media_type_tolerates_a_response_without_headers():
    assert m._hw_media_type(NoHeadersResponse()) == ""


# --------------------------------------------------------------------------
# _hw_body_is_binary
# --------------------------------------------------------------------------
@pytest.mark.parametrize("content_type,body,expected", [
    # Office/PDF/media: real attachment media types must never read as text.
    (DOCX_CT, b"PK\x03\x04\x14\x00\x06\x00", True),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK\x03\x04", True),
    # ...including the subtypes whose `+xml` suffix would otherwise be text.
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml", b"PK\x03\x04", True),
    ("application/vnd.ms-excel", b"\xd0\xcf\x11\xe0", True),
    ("application/pdf", b"%PDF-1.7", True),
    ("image/jpeg", b"\xff\xd8\xff\xe0", True),
    ("video/quicktime", b"\x00\x00\x00\x14ftyp", True),
    ("application/octet-stream", b"\x01\x02", True),
    # Text must NOT be flagged.
    ("text/html", b"<!DOCTYPE html><html></html>", False),
    ("text/html; charset=UTF-8", b"<!DOCTYPE html>", False),
    ("text/plain", b"hello", False),
    ("application/json", b'{"a": 1}', False),
    ("application/json; charset=utf-8", b"[1,2]", False),
    ("image/svg+xml", b"<svg/>", False),
    ("application/xhtml+xml", b"<html/>", False),
    # A missing Content-Type is under-detected on purpose: the NUL probe decides.
    ("", b"plain text", False),
    (None, b"plain text", False),
    # The NUL probe is the safety net for a server that mislabels a download.
    ("text/plain", b"PK\x03\x04\x14\x00\x00\x00\x00", True),
    (None, b"head\x00tail", True),
])
def test_body_is_binary(content_type, body, expected):
    assert m._hw_body_is_binary(content_type, body) is expected


def test_body_is_binary_only_probes_the_first_4k():
    # A NUL past the probe window is ignored, so a long text body is not
    # mistaken for a download just because it embeds binary further down.
    assert m._hw_body_is_binary("text/plain", b"a" * 5000 + b"\x00") is False
    assert m._hw_body_is_binary("text/plain", b"a" * 4000 + b"\x00") is True


# --------------------------------------------------------------------------
# _hw_absolute_url — shared by custom_request and download_attachment
# --------------------------------------------------------------------------
def test_absolute_url_passes_through_a_full_url():
    assert m._hw_absolute_url(f"{BASE}/data/sck1/priloha.pdf") == f"{BASE}/data/sck1/priloha.pdf"


def test_absolute_url_resolves_the_relative_form_get_timeline_returns():
    # `get_timeline` hands out attachment keys exactly like this one.
    key = "/elearning/ruqjzfpv?z%3A1KmpBrCkpSed5RnzpItvH%2F3pycQ1JzFWrKYOa6KaRDM%3D%3D"
    assert m._hw_absolute_url(key, SUB) == f"{BASE}{key}"


def test_absolute_url_adds_the_missing_leading_slash():
    assert m._hw_absolute_url("export/ajax_prevedene_meno.php", SUB) == (
        f"{BASE}/export/ajax_prevedene_meno.php")


def test_absolute_url_needs_a_subdomain_for_a_relative_path():
    with pytest.raises(RuntimeError, match="without a resolved subdomain"):
        m._hw_absolute_url("/x", None)


# --------------------------------------------------------------------------
# _hw_reject_html_page
# --------------------------------------------------------------------------
def _page(url, body=b"<!DOCTYPE html><html><body>login</body></html>"):
    return FakeResponse(url=url, content=body, headers={"content-type": "text/html; charset=utf-8"})


def test_rejects_the_200_login_page_and_names_login_as_the_fix():
    with pytest.raises(RuntimeError) as exc:
        m._hw_reject_html_page(_page(f"{BASE}/login/"), f"{BASE}/timeline/")
    msg = str(exc.value)
    assert "session has expired" in msg
    assert "`login`" in msg


def test_rejects_a_plain_html_page():
    with pytest.raises(RuntimeError, match="instead of a file"):
        m._hw_reject_html_page(_page(f"{BASE}/"), f"{BASE}/")


def test_rejects_a_captcha_page_regardless_of_media_type():
    resp = FakeResponse(url=f"{BASE}/x", content=b"reCAPTCHA challenge",
                        headers={"content-type": "application/octet-stream"})
    with pytest.raises(RuntimeError, match="captcha"):
        m._hw_reject_html_page(resp, f"{BASE}/x")


def test_lets_a_named_html_attachment_through():
    """Every EduPage page/error lacks `content-disposition`; every attachment has
    one, so that header is what separates a page from a real HTML file."""
    resp = FakeResponse(url=f"{BASE}/x", content=b"<html>report</html>",
                        headers={"content-type": "text/html",
                                 "content-disposition": 'inline; filename="rozvrh.html"'})
    assert m._hw_reject_html_page(resp, f"{BASE}/x") is None


def test_lets_a_named_attachment_through_when_the_disposition_is_absent():
    resp = FakeResponse(url=f"{BASE}/x", content=b"%PDF-1.7",
                        headers={"content-type": "application/pdf"})
    assert m._hw_reject_html_page(resp, f"{BASE}/x") is None


# --------------------------------------------------------------------------
# custom_request
# --------------------------------------------------------------------------
def test_custom_request_returns_metadata_for_a_text_body(client):
    client.default = FakeResponse(text="<html>ok</html>", content=b"<html>ok</html>",
                                  headers={"content-type": "text/html; charset=UTF-8"})
    res = m.custom_request(url="/", method="GET", subdomain=SUB)
    assert not res.get("isError")
    assert res["status_code"] == 200
    assert res["content_type"] == "text/html"
    assert res["bytes"] == len(b"<html>ok</html>")
    assert res["text"] == "<html>ok</html>"
    assert res["truncated"] is False
    json.dumps(res)


def test_custom_request_resolves_a_relative_url(client):
    client.default = FakeResponse(text="ok", content=b"ok",
                                  headers={"content-type": "text/plain"})
    res = m.custom_request(url="/export/ajax_prevedene_meno.php", method="GET", subdomain=SUB)
    assert not res.get("isError")
    assert client.calls[0][0] == f"{BASE}/export/ajax_prevedene_meno.php"


def test_custom_request_refuses_a_binary_body_and_names_the_download_tool(client):
    """The .docx incident: `.text` decoded 23455 bytes of ZIP into 22531 chars
    of U+FFFD. It must be refused, not returned mangled."""
    raw = b"PK\x03\x04\x14\x00\x06\x00\x00\x00" + bytes(range(256))
    client.default = FakeResponse(
        text=raw.decode("utf-8", "replace"), content=raw,
        headers={"content-type": DOCX_CT,
                 "content-disposition": 'inline; filename="harmonogram.docx"'})
    res = m.custom_request(url="/elearning/ruqjzfpv?z%3Aabc", method="GET", subdomain=SUB)
    assert res["isError"] is True
    text = res["content"][0]["text"]
    assert "binary body" in text
    assert DOCX_CT in text
    assert "download_attachment" in text
    assert "Traceback" not in text


def test_custom_request_text_html_is_not_a_false_positive(client):
    """`text/html` is a legitimate EduPage response and must come back intact."""
    client.default = FakeResponse(text="<html>hi</html>", content=b"<html>hi</html>",
                                  headers={"content-type": "text/html; charset=utf-8"})
    res = m.custom_request(url="/", method="GET", subdomain=SUB)
    assert not res.get("isError")
    assert res["text"] == "<html>hi</html>"
    assert "binary body" not in json.dumps(res)


@pytest.mark.parametrize("content_type,content", [
    ("image/jpeg", b"\xff\xd8\xff\xe0\x00\x10JFIF"),
    ("application/pdf", b"%PDF-1.7\n%\xb5\xb5"),
    ("video/quicktime", b"\x00\x00\x00\x14ftypqt"),
])
def test_custom_request_refuses_other_binary_media_types(client, content_type, content):
    client.default = FakeResponse(content=content, headers={"content-type": content_type})
    res = m.custom_request(url="/elearning/ruqjzfpv?z%3Aabc", method="GET", subdomain=SUB)
    assert res["isError"] is True
    assert "download_attachment" in res["content"][0]["text"]


def test_custom_request_truncates_a_large_text_body(client):
    limit = m._CUSTOM_TEXT_MAX
    body = ("x" * (limit + 5000)).encode()
    client.default = FakeResponse(text=body.decode(), content=body,
                                  headers={"content-type": "text/html"})
    res = m.custom_request(url="/", method="GET", subdomain=SUB)
    assert not res.get("isError")
    assert res["truncated"] is True
    assert res["bytes"] == len(body)              # the true size is still reported
    assert len(res["text"]) == limit              # and the body is a clean prefix
    assert res["text"] == body.decode()[:limit]


def test_custom_request_does_not_truncate_at_the_boundary(client):
    body = ("y" * m._CUSTOM_TEXT_MAX).encode()
    client.default = FakeResponse(text=body.decode(), content=body,
                                  headers={"content-type": "text/plain"})
    res = m.custom_request(url="/", method="GET", subdomain=SUB)
    assert res["truncated"] is False
    assert len(res["text"]) == m._CUSTOM_TEXT_MAX


def test_custom_request_requires_a_logged_in_session():
    res = m.custom_request(url="/", method="GET", subdomain=SUB)
    assert res["isError"] is True
    assert "Not logged in" in res["content"][0]["text"]


# --------------------------------------------------------------------------
# download_attachment — relative URL + error-page guards
# --------------------------------------------------------------------------
def test_download_accepts_the_relative_url_from_get_timeline(client, tmp_path):
    key = "/elearning/ruqjzfpv?z%3A1KmpBrCkpSed5RnzpItvH%2F3pycQ1JzFWrKYOa6KaRDM%3D%3D"
    client.default = FakeResponse(
        content=b"PK\x03\x04\x14\x00", headers={"content-type": DOCX_CT,
                                                "content-disposition": 'inline; filename="a.docx"'})
    res = m.download_attachment(url=key, dest_dir=str(tmp_path), subdomain=SUB)
    assert not res.get("isError")
    assert client.calls[0][0] == f"{BASE}{key}"
    assert Path(res["saved_to"]).name == "a.docx"
    assert res["source_url"] == f"{BASE}{key}"   # the resolved URL is echoed back
    assert Path(res["saved_to"]).read_bytes() == b"PK\x03\x04\x14\x00"


def test_download_accepts_a_relative_url_without_a_leading_slash(client, tmp_path):
    client.default = FakeResponse(content=b"x", headers={"content-type": "text/plain"})
    res = m.download_attachment(url="elearning/ruqjzfpv?z%3Aabc",
                               dest_dir=str(tmp_path), subdomain=SUB)
    assert not res.get("isError")
    assert client.calls[0][0] == f"{BASE}/elearning/ruqjzfpv?z%3Aabc"


def test_download_does_not_write_an_html_page_served_with_200(client, tmp_path):
    """The residual live failure shape: an unauthenticated school page answers
    HTTP 200 with the login page, which used to be saved as the attachment."""
    client.default = FakeResponse(
        content=b"<!DOCTYPE html><html><body>login</body></html>",
        headers={"content-type": "text/html; charset=utf-8"},
        url=f"{BASE}/login/")
    res = m.download_attachment(url="/timeline/", dest_dir=str(tmp_path), subdomain=SUB)
    assert res["isError"] is True
    assert "session has expired" in res["content"][0]["text"]
    assert list(tmp_path.iterdir()) == []


def test_download_does_not_write_an_html_page_served_with_404(client, tmp_path):
    client.default = FakeResponse(
        status_code=404,
        content=b"Requested file was not found on this server!",
        headers={"content-type": "text/html; charset=UTF-8"})
    res = m.download_attachment(url="/elearning/ruqjzfpv?z%3Abad",
                               dest_dir=str(tmp_path), subdomain=SUB)
    assert res["isError"] is True
    assert "HTTP 404" in res["content"][0]["text"]
    assert list(tmp_path.iterdir()) == []


def test_download_still_requires_a_logged_in_session(tmp_path):
    res = m.download_attachment(url="/elearning/ruqjzfpv?z%3Aabc",
                               dest_dir=str(tmp_path), subdomain=SUB)
    assert res["isError"] is True
    assert "Not logged in" in res["content"][0]["text"]
    assert list(tmp_path.iterdir()) == []


def test_download_needs_a_subdomain_only_for_a_relative_url(tmp_path):
    """An absolute URL needs no resolved subdomain, so the relative-URL
    resolution must not run first and mask the real 'Not logged in' error."""
    res = m.download_attachment(url=f"{BASE}/elearning/ruqjzfpv?z%3Aabc",
                               dest_dir=str(tmp_path), subdomain=SUB)
    assert res["isError"] is True
    assert "Not logged in" in res["content"][0]["text"]


# --------------------------------------------------------------------------
# _hw_repair_utf8_filename / _hw_filename_from_response
# --------------------------------------------------------------------------
def test_repair_recovers_utf8_bytes_that_arrived_as_latin1():
    # The exact live header value observed on 2026-10-02 against iprskola.
    raw = "Ä\x8dasovÃ½ harmonogram - jesennÃ© ÃºÄ\x8delovÃ© cviÄ\x8denie 2026.docx"
    assert m._hw_repair_utf8_filename(raw) == (
        "časový harmonogram - jesenné účelové cvičenie 2026.docx")


@pytest.mark.parametrize("name", [
    "český.docx",                      # already decoded correctly -> no-op
    "vyučovacia tabuľka.pdf",
    "römer/priloženie.txt",
    "70539.jpg",
    "download",
])
def test_repair_is_a_no_op_when_the_bytes_are_not_latin1_encoded_utf8(name):
    """A name that decoded correctly (any char above U+00FF) cannot be
    re-encoded to latin-1, so it is returned untouched — the repair is
    idempotent and never mangles a name that is already right."""
    assert m._hw_repair_utf8_filename(name) == name


@pytest.mark.parametrize("name", [
    "caf\xe9-termin.pdf",              # latin-1 'é' is an invalid UTF-8 byte
    "r\xe9sum\xe9.pdf",
    "j\xf3ger.pdf",
])
def test_repair_leaves_a_true_latin1_filename_unchanged(name):
    """The whole point of the try/except: an unconditional `errors="replace"`
    decode would turn these into U+FFFD."""
    repaired = m._hw_repair_utf8_filename(name)
    assert repaired == name
    assert chr(0xFFFD) not in repaired


def test_filename_from_response_repairs_a_mojibake_filename():
    resp = FakeResponse(headers={"content-disposition":
                                 'inline; filename="%s"' % mojibake(
                                     "časový harmonogram - jesenné účelové cvičenie 2026.docx")})
    assert m._hw_filename_from_response(resp, f"{BASE}/x") == (
        "časový harmonogram - jesenné účelové cvičenie 2026.docx")


def test_filename_from_response_leaves_a_latin1_filename_alone():
    resp = FakeResponse(headers={"content-disposition":
                                 'inline; filename="caf\xe9-termin.pdf"'})
    assert m._hw_filename_from_response(resp, f"{BASE}/x") == "caf\xe9-termin.pdf"


def test_filename_from_response_leaves_ascii_untouched():
    resp = FakeResponse(headers={"content-disposition": 'inline; filename="plan10-2026.pdf"'})
    assert m._hw_filename_from_response(resp, f"{BASE}/x") == "plan10-2026.pdf"


def test_filename_star_still_wins_and_is_not_double_decoded():
    """RFC 5987 is already percent-decoded UTF-8, so the repair must not run on
    it. A value that happens to be valid-UTF-8-reinterpretable would otherwise be
    decoded a second time and silently corrupted."""
    resp = FakeResponse(headers={"content-disposition":
                                 'attachment; filename="%s"; '
                                 "filename*=UTF-8''%%C4%%8Desk%%C3%%BD.pdf"
                                 % mojibake("zly-nazov.docx")})
    assert m._hw_filename_from_response(resp, f"{BASE}/x") == "český.pdf"


def test_filename_star_value_is_not_reinterpreted():
    """`Ã©` IS the mojibake of `é`, and re-encodes to valid UTF-8, so this is
    the exact case a misplaced repair would break. The star branch must hand it
    back byte-for-byte as the server encoded it."""
    resp = FakeResponse(headers={"content-disposition":
                                 "attachment; filename*=UTF-8''%C3%83%C2%A9.pdf"})
    assert m._hw_filename_from_response(resp, f"{BASE}/x") == "Ã©.pdf"


def test_filename_star_wins_even_when_both_are_valid():
    resp = FakeResponse(headers={"content-disposition":
                                 'attachment; filename="fallback.docx"; '
                                 "filename*=UTF-8''harmonogram.docx"})
    assert m._hw_filename_from_response(resp, f"{BASE}/x") == "harmonogram.docx"


def test_filename_from_response_still_falls_back_to_the_url_path():
    resp = FakeResponse(headers={"content-disposition": "inline"})
    assert m._hw_filename_from_response(resp, f"{BASE}/data/sck1/priloha.pdf") == "priloha.pdf"


def test_download_saves_under_the_repaired_name(client, tmp_path):
    """End to end: the file the user sees is named from the repaired header."""
    client.default = FakeResponse(
        content=b"PK\x03\x04", headers={"content-type": DOCX_CT, "content-disposition":
                                        'inline; filename="%s"' % mojibake(
                                            "časový harmonogram - jesenné účelové cvičenie 2026.docx")})
    res = m.download_attachment(url="/elearning/ruqjzfpv?z%3Aabc",
                               dest_dir=str(tmp_path), subdomain=SUB)
    assert not res.get("isError")
    saved = Path(res["saved_to"])
    assert saved.name == "časový harmonogram - jesenné účelové cvičenie 2026.docx"
    assert saved.read_bytes() == b"PK\x03\x04"
    assert "\ufffd" not in saved.name and "Ã" not in saved.name


def test_repaired_name_still_cannot_escape_dest_dir(client, tmp_path):
    """`_hw_sanitize_filename` runs after the repair, so a repaired name that
    carries traversal or characters illegal on Windows is still neutralised."""
    client.default = FakeResponse(
        content=b"x", headers={"content-type": DOCX_CT, "content-disposition":
                               'inline; filename="%s"' % mojibake("../../zlé/český?.pdf")})
    res = m.download_attachment(url="/elearning/ruqjzfpv?z%3Aabc",
                               dest_dir=str(tmp_path), subdomain=SUB)
    assert not res.get("isError")
    saved = Path(res["saved_to"])
    assert saved.parent == tmp_path
    assert "/" not in saved.name and ".." not in saved.name
    assert saved.name == m._hw_sanitize_filename("../../zlé/český?.pdf")
    assert not (tmp_path.parent / "zlé").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [saved.name]

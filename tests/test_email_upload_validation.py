"""Magic-byte validation must accept .eml (text) and .msg (OLE) uploads while
still rejecting content whose bytes don't match the claimed extension."""

from document_search.services.upload_validation import magic_matches_extension

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 48


def test_eml_text_content_is_accepted():
    content = b"From: a@b.com\r\nSubject: hi\r\n\r\nplain body text\r\n"
    ok, reason = magic_matches_extension(content, ".eml")
    assert ok, reason


def test_msg_ole_content_is_accepted():
    ok, reason = magic_matches_extension(_OLE_MAGIC, ".msg")
    assert ok, reason


def test_msg_rejects_renamed_executable():
    ok, _ = magic_matches_extension(b"MZ\x90\x00\x03\x00\x00\x00", ".msg")
    assert not ok


def test_eml_rejects_binary_content():
    ok, _ = magic_matches_extension(b"\x00\x01\x02\x03\xff\xfe", ".eml")
    assert not ok

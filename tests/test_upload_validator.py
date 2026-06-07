from document_search.services.upload_validation import (
    is_within,
    magic_matches_extension,
    reject_traversal,
)


def test_is_within_accepts_child(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    assert is_within(root, root / "sub" / "a.pdf") is True


def test_is_within_rejects_parent_escape(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    assert is_within(root, tmp_path / "etc" / "passwd") is False


def test_reject_traversal_flags_dotdot():
    assert reject_traversal("../../etc/passwd") is True
    assert reject_traversal("a/../../b") is True
    assert reject_traversal("normal/sub/dir") is False
    assert reject_traversal("") is False


PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"
EXE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"
TXT_BYTES = b"hello world, this is a plain text note.\n"


def test_magic_accepts_real_pdf_as_pdf():
    ok, _ = magic_matches_extension(PDF_BYTES, ".pdf")
    assert ok is True


def test_magic_rejects_exe_renamed_to_pdf():
    ok, reason = magic_matches_extension(EXE_BYTES, ".pdf")
    assert ok is False
    assert "pdf" in reason.lower()


def test_magic_accepts_plain_text_as_txt():
    ok, _ = magic_matches_extension(TXT_BYTES, ".txt")
    assert ok is True


def test_magic_rejects_binary_blob_as_txt():
    ok, _ = magic_matches_extension(EXE_BYTES, ".txt")
    assert ok is False

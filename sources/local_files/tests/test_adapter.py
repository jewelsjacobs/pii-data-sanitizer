"""Tests for the local-files adapter.

Fixtures are built on-the-fly from tmp_path, so no binary blobs ship
with the repo. Every filename and content string here is synthetic.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sources.local_files.adapter import (
    AdapterError,
    extract_files,
)

# ---------------------------------------------------------------------------
# Plain text / markdown
# ---------------------------------------------------------------------------


def test_txt_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("Hello, world.\nSecond line.")
    [record] = list(extract_files([p]))
    assert record["source"] == "local-files"
    assert record["content"] == "Hello, world.\nSecond line."
    assert record["metadata"]["format"] == "txt"
    assert record["metadata"]["extraction_method"] == "native"
    assert record["metadata"]["path"] == str(p.resolve())
    assert record["source_id"].startswith("sha256:")
    assert record["metadata"]["size_bytes"] == len(b"Hello, world.\nSecond line.")


def test_md_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "notes.md"
    p.write_text("# Heading\n\nSome body text.")
    [record] = list(extract_files([p]))
    assert record["metadata"]["format"] == "md"
    assert "# Heading" in record["content"]


def test_txt_non_utf8_halts(tmp_path: Path) -> None:
    p = tmp_path / "latin1.txt"
    p.write_bytes("caf\xe9".encode("latin-1"))  # 0xE9 is invalid UTF-8
    with pytest.raises(AdapterError, match="UTF-8 decode error"):
        list(extract_files([p]))


# ---------------------------------------------------------------------------
# RTF
# ---------------------------------------------------------------------------


def test_rtf_basic(tmp_path: Path) -> None:
    rtf = (
        r"{\rtf1\ansi\ansicpg1252\cocoartf2580"
        r"\fonttbl\f0\fswiss\fcharset0 Helvetica;"
        r"}"
        r"{\colortbl;\red255\green255\blue255;}"
        r"\pard\qj\f0\fs24 Hello RTF world.}"
    )
    p = tmp_path / "note.rtf"
    p.write_text(rtf)
    [record] = list(extract_files([p]))
    assert "Hello RTF world." in record["content"]
    assert record["metadata"]["format"] == "rtf"


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def _make_docx(path: Path, paragraphs: list[str]) -> None:
    import docx

    document = docx.Document()
    for para in paragraphs:
        document.add_paragraph(para)
    document.save(str(path))


def test_docx_paragraphs(tmp_path: Path) -> None:
    p = tmp_path / "memo.docx"
    _make_docx(p, ["First para.", "Second para.", "Third para."])
    [record] = list(extract_files([p]))
    assert "First para." in record["content"]
    assert "Second para." in record["content"]
    assert "Third para." in record["content"]
    assert record["metadata"]["format"] == "docx"
    assert record["metadata"]["extraction_method"] == "native"


def test_docx_tables_included(tmp_path: Path) -> None:
    import docx

    p = tmp_path / "with-table.docx"
    document = docx.Document()
    document.add_paragraph("Above table.")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "cell-a"
    table.rows[0].cells[1].text = "cell-b"
    document.save(str(p))

    [record] = list(extract_files([p]))
    assert "Above table." in record["content"]
    assert "cell-a" in record["content"]
    assert "cell-b" in record["content"]


def test_corrupted_docx_halts(tmp_path: Path) -> None:
    p = tmp_path / "bogus.docx"
    p.write_bytes(b"not a docx")
    with pytest.raises(AdapterError, match="not a valid .docx"):
        list(extract_files([p]))


# ---------------------------------------------------------------------------
# PDF — text layer
# ---------------------------------------------------------------------------


def _make_pdf_with_text(path: Path, lines: list[str]) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    y = 750
    for line in lines:
        c.drawString(50, y, line)
        y -= 20
    c.showPage()
    c.save()


def test_pdf_text_layer(tmp_path: Path) -> None:
    p = tmp_path / "doc.pdf"
    _make_pdf_with_text(p, ["Quarterly report.", "Revenue is up."])
    [record] = list(extract_files([p]))
    assert "Quarterly report" in record["content"]
    assert "Revenue is up" in record["content"]
    assert record["metadata"]["format"] == "pdf"
    assert record["metadata"]["extraction_method"] == "text_layer"
    assert record["metadata"]["ocr_low_confidence"] is False


def test_pdf_corrupted_halts(tmp_path: Path) -> None:
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"not actually a pdf")
    with pytest.raises(AdapterError, match="pdf open error"):
        list(extract_files([p]))


# ---------------------------------------------------------------------------
# PDF — OCR fallback
# ---------------------------------------------------------------------------


def _make_scanned_pdf(path: Path) -> None:
    """Produce a PDF with an embedded raster and no text layer."""
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    # Render some text into a PNG, then drop that PNG onto a PDF page.
    img_path = path.with_suffix(".png")
    img = Image.new("RGB", (400, 100), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 28)
    except OSError:
        font = ImageFont.load_default()
    draw.text((10, 30), "Scanned Text Sample", fill="black", font=font)
    img.save(img_path)

    c = canvas.Canvas(str(path), pagesize=letter)
    c.drawImage(str(img_path), 50, 600, width=400, height=100)
    c.showPage()
    c.save()
    img_path.unlink()


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract binary not installed")
def test_pdf_ocr_fallback(tmp_path: Path) -> None:
    p = tmp_path / "scan.pdf"
    _make_scanned_pdf(p)
    [record] = list(extract_files([p]))
    assert record["metadata"]["extraction_method"] == "ocr"
    # Exact OCR text varies with the installed tesseract version, but the
    # output should be non-empty and contain at least one recognisable token.
    assert record["content"].strip()


# ---------------------------------------------------------------------------
# Directory walk + dispatch
# ---------------------------------------------------------------------------


def test_directory_walk_dispatches_by_extension(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.md").write_text("# beta")
    (tmp_path / "c.log").write_text("ignored — not a supported extension")
    records = list(extract_files([tmp_path]))
    formats = sorted(r["metadata"]["format"] for r in records)
    assert formats == ["md", "txt"]  # .log silently skipped in a walk


def test_explicit_unsupported_file_halts(tmp_path: Path) -> None:
    p = tmp_path / "a.log"
    p.write_text("hello")
    with pytest.raises(AdapterError, match="unsupported extension"):
        list(extract_files([p]))


def test_dot_doc_halts_with_guidance(tmp_path: Path) -> None:
    p = tmp_path / "legacy.doc"
    p.write_bytes(b"legacy binary format, unsupported")
    with pytest.raises(AdapterError, match="Convert to .docx or .pdf"):
        list(extract_files([p]))


def test_idempotent_on_same_input(tmp_path: Path) -> None:
    p = tmp_path / "note.txt"
    p.write_text("stable")
    first = list(extract_files([p]))
    second = list(extract_files([p]))
    assert first == second


def test_missing_path_halts(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="Not a file or directory"):
        list(extract_files([tmp_path / "does-not-exist"]))

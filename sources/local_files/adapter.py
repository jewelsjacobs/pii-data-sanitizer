"""Local-files source adapter — Stage 1 extract for document files.

Reads ``.txt``, ``.md``, ``.rtf``, ``.docx``, and ``.pdf`` (text-layer or
OCR'd) from configured source directories and emits normalized records
matching files/pipeline-architecture.md → Stage 1. Never redacts, never
writes outside ``/raw-staging/local-files/``. Halts loudly on decode
errors, encrypted files, password-protected PDFs, corrupted inputs, or
unsupported formats.

Legacy ``.doc`` (Word 97-2003) is intentionally **not** supported right
now — it's rare in practice and the existing Python tooling (textract,
antiword) has heavy or fragile install requirements. Convert to
``.docx`` or ``.pdf`` and re-run.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

SUPPORTED_EXTENSIONS = frozenset({".txt", ".md", ".rtf", ".docx", ".pdf"})
UNSUPPORTED_EXTENSIONS = frozenset({".doc"})

# OCR fallback thresholds per files/source-adapters.md → PDF OCR Policy.
OCR_RESOLUTION = 300
OCR_CONFIDENCE_THRESHOLD = 60.0


class AdapterError(RuntimeError):
    """Raised when the adapter encounters input it refuses to pass through."""


def extract_files(
    paths: list[Path],
    *,
    include_extensions: frozenset[str] | None = None,
    skip_unsupported: bool = False,
    continue_on_error: bool = False,
    exclude_dirs: list[Path] | None = None,
    exclude_dir_names: list[str] | None = None,
    modified_since: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield normalized records from every supported file under ``paths``.

    Each entry is either a single file or a directory walked recursively.
    Unknown / unsupported extensions inside a walk are skipped silently
    (most directories have junk) — but a file explicitly named on the
    command line with an unsupported extension raises.

    ``include_extensions`` restricts the walk to a subset of
    ``SUPPORTED_EXTENSIONS``. Useful when a source tree is dominated by
    one extension you don't care about (e.g. a third-party Obsidian
    vault syncing 100k ``.md`` files into iCloud Drive). Pass extensions
    with leading dots, lowercase, e.g. ``frozenset({".pdf", ".docx"})``.
    Any extension passed here that isn't in ``SUPPORTED_EXTENSIONS`` is
    rejected — this is a filter, not a way to add new format support.

    ``skip_unsupported=True`` turns the ``.doc`` halt into a stderr
    warning + skip. Default ``False`` preserves the fail-loud behavior
    documented in source-adapters.md; set ``True`` only when you've
    decided to accept the loss (e.g. a one-shot historical archive run
    where you'd rather get most of the files than convert 100 .doc
    files first).

    ``continue_on_error=True`` turns per-file extraction failures
    (corrupted PDFs, password-protected docx, UTF-8 decode failures,
    permission denied on iCloud stubs, etc.) into stderr warnings + skip
    instead of halting the whole run. Default ``False`` matches the
    fail-loud policy. Use with the same intent as ``skip_unsupported``
    — one-shot historical runs across heterogeneous archives where a
    handful of broken files shouldn't stop you from ingesting the
    other 16,995.

    ``exclude_dirs`` skips any file whose path is under one of the
    given directories. Use this to prune known-noise subtrees (icon
    libraries, third-party Obsidian vaults syncing into iCloud,
    corrupted Dropbox-to-iCloud migration dumps) without having to
    enumerate all the *good* dirs.

    ``exclude_dir_names`` skips any file under a directory whose
    *name* matches one of the given strings, anywhere in the tree.
    Use for dev-junk that recurs throughout the source tree
    (``node_modules``, ``.git``, ``__pycache__``, ``.venv``).
    Distinct from ``exclude_dirs`` which takes absolute paths.

    ``modified_since`` (``YYYY-MM-DD``) skips any file whose mtime date
    (UTC — same clock the record ``timestamp`` uses, so the filter is
    consistent with sanitize's ``--since`` window) is before the given
    date, *before* the file is opened or its extension policy applied.
    This is the incremental-run mode from source-adapters.md → Open
    Items: a windowed vault update only reads files touched in the
    window, so legacy formats and broken encodings in the untouched
    archive can't halt the run.
    """
    if include_extensions is not None:
        bad = include_extensions - SUPPORTED_EXTENSIONS
        if bad:
            raise AdapterError(
                f"--include-extensions includes unsupported entries: {sorted(bad)}. "
                f"Allowed: {sorted(SUPPORTED_EXTENSIONS)}"
            )
        active_extensions = include_extensions
    else:
        active_extensions = SUPPORTED_EXTENSIONS

    excluded = [Path(d).expanduser().resolve() for d in (exclude_dirs or [])]
    excluded_names = frozenset(exclude_dir_names or [])

    files_seen = 0
    files_extracted = 0
    files_skipped = 0
    files_pre_window = 0
    progress_every = 100

    def _before_window(file: Path) -> bool:
        """True when the file's UTC mtime date falls before ``modified_since``."""
        if modified_since is None:
            return False
        mtime_day = datetime.fromtimestamp(file.stat().st_mtime, tz=UTC).date().isoformat()
        return mtime_day < modified_since

    def _heartbeat() -> None:
        if files_seen and files_seen % progress_every == 0:
            print(
                f"  ...processed {files_seen} files "
                f"({files_extracted} extracted, {files_skipped} skipped)",
                file=sys.stderr,
                flush=True,
            )

    for entry in paths:
        entry = Path(entry)
        if entry.is_dir():
            for file in sorted(entry.rglob("*")):
                if not file.is_file():
                    continue
                if _is_under_any(file, excluded):
                    continue
                if excluded_names and _has_excluded_ancestor_name(file, entry, excluded_names):
                    continue
                # mtime window check comes BEFORE extension policy: an old
                # legacy .doc or broken-encoding file outside the window
                # must not halt a windowed incremental run.
                if _before_window(file):
                    files_pre_window += 1
                    continue
                ext = file.suffix.lower()
                if ext in UNSUPPORTED_EXTENSIONS:
                    if skip_unsupported:
                        print(
                            f"warning: {file}: legacy .doc skipped (--skip-unsupported)",
                            file=sys.stderr,
                        )
                        files_seen += 1
                        files_skipped += 1
                        _heartbeat()
                        continue
                    raise AdapterError(
                        f"{file}: legacy .doc is not supported. "
                        f"Convert to .docx or .pdf and re-run."
                    )
                if ext in active_extensions:
                    files_seen += 1
                    record = _safe_extract(file, continue_on_error=continue_on_error)
                    if record is not None:
                        files_extracted += 1
                        yield record
                    else:
                        files_skipped += 1
                    _heartbeat()
        elif entry.is_file():
            if _is_under_any(entry, excluded):
                continue
            if _before_window(entry):
                files_pre_window += 1
                continue
            ext = entry.suffix.lower()
            if ext in UNSUPPORTED_EXTENSIONS:
                if skip_unsupported:
                    print(
                        f"warning: {entry}: legacy .doc skipped (--skip-unsupported)",
                        file=sys.stderr,
                    )
                    continue
                raise AdapterError(
                    f"{entry}: legacy .doc is not supported. Convert to .docx or .pdf and re-run."
                )
            if ext not in active_extensions:
                raise AdapterError(f"{entry}: unsupported extension {ext!r}")
            record = _safe_extract(entry, continue_on_error=continue_on_error)
            if record is not None:
                yield record
        else:
            raise AdapterError(f"Not a file or directory: {entry}")

    # Final summary, regardless of whether the periodic threshold landed exactly.
    if files_seen or files_pre_window:
        window_note = (
            f", {files_pre_window} outside --modified-since window" if modified_since else ""
        )
        print(
            f"  total: {files_seen} files visited "
            f"({files_extracted} extracted, {files_skipped} skipped{window_note})",
            file=sys.stderr,
            flush=True,
        )


def _is_under_any(path: Path, dirs: list[Path]) -> bool:
    """True iff ``path`` (resolved) lives under one of ``dirs`` (already resolved)."""
    if not dirs:
        return False
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for d in dirs:
        try:
            resolved.relative_to(d)
            return True
        except ValueError:
            continue
    return False


def _has_excluded_ancestor_name(file: Path, root: Path, excluded_names: frozenset[str]) -> bool:
    """True iff any ancestor directory of ``file`` (between ``root`` and
    the file itself) has a name in ``excluded_names``.

    Used for name-based exclusion of recurring dev-junk subtrees:
    ``node_modules`` appears in 100+ places under a typical projects
    directory; a name-based check is the only practical way to skip
    them all without enumerating every absolute path.
    """
    if not excluded_names:
        return False
    try:
        rel = file.relative_to(root)
    except ValueError:
        return False
    # rel.parts excludes ``root`` and includes the file's own basename
    # at the end. We only care about directory components, so we slice
    # off the last entry.
    for part in rel.parts[:-1]:
        if part in excluded_names:
            return True
    return False


def _safe_extract(file: Path, *, continue_on_error: bool) -> dict[str, Any] | None:
    """Run ``_file_to_record`` with optional per-file error tolerance.

    Returns ``None`` when extraction failed and ``continue_on_error``
    swallowed the exception (a stderr warning was logged). Otherwise
    returns the record, or re-raises.
    """
    try:
        return _file_to_record(file)
    except (AdapterError, OSError) as exc:
        if continue_on_error:
            print(
                f"warning: {file}: extraction failed ({exc}); skipped (--continue-on-error)",
                file=sys.stderr,
            )
            return None
        raise


def _file_to_record(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    source_id = "sha256:" + sha256(data).hexdigest()
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    size_bytes = len(data)
    ext = path.suffix.lower()

    content, extraction_method, page_count, ocr_low_confidence = _extract(path, data, ext)

    return {
        "source": "local-files",
        "source_id": source_id,
        "timestamp": mtime,
        "content": content,
        "metadata": {
            "path": str(path.resolve()),
            "format": ext.lstrip("."),
            "extraction_method": extraction_method,
            "ocr_low_confidence": ocr_low_confidence,
            "page_count": page_count,
            "size_bytes": size_bytes,
        },
    }


def _extract(path: Path, data: bytes, ext: str) -> tuple[str, str, int, bool]:
    """Dispatch to the per-format extractor.

    Returns ``(content, extraction_method, page_count, ocr_low_confidence)``.
    ``extraction_method`` is one of ``"native"``, ``"text_layer"``, ``"ocr"``.
    """
    if ext in {".txt", ".md"}:
        return _extract_text(path, data), "native", 1, False
    if ext == ".rtf":
        return _extract_rtf(path, data), "native", 1, False
    if ext == ".docx":
        content, pages = _extract_docx(path)
        return content, "native", pages, False
    if ext == ".pdf":
        return _extract_pdf(path)
    raise AdapterError(f"{path}: no extractor for extension {ext!r}")


def _extract_text(path: Path, data: bytes) -> str:
    """UTF-8 only. Halt on decode errors rather than silently swap encodings."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdapterError(f"{path}: UTF-8 decode error: {exc}") from exc


def _extract_rtf(path: Path, data: bytes) -> str:
    from striprtf.striprtf import rtf_to_text

    try:
        text = data.decode("utf-8", errors="replace")
    except Exception as exc:  # unreachable with errors=replace but keeps symmetry
        raise AdapterError(f"{path}: rtf decode error: {exc}") from exc
    try:
        return rtf_to_text(text)
    except Exception as exc:
        raise AdapterError(f"{path}: rtf parse error: {exc}") from exc


def _extract_docx(path: Path) -> tuple[str, int]:
    """Paragraphs + table cell text. Headers/footers included. Comments
    skipped. Halt on encrypted / password-protected / corrupted documents.
    """
    try:
        import docx
        from docx.opc.exceptions import PackageNotFoundError
    except ImportError as exc:  # pragma: no cover — declared in pyproject
        raise AdapterError(f"{path}: python-docx not installed: {exc}") from exc

    try:
        document = docx.Document(str(path))
    except PackageNotFoundError as exc:
        raise AdapterError(f"{path}: not a valid .docx (corrupted or encrypted): {exc}") from exc
    except Exception as exc:
        raise AdapterError(f"{path}: docx parse error: {exc}") from exc

    parts: list[str] = []
    for para in document.paragraphs:
        if para.text:
            parts.append(para.text)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text:
                    parts.append(cell.text)
    # Headers / footers, if present.
    for section in document.sections:
        for container in (section.header, section.footer):
            for para in container.paragraphs:
                if para.text:
                    parts.append(para.text)

    # Rough "page" count: docx doesn't carry hard page boundaries without
    # rendering; report 1 as a conservative placeholder.
    return "\n\n".join(parts), 1


def _extract_pdf(path: Path) -> tuple[str, str, int, bool]:
    """Prefer the text layer; fall back to Tesseract OCR when it's missing
    or empty. Halt on encrypted PDFs (no bypass attempt).
    """
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise AdapterError(f"{path}: pdfplumber not installed: {exc}") from exc

    try:
        pdf = pdfplumber.open(str(path))
    except Exception as exc:
        raise AdapterError(f"{path}: pdf open error: {exc}") from exc

    with pdf:
        if getattr(pdf, "is_encrypted", False):
            raise AdapterError(f"{path}: password-protected PDF; refusing to bypass.")

        page_count = len(pdf.pages)

        # Try text layer first.
        text_pages = [page.extract_text() or "" for page in pdf.pages]
        joined = "\n\n".join(p for p in text_pages if p.strip())
        if joined.strip():
            return joined, "text_layer", page_count, False

        # Fall back to OCR per files/source-adapters.md → PDF OCR Policy.
        return _ocr_pdf(path, pdf, page_count)


def _ocr_pdf(path: Path, pdf: Any, page_count: int) -> tuple[str, str, int, bool]:
    try:
        import pytesseract
    except ImportError as exc:  # pragma: no cover
        raise AdapterError(f"{path}: pytesseract not installed: {exc}") from exc

    ocr_parts: list[str] = []
    low_confidence_any = False
    for page in pdf.pages:
        image = page.to_image(resolution=OCR_RESOLUTION).original
        try:
            data = pytesseract.image_to_data(
                image,
                lang="eng",
                output_type=pytesseract.Output.DICT,
            )
        except pytesseract.TesseractNotFoundError as exc:
            raise AdapterError(
                f"{path}: tesseract binary not found. Install via `brew install tesseract` "
                f"or see files/source-adapters.md → PDF OCR Policy."
            ) from exc
        except Exception as exc:
            raise AdapterError(f"{path}: OCR failed: {exc}") from exc

        confidences = [
            float(c)
            for c in data.get("conf", [])
            if c not in ("-1", "", None) and str(c).strip() != "-1"
        ]
        avg = sum(confidences) / len(confidences) if confidences else 0.0
        if avg < OCR_CONFIDENCE_THRESHOLD:
            low_confidence_any = True

        words = [w for w in data.get("text", []) if w and w.strip()]
        ocr_parts.append(" ".join(words))

    return "\n\n".join(p for p in ocr_parts if p), "ocr", page_count, low_confidence_any

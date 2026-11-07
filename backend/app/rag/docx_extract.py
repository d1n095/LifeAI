"""Complete, deterministic DOCX text extraction for the ingestion pipeline.

Replaces the previous `"\n".join(p.text for p in doc.paragraphs)` in app/rag/extract.py, which
read ONLY body-level paragraphs and therefore silently dropped every table (cells, nested
tables), every header and every footer -- a measured fidelity gap (see
docs/LIFE_MASTER_BLUEPRINT.md §1.2). This walks the document in true reading order and renders
tables and headers/footers too.

Design constraints (deliberate):
  - REUSE python-docx, the parser already in the pipeline -- no second parser, no new dependency.
  - Interface unchanged: app/rag/extract.py still calls one function returning `str`, so chunking,
    provenance, embedding and retrieval downstream are untouched.
  - Never execute macros/embedded code: python-docx only reads the OOXML parts; a vbaProject is
    never evaluated. We also never follow external relationships.
  - Original bytes are never modified -- we parse a BytesIO copy of the received content.
  - NEVER silently discard unsupported content: constructs this extractor does not yet capture
    (text boxes, footnotes/endnotes, comments, SmartArt/drawings) are DETECTED and appended as an
    explicit limitation note, so a reader/auditor knows something was present but unextracted.
  - Deterministic output for identical input, and bounded (character + nesting caps) so a crafted
    document cannot exhaust memory.
"""

from __future__ import annotations

import re
from io import BytesIO

from docx import Document
from docx.document import Document as _Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

# Bounded output: a crafted DOCX (deeply nested / millions of cells) must never produce unbounded
# text. The caller (app/rag/ingest.py -> chunking) has its own per-chunk ceiling, but extraction
# itself is capped first. Both are advisory limits, not security boundaries (upload size is gated
# upstream in storage/zip_import); they exist so one pathological file fails cleanly.
MAX_OUTPUT_CHARS = 5_000_000
MAX_TABLE_NESTING = 10

CELL_SEP = " | "            # between cells in a rendered table row
NESTED_ROW_SEP = " / "      # between rows of a table nested inside a cell (kept on one line)
_HEADER_LABEL = "[sidhuvud]"
_FOOTER_LABEL = "[sidfot]"

# Local tag names (namespace-insensitive) of constructs we detect-but-do-not-yet-extract, so they
# are reported rather than silently lost. Matched against the serialized document XML.
_UNSUPPORTED_MARKERS = {
    "txbxContent": "textrutor",
    "footnoteReference": "fotnoter",
    "endnoteReference": "slutkommentarer",
    "commentReference": "kommentarer",
}


class DocxExtractionError(ValueError):
    """Raised for a document python-docx cannot open (corrupt/not a real .docx). The caller
    (app/rag/library_import.py) already maps any extraction exception to IndexStatus.
    extraction_failed, so the received original is preserved and the failure is visible."""


def _iter_block_items(parent_elm, parent):
    """Yield Paragraph / Table objects in document order from a body-or-cell element.

    The canonical python-docx recipe: body-level content interleaves <w:p> and <w:tbl>, and
    `doc.paragraphs` / `doc.tables` each see only one kind and lose their relative order. Iterating
    the element's own children is the only way to recover true reading order."""
    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def _dedup_row_cells(row):
    """Cells of a row with horizontally-merged cells repeat the SAME `<w:tc>` once per grid column
    in python-docx. Emitting each would duplicate the merged cell's text across columns. Collapse
    consecutive references to the same underlying element."""
    seen_tc = None
    for cell in row.cells:
        if cell._tc is seen_tc:
            continue
        seen_tc = cell._tc
        yield cell


def _render_cell_inline(cell: _Cell, depth: int) -> str:
    """A cell's content as one line: its paragraphs joined by spaces, any nested table inlined
    (rows by NESTED_ROW_SEP, cells by CELL_SEP) so the parent row stays a single line. Recursion is
    depth-capped; beyond the cap the nested table is marked, never silently dropped."""
    parts: list[str] = []
    for block in _iter_block_items(cell._tc, cell):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if text:
                parts.append(text)
        else:  # nested Table
            if depth >= MAX_TABLE_NESTING:
                parts.append("[nästlad tabell utelämnad: för djup]")
                continue
            nested_rows = [
                CELL_SEP.join(_render_cell_inline(c, depth + 1) for c in _dedup_row_cells(r))
                for r in block.rows
            ]
            parts.append(NESTED_ROW_SEP.join(nested_rows))
    return " ".join(parts)


def _render_table_lines(table: Table, depth: int) -> list[str]:
    """A top-level table as one line per row (cells joined by CELL_SEP), preserving row/column
    adjacency and order."""
    return [
        CELL_SEP.join(_render_cell_inline(cell, depth + 1) for cell in _dedup_row_cells(row))
        for row in table.rows
    ]


def _render_container(parent_elm, parent) -> list[str]:
    """Body-or-header/footer content as lines, in document order."""
    lines: list[str] = []
    for block in _iter_block_items(parent_elm, parent):
        if isinstance(block, Paragraph):
            lines.append(block.text)
        else:
            lines.extend(_render_table_lines(block, depth=0))
    return lines


def _collect_headers_footers(doc: _Document) -> tuple[list[str], list[str]]:
    """Distinct header and footer texts across all sections, order-preserving and de-duplicated
    (the same header/footer is repeated across sections/pages; we keep each unique block once)."""
    headers: list[str] = []
    footers: list[str] = []
    for section in doc.sections:
        for src, sink in ((section.header, headers), (section.footer, footers)):
            if getattr(src, "is_linked_to_previous", False):
                continue  # inherits the previous section's -- already collected
            text = "\n".join(_render_container(src._element, src)).strip()
            if text and text not in sink:
                sink.append(text)
    return headers, footers


def _detect_unsupported(doc: _Document) -> list[str]:
    """Report (do not extract) constructs this extractor does not yet capture, so nothing is lost
    silently. Scans the serialized body for their marker tags (namespace-insensitive)."""
    from lxml import etree

    xml = etree.tostring(doc.element.body, encoding="unicode")
    found = []
    for tag, label in _UNSUPPORTED_MARKERS.items():
        if tag in xml and label not in found:
            found.append(label)
    return found


def _normalize(text: str) -> str:
    """Collapse 3+ blank lines to one blank line and strip trailing whitespace per line -- a
    deterministic tidy that never changes content, only runs of blank space."""
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def extract_docx_text(raw: bytes) -> str:
    """Full-fidelity DOCX → text: headers, then body (paragraphs + tables + nested tables in
    reading order), then footers, then an explicit note for any detected-but-unextracted content.
    Deterministic and bounded. Raises DocxExtractionError if the bytes are not a readable .docx."""
    if not raw:
        raise DocxExtractionError("tomt dokument")
    try:
        doc = Document(BytesIO(raw))
    except Exception as exc:  # noqa: BLE001 - python-docx raises several unrelated types for bad input
        raise DocxExtractionError(f"kunde inte öppna .docx: {type(exc).__name__}") from exc

    headers, footers = _collect_headers_footers(doc)
    body_lines = _render_container(doc.element.body, doc)

    sections: list[str] = []
    for header in headers:
        sections.append(f"{_HEADER_LABEL}\n{header}")
    sections.append("\n".join(body_lines))
    for footer in footers:
        sections.append(f"{_FOOTER_LABEL}\n{footer}")

    unsupported = _detect_unsupported(doc)
    if unsupported:
        sections.append(
            "[extraktionsbegränsning: följande upptäcktes men extraheras inte ännu: "
            + ", ".join(unsupported)
            + "]"
        )

    out = _normalize("\n".join(s for s in sections if s))

    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + "\n[extraktion avkortad: dokumentet överskred storleksgränsen]"
    return out

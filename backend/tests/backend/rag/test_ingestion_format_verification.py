"""Representative, executable verification of Life's real ingestion / "read and understand"
path, using synthetic documents and NO AI provider.

Founder directive (2026-11-09, "Life first"): "Verify the actual ingestion path using
representative synthetic documents. Identify unsupported formats and incomplete integration
honestly." This module exercises the REAL extraction dispatch (`app.rag.extract.extract_text`),
the REAL historical-vs-current trust enforcement (`app.rag.trust.assess_confidence`), and the
REAL source-reference provenance (`DocumentChunk` → `Document`) — reusing existing services, never
a second memory system, and never bypassing the provider-gated embed/index step.

HONEST FORMAT-SUPPORT MATRIX on default `64c07d5b` (pinned by the tests below):

  format        | extract_text behavior                                  | honest status
  --------------|--------------------------------------------------------|------------------------
  .txt .md      | raw UTF-8, content preserved                            | SUPPORTED
  .markdown     | raw UTF-8 (same path)                                   | SUPPORTED
  .html .htm    | BeautifulSoup get_text — tags stripped, text kept       | SUPPORTED
  .pdf          | pypdf text layer only; NO OCR                           | PARTIAL (text-layer PDFs
                |                                                        |   only; scans → empty)
  .docx         | paragraphs only — TABLE / header / footer text DROPPED  | PARTIAL (completeness fix
                |                                                        |   is unmerged PR #327)
  .json         | raw UTF-8 passthrough — NOT structured                  | RAW-ONLY (structured
                |                                                        |   ChatGPT-export importer
                |                                                        |   is unbuilt; spec PR #369)
  .csv          | raw UTF-8 passthrough — NOT parsed into columns         | RAW-ONLY
  images/media  | handled by a separate path (app.rag.media_import),      | OUT-OF-SCOPE here
                |   not extract_text                                      |

The embed/index step (`app.rag.ingest.index_document`) is provider-gated: it calls
`resolve_active(role="embedding")` / `embed_with_policy` and PAUSES a document on
`awaiting_provider`/`blocked_provider` when no verified provider exists. That step is deliberately
NOT exercised here (no provider activation; `first_cycle_go=false`) — these tests cover the
model-independent part of the path: extraction, truth-classification, and provenance.
"""

from io import BytesIO

import pytest
from docx import Document as DocxDocument
from pypdf import PdfWriter
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.rag.extract import extract_text
from app.rag.trust import HIGH_THRESHOLD, NON_ACTIVE_STATUSES, assess_confidence
from app.request_context import current_user_id as current_user_id_var

EMBEDDING_DIM = get_settings().embedding_dim


# ---------------------------------------------------------------------------------------------
# 1. Format extraction matrix — the REAL extract_text dispatch, provider-free, deterministic.
# ---------------------------------------------------------------------------------------------

def test_plain_text_and_markdown_are_preserved_verbatim():
    assert extract_text("note.txt", "ren text rad 1\nrad 2".encode("utf-8")) == "ren text rad 1\nrad 2"
    md = "# Rubrik\n\nEtt stycke med **fetstil**."
    assert extract_text("doc.md", md.encode("utf-8")) == md
    assert extract_text("doc.markdown", md.encode("utf-8")) == md


def test_html_tags_are_stripped_and_text_is_kept():
    html = "<html><body><h1>Rubrik</h1><p>Brödtext här.</p></body></html>"
    out = extract_text("page.html", html.encode("utf-8"))
    assert "Rubrik" in out
    assert "Brödtext här." in out
    assert "<h1>" not in out and "<p>" not in out


def test_docx_extracts_paragraphs_but_NOT_table_text_on_default():
    """Honest incomplete-integration finding: default extract_text reads only
    `doc.paragraphs`, so table / nested-table / header / footer text is silently dropped. The
    completeness fix is unmerged PR #327 — this test pins the CURRENT behavior so the gap is
    explicit and tracked, not assumed solved."""
    doc = DocxDocument()
    doc.add_paragraph("Grundaren beslutade 2019 i Stockholm.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "TABELLCELL_ALPHA"
    table.rows[0].cells[1].text = "TABELLCELL_BETA"
    buf = BytesIO()
    doc.save(buf)
    extracted = extract_text("beslut.docx", buf.getvalue())

    assert "Grundaren beslutade 2019 i Stockholm." in extracted  # paragraph text IS read
    assert "TABELLCELL_ALPHA" not in extracted  # table text is NOT read on default (gap: #327)
    assert "TABELLCELL_BETA" not in extracted


def test_pdf_without_a_text_layer_yields_no_text_no_ocr():
    """Honest finding: extraction is pypdf text-layer only. A scanned/image-only PDF (here a
    blank page stands in for "no extractable text layer") yields empty text — there is NO OCR.
    A real text-layer PDF would extract; an image-only one silently yields nothing."""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = BytesIO()
    writer.write(buf)
    extracted = extract_text("scan.pdf", buf.getvalue())
    assert extracted.strip() == ""


def test_json_is_raw_passthrough_not_structured_import():
    """Honest finding: a .json file is decoded as raw UTF-8, NOT parsed into structure. A
    ChatGPT-style export is therefore ingested as an opaque blob, not as conversations — the
    structured importer is unbuilt (spec: PR #369)."""
    payload = '{"mapping": {"n1": {"message": {"content": {"parts": ["hemligt svar"]}}}}}'
    extracted = extract_text("conversations.json", payload.encode("utf-8"))
    assert extracted == payload  # byte-for-byte passthrough, no JSON structure understood


def test_csv_is_raw_passthrough_not_parsed_into_columns():
    csv_text = "namn,stad\nAcme,Stockholm\nBolag,Göteborg\n"
    extracted = extract_text("sales.csv", csv_text.encode("utf-8"))
    assert extracted == csv_text  # raw text, not a parsed table


def test_extraction_is_deterministic_and_needs_no_provider():
    """The whole read path above touches no network and no AI provider: same bytes in, same
    text out, twice."""
    raw = "Determinismövning".encode("utf-8")
    assert extract_text("a.txt", raw) == extract_text("a.txt", raw)


# ---------------------------------------------------------------------------------------------
# 2. Historical vs current truth — the REAL enforcement point, pure and provider-free.
# ---------------------------------------------------------------------------------------------

def test_historical_source_is_distinguished_from_current_and_never_manufactures_high_confidence():
    """`assess_confidence` must downgrade an otherwise-high similarity hit when the founder has
    classified the source as historical/superseded/disputed, and expose that status
    machine-readably — this is how Life distinguishes historical information from current
    truth (ActiveTruthStatus / app.rag.trust)."""
    strong = HIGH_THRESHOLD + 0.2
    current = assess_confidence([{"score": strong, "active_truth_status": "active"}])
    historical = assess_confidence([{"score": strong, "active_truth_status": "historical"}])

    assert current.level == "high"  # a current source at a strong score is high confidence
    assert historical.level == "medium"  # the SAME score, but historical, is capped below high
    assert historical.top_source_status == "historical"  # distinction is machine-readable
    assert "historical" in NON_ACTIVE_STATUSES


@pytest.mark.parametrize("status", sorted(NON_ACTIVE_STATUSES))
def test_every_non_active_status_caps_confidence_below_high(status):
    strong = HIGH_THRESHOLD + 0.2
    assessed = assess_confidence([{"score": strong, "active_truth_status": status}])
    assert assessed.level != "high"
    assert assessed.top_source_status == status


# ---------------------------------------------------------------------------------------------
# 3. Source references / provenance — a chunk ties back to its owning Document (DB, no provider).
# ---------------------------------------------------------------------------------------------

def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_document(session, owner_id, *, title, truth_status) -> Document:
    _set_rls_user(session, owner_id)
    document = Document(
        title=title,
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=truth_status,
    )
    session.add(document)
    session.commit()
    return document


def test_document_carries_its_truth_status_for_historical_vs_current(db_session, make_verified_user):
    """At the source level too, a historical document is durably distinguishable from a current
    one — the content-classification axis is separate from technical index status."""
    user, _ = make_verified_user()
    current = _make_document(db_session, user.id, title="Aktuell policy", truth_status=ActiveTruthStatus.active)
    historical = _make_document(db_session, user.id, title="Gammal policy", truth_status=ActiveTruthStatus.historical)

    _set_rls_user(db_session, user.id)
    db_session.refresh(current)
    db_session.refresh(historical)
    assert current.active_truth_status == ActiveTruthStatus.active
    assert historical.active_truth_status == ActiveTruthStatus.historical
    assert historical.active_truth_status.value in NON_ACTIVE_STATUSES


def test_chunk_references_its_source_document_for_provenance(db_session, make_verified_user):
    """Every retrievable unit carries a source reference back to its document and owner — the
    provenance a grounded answer cites. (Embedding is set directly here; no provider is used.)"""
    user, _ = make_verified_user()
    document = _make_document(
        db_session, user.id, title="Källa", truth_status=ActiveTruthStatus.active
    )

    _set_rls_user(db_session, user.id)
    chunk = DocumentChunk(
        document_id=document.id,
        owner_id=user.id,
        chunk_index=0,
        text="Bolaget grundades 2019.",
        embedding=[0.1] * EMBEDDING_DIM,
    )
    db_session.add(chunk)
    db_session.commit()

    _set_rls_user(db_session, user.id)
    reloaded = db_session.get(DocumentChunk, chunk.id)
    assert reloaded is not None
    assert reloaded.document_id == document.id  # back-reference to the source document
    assert reloaded.owner_id == user.id  # owner-scoped provenance
    # And the source document the chunk points at is resolvable (a citation can open it).
    source_doc = db_session.get(Document, reloaded.document_id)
    assert source_doc is not None
    assert source_doc.title == "Källa"

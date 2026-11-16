"""DOCX extraction completeness (app/rag/docx_extract.py + app/rag/extract.py docx branch).

Proves the measured fidelity gap is closed: the previous extractor read only body paragraphs and
silently dropped tables, nested tables, headers and footers. These build real .docx bytes with
python-docx and assert full, ordered, deterministic extraction -- plus safe handling of empty/
malformed input -- and an end-to-end check that table content now reaches the ingestion pipeline's
chunks (compatibility with the existing import path).
"""

import io

import pytest
from docx import Document

from app.rag.docx_extract import DocxExtractionError, extract_docx_text
from app.rag.extract import extract_text


def _docx_bytes(build) -> bytes:
    doc = Document()
    build(doc)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# --- core completeness ----------------------------------------------------------------------


def test_paragraphs_and_tables_in_document_order():
    def build(doc):
        doc.add_paragraph("FORSTA_STYCKET")
        t = doc.add_table(rows=2, cols=2)
        t.cell(0, 0).text = "R1C1"
        t.cell(0, 1).text = "R1C2"
        t.cell(1, 0).text = "R2C1"
        t.cell(1, 1).text = "R2C2"
        doc.add_paragraph("SISTA_STYCKET")

    out = extract_docx_text(_docx_bytes(build))
    # all content present
    for token in ("FORSTA_STYCKET", "R1C1", "R1C2", "R2C1", "R2C2", "SISTA_STYCKET"):
        assert token in out, token
    # order preserved: paragraph -> table -> paragraph
    assert out.index("FORSTA_STYCKET") < out.index("R1C1") < out.index("SISTA_STYCKET")
    # row/column adjacency: a row is one line with a cell separator
    assert "R1C1 | R1C2" in out
    assert "R2C1 | R2C2" in out


def test_nested_table_content_is_extracted():
    def build(doc):
        outer = doc.add_table(rows=1, cols=1)
        cell = outer.cell(0, 0)
        cell.paragraphs[0].text = "YTTRE_CELL"
        inner = cell.add_table(rows=1, cols=2)
        inner.cell(0, 0).text = "INRE_A"
        inner.cell(0, 1).text = "INRE_B"

    out = extract_docx_text(_docx_bytes(build))
    assert "YTTRE_CELL" in out
    assert "INRE_A" in out and "INRE_B" in out
    # nested cells kept adjacent on the parent line
    assert "INRE_A | INRE_B" in out


def test_headers_and_footers_are_extracted_and_labelled():
    def build(doc):
        doc.add_paragraph("KROPP")
        sec = doc.sections[0]
        sec.header.paragraphs[0].text = "SIDHUVUD_TEXT"
        sec.footer.paragraphs[0].text = "SIDFOT_TEXT"

    out = extract_docx_text(_docx_bytes(build))
    assert "SIDHUVUD_TEXT" in out and "SIDFOT_TEXT" in out
    assert "[sidhuvud]" in out and "[sidfot]" in out
    # header before body before footer
    assert out.index("SIDHUVUD_TEXT") < out.index("KROPP") < out.index("SIDFOT_TEXT")


def test_horizontally_merged_cell_text_not_duplicated():
    def build(doc):
        t = doc.add_table(rows=1, cols=3)
        a, b, c = t.cell(0, 0), t.cell(0, 1), t.cell(0, 2)
        merged = a.merge(b)
        merged.text = "SAMMANSLAGEN"
        c.text = "ENSAM"

    out = extract_docx_text(_docx_bytes(build))
    assert out.count("SAMMANSLAGEN") == 1  # merged cell not emitted once per grid column
    assert "ENSAM" in out


def test_unicode_and_swedish_text_preserved_exactly():
    def build(doc):
        doc.add_paragraph("Åäö ÅÄÖ — résumé ✓ 日本語")
        t = doc.add_table(rows=1, cols=1)
        t.cell(0, 0).text = "Smörgåsbord へ"

    out = extract_docx_text(_docx_bytes(build))
    assert "Åäö ÅÄÖ — résumé ✓ 日本語" in out
    assert "Smörgåsbord へ" in out


# --- safety / determinism -------------------------------------------------------------------


def test_empty_bytes_raise_extraction_error():
    with pytest.raises(DocxExtractionError):
        extract_docx_text(b"")


def test_malformed_bytes_raise_extraction_error_not_garbage():
    with pytest.raises(DocxExtractionError):
        extract_docx_text(b"this is definitely not a .docx package")


def test_zip_that_is_not_a_docx_raises_extraction_error():
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "not office xml")
    with pytest.raises(DocxExtractionError):
        extract_docx_text(buf.getvalue())


def test_output_is_deterministic_for_identical_input():
    def build(doc):
        doc.add_paragraph("P")
        t = doc.add_table(rows=2, cols=2)
        for r in range(2):
            for c in range(2):
                t.cell(r, c).text = f"c{r}{c}"

    raw = _docx_bytes(build)
    assert extract_docx_text(raw) == extract_docx_text(raw)


def test_extract_text_dispatches_docx_to_full_extractor():
    def build(doc):
        doc.add_paragraph("PARA")
        doc.add_table(rows=1, cols=1).cell(0, 0).text = "TABELLCELL_VIA_DISPATCH"

    out = extract_text("brev.docx", _docx_bytes(build))
    assert "PARA" in out and "TABELLCELL_VIA_DISPATCH" in out


def test_empty_document_yields_empty_string_not_error():
    out = extract_docx_text(_docx_bytes(lambda d: None))
    assert out == ""


# --- end-to-end compatibility with the real ingestion pipeline ------------------------------


@pytest.mark.asyncio
async def test_docx_table_content_reaches_ingestion_chunks(db_session, make_verified_user, monkeypatch):
    """A .docx whose only occurrence of a token is inside a TABLE CELL must end up in an indexed
    DocumentChunk -- proving the completeness fix flows through extract -> chunk -> store, not just
    the unit extractor. Mirrors test_rag_ingest_egress.py's real-import-job pattern."""
    from sqlalchemy import text as sa_text

    from app.config import get_settings
    from app.models.document import Document as DocModel, IndexStatus
    from app.models.document_chunk import DocumentChunk
    from app.models.import_job import ImportJob, ImportJobStatus
    from app.providers.openai_provider import OpenAIProvider
    from app.rag.library_import import run_import_job
    from app.request_context import current_user_id as current_user_id_var
    from app.storage import get_storage

    dim = get_settings().embedding_dim

    async def _fake_embed(self, texts, model, **kwargs):
        return [[0.01] * dim for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)

    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    def build(doc):
        doc.add_paragraph("Vanligt stycke.")
        t = doc.add_table(rows=1, cols=2)
        t.cell(0, 0).text = "ORGNR"
        t.cell(0, 1).text = "TABELLTOKEN556"

    raw = _docx_bytes(build)

    def _reader(_pos=[0]):
        chunk = raw[_pos[0] : _pos[0] + (1 << 16)]
        _pos[0] += len(chunk)
        return chunk

    blob = get_storage().write_stream(_reader, max_bytes=max(len(raw), 1))
    job = ImportJob(
        owner_id=user.id,
        status=ImportJobStatus.pending,
        source_filename="bolag.docx",
        source_checksum=blob.sha256,
        source_storage_key=blob.storage_key,
        source_size_bytes=blob.size_bytes,
    )
    db_session.add(job)
    db_session.commit()

    await run_import_job(db_session, job.id, user.id)

    doc = db_session.query(DocModel).filter_by(uploaded_by=user.id, original_filename="bolag.docx").one()
    assert doc.status == IndexStatus.indexed, f"{doc.status} / {doc.error_message}"
    chunk_text_all = " ".join(
        c.text for c in db_session.query(DocumentChunk).filter_by(document_id=doc.id).all()
    )
    assert "TABELLTOKEN556" in chunk_text_all  # table-cell content reached the chunks

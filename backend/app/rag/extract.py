from io import BytesIO
from pathlib import Path

from bs4 import BeautifulSoup
from pypdf import PdfReader

from app.rag.docx_extract import extract_docx_text


def extract_text(filename: str, raw: bytes) -> str:
    suffix = Path(filename).suffix.lower()

    if suffix == ".pdf":
        reader = PdfReader(BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    if suffix == ".docx":
        # Full-fidelity extraction: body paragraphs + tables (incl. nested) in reading order,
        # plus headers/footers -- see app/rag/docx_extract.py. The previous one-liner here read
        # only doc.paragraphs and silently dropped all table/header/footer content.
        return extract_docx_text(raw)

    if suffix in (".html", ".htm"):
        soup = BeautifulSoup(raw.decode("utf-8", errors="ignore"), "html.parser")
        return soup.get_text(separator="\n")

    # Plain text, markdown, code files, etc.
    return raw.decode("utf-8", errors="ignore")

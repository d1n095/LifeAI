from io import BytesIO
from pathlib import Path

from bs4 import BeautifulSoup
from docx import Document as DocxDocument
from pypdf import PdfReader


def extract_text(filename: str, raw: bytes) -> str:
    suffix = Path(filename).suffix.lower()

    if suffix == ".pdf":
        reader = PdfReader(BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    if suffix == ".docx":
        doc = DocxDocument(BytesIO(raw))
        return "\n".join(p.text for p in doc.paragraphs)

    if suffix in (".html", ".htm"):
        soup = BeautifulSoup(raw.decode("utf-8", errors="ignore"), "html.parser")
        return soup.get_text(separator="\n")

    # Plain text, markdown, code files, etc.
    return raw.decode("utf-8", errors="ignore")

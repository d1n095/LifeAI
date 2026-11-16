"""Attachment Chamber -- active-content detection (Milestone 3).

Pure byte/string inspection of already-in-memory content. Nothing here executes, opens, or
invokes ANYTHING on a file -- every check is a signature/substring inspection, matching
app.rag.zip_import's own no-execution discipline. Detection results are DATA
(ActiveContentSignal), never a decision -- see risk.py for how they feed a QuarantineState
transition.
"""

from __future__ import annotations

import io
import re
import zipfile

from app.attachment_chamber.types import ActiveContentRisk, ActiveContentSignal

_SCRIPT_EXTENSIONS = {".js", ".vbs", ".ps1", ".sh", ".bat", ".cmd", ".py", ".rb", ".pl"}
_INSTALLER_EXTENSIONS = {".msi", ".dmg", ".pkg", ".deb", ".rpm", ".app"}
_HTML_SCRIPT_RE = re.compile(rb"<script", re.IGNORECASE)
_HTML_JS_URI_RE = re.compile(rb"javascript:", re.IGNORECASE)
_HTML_EVENT_HANDLER_RE = re.compile(rb'\bon[a-z]+\s*=', re.IGNORECASE)
_PDF_ACTION_MARKERS = (b"/OpenAction", b"/JavaScript", b"/Launch", b"/AA")
_SHEBANG_RE = re.compile(rb"^#!")


def detect_extension_risk(extension: str) -> ActiveContentSignal | None:
    ext = extension.lower()
    if ext in (".exe", ".dll", ".scr", ".com"):
        return ActiveContentSignal(risk=ActiveContentRisk.EXECUTABLE, detail=f"executable-shaped extension {ext!r}")
    if ext in _SCRIPT_EXTENSIONS:
        return ActiveContentSignal(risk=ActiveContentRisk.SCRIPT, detail=f"script-shaped extension {ext!r}")
    if ext in _INSTALLER_EXTENSIONS:
        return ActiveContentSignal(risk=ActiveContentRisk.INSTALLER, detail=f"installer-shaped extension {ext!r}")
    return None


def detect_binary_signatures(content: bytes) -> tuple[ActiveContentSignal, ...]:
    """Byte-level executable/shebang signals independent of extension -- an extension can
    lie, a magic byte at the start of the file is a stronger (though still not conclusive)
    signal."""
    signals: list[ActiveContentSignal] = []
    if content.startswith(b"MZ"):
        signals.append(ActiveContentSignal(risk=ActiveContentRisk.EXECUTABLE, detail="Windows PE (MZ) header detected"))
    if content.startswith(b"\x7fELF"):
        signals.append(ActiveContentSignal(risk=ActiveContentRisk.EXECUTABLE, detail="Linux ELF header detected"))
    if _SHEBANG_RE.match(content):
        signals.append(ActiveContentSignal(risk=ActiveContentRisk.SHELL_SCRIPT, detail="shebang (#!) detected"))
    return tuple(signals)


def detect_html_js_signatures(content: bytes) -> tuple[ActiveContentSignal, ...]:
    signals: list[ActiveContentSignal] = []
    if _HTML_SCRIPT_RE.search(content):
        signals.append(ActiveContentSignal(risk=ActiveContentRisk.HTML_JS, detail="<script> tag detected"))
    if _HTML_JS_URI_RE.search(content):
        signals.append(ActiveContentSignal(risk=ActiveContentRisk.HTML_JS, detail="javascript: URI detected"))
    if _HTML_EVENT_HANDLER_RE.search(content):
        signals.append(ActiveContentSignal(risk=ActiveContentRisk.HTML_JS, detail="inline event-handler attribute detected"))
    return tuple(signals)


def detect_pdf_action_signatures(content: bytes) -> tuple[ActiveContentSignal, ...]:
    """Simple byte-string presence check, no full PDF object-graph parsing -- a real, well-
    known signal for a foundation stage. A false positive (the marker appears inside an
    unrelated content stream) is an acceptable cost for a stage that only ever FLAGS, never
    blocks unconditionally on this alone (see risk.py)."""
    return tuple(
        ActiveContentSignal(risk=ActiveContentRisk.PDF_ACTION, detail=f"{marker.decode()!r} marker present in PDF-shaped bytes")
        for marker in _PDF_ACTION_MARKERS
        if marker in content
    )


def detect_zip_container_signatures(zip_bytes: bytes) -> tuple[ActiveContentSignal, ...]:
    """Office-macro / embedded-object signal for a zip-container document (docx/xlsx/pptx
    are all zip containers -- see mime_detection.py's own note). Detecting the mere PRESENCE
    of a vbaProject-named member is a reasonable foundation-stage signal; this does not
    parse OLE/VBA content itself. External references / remote templates (a URL inside a
    document's internal relationship XML) are NOT inspected this round -- a real
    OOXML-relationship parser is out of scope for this foundation stage; callers should
    treat 'no external-reference signal produced' as 'not inspected', never as 'confirmed
    absent' (see the KNOWN LIMITATION test in the test file)."""
    signals: list[ActiveContentSignal] = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                lowered = name.lower()
                if "vbaproject" in lowered:
                    signals.append(ActiveContentSignal(risk=ActiveContentRisk.MACRO, detail="vbaProject member found", source_path=name))
                if lowered.startswith("word/embeddings/") or lowered.startswith("xl/embeddings/"):
                    signals.append(ActiveContentSignal(risk=ActiveContentRisk.EMBEDDED_OBJECT, detail="embedded object member found", source_path=name))
    except zipfile.BadZipFile:
        pass  # not actually a valid zip container -- mime_detection's conflict flag already covers this
    return tuple(signals)


def scan_active_content(*, extension: str, content: bytes, detected_kind: str | None) -> tuple[ActiveContentSignal, ...]:
    """The single entry point Milestone 7's tests exercise. Composes every check above --
    never executes anything, purely byte/string inspection of already-in-memory content."""
    signals: list[ActiveContentSignal] = []
    ext_signal = detect_extension_risk(extension)
    if ext_signal is not None:
        signals.append(ext_signal)
    signals.extend(detect_binary_signatures(content))
    if detected_kind == "text" or extension.lower() in (".html", ".htm"):
        signals.extend(detect_html_js_signatures(content))
    if detected_kind == "pdf":
        signals.extend(detect_pdf_action_signatures(content))
    if detected_kind == "zip":
        signals.extend(detect_zip_container_signatures(content))
    return tuple(signals)

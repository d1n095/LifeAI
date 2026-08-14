"""Format-agnostic structured-export ingestion foundation.

No ChatGPT format adapter is implemented here; that boundary requires a real export sample.
"""

from app.structured_import.adapter import (
    AdapterDiscovery,
    AdapterItemFailure,
    StructuredExportAdapter,
    StructuredExportItem,
    StructuredItemOutcome,
    StructuredItemProcessor,
)

__all__ = [
    "AdapterDiscovery",
    "AdapterItemFailure",
    "StructuredExportAdapter",
    "StructuredExportItem",
    "StructuredItemOutcome",
    "StructuredItemProcessor",
]

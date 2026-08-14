"""Contract for deterministic, streaming structured-export adapters.

Adapters translate an external container into stable item identities and provenance. They do
not write the database, call providers, or decide source truth. A future ChatGPT adapter must
be implemented only after its contract has been verified against a real export sample.
"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import BinaryIO, Protocol, runtime_checkable

from app.models.structured_import import StructuredImportItemState

Checkpoint = Mapping[str, object]
ChunkIteratorFactory = Callable[[], Iterator[bytes]]


@dataclass(frozen=True)
class AdapterDiscovery:
    total_items: int | None
    metadata: Mapping[str, object]


@dataclass(frozen=True)
class StructuredExportItem:
    """One bounded item discovered in the canonical export.

    ``content_chunks`` must return a fresh iterator and must never materialize the complete
    export. ``provenance`` identifies the item's exact location inside the original archive;
    it is metadata, never a replacement copy of the original.
    """

    source_identity: str
    provenance: Mapping[str, object]
    checkpoint_after: Checkpoint
    content_chunks: ChunkIteratorFactory


@dataclass(frozen=True)
class AdapterItemFailure:
    """A malformed/unsupported individual record that must not abort the surrounding run."""

    source_identity: str
    provenance: Mapping[str, object]
    checkpoint_after: Checkpoint
    failure_code: str
    retryable: bool = False


@dataclass(frozen=True)
class StructuredItemOutcome:
    state: StructuredImportItemState
    content_sha256: str | None = None
    size_bytes: int | None = None
    failure_code: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.state is StructuredImportItemState.failed and not self.failure_code:
            raise ValueError("failed outcomes require a closed-vocabulary failure_code")
        if self.state is not StructuredImportItemState.failed and self.failure_code is not None:
            raise ValueError("only failed outcomes may carry failure_code")


@runtime_checkable
class StructuredExportAdapter(Protocol):
    key: str
    version: str

    def discover(self, source: BinaryIO) -> AdapterDiscovery:
        """Inspect the source with bounded memory; return a count only when knowable safely."""

    def iter_items(
        self, source: BinaryIO, checkpoint: Checkpoint
    ) -> Iterator[StructuredExportItem | AdapterItemFailure]:
        """Yield after ``checkpoint`` in deterministic order, one bounded item at a time."""


@runtime_checkable
class StructuredItemProcessor(Protocol):
    def process(self, item: StructuredExportItem) -> StructuredItemOutcome:
        """Deterministically process one item without AI/provider/network access."""

"""Parent-Child (Small-to-Big) Multi-Level Document Splitter.

Inspired by Dify and LlamaIndex's hierarchical indexing, this module breaks
documents into large contextual Parent chunks (800~1200 runes) and fine-grained
Child chunks (100~200 runes). Child chunks are indexed for high-density lexical
and embedding retrieval, while full Parent chunks are returned as LLM context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from internal.rag.splitter import RecursiveSplitter


@dataclass
class ParentChildChunk:
    child_id: int
    child_content: str
    parent_id: int
    parent_content: str


class ParentChildSplitter:
    """Hierarchical splitter implementing the Small-to-Big retrieval pattern."""

    def __init__(
        self,
        parent_chunk_size: int = 800,
        parent_overlap: int = 100,
        child_chunk_size: int = 150,
        child_overlap: int = 30,
    ):
        self.parent_chunk_size = max(200, int(parent_chunk_size))
        self.parent_overlap = max(0, int(parent_overlap))
        self.child_chunk_size = max(50, int(child_chunk_size))
        self.child_overlap = max(0, int(child_overlap))

        # Splitter for parents: breaks by double-newline and section boundaries
        self._parent_splitter = RecursiveSplitter(
            chunk_size=self.parent_chunk_size,
            chunk_overlap=self.parent_overlap,
            separators=["\n\n", "\n", "。"],
        )

        # Splitter for children: breaks by sentences and punctuation
        self._child_splitter = RecursiveSplitter(
            chunk_size=self.child_chunk_size,
            chunk_overlap=self.child_overlap,
            separators=["\n", "。", "！", "？", "；", " "],
        )

    def split(self, text: str) -> List[ParentChildChunk]:
        """Split raw text into fine-grained child chunks bound to their parent context."""
        if not text or not text.strip():
            return []

        parents = self._parent_splitter.split(text)
        results: List[ParentChildChunk] = []
        child_counter = 0

        for parent_idx, parent in enumerate(parents):
            parent_text = parent.content.strip()
            if not parent_text:
                continue

            # Generate child chunks within this parent
            children = self._child_splitter.split(parent_text)
            if not children:
                # Fallback if parent is too short to split further
                results.append(ParentChildChunk(
                    child_id=child_counter,
                    child_content=parent_text,
                    parent_id=parent_idx,
                    parent_content=parent_text,
                ))
                child_counter += 1
                continue

            for child in children:
                child_text = child.content.strip()
                if child_text:
                    results.append(ParentChildChunk(
                        child_id=child_counter,
                        child_content=child_text,
                        parent_id=parent_idx,
                        parent_content=parent_text,
                    ))
                    child_counter += 1

        return results

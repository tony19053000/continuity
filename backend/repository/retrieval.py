"""Relevant-context retrieval.

The last step before a model sees anything, and the enforcement point for
`02_ARCHITECTURE.md` §4: **slices, never repositories.**

Three guarantees, each tested:

* **Bounded.** Total retrieved bytes never exceed `CONTEXT_BUDGET_BYTES`. The
  budget is enforced while assembling, not checked afterwards, so a large
  repository degrades to fewer slices rather than a larger prompt.
* **Evidenced.** Every slice carries an `Evidence` record with a file path and
  line span, so any claim a model later makes can be traced to the exact lines
  it was shown.
* **Filtered.** Every slice passes through the secret filter on the way out.
  Excluded files never reached the index, so this is the second line rather than
  the first.

Slices are symbol-scoped by default. A whole file is emitted only when the
requested symbol genuinely spans it, because sending a 900-line module to
explain a 12-line function is how context budgets disappear.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from backend.models.enums import Confidence, EvidenceKind
from backend.models.schemas import Evidence
from backend.repository.indexer import IndexedFile, IndexedSymbol, RepositoryIndex
from backend.repository.source import RepositorySource
from backend.security.secret_filter import redact

#: Lines of surrounding context included with each symbol slice. Enough for a
#: decorator and a docstring above, and a return below.
CONTEXT_LINES: Final = 3


@dataclass(frozen=True, slots=True)
class ContextSlice:
    """One bounded, evidenced excerpt destined for a prompt."""

    path: str
    line_start: int
    line_end: int
    content: str
    reason: str
    evidence: Evidence

    @property
    def size_bytes(self) -> int:
        return len(self.content.encode("utf-8"))

    def render(self) -> str:
        return f"--- {self.path}:{self.line_start}-{self.line_end} ---\n{self.content}"


@dataclass(slots=True)
class RetrievedContext:
    """Everything selected for one model call, and what was left out."""

    slices: list[ContextSlice]
    total_bytes: int
    budget_bytes: int
    truncated: bool = False
    omitted_count: int = 0

    def render(self) -> str:
        body = "\n\n".join(slice_.render() for slice_ in self.slices)
        if self.truncated:
            # Stating the omission matters: a model told it has the full picture
            # will reason as if it does.
            body += (
                f"\n\n[{self.omitted_count} further excerpts omitted — "
                f"context budget of {self.budget_bytes} bytes reached]"
            )
        return body


def _evidence_for(path: str, start: int, end: int, excerpt: str) -> Evidence:
    return Evidence(
        kind=EvidenceKind.SOURCE,
        confidence=Confidence.CONFIRMED,
        file_path=path,
        line_start=start,
        line_end=end,
        excerpt=redact(excerpt[:4000]),
    )


def slice_symbol(
    source: RepositorySource,
    file: IndexedFile,
    *,
    line_start: int,
    line_end: int,
    reason: str,
    context_lines: int = CONTEXT_LINES,
) -> ContextSlice | None:
    """Extract one symbol's lines, with a little surrounding context."""
    try:
        content = source.read_file(file.path)
    except Exception:
        return None
    if not content:
        return None

    lines = content.splitlines()
    if not lines:
        return None

    start = max(1, line_start - context_lines)
    end = min(len(lines), line_end + context_lines)
    excerpt = "\n".join(lines[start - 1 : end])
    safe = redact(excerpt)

    return ContextSlice(
        path=file.path,
        line_start=start,
        line_end=end,
        content=safe,
        reason=reason,
        evidence=_evidence_for(file.path, start, end, safe),
    )


class ContextRetriever:
    """Assembles bounded context from the index."""

    def __init__(
        self, index: RepositoryIndex, source: RepositorySource, *, budget_bytes: int
    ) -> None:
        self._index = index
        self._source = source
        self._budget = budget_bytes

    def for_call_sites(self, needle: str, *, limit: int = 20) -> RetrievedContext:
        """Slices around every call site matching `needle`.

        The primary path for impact analysis: given a changed provider resource,
        show the model exactly the code that calls it and nothing else.
        """
        candidates: list[ContextSlice] = []

        for file, call in self._index.call_sites_matching(needle)[:limit]:
            symbol = _enclosing_symbol(file, call.line)
            start = symbol.line_start if symbol else call.line
            end = symbol.line_end if symbol else call.line

            piece = slice_symbol(
                self._source,
                file,
                line_start=start,
                line_end=end,
                # The needle is recorded, not just the callee. "calls
                # client.post" does not say why this slice was chosen; "matches
                # '/v1/charges'" does, and that is what the evidence trail needs
                # when a human asks why a file was flagged.
                reason=f"matches {needle!r} — calls {call.callee} at line {call.line}",
            )
            if piece:
                candidates.append(piece)

        return self._apply_budget(candidates)

    def for_symbols(self, references: list[tuple[str, str]]) -> RetrievedContext:
        """Slices for explicit `(path, qualified_name)` pairs."""
        candidates: list[ContextSlice] = []

        for path, qualified_name in references:
            file = self._index.files.get(path)
            if file is None:
                continue
            symbol = next(
                (s for s in file.symbols if s.qualified_name == qualified_name), None
            )
            if symbol is None:
                continue

            piece = slice_symbol(
                self._source,
                file,
                line_start=symbol.line_start,
                line_end=symbol.line_end,
                reason=f"symbol {qualified_name}",
            )
            if piece:
                candidates.append(piece)

        return self._apply_budget(candidates)

    def for_files(self, paths: list[str]) -> RetrievedContext:
        """Whole files, for the rare case where the unit of interest is a file.

        Still budgeted and still filtered — "whole file" is a scope, not an
        exemption.
        """
        candidates: list[ContextSlice] = []

        for path in paths:
            file = self._index.files.get(path)
            if file is None:
                continue
            piece = slice_symbol(
                self._source,
                file,
                line_start=1,
                line_end=max(file.line_count, 1),
                reason="whole file requested",
                context_lines=0,
            )
            if piece:
                candidates.append(piece)

        return self._apply_budget(candidates)

    def _apply_budget(self, candidates: list[ContextSlice]) -> RetrievedContext:
        """Take slices until the budget is spent.

        Smaller slices first, so a budget buys the most distinct evidence rather
        than one enormous file. Assembling under the budget — rather than
        trimming afterwards — is what makes the guarantee hold.
        """
        selected: list[ContextSlice] = []
        total = 0

        for piece in sorted(candidates, key=lambda item: item.size_bytes):
            if total + piece.size_bytes > self._budget:
                continue
            selected.append(piece)
            total += piece.size_bytes

        selected.sort(key=lambda item: (item.path, item.line_start))
        omitted = len(candidates) - len(selected)

        return RetrievedContext(
            slices=selected,
            total_bytes=total,
            budget_bytes=self._budget,
            truncated=omitted > 0,
            omitted_count=omitted,
        )


def _enclosing_symbol(file: IndexedFile, line: int) -> IndexedSymbol | None:
    """The tightest symbol containing `line`, so a slice is minimal."""
    containing = [s for s in file.symbols if s.line_start <= line <= s.line_end]
    if not containing:
        return None
    return min(containing, key=lambda s: s.line_end - s.line_start)

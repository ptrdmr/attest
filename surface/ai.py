"""Deterministic AI draft provider stub — no external API calls."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_BULLET_RE = re.compile(r"^[-*•]\s+|^\d+[.)]\s+")
_HEADER_PREFIXES = ("from:", "to:", "subject:", "date:", "sent:", "cc:", "reply-to:")
_SECTION_PREFIXES = ("acceptance:", "criteria:", "deliverables:")


@dataclass(frozen=True)
class DraftCriteria:
    """One drafted acceptance criterion line."""

    text: str
    order: int


@dataclass(frozen=True)
class DraftResult:
    """Draft brief and criteria produced from a source dump."""

    brief: str
    criteria: tuple[DraftCriteria, ...]


def draft_from_dump(source_text: str) -> DraftResult:
    """Parse email or Slack paste into a brief and ordered criteria.

    Deterministic for the same input: no network calls and no randomness.
    """
    text = (source_text or "").strip()
    if not text:
        return DraftResult(brief="", criteria=())

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    content_lines = _strip_headers(lines)
    criteria_lines, narrative_lines = _split_criteria_and_narrative(content_lines)

    if not criteria_lines:
        criteria_lines = _fallback_criteria(text)

    brief = _build_brief(narrative_lines, content_lines)
    criteria = tuple(
        DraftCriteria(text=line, order=index + 1)
        for index, line in enumerate(criteria_lines[:10])
        if line
    )
    return DraftResult(brief=brief, criteria=criteria)


def _strip_headers(lines: list[str]) -> list[str]:
    """Drop common email header lines from pasted threads."""
    content_lines: list[str] = []
    for line in lines:
        lower = line.lower()
        if any(lower.startswith(prefix) for prefix in _HEADER_PREFIXES):
            continue
        if lower.startswith("on ") and " wrote:" in lower:
            continue
        content_lines.append(line)
    return content_lines or lines


def _split_criteria_and_narrative(lines: list[str]) -> tuple[list[str], list[str]]:
    """Separate bullet or numbered lines from narrative prose."""
    criteria_lines: list[str] = []
    narrative_lines: list[str] = []
    for line in lines:
        lower = line.lower()
        if any(lower.startswith(prefix) for prefix in _SECTION_PREFIXES):
            continue
        if _BULLET_RE.match(line):
            criteria_lines.append(_BULLET_RE.sub("", line).strip())
        else:
            narrative_lines.append(line)
    return criteria_lines, narrative_lines


def _build_brief(narrative_lines: list[str], content_lines: list[str]) -> str:
    """Compose a concise project brief from narrative lines."""
    brief_source = narrative_lines if narrative_lines else content_lines[:3]
    brief = " ".join(brief_source).strip()
    if len(brief) > 2000:
        return brief[:1997] + "..."
    return brief


def _fallback_criteria(source_text: str) -> list[str]:
    """Return deterministic placeholder criteria when none are detected."""
    digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    token = digest[:8]
    return [
        f"Deliver the agreed scope captured in the source thread ({token}).",
        "Provide handoff notes and respond to one revision round within the project limit.",
        "Confirm deliverables work in the agreed target environment before sign-off.",
    ]

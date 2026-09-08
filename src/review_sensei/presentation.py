"""Provider-neutral rendering for structured review finding metadata."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from .models import ReviewComment

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_QUICK_WIN_EFFORTS = frozenset(("trivial", "small"))
_MARKDOWN_LABEL_CHARS = re.compile(r"([\\`*_[\]{}()<>#!|~])")


def humanize_lens(category: str) -> str:
    """Turn a stable category id into a readable lens label."""

    words = category.replace("_", " ").replace("-", " ").split()
    return " ".join(word[:1].upper() + word[1:] for word in words)


def _label(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()


def _escape_markdown_label(value: str) -> str:
    """Keep untrusted classification text inside one readable Markdown label."""

    # Classification values are currently rejected when they contain control
    # characters, but keep this renderer safe if it is reused with a less
    # restrictive caller in the future. Escaping line breaks here prevents a
    # label from ever changing the surrounding Markdown line structure.
    escaped = _MARKDOWN_LABEL_CHARS.sub(r"\\\1", value)
    return escaped.replace("\r", r"\r").replace("\n", r"\n")


def format_review_comment(comment: ReviewComment) -> str:
    """Render a validated comment with any available classification labels."""

    labels: list[str] = []
    if comment.severity is not None:
        labels.append(f"Severity: {_escape_markdown_label(_label(comment.severity))}")
    if comment.fix_effort is not None:
        labels.append(
            f"Fix effort: {_escape_markdown_label(_label(comment.fix_effort))}"
        )
    if comment.category is not None:
        labels.append(
            f"Lens: {_escape_markdown_label(humanize_lens(comment.category))}"
        )
    if not labels:
        return comment.body
    return f"[{'] ['.join(labels)}]\n\n{comment.body}"


def _severity_sort_key(value: str) -> tuple[int, str]:
    return (_SEVERITY_ORDER.get(value.lower(), len(_SEVERITY_ORDER)), value.lower())


def _count_lines(title: str, counts: Counter[str], *, sort_key=None) -> str:
    if not counts:
        return ""
    ordered = sorted(counts.items(), key=sort_key or (lambda item: item[0].lower()))
    values = ", ".join(
        f"{_escape_markdown_label(_label(label))} ({count})" for label, count in ordered
    )
    return f"- {title}: {values}"


def format_review_summary(summary: str, comments: Iterable[ReviewComment]) -> str:
    """Append deterministic classification counts when any finding is labeled.

    A result with no structured metadata is returned byte-for-byte unchanged so
    legacy summaries retain their existing publication behavior.
    """

    comments = tuple(comments)
    if not any(
        comment.severity is not None
        or comment.fix_effort is not None
        or comment.category is not None
        for comment in comments
    ):
        return summary

    severity_counts: Counter[str] = Counter(
        comment.severity for comment in comments if comment.severity is not None
    )
    lens_counts: Counter[str] = Counter(
        humanize_lens(comment.category)
        for comment in comments
        if comment.category is not None
    )
    quick_wins = sum(
        comment.fix_effort is not None
        and comment.fix_effort.lower() in _QUICK_WIN_EFFORTS
        for comment in comments
    )

    lines = ["Review classification:"]
    severity_line = _count_lines(
        "Severity",
        severity_counts,
        sort_key=lambda item: _severity_sort_key(item[0]),
    )
    if severity_line:
        lines.append(severity_line)
    lens_line = _count_lines("Lens", lens_counts)
    if lens_line:
        lines.append(lens_line)
    if any(comment.fix_effort is not None for comment in comments):
        lines.append(f"- Quick wins (trivial/small effort): {quick_wins}")
    return f"{summary}\n\n" + "\n".join(lines)


__all__ = ["format_review_comment", "format_review_summary", "humanize_lens"]

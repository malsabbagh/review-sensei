"""Provider-neutral rendering for structured review finding metadata."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable

from .errors import ReviewInputError
from .models import ReviewComment, ReviewResult

RENDER_FORMATS = ("text", "markdown", "json")
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_QUICK_WIN_EFFORTS = frozenset(("trivial", "small"))
_MARKDOWN_LABEL_CHARS = re.compile(r"([\\`*_[\]{}()<>#!|~])")
_SEVERITY_ICONS = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
}
_FIX_EFFORT_ICONS = {
    "trivial": "⚡",
    "small": "⚡",
    "moderate": "🔧",
    "large": "🛠️",
    "unknown": "❔",
}
_LENS_ICONS = {
    "architecture": "🏗️",
    "correctness": "✅",
    "maintainability": "🧹",
    "security": "🔒",
    "tests": "🧪",
}
_DEFAULT_ICON = "🔎"
_BLOCKING_ICONS = {True: "🚫", False: "💬"}


def humanize_lens(category: str) -> str:
    """Turn a stable category id into a readable lens label."""

    words = category.replace("_", " ").replace("-", " ").split()
    return " ".join(word[:1].upper() + word[1:] for word in words)


def _label(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()


def escape_markdown_label(value: str) -> str:
    """Escape untrusted text for safe Markdown label or body interpolation."""

    # Classification values are currently rejected when they contain control
    # characters, but keep this renderer safe if it is reused with a less
    # restrictive caller in the future. Escaping line breaks here prevents a
    # label from ever changing the surrounding Markdown line structure.
    escaped = _MARKDOWN_LABEL_CHARS.sub(r"\\\1", value)
    return escaped.replace("\r", r"\r").replace("\n", r"\n")


def _escape_markdown_label(value: str) -> str:
    return escape_markdown_label(value)


# The control, format, and surrogate categories the repository already rejects
# in paths, configuration, and workflow values.  Provider output is bounded but
# not restricted to printable text, so the terminal renderer neutralizes them
# at the sink instead of rejecting a legitimate review.
_TERMINAL_UNSAFE_CATEGORIES = frozenset(("Cc", "Cf", "Cs"))


def escape_terminal_text(value: str) -> str:
    """Neutralize terminal control characters in untrusted review text.

    The text format is written to a terminal, where a provider-supplied escape
    sequence could move the cursor, recolor the session, or rewrite lines that
    were already printed.  Line feeds and tabs are the format's own line
    structure and stay; every other control, format, or surrogate character is
    rendered as its hexadecimal code point so the text stays readable and inert.
    """

    return "".join(
        character
        if character in "\n\t"
        or unicodedata.category(character) not in _TERMINAL_UNSAFE_CATEGORIES
        else f"\\x{ord(character):02x}"
        for character in value
    )


def _metadata_icon(icons: dict[str, str], value: str) -> str:
    """Return a static visual cue without trusting metadata as presentation."""

    return icons.get(value.lower(), _DEFAULT_ICON)


def format_review_comment(comment: ReviewComment) -> str:
    """Render a validated comment with any available classification labels."""

    labels: list[str] = []
    if (
        comment.blocking is not None
        or comment.effective_blocking is not None
        or comment.blocks_approval
        or comment.needs_human
    ):
        label = "Blocking" if comment.blocks_approval else "Non-blocking"
        labels.append(f"{_BLOCKING_ICONS[comment.blocks_approval]} {label}")
        if (
            comment.blocking is not None
            and comment.effective_blocking is not None
            and comment.blocking != comment.effective_blocking
        ):
            proposed = "Blocking" if comment.blocking else "Non-blocking"
            labels.append(f"Proposed: {proposed}")
        if comment.needs_human:
            labels.append("Needs human")
    if comment.severity is not None:
        labels.append(
            f"{_metadata_icon(_SEVERITY_ICONS, comment.severity)} "
            f"Severity: {_escape_markdown_label(_label(comment.severity))}"
        )
    if comment.fix_effort is not None:
        labels.append(
            f"{_metadata_icon(_FIX_EFFORT_ICONS, comment.fix_effort)} "
            f"Fix effort: {_escape_markdown_label(_label(comment.fix_effort))}"
        )
    if comment.category is not None:
        labels.append(
            f"{_metadata_icon(_LENS_ICONS, comment.category)} "
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
        or comment.blocking is not None
        or comment.effective_blocking is not None
        or comment.needs_human
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
    blocking_counts: Counter[str] = Counter(
        "Blocking" if comment.blocks_approval else "Non-blocking"
        for comment in comments
        if (
            comment.blocking is not None
            or comment.effective_blocking is not None
            or comment.blocks_approval
            or comment.needs_human
        )
    )

    lines = ["Review classification:"]
    severity_line = _count_lines(
        "Severity",
        severity_counts,
        sort_key=lambda item: _severity_sort_key(item[0]),
    )
    if severity_line:
        lines.append(severity_line)
    blocking_line = _count_lines("Merge impact", blocking_counts)
    if blocking_line:
        lines.append(blocking_line)
    lens_line = _count_lines("Lens", lens_counts)
    if lens_line:
        lines.append(lens_line)
    if any(comment.fix_effort is not None for comment in comments):
        lines.append(f"- Quick wins (trivial/small effort): {quick_wins}")
    return f"{summary}\n\n" + "\n".join(lines)


def _finding_groups(
    result: ReviewResult,
) -> tuple[tuple[str, tuple[ReviewComment, ...]], ...]:
    required = tuple(comment for comment in result.comments if comment.blocks_approval)
    optional = tuple(
        comment for comment in result.comments if not comment.blocks_approval
    )
    return (("Required fixes", required), ("Optional findings", optional))


def _finding_location(comment: ReviewComment) -> str:
    if comment.side == "FILE" or comment.line is None:
        return comment.path
    return f"{comment.path}:{comment.line}"


def _finding_labels(comment: ReviewComment) -> str:
    labels = ["required fix" if comment.blocks_approval else "optional"]
    if comment.severity is not None:
        labels.append(f"severity: {comment.severity.lower()}")
    if comment.category is not None:
        labels.append(f"lens: {humanize_lens(comment.category)}")
    if comment.fix_effort is not None:
        labels.append(f"effort: {comment.fix_effort.lower()}")
    if comment.needs_human:
        labels.append("needs human")
    return " ".join(f"[{label}]" for label in labels)


def render_review_text(result: ReviewResult) -> str:
    """Render one validated review result as readable terminal text.

    Provider-supplied text reaches a terminal here, so every external string is
    neutralized with :func:`escape_terminal_text` before it is printed.
    """

    groups = _finding_groups(result)
    lines = [
        f"ReviewSensei review: {result.review_status}",
        f"provider: {escape_terminal_text(result.provider)}",
    ]
    if result.model:
        lines.append(f"model: {escape_terminal_text(result.model)}")
    if result.coverage_mode != "full":
        lines.append(f"coverage: {result.coverage_mode}")
    for title, group in groups:
        lines.append(f"{title.lower()}: {len(group)}")
    lines.append("")
    lines.append("Summary:")
    lines.append(escape_terminal_text(result.summary))
    index = 0
    for title, group in groups:
        if not group:
            continue
        lines.append("")
        lines.append(f"{title}:")
        for comment in group:
            index += 1
            lines.append("")
            lines.append(f"{index}) {_finding_location(comment)}")
            labels = _finding_labels(comment)
            if labels:
                lines.append(f"   {labels}")
            lines.append("")
            lines.append(escape_terminal_text(comment.body))
    return "\n".join(lines) + "\n"


def render_review_markdown(result: ReviewResult) -> str:
    """Render one validated review result as readable Markdown."""

    parts = [format_review_summary(result.summary, result.comments)]
    for title, group in _finding_groups(result):
        if not group:
            continue
        parts.append("")
        parts.append(f"### {title}")
        for comment in group:
            parts.append("")
            parts.append(f"**{escape_markdown_label(_finding_location(comment))}**")
            parts.append("")
            parts.append(format_review_comment(comment))
    return "\n".join(parts) + "\n"


def render_review(result: ReviewResult, *, output_format: str) -> str:
    """Render one validated review result in the requested explicit format."""

    if output_format == "text":
        return render_review_text(result)
    if output_format == "markdown":
        return render_review_markdown(result)
    if output_format == "json":
        # The versioned ``review-result`` v1 document, unchanged from the
        # historical single-format output.
        return json.dumps(result.to_dict(), indent=2) + "\n"
    raise ReviewInputError(f"unsupported review output format: {output_format}")


__all__ = [
    "RENDER_FORMATS",
    "escape_markdown_label",
    "escape_terminal_text",
    "format_review_comment",
    "format_review_summary",
    "humanize_lens",
    "render_review",
    "render_review_markdown",
    "render_review_text",
]

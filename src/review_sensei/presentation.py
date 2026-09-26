"""Shared typed renderers for ReviewSensei review presentation.

The package owns published Markdown. Models provide validated structured
content, never authoritative status lines, host instructions, or raw
classification labels. Renderers here consume typed views so every published
surface (inline finding, body finding, review summary) shares one formatting
and escaping path.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .models import ReviewComment, ReviewInputError, ReviewResult

RENDER_FORMATS = ("text", "markdown", "json")

REQUIRED_FINDING = "required"
OPTIONAL_FINDING = "optional"

FINDING_ID_PREFIX = "RS-"
_FINDING_ID_HEX = 6
_FINDING_ID_MAX_HEX = 16
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]+$")

CHANGES_REQUIRED_STATE = "changes-required"
NO_REQUIRED_FIXES_STATE = "no-required-fixes"
WITHHELD_STATE = "withheld"
INCOMPLETE_STATE = "incomplete"
ADVISORY_STATE = "advisory"

_HIGHLIGHTED_OPTIONAL_LIMIT = 3
_REFERENCE_EXCERPT_LIMIT = 160
_UNSPECIFIC_DEFECT_KINDS = frozenset(("", "unknown"))
_MARKDOWN_LABEL_CHARS = re.compile(r"([\\`*_[\]{}()<>#!|~])")
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_MENTION_PREFIX = re.compile(r"(^|\s)@")
_FENCE_LINE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})")
_INLINE_LINK = re.compile(
    r"(?P<image>!?)\[(?P<label>[^\[\]]*)\]\(\s*(?P<destination>[^\s)]*)"
)
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_SAFE_LINK_SCHEMES = frozenset(("http", "https"))
_DISCUSSION_REFERENCE_INLINE = "See the inline discussion."
_DISCUSSION_REFERENCE_BODY = "See the full explanation below."
_OPTIONAL_NOT_REQUIRED = "This is not required for this PR."

_HEADING_STATES = {
    CHANGES_REQUIRED_STATE: "## ReviewSensei — Changes required",
    NO_REQUIRED_FIXES_STATE: "## ReviewSensei — No required fixes found",
    WITHHELD_STATE: "## ReviewSensei — Approval withheld",
    INCOMPLETE_STATE: "## ReviewSensei — Review incomplete",
    ADVISORY_STATE: "## ReviewSensei — Advisory review (enforcement disabled)",
}


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


# Inline Markdown constructs reshape or hide content regardless of position:
# raw HTML tags and comments, code spans and fences, links and images, and the
# escape character itself.
_MARKDOWN_INLINE_CHARS = re.compile(r"([\\`<>\[\]])")

# Block markers only reshape the document when they open a line (after at most
# three spaces, per CommonMark): headings, blockquotes, tables, lists, and
# thematic or setext lines.
_MARKDOWN_BLOCK_MARKER = re.compile(
    r"(?m)^( {0,3})([#>|]|[-+=*_]+\s|[-=*+_]{3,}$|\d{1,9}[.)]\s)"
)


def escape_markdown_text(value: str) -> str:
    """Escape untrusted text so it cannot reshape a rendered Markdown document.

    The Markdown format is a document sink: provider text that opens a fence,
    an HTML comment, or a heading would restructure (or hide parts of) the
    review wherever the document is rendered.  Every inline construct and
    every line-opening block marker is backslash-escaped, mirroring the
    terminal renderer's neutralization.  Line feeds are the format's own line
    structure and stay; the rendered text keeps its plain characters, only the
    Markdown meaning is removed.
    """

    escaped = _MARKDOWN_INLINE_CHARS.sub(r"\\\1", value)
    return _MARKDOWN_BLOCK_MARKER.sub(r"\1\\\2", escaped)


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


def finding_identifier(fingerprint: str, *, hex_digits: int = _FINDING_ID_HEX) -> str:
    """Project a concern fingerprint onto one stable human-readable ID.

    The ID is a deterministic projection of the fingerprint, so the same
    concern keeps its ID across reruns without persisted identifier state.
    """

    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise ReviewInputError("finding identifier requires a fingerprint digest")
    if hex_digits < _FINDING_ID_HEX or hex_digits > len(fingerprint):
        raise ReviewInputError("finding identifier length is invalid")
    return f"{FINDING_ID_PREFIX}{fingerprint[:hex_digits].upper()}"


def assign_finding_identifiers(fingerprints: Iterable[str]) -> dict[str, str]:
    """Assign unique stable IDs, extending only genuinely colliding prefixes."""

    unique = list(dict.fromkeys(fingerprints))
    lengths = dict.fromkeys(unique, _FINDING_ID_HEX)
    while True:
        identifiers = {
            fingerprint: finding_identifier(
                fingerprint, hex_digits=lengths[fingerprint]
            )
            for fingerprint in unique
        }
        grouped: dict[str, list[str]] = {}
        for fingerprint, identifier in identifiers.items():
            grouped.setdefault(identifier, []).append(fingerprint)
        collisions = [group for group in grouped.values() if len(group) > 1]
        if not collisions:
            return identifiers
        widened = False
        for group in collisions:
            for fingerprint in group:
                if lengths[fingerprint] < min(_FINDING_ID_MAX_HEX, len(fingerprint)):
                    lengths[fingerprint] = min(
                        lengths[fingerprint] + 2, _FINDING_ID_MAX_HEX, len(fingerprint)
                    )
                    widened = True
        if not widened:
            raise ReviewInputError("finding identifier collision cannot be resolved")


def _code_spans(line: str) -> list[tuple[str, bool]]:
    """Split a line into (text, is_code) segments using backtick runs."""

    segments: list[tuple[str, bool]] = []
    position = 0
    while position < len(line):
        start = line.find("`", position)
        if start < 0:
            segments.append((line[position:], False))
            break
        run_end = start
        while run_end < len(line) and line[run_end] == "`":
            run_end += 1
        fence = line[start:run_end]
        close = line.find(fence, run_end)
        if close < 0:
            segments.append((line[position:], False))
            break
        segments.append((line[position:start], False))
        segments.append((line[start : close + len(fence)], True))
        position = close + len(fence)
    return segments


def _neutralize_mentions(text: str) -> str:
    return _MENTION_PREFIX.sub(r"\1\\@", text)


def _safe_link_destination(destination: str) -> bool:
    if destination.startswith(("//", "#")):
        return True
    scheme = _SCHEME.match(destination)
    return scheme is None or scheme.group(1).lower() in _SAFE_LINK_SCHEMES


def _neutralize_links(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        destination = match.group("destination")
        if _safe_link_destination(destination):
            return match.group(0)
        # Escaping the opening bracket keeps the visible text identical while
        # preventing a misleading or unsafe destination from becoming a link.
        return f"\\[{match.group('label')}]({destination}"

    return _INLINE_LINK.sub(replace, text)


def sanitize_finding_markdown(value: str) -> str:
    """Escape untrusted explanation text while preserving legitimate Markdown.

    Newlines, balanced code fences, and real links are preserved. Forged
    HTML comment markers, mention notifications, unsafe link destinations,
    and unbalanced fences are neutralized so injected text cannot forge
    ReviewSensei metadata, notify users, or swallow later sections.
    """

    if not isinstance(value, str):
        raise ReviewInputError("finding text must be a string")
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    fence_lines = [bool(_FENCE_LINE.match(line)) for line in lines]
    unbalanced = sum(fence_lines) % 2 == 1
    sanitized: list[str] = []
    in_fence = False
    for line, is_fence_line in zip(lines, fence_lines, strict=True):
        if unbalanced and is_fence_line:
            # Escaping every fence marker guarantees the text cannot open a
            # fence that would swallow later published sections.
            line = line.replace("`", "\\`").replace("~", "\\~")
        if is_fence_line or in_fence:
            # Inside code fences only marker forging is a hazard; mentions
            # and links are already inert in rendered code.
            sanitized.append(line.replace("<!--", r"<\!--"))
        else:
            segments = _code_spans(line)
            rendered = "".join(
                segment if is_code else _neutralize_links(_neutralize_mentions(segment))
                for segment, is_code in segments
            )
            sanitized.append(rendered.replace("<!--", r"<\!--"))
        if is_fence_line and not unbalanced:
            in_fence = not in_fence
    return "\n".join(sanitized)


def _reference_excerpt(value: str) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) > _REFERENCE_EXCERPT_LIMIT:
        collapsed = f"{collapsed[: _REFERENCE_EXCERPT_LIMIT - 3]}..."
    return escape_markdown_label(collapsed)


def _excerpt_from_raw_body(value: str) -> str:
    """One-pass excerpt of a raw finding body for short summary references.

    The excerpt is escaped before sanitization so already-sanitized text is
    never escaped twice, while mentions and forged markers stay neutralized.
    """

    return sanitize_finding_markdown(_reference_excerpt(value))


def _impact_label(severity: str | None) -> str | None:
    if severity is None:
        return None
    normalized = severity.strip().lower()
    if not normalized:
        return None
    return f"{_label(normalized)} impact"


def _title_label(defect_kind: str | None) -> str | None:
    if defect_kind is None:
        return None
    normalized = " ".join(defect_kind.split())
    if normalized.lower() in _UNSPECIFIC_DEFECT_KINDS:
        return None
    return escape_markdown_label(_label(normalized))


@dataclass(frozen=True)
class FindingView:
    """Typed presentation input for one published finding."""

    identifier: str
    requirement: str = REQUIRED_FINDING
    severity: str | None = None
    title: str | None = None
    detail: str | None = None
    placement: str = "body"
    lifecycle_state: str = "new"
    advisory: bool = False
    repeat: bool = True
    needs_human: bool = False
    location: str | None = None
    excerpt: str | None = None

    def __post_init__(self) -> None:
        if self.requirement not in (REQUIRED_FINDING, OPTIONAL_FINDING):
            raise ReviewInputError("finding requirement is invalid")
        if self.placement not in ("inline", "body"):
            raise ReviewInputError("finding placement is invalid")
        if not self.identifier.startswith(FINDING_ID_PREFIX):
            raise ReviewInputError("finding identifier is invalid")

    @property
    def reference(self) -> str:
        """One-line summary reference that never duplicates the explanation."""

        if self.title is not None:
            subject = self.title
        elif self.excerpt is not None:
            subject = self.excerpt
        elif self.detail is not None:
            subject = _reference_excerpt(self.detail)
        else:
            subject = "Details are in this review body."
        return subject


def _comment_location(comment: ReviewComment, placement: str) -> str | None:
    """Return the reviewed-code reference for findings without a thread."""

    if placement == "inline" or not isinstance(comment.path, str):
        return None
    path = comment.path.strip()
    if not path:
        return None
    escaped = escape_markdown_label(path)
    if isinstance(comment.line, int) and not isinstance(comment.line, bool):
        return f"`{escaped}:{comment.line}`"
    return f"`{escaped}`"


def build_finding_view(
    comment: ReviewComment,
    *,
    fingerprint: str,
    identifier: str | None = None,
    lifecycle_state: str = "new",
    placement: str = "body",
    advisory: bool = False,
) -> FindingView:
    """Map a validated comment onto its presentation view."""

    requirement = REQUIRED_FINDING if comment.blocks_approval else OPTIONAL_FINDING
    repeat = not (
        requirement == OPTIONAL_FINDING and lifecycle_state == "still-present"
    )
    return FindingView(
        identifier=identifier or finding_identifier(fingerprint),
        requirement=requirement,
        severity=comment.severity,
        title=_title_label(comment.defect_kind),
        detail=sanitize_finding_markdown(comment.body),
        placement=placement,
        lifecycle_state=lifecycle_state,
        advisory=advisory,
        repeat=repeat,
        needs_human=bool(comment.needs_human),
        location=_comment_location(comment, placement),
        excerpt=_excerpt_from_raw_body(comment.body) if comment.body else None,
    )


def render_finding(view: FindingView) -> str:
    """Render one finding as a full published explanation."""

    if view.advisory and view.requirement == REQUIRED_FINDING:
        # Advisory mode keeps the defect wording and severity; only the
        # enforcement is disabled and stated once in the summary. Optional
        # feedback keeps its own wording, which never asserts a defect.
        header = "Defect"
    else:
        header = (
            "Required fix"
            if view.requirement == REQUIRED_FINDING
            else "Optional improvement"
        )
    segments = [header]
    impact = _impact_label(view.severity)
    if impact is not None:
        segments.append(impact)
    text = " · ".join(segments)
    if view.title is not None:
        text = f"{text} — {view.title}"
    lines = [f"**{text}**", ""]
    if view.location is not None:
        lines.extend([view.location, ""])
    lines.append(view.detail or "")
    if view.requirement == OPTIONAL_FINDING:
        lines.extend(["", _OPTIONAL_NOT_REQUIRED])
    lines.extend(["", f"`{view.identifier}`"])
    return "\n".join(lines)


def render_finding_reference(view: FindingView) -> str:
    """Render a short summary reference for one finding."""

    reference = (
        _DISCUSSION_REFERENCE_INLINE
        if view.placement == "inline"
        else _DISCUSSION_REFERENCE_BODY
    )
    if view.requirement == OPTIONAL_FINDING and not view.repeat:
        reference = "Previously reported and unchanged."
    subject = view.reference
    if subject and subject[-1] not in ".!?…":
        subject = f"{subject}."
    return f"- **{view.identifier}:** {subject} {reference}"


def order_findings(views: Iterable[FindingView]) -> tuple[FindingView, ...]:
    """Order findings deterministically: required first, then severity and ID."""

    def sort_key(view: FindingView) -> tuple[int, int, str]:
        requirement = 0 if view.requirement == REQUIRED_FINDING else 1
        severity = _SEVERITY_ORDER.get((view.severity or "").strip().lower(), 4)
        return (requirement, severity, view.identifier)

    return tuple(sorted(views, key=sort_key))


def render_body_findings(
    views: Iterable[FindingView],
    *,
    heading: str = "## Findings explained in this review body",
) -> str:
    """Render full explanations for findings the host cannot inline."""

    ordered = order_findings(views)
    lines = [heading]
    for view in ordered:
        if not view.detail:
            continue
        lines.extend(["", render_finding(view)])
    return "\n".join(lines)


def _required_heading(required: Sequence[FindingView], *, advisory: bool) -> str:
    count = len(required)
    if count == 0:
        return "No required fixes"
    if advisory:
        # Enforcement is disabled, so "required fix" would claim gate power
        # this run does not have; the severity itself is not downgraded.
        return "1 defect" if count == 1 else f"{count} defects"
    return f"{count} required fix" if count == 1 else f"{count} required fixes"


def _count_line(
    required: Sequence[FindingView], optional_total: int, *, advisory: bool
) -> str | None:
    required_part = _required_heading(required, advisory=advisory)
    optional_part = (
        f"{optional_total} optional improvement"
        if optional_total == 1
        else f"{optional_total} optional improvements"
        if optional_total > 1
        else ""
    )
    if not optional_part:
        return f"**{required_part}**" if len(required) else None
    return f"**{required_part} · {optional_part}**"


def _next_action(
    *,
    state: str,
    required: Sequence[FindingView],
    optional_total: int,
    reason: str | None,
) -> str:
    identifiers = [view.identifier for view in required]
    if state == ADVISORY_STATE:
        return (
            "Advisory mode: ReviewSensei enforcement is disabled, so these findings "
            "do not affect the review gate."
        )
    if state == INCOMPLETE_STATE:
        detail = reason or "the review did not complete"
        return f"This review is incomplete because {detail}. Re-run ReviewSensei once resolved."
    if state == WITHHELD_STATE:
        detail = reason or "automatic approval is disabled for this repository"
        return f"Approval was withheld because {detail}."
    if state == NO_REQUIRED_FIXES_STATE:
        # The approval itself is a separate, later review operation, so this
        # summary never claims that an approval has already happened.
        action = "ReviewSensei records its decision in the separate approval review."
        if optional_total:
            action = f"{action} Optional suggestions do not affect the review gate."
        return action
    if identifiers:
        if len(identifiers) <= 4:
            addressed = " and ".join(identifiers)
        else:
            addressed = "the required fixes above"
        action = f"Address {addressed} and run verification."
        if optional_total:
            action = f"{action} Optional suggestions do not affect the review gate."
        return action
    if optional_total:
        return "Address the optional improvements at your discretion; they do not affect the review gate."
    return "No further action is required."


@dataclass(frozen=True)
class ReviewSummaryView:
    """Typed presentation input for the published review body."""

    state: str
    head_sha: str
    overview: str | None = None
    coverage_line: str | None = None
    required: tuple[FindingView, ...] = ()
    optional: tuple[FindingView, ...] = ()
    optional_grouped: int = 0
    limitations: tuple[str, ...] = ()
    placement_note: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.state not in _HEADING_STATES:
            raise ReviewInputError("review summary state is invalid")
        if len(self.optional) > _HIGHLIGHTED_OPTIONAL_LIMIT:
            raise ReviewInputError("review summary highlights too many optional items")


def build_review_summary_view(
    *,
    state: str,
    head_sha: str,
    overview: str | None = None,
    coverage_line: str | None = None,
    required: Sequence[FindingView] = (),
    optional: Sequence[FindingView] = (),
    optional_grouped: int = 0,
    limitations: Sequence[str] = (),
    placement_note: str | None = None,
    reason: str | None = None,
) -> ReviewSummaryView:
    """Order and bound summary content deterministically before rendering."""

    required_views = order_findings(required)
    ordered_optional = order_findings(optional)
    return ReviewSummaryView(
        state=state,
        head_sha=head_sha,
        overview=sanitize_finding_markdown(overview) if overview else None,
        coverage_line=coverage_line,
        required=required_views,
        optional=ordered_optional[:_HIGHLIGHTED_OPTIONAL_LIMIT],
        optional_grouped=max(
            0, optional_grouped + len(ordered_optional[_HIGHLIGHTED_OPTIONAL_LIMIT:])
        ),
        limitations=tuple(limitations),
        placement_note=placement_note,
        reason=reason,
    )


def render_review_summary(view: ReviewSummaryView) -> str:
    """Render the §9 review summary: heading, facts, counts, references, action."""

    lines = [_HEADING_STATES[view.state], ""]
    lines.append(f"Reviewed head: `{view.head_sha[:7]}`")
    if view.coverage_line:
        lines.append(view.coverage_line)
    if view.placement_note:
        lines.extend(["", view.placement_note])
    if view.overview:
        lines.extend(["", view.overview])
    optional_total = len(view.optional) + view.optional_grouped
    advisory = view.state == ADVISORY_STATE
    count_line = _count_line(view.required, optional_total, advisory=advisory)
    if count_line:
        lines.extend(["", count_line])
    if view.required:
        lines.extend(["", "### Defects" if advisory else "### Required"])
        lines.extend(render_finding_reference(item) for item in view.required)
    if view.optional or view.optional_grouped:
        lines.extend(["", "### Optional"])
        lines.extend(render_finding_reference(item) for item in view.optional)
        if view.optional_grouped:
            label = (
                "1 additional optional suggestion is"
                if view.optional_grouped == 1
                else f"{view.optional_grouped} additional optional suggestions are"
            )
            lines.append(f"- {label} covered in this review body.")
    if view.limitations:
        lines.extend(["", "### Limitations"])
        lines.extend(f"- {item}" for item in view.limitations)
    lines.extend(
        [
            "",
            "### Next action",
            _next_action(
                state=view.state,
                required=view.required,
                optional_total=optional_total,
                reason=view.reason,
            ),
        ]
    )
    return "\n".join(lines)


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
    """Render one validated review result as readable Markdown.

    Provider text passes through :func:`escape_markdown_text` at this sink, so
    a provider can neither restructure the document nor hide parts of it; the
    structured finding views reach the published sinks through their own
    neutralizing renderers.
    """

    parts = [escape_markdown_text(result.summary)]
    for title, group in _finding_groups(result):
        if not group:
            continue
        parts.append("")
        parts.append(f"### {title}")
        for comment in group:
            parts.append("")
            parts.append(f"**{escape_markdown_label(_finding_location(comment))}**")
            labels = _finding_labels(comment)
            if labels:
                parts.extend(["", labels])
            parts.extend(["", escape_markdown_text(comment.body)])
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
    "ADVISORY_STATE",
    "CHANGES_REQUIRED_STATE",
    "FINDING_ID_PREFIX",
    "FindingView",
    "INCOMPLETE_STATE",
    "NO_REQUIRED_FIXES_STATE",
    "OPTIONAL_FINDING",
    "RENDER_FORMATS",
    "REQUIRED_FINDING",
    "ReviewSummaryView",
    "WITHHELD_STATE",
    "assign_finding_identifiers",
    "build_finding_view",
    "build_review_summary_view",
    "escape_markdown_label",
    "escape_markdown_text",
    "escape_terminal_text",
    "finding_identifier",
    "humanize_lens",
    "order_findings",
    "render_body_findings",
    "render_finding",
    "render_finding_reference",
    "render_review",
    "render_review_markdown",
    "render_review_summary",
    "render_review_text",
    "sanitize_finding_markdown",
]

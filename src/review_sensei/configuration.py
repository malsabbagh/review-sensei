"""Canonical ReviewSensei configuration: bounded YAML, typed schema, resolution.

The repository-root ``.reviewsensei.yml`` file owns product behavior. This
module parses it with a bounded, dependency-free YAML subset parser, validates
it against a versioned typed schema, and resolves the effective provider/model
combination from exactly three sources of differing authority:

    explicit invocation ``--provider``/``--model``
        > the two supported non-empty environment overrides
        > the selected YAML file
        > documented packaged backend defaults

Only ``REVIEWSENSEI_PROVIDER`` and ``REVIEWSENSEI_MODEL`` are supported
overrides. Everything else is YAML or a packaged default; there is no second
configuration hierarchy, no environment interpolation, and no executable YAML.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .errors import ReviewInputError
from .provider_config import (
    DEFAULT_CLOUD_BASE_URL,
    DEFAULT_CLOUD_MODEL,
    DEFAULT_LOCAL_BASE_URL,
    DEFAULT_LOCAL_MODEL,
    DEFAULT_OPENAI_BASE_URL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_OPENROUTER_UPSTREAM,
    hosted_openrouter_upstream,
    published_hosted_openrouter_models,
)
from .providers.openrouter import (
    DEFAULT_OPENROUTER_BASE_URL,
    is_allowlisted_openrouter_endpoint,
)
from .validation import (
    DEFAULT_TOTAL_WORK_MAX_PROVIDER_CALLS,
    read_bounded_utf8,
)

SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_CONFIG_FILENAME = ".reviewsensei.yml"
CONVENTIONAL_DIRECTORY_NAME = ".reviewsensei"
STAGES_DIRECTORY_NAME = "stages"
CATEGORIES_DIRECTORY_NAME = "categories"

MAX_CONFIG_BYTES = 65_536
MAX_CONFIG_LINES = 2_000
MAX_CONFIG_DEPTH = 8
MAX_SCALAR_BYTES = 4_096
MAX_SEQUENCE_ITEMS = 128
MAX_KEY_BYTES = 64
MAX_ECHO_BYTES = 64

SYMBOL_CONTEXT_MAX_FILES_CEILING = 16
SYMBOL_CONTEXT_MAX_BYTES_CEILING = 128 * 1024
SYMBOL_CONTEXT_MAX_DEPTH_CEILING = 1
TIMEOUT_SECONDS_CEILING = 3_600.0

REVIEW_POLICIES = ("auto-approve", "blocking", "advisory")
LEARNING_MODES = ("disabled", "proposals", "pull-requests")
ARTIFACT_MODES = ("none", "diagnostics")

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_INTEGER_PATTERN = re.compile(r"^-?\d+$")
_FLOAT_PATTERN = re.compile(r"^-?\d+\.\d+$")
_OLLAMA_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]+$")
_OPENROUTER_MODEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")
_UPSTREAM_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CREDENTIAL_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_RETIRED_REPLACEMENT_PATTERN = re.compile(r"^[a-z][a-z0-9_.]*$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_OPENAI_DEFAULT_HOSTNAME = "api.openai.com"

ROOT_FIELDS = ("schema", "inference", "github", "advanced")
INFERENCE_FIELDS = ("backend", "model")
GITHUB_FIELDS = (
    "automatic_reviews",
    "writes",
    "reviews",
    "mentions",
    "learning",
    "artifacts",
)
ADVANCED_FIELDS = (
    "endpoint",
    "routing",
    "context",
    "egress",
    "large_changes",
    "resources",
)
ENDPOINT_FIELDS = ("base_url", "allow_custom_endpoint", "credential_env")
ROUTING_FIELDS = ("upstream_provider",)
CONTEXT_FIELDS = ("symbol_context",)
SYMBOL_CONTEXT_FIELDS = (
    "enabled",
    "allowed_paths",
    "max_files",
    "max_bytes",
    "max_depth",
)
EGRESS_FIELDS = ("allow_data_egress",)
LARGE_CHANGE_FIELDS = ("orchestrate",)
RESOURCE_FIELDS = ("timeout_seconds", "max_provider_calls")

RETIRED_FIELDS: Mapping[str, str] = {
    "provider": "inference.backend",
    "backend": "inference.backend",
    "model": "inference.model",
    "provider_mode": "inference.backend (use 'local-ollama' or 'cloud-ollama')",
    "review_mode": (
        "one evidence-focused pipeline is the only engine; integration policy is "
        "github.reviews"
    ),
    "auto_review": "github.automatic_reviews",
    "github_writes": "github.writes",
    "auto_approve": "github.reviews",
    "mention_replies": "github.mentions",
    "version": "release/installer identity; the configuration version is 'schema'",
    "setup_version": "release/installer identity; the configuration version is 'schema'",
    "reviewsensei_version": "release/installer identity; remove it",
    "stages_dir": ".reviewsensei/stages/",
    "categories_dir": ".reviewsensei/categories/",
    "learning_root": "the repository checkout that contains .reviewsensei.yml",
}

RETIRED_ENVIRONMENT_SETTINGS: Mapping[str, str] = {
    "REVIEWSENSEI_PROVIDER_MODE": (
        "select inference.backend ('local-ollama' or 'cloud-ollama') or set "
        "REVIEWSENSEI_PROVIDER"
    ),
    "OLLAMA_MODEL": "set inference.model in .reviewsensei.yml or REVIEWSENSEI_MODEL",
    "REVIEWSENSEI_LOCAL_MODEL": (
        "set inference.model in .reviewsensei.yml or REVIEWSENSEI_MODEL"
    ),
    "REVIEWSENSEI_CLOUD_MODEL": (
        "set inference.model in .reviewsensei.yml or REVIEWSENSEI_MODEL"
    ),
}
RETIRED_BEHAVIOR_ENVIRONMENT_SETTINGS: Mapping[str, str] = {
    "REVIEWSENSEI_AUTO_APPROVE": "set github.reviews: auto-approve",
    "AUTO_APPROVE": "set github.reviews in .reviewsensei.yml",
    "AUTO_REVIEW": "set github.automatic_reviews in .reviewsensei.yml",
    "GITHUB_WRITES": "set github.writes in .reviewsensei.yml",
    "MENTION_REPLIES": "set github.mentions in .reviewsensei.yml",
    "REVIEWSENSEI_REVIEW_MODE": (
        "one evidence-focused pipeline is the only engine; set github.reviews"
    ),
    "REVIEWSENSEI_STAGES_DIR": "place stage JSON files under .reviewsensei/stages/",
    "REVIEWSENSEI_CATEGORIES_DIR": (
        "place category JSON files under .reviewsensei/categories/"
    ),
    "OPENROUTER_UPSTREAM_PROVIDER": (
        "set advanced.routing.upstream_provider in .reviewsensei.yml"
    ),
}


class ConfigurationError(ReviewInputError):
    """Raised when ReviewSensei configuration cannot be trusted."""

    def __init__(
        self,
        message: str,
        *,
        source: str | None = None,
        field: str | None = None,
        line: int | None = None,
    ) -> None:
        self.source = source
        self.field = field
        self.line = line
        location = ""
        if source:
            location = source if line is None else f"{source}:{line}"
        prefix = f"{location}: " if location else ""
        path = f"{field}: " if field else ""
        super().__init__(f"{prefix}{path}{message}")


@dataclass(frozen=True)
class BackendDefaults:
    """Documented packaged defaults for one canonical inference backend."""

    backend: str
    model: str
    base_url: str
    credential_env: str | None
    credential_required: bool
    runner_kind: str
    timeout_seconds: float | None
    upstream_provider: str | None = None
    internal: bool = False


BACKEND_DEFAULTS: Mapping[str, BackendDefaults] = {
    "local-ollama": BackendDefaults(
        backend="local-ollama",
        model=DEFAULT_LOCAL_MODEL,
        base_url=DEFAULT_LOCAL_BASE_URL,
        credential_env="OLLAMA_API_KEY",
        credential_required=False,
        runner_kind="local",
        timeout_seconds=900.0,
    ),
    "cloud-ollama": BackendDefaults(
        backend="cloud-ollama",
        model=DEFAULT_CLOUD_MODEL,
        base_url=DEFAULT_CLOUD_BASE_URL,
        credential_env="OLLAMA_API_KEY",
        credential_required=True,
        runner_kind="hosted",
        timeout_seconds=900.0,
    ),
    "openrouter": BackendDefaults(
        backend="openrouter",
        model=DEFAULT_OPENROUTER_MODEL,
        base_url=DEFAULT_OPENROUTER_BASE_URL,
        credential_env="OPENROUTER_API_KEY",
        credential_required=True,
        runner_kind="hosted",
        timeout_seconds=120.0,
        upstream_provider=DEFAULT_OPENROUTER_UPSTREAM,
    ),
    "openai-compatible": BackendDefaults(
        backend="openai-compatible",
        model=DEFAULT_OPENAI_MODEL,
        base_url=DEFAULT_OPENAI_BASE_URL,
        credential_env="OPENAI_API_KEY",
        credential_required=True,
        runner_kind="hosted",
        timeout_seconds=900.0,
    ),
    "fixture": BackendDefaults(
        backend="fixture",
        model="fixture-v1",
        base_url="",
        credential_env=None,
        credential_required=False,
        runner_kind="local",
        timeout_seconds=None,
        internal=True,
    ),
}


def documented_backend_names() -> tuple[str, ...]:
    """Return the canonical backend names supported by a configuration file."""

    return tuple(
        name for name, defaults in BACKEND_DEFAULTS.items() if not defaults.internal
    )


def backend_defaults(backend: str) -> BackendDefaults:
    """Return the documented packaged defaults for ``backend``."""

    defaults = BACKEND_DEFAULTS.get(backend)
    if defaults is None:
        raise ConfigurationError(
            f"backend '{_bounded_echo(backend)}' is not supported; supported "
            f"backends are {', '.join(documented_backend_names())}"
        )
    return defaults


@dataclass(frozen=True)
class InferenceSection:
    backend: str = "local-ollama"
    model: str | None = None


@dataclass(frozen=True)
class GitHubSection:
    automatic_reviews: bool = True
    writes: bool = False
    reviews: str = "auto-approve"
    mentions: bool = True
    learning: str = "disabled"
    artifacts: str = "none"


@dataclass(frozen=True)
class EndpointSection:
    base_url: str | None = None
    allow_custom_endpoint: bool = False
    credential_env: str | None = None


@dataclass(frozen=True)
class RoutingSection:
    upstream_provider: str | None = None


@dataclass(frozen=True)
class SymbolContextSection:
    enabled: bool = False
    allowed_paths: tuple[str, ...] = ()
    max_files: int = SYMBOL_CONTEXT_MAX_FILES_CEILING
    max_bytes: int = SYMBOL_CONTEXT_MAX_BYTES_CEILING
    max_depth: int = SYMBOL_CONTEXT_MAX_DEPTH_CEILING


@dataclass(frozen=True)
class ContextSection:
    symbol_context: SymbolContextSection = field(default_factory=SymbolContextSection)


@dataclass(frozen=True)
class EgressSection:
    allow_data_egress: bool = False


@dataclass(frozen=True)
class LargeChangeSection:
    orchestrate: bool = False


@dataclass(frozen=True)
class ResourceSection:
    timeout_seconds: float | None = None
    max_provider_calls: int | None = None


@dataclass(frozen=True)
class AdvancedSection:
    endpoint: EndpointSection = field(default_factory=EndpointSection)
    routing: RoutingSection = field(default_factory=RoutingSection)
    context: ContextSection = field(default_factory=ContextSection)
    egress: EgressSection = field(default_factory=EgressSection)
    large_changes: LargeChangeSection = field(default_factory=LargeChangeSection)
    resources: ResourceSection = field(default_factory=ResourceSection)


@dataclass(frozen=True)
class ProductConfiguration:
    """A validated configuration document plus its provenance."""

    schema: int
    inference: InferenceSection
    github: GitHubSection
    advanced: AdvancedSection
    source: str | None
    root: Path
    declared: Mapping[str, int]

    def is_declared(self, field_path: str) -> bool:
        return field_path in self.declared

    def line_of(self, field_path: str) -> int | None:
        return self.declared.get(field_path)


def default_configuration(*, root: Path | None = None) -> ProductConfiguration:
    """Return the configuration used when no file is selected or present."""

    return ProductConfiguration(
        schema=SUPPORTED_SCHEMA_VERSION,
        inference=InferenceSection(),
        github=GitHubSection(),
        advanced=AdvancedSection(),
        source=None,
        root=root if root is not None else Path.cwd(),
        declared={},
    )


def load_configuration(path: Path | None = None) -> ProductConfiguration:
    """Load one configuration file, or packaged defaults when none is present."""

    if path is None:
        candidate = Path(DEFAULT_CONFIG_FILENAME)
        if not candidate.exists() and not candidate.is_symlink():
            return default_configuration()
        path = candidate
    elif not path.exists() and not path.is_symlink():
        raise ConfigurationError(f"configuration file not found: {path}")
    if path.is_symlink():
        raise ConfigurationError(
            f"configuration file must not be a symbolic link: {path}"
        )
    text = read_bounded_utf8(path, maximum=MAX_CONFIG_BYTES, label="configuration")
    # the file's directory is the trusted root for the conventional directories,
    # so it is resolved once, here, rather than at each lookup
    return parse_configuration_text(text, source=str(path), root=path.resolve().parent)


def parse_configuration_text(
    text: object,
    *,
    source: str = DEFAULT_CONFIG_FILENAME,
    root: Path | None = None,
) -> ProductConfiguration:
    """Parse and validate one configuration document."""

    values, lines = _parse_document(text, source=source)
    return _build_configuration(
        values,
        lines,
        source=source,
        root=root if root is not None else Path.cwd(),
    )


def _parse_document(
    text: object, *, source: str
) -> tuple[dict[str, Any], dict[str, int]]:
    if not isinstance(text, str):
        raise ConfigurationError("configuration text must be a string", source=source)
    try:
        size = len(text.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ConfigurationError(
            "configuration must be valid UTF-8", source=source
        ) from exc
    if size > MAX_CONFIG_BYTES:
        raise ConfigurationError(
            f"configuration exceeds {MAX_CONFIG_BYTES} bytes", source=source
        )
    entries = _significant_entries(text, source=source)
    if not entries:
        raise ConfigurationError(
            "configuration is empty; add 'schema: 1'", source=source
        )
    if entries[0].indent != 0:
        raise ConfigurationError(
            "the document root must not be indented",
            source=source,
            line=entries[0].number,
        )
    lines: dict[str, int] = {}
    values, position = _parse_block(
        entries,
        0,
        0,
        (),
        lines,
        source=source,
        depth=1,
    )
    if position != len(entries):
        raise ConfigurationError(
            "unexpected indentation",
            source=source,
            line=entries[position].number,
        )
    return values, lines


@dataclass(frozen=True)
class _Entry:
    number: int
    indent: int
    content: str


def _significant_entries(text: str, *, source: str) -> list[_Entry]:
    raw_lines = text.splitlines()
    if len(raw_lines) > MAX_CONFIG_LINES:
        raise ConfigurationError(
            f"configuration exceeds {MAX_CONFIG_LINES} lines", source=source
        )
    entries: list[_Entry] = []
    document_start_seen = False
    for number, raw in enumerate(raw_lines, start=1):
        line = raw.rstrip()
        prefix = line[: len(line) - len(line.lstrip(" \t"))]
        if "\t" in prefix:
            raise ConfigurationError(
                "indentation must use spaces; tabs are not supported",
                source=source,
                line=number,
            )
        content = _strip_comment(line[len(prefix) :]).rstrip()
        if not content:
            continue
        if content in {"---", "..."}:
            if content == "---" and not entries and not document_start_seen:
                document_start_seen = True
                continue
            raise ConfigurationError(
                "multiple YAML documents are not supported",
                source=source,
                line=number,
            )
        entries.append(_Entry(number, len(prefix), content))
    return entries


def _strip_comment(text: str) -> str:
    quote: str | None = None
    index = 0
    while index < len(text):
        character = text[index]
        if quote is not None:
            if quote == "'" and character == "'":
                if index + 1 < len(text) and text[index + 1] == "'":
                    index += 2
                    continue
                quote = None
            elif quote == '"' and character == "\\":
                index += 2
                continue
            elif quote == '"' and character == '"':
                quote = None
        elif character in {"'", '"'}:
            quote = character
        elif character == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index]
        index += 1
    return text


def _parse_block(
    entries: list[_Entry],
    position: int,
    indent: int,
    path: tuple[str, ...],
    lines: dict[str, int],
    *,
    source: str,
    depth: int,
) -> tuple[Any, int]:
    if depth > MAX_CONFIG_DEPTH:
        raise ConfigurationError(
            "configuration nesting is too deep",
            source=source,
            line=entries[position].number,
        )
    if _is_sequence_entry(entries[position].content):
        return _parse_sequence(entries, position, indent, path, lines, source=source)
    return _parse_mapping(
        entries, position, indent, path, lines, source=source, depth=depth
    )


def _parse_sequence(
    entries: list[_Entry],
    position: int,
    indent: int,
    path: tuple[str, ...],
    lines: dict[str, int],
    *,
    source: str,
) -> tuple[list[Any], int]:
    items: list[Any] = []
    while position < len(entries):
        entry = entries[position]
        if entry.indent < indent:
            break
        if entry.indent > indent:
            raise ConfigurationError(
                "unexpected indentation", source=source, line=entry.number
            )
        if not _is_sequence_entry(entry.content):
            raise ConfigurationError(
                "a sequence cannot contain a 'key: value' entry",
                source=source,
                line=entry.number,
            )
        item = entry.content[1:].strip()
        if not item:
            raise ConfigurationError(
                "empty sequence items are not supported",
                source=source,
                line=entry.number,
            )
        if _split_mapping_entry(item) is not None:
            raise ConfigurationError(
                "sequence items must be scalars; nested mappings are not supported",
                source=source,
                line=entry.number,
            )
        if len(items) >= MAX_SEQUENCE_ITEMS:
            raise ConfigurationError(
                f"sequences are limited to {MAX_SEQUENCE_ITEMS} items",
                source=source,
                line=entry.number,
            )
        items.append(_parse_scalar(item, source=source, line=entry.number))
        lines[_render_path(path + (str(len(items) - 1),))] = entry.number
        position += 1
    return items, position


def _parse_mapping(
    entries: list[_Entry],
    position: int,
    indent: int,
    path: tuple[str, ...],
    lines: dict[str, int],
    *,
    source: str,
    depth: int,
) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}
    while position < len(entries):
        entry = entries[position]
        if entry.indent < indent:
            break
        if entry.indent > indent:
            raise ConfigurationError(
                "unexpected indentation", source=source, line=entry.number
            )
        split = _split_mapping_entry(entry.content)
        if split is None:
            raise ConfigurationError(
                "expected a 'key: value' entry", source=source, line=entry.number
            )
        key, value_text = split
        _validate_key(key, source=source, line=entry.number)
        field_path = _render_path(path + (key,))
        if key in mapping:
            raise ConfigurationError(
                f"duplicate field '{key}'",
                source=source,
                field=field_path,
                line=entry.number,
            )
        if value_text:
            value: Any = _parse_scalar(value_text, source=source, line=entry.number)
            position += 1
        else:
            child_position = position + 1
            if (
                child_position < len(entries)
                and entries[child_position].indent > indent
            ):
                value, position = _parse_block(
                    entries,
                    child_position,
                    entries[child_position].indent,
                    path + (key,),
                    lines,
                    source=source,
                    depth=depth + 1,
                )
            else:
                value = None
                position = child_position
        # a key without a usable value is reported like an omitted field, so
        # `config show --explain` attributes the effective value to its default
        if value is not None:
            lines[field_path] = entry.number
        mapping[key] = value
    return mapping, position


def _is_sequence_entry(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _split_mapping_entry(content: str) -> tuple[str, str] | None:
    quote: str | None = None
    index = 0
    while index < len(content):
        character = content[index]
        if quote is not None:
            if quote == "'" and character == "'":
                if index + 1 < len(content) and content[index + 1] == "'":
                    index += 2
                    continue
                quote = None
            elif quote == '"' and character == "\\":
                index += 2
                continue
            elif quote == '"' and character == '"':
                quote = None
        elif character in {"'", '"'}:
            quote = character
        elif character == ":" and (
            index + 1 == len(content) or content[index + 1] in " \t"
        ):
            return content[:index].strip(), content[index + 1 :].strip()
        index += 1
    return None


def _validate_key(key: str, *, source: str, line: int) -> None:
    if not key:
        raise ConfigurationError(
            "an empty field name is not allowed", source=source, line=line
        )
    if key[0] in {"'", '"'}:
        raise ConfigurationError(
            "quoted field names are not supported", source=source, line=line
        )
    if len(key.encode("utf-8", errors="strict")) > MAX_KEY_BYTES:
        raise ConfigurationError(
            "field name exceeds the supported size", source=source, line=line
        )
    if not _KEY_PATTERN.fullmatch(key):
        raise ConfigurationError(
            f"field name '{_bounded_echo(key)}' is not supported; use lower-case "
            "letters, digits, and underscores",
            source=source,
            line=line,
        )


def _parse_scalar(text: str, *, source: str, line: int) -> Any:
    if len(text.encode("utf-8", errors="strict")) > MAX_SCALAR_BYTES:
        raise ConfigurationError(
            "value exceeds the supported size", source=source, line=line
        )
    if text[0] in {"&", "*", "!", "{", "[", "|", ">", "@", "`"}:
        raise ConfigurationError(
            _unsupported_construct_message(text[0]), source=source, line=line
        )
    if text[0] in {"'", '"'}:
        value = _parse_quoted_scalar(text, source=source, line=line)
    elif text.startswith("- "):
        raise ConfigurationError(
            "a mapping value cannot be a sequence on the same line; use '- item' "
            "entries on the following lines",
            source=source,
            line=line,
        )
    else:
        value = _plain_scalar(text)
    _reject_control_characters(value, source=source, line=line)
    return value


def _unsupported_construct_message(character: str) -> str:
    if character == "&":
        return "YAML anchors are not supported"
    if character == "*":
        return "YAML aliases are not supported"
    if character == "!":
        return "YAML tags are not supported"
    if character in {"{", "["}:
        return (
            "flow collections are not supported; use block mappings and '- ' sequences"
        )
    if character in {"|", ">"}:
        return "block scalars are not supported; use a single-line quoted value"
    return "this value syntax is not supported"


def _parse_quoted_scalar(text: str, *, source: str, line: int) -> str:
    quote = text[0]
    if len(text) < 2 or not text.endswith(quote):
        raise ConfigurationError("unterminated quoted value", source=source, line=line)
    body = text[1:-1]
    if quote == "'":
        return _decode_single_quoted(body, source=source, line=line)
    return _decode_double_quoted(body, source=source, line=line)


def _decode_single_quoted(body: str, *, source: str, line: int) -> str:
    result: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character != "'":
            result.append(character)
            index += 1
            continue
        if index + 1 < len(body) and body[index + 1] == "'":
            result.append("'")
            index += 2
            continue
        raise ConfigurationError(
            "single quotes inside a single-quoted value must be doubled",
            source=source,
            line=line,
        )
    return "".join(result)


_QUOTE_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "/": "/",
}


def _decode_double_quoted(body: str, *, source: str, line: int) -> str:
    result: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character != "\\":
            result.append(character)
            index += 1
            continue
        if index + 1 >= len(body):
            raise ConfigurationError(
                "unterminated escape sequence", source=source, line=line
            )
        escape = body[index + 1]
        if escape in _QUOTE_ESCAPES:
            result.append(_QUOTE_ESCAPES[escape])
            index += 2
            continue
        if escape == "u" and index + 6 <= len(body):
            digits = body[index + 2 : index + 6]
            try:
                result.append(chr(int(digits, 16)))
            except ValueError as exc:
                raise ConfigurationError(
                    "invalid \\u escape sequence", source=source, line=line
                ) from exc
            index += 6
            continue
        raise ConfigurationError(
            f"escape sequence '\\{escape}' is not supported",
            source=source,
            line=line,
        )
    return "".join(result)


def _plain_scalar(text: str) -> Any:
    if text in {"null", "Null", "NULL", "~"}:
        return None
    if text in {"true", "True", "TRUE"}:
        return True
    if text in {"false", "False", "FALSE"}:
        return False
    if _INTEGER_PATTERN.fullmatch(text):
        return int(text)
    if _FLOAT_PATTERN.fullmatch(text):
        return float(text)
    return text


def _reject_control_characters(value: Any, *, source: str, line: int) -> None:
    if not isinstance(value, str):
        return
    for character in value:
        if character in {"\n", "\t", "\r"}:
            continue
        if ord(character) < 0x20 or ord(character) == 0x7F:
            raise ConfigurationError(
                "value contains unsupported control characters",
                source=source,
                line=line,
            )


def _render_path(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _build_configuration(
    values: Mapping[str, Any],
    lines: Mapping[str, int],
    *,
    source: str,
    root: Path,
) -> ProductConfiguration:
    def fail(field_path: str, message: str) -> ConfigurationError:
        return ConfigurationError(
            message,
            source=source,
            field=field_path,
            line=lines.get(field_path),
        )

    _reject_unknown_fields(values, (), lines, source=source)
    if "schema" not in values:
        raise fail("schema", "'schema: 1' is required")
    schema = values["schema"]
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise fail("schema", "schema must be the integer version '1'")
    if schema != SUPPORTED_SCHEMA_VERSION:
        raise fail(
            "schema",
            f"schema {schema} is not supported; this build supports schema "
            f"{SUPPORTED_SCHEMA_VERSION}",
        )
    inference = _build_inference(
        values.get("inference"), lines, source=source, fail=fail
    )
    github = _build_github(values.get("github"), lines, source=source, fail=fail)
    advanced = _build_advanced(values.get("advanced"), lines, source=source, fail=fail)
    return ProductConfiguration(
        schema=schema,
        inference=inference,
        github=github,
        advanced=advanced,
        source=source,
        root=root,
        declared=dict(lines),
    )


def _reject_unknown_fields(
    values: Mapping[str, Any],
    path: tuple[str, ...],
    lines: Mapping[str, int],
    *,
    source: str,
    supported: Sequence[str] = ROOT_FIELDS,
) -> None:
    for key in values:
        if key in supported:
            continue
        field_path = _render_path(path + (key,))
        retired = RETIRED_FIELDS.get(field_path) or RETIRED_FIELDS.get(key)
        if retired is not None:
            message = (
                f"'{key}' was retired; use {retired}"
                if _RETIRED_REPLACEMENT_PATTERN.fullmatch(retired)
                else f"'{key}' was retired: {retired}"
            )
        else:
            message = (
                f"field '{key}' is not supported; supported fields are "
                f"{', '.join(supported)}"
            )
        raise ConfigurationError(
            message,
            source=source,
            field=field_path,
            line=lines.get(field_path),
        )


def _mapping_field(
    value: Any,
    field_path: str,
    *,
    source: str,
    fail: Any,
) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise fail(field_path, "must be a mapping of fields")
    return value


def _bool_field(value: Any, field_path: str, default: bool, *, fail: Any) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        raise fail(
            field_path,
            f"must be true or false, not the string '{_bounded_echo(value)}'",
        )
    raise fail(field_path, "must be true or false")


def _string_field(
    value: Any, field_path: str, default: str | None, *, fail: Any
) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str):
        raise fail(field_path, "must be a string")
    if not value.strip():
        raise fail(field_path, "must not be empty")
    if len(value.encode("utf-8", errors="strict")) > MAX_SCALAR_BYTES:
        raise fail(field_path, "exceeds the supported size")
    return value


def _positive_int_field(
    value: Any,
    field_path: str,
    default: int,
    *,
    ceiling: int,
    fail: Any,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise fail(field_path, "must be an integer")
    if value < 1:
        raise fail(field_path, "must be a positive integer")
    if value > ceiling:
        raise fail(
            field_path,
            f"must not exceed the packaged ceiling {ceiling}; hard safety "
            "ceilings are not configurable",
        )
    return value


def _enum_field(
    value: Any,
    field_path: str,
    allowed: Sequence[str],
    default: str,
    *,
    fail: Any,
) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise fail(field_path, f"must be one of {', '.join(allowed)}")
    if value not in allowed:
        raise fail(
            field_path,
            f"'{_bounded_echo(value)}' is not supported; use one of "
            f"{', '.join(allowed)}",
        )
    return value


def _build_inference(
    value: Any, lines: Mapping[str, int], *, source: str, fail: Any
) -> InferenceSection:
    mapping = _mapping_field(value, "inference", source=source, fail=fail)
    _reject_unknown_fields(
        mapping, ("inference",), lines, source=source, supported=INFERENCE_FIELDS
    )
    backend = _string_field(
        mapping.get("backend"), "inference.backend", None, fail=fail
    )
    model = _string_field(mapping.get("model"), "inference.model", None, fail=fail)
    if backend is not None and backend not in documented_backend_names():
        raise fail(
            "inference.backend",
            f"'{_bounded_echo(backend)}' is not a canonical backend; use one of "
            f"{', '.join(documented_backend_names())}",
        )
    return InferenceSection(
        backend=backend if backend is not None else "local-ollama",
        model=model,
    )


def _build_github(
    value: Any, lines: Mapping[str, int], *, source: str, fail: Any
) -> GitHubSection:
    mapping = _mapping_field(value, "github", source=source, fail=fail)
    _reject_unknown_fields(
        mapping, ("github",), lines, source=source, supported=GITHUB_FIELDS
    )
    return GitHubSection(
        automatic_reviews=_bool_field(
            mapping.get("automatic_reviews"),
            "github.automatic_reviews",
            True,
            fail=fail,
        ),
        writes=_bool_field(mapping.get("writes"), "github.writes", False, fail=fail),
        reviews=_enum_field(
            mapping.get("reviews"),
            "github.reviews",
            REVIEW_POLICIES,
            "auto-approve",
            fail=fail,
        ),
        mentions=_bool_field(
            mapping.get("mentions"), "github.mentions", True, fail=fail
        ),
        learning=_enum_field(
            mapping.get("learning"),
            "github.learning",
            LEARNING_MODES,
            "disabled",
            fail=fail,
        ),
        artifacts=_enum_field(
            mapping.get("artifacts"),
            "github.artifacts",
            ARTIFACT_MODES,
            "none",
            fail=fail,
        ),
    )


def _build_advanced(
    value: Any, lines: Mapping[str, int], *, source: str, fail: Any
) -> AdvancedSection:
    mapping = _mapping_field(value, "advanced", source=source, fail=fail)
    _reject_unknown_fields(
        mapping, ("advanced",), lines, source=source, supported=ADVANCED_FIELDS
    )
    return AdvancedSection(
        endpoint=_build_endpoint(
            mapping.get("endpoint"), lines, source=source, fail=fail
        ),
        routing=_build_routing(mapping.get("routing"), lines, source=source, fail=fail),
        context=_build_context(mapping.get("context"), lines, source=source, fail=fail),
        egress=_build_egress(mapping.get("egress"), lines, source=source, fail=fail),
        large_changes=_build_large_changes(
            mapping.get("large_changes"), lines, source=source, fail=fail
        ),
        resources=_build_resources(
            mapping.get("resources"), lines, source=source, fail=fail
        ),
    )


def _build_endpoint(
    value: Any, lines: Mapping[str, int], *, source: str, fail: Any
) -> EndpointSection:
    mapping = _mapping_field(value, "advanced.endpoint", source=source, fail=fail)
    _reject_unknown_fields(
        mapping,
        ("advanced", "endpoint"),
        lines,
        source=source,
        supported=ENDPOINT_FIELDS,
    )
    base_url = _string_field(
        mapping.get("base_url"), "advanced.endpoint.base_url", None, fail=fail
    )
    credential_env = _string_field(
        mapping.get("credential_env"),
        "advanced.endpoint.credential_env",
        None,
        fail=fail,
    )
    if credential_env is not None and not _CREDENTIAL_ENV_PATTERN.fullmatch(
        credential_env
    ):
        raise fail(
            "advanced.endpoint.credential_env",
            "must be an environment variable name such as OPENAI_API_KEY",
        )
    return EndpointSection(
        base_url=base_url,
        allow_custom_endpoint=_bool_field(
            mapping.get("allow_custom_endpoint"),
            "advanced.endpoint.allow_custom_endpoint",
            False,
            fail=fail,
        ),
        credential_env=credential_env,
    )


def _build_routing(
    value: Any, lines: Mapping[str, int], *, source: str, fail: Any
) -> RoutingSection:
    mapping = _mapping_field(value, "advanced.routing", source=source, fail=fail)
    _reject_unknown_fields(
        mapping, ("advanced", "routing"), lines, source=source, supported=ROUTING_FIELDS
    )
    upstream = _string_field(
        mapping.get("upstream_provider"),
        "advanced.routing.upstream_provider",
        None,
        fail=fail,
    )
    if upstream is not None and not _UPSTREAM_PROVIDER_PATTERN.fullmatch(upstream):
        raise fail(
            "advanced.routing.upstream_provider",
            "must be a lower-case provider slug such as 'morph'",
        )
    return RoutingSection(upstream_provider=upstream)


def _build_context(
    value: Any, lines: Mapping[str, int], *, source: str, fail: Any
) -> ContextSection:
    mapping = _mapping_field(value, "advanced.context", source=source, fail=fail)
    _reject_unknown_fields(
        mapping, ("advanced", "context"), lines, source=source, supported=CONTEXT_FIELDS
    )
    symbol_mapping = _mapping_field(
        mapping.get("symbol_context"),
        "advanced.context.symbol_context",
        source=source,
        fail=fail,
    )
    _reject_unknown_fields(
        symbol_mapping,
        ("advanced", "context", "symbol_context"),
        lines,
        source=source,
        supported=SYMBOL_CONTEXT_FIELDS,
    )
    allowed_paths_value = symbol_mapping.get("allowed_paths")
    allowed_paths: tuple[str, ...] = ()
    if allowed_paths_value is not None:
        if not isinstance(allowed_paths_value, list):
            raise fail(
                "advanced.context.symbol_context.allowed_paths",
                "must be a sequence of repository-relative paths",
            )
        allowed_paths = tuple(
            _validate_allowed_path(
                item,
                fail=fail,
                field_path="advanced.context.symbol_context.allowed_paths",
            )
            for item in allowed_paths_value
        )
    return ContextSection(
        symbol_context=SymbolContextSection(
            enabled=_bool_field(
                symbol_mapping.get("enabled"),
                "advanced.context.symbol_context.enabled",
                False,
                fail=fail,
            ),
            allowed_paths=allowed_paths,
            max_files=_positive_int_field(
                symbol_mapping.get("max_files"),
                "advanced.context.symbol_context.max_files",
                SYMBOL_CONTEXT_MAX_FILES_CEILING,
                ceiling=SYMBOL_CONTEXT_MAX_FILES_CEILING,
                fail=fail,
            ),
            max_bytes=_positive_int_field(
                symbol_mapping.get("max_bytes"),
                "advanced.context.symbol_context.max_bytes",
                SYMBOL_CONTEXT_MAX_BYTES_CEILING,
                ceiling=SYMBOL_CONTEXT_MAX_BYTES_CEILING,
                fail=fail,
            ),
            max_depth=_positive_int_field(
                symbol_mapping.get("max_depth"),
                "advanced.context.symbol_context.max_depth",
                SYMBOL_CONTEXT_MAX_DEPTH_CEILING,
                ceiling=SYMBOL_CONTEXT_MAX_DEPTH_CEILING,
                fail=fail,
            ),
        )
    )


def _validate_allowed_path(item: Any, *, fail: Any, field_path: str) -> str:
    if not isinstance(item, str) or not item.strip():
        raise fail(field_path, "must contain non-empty repository-relative paths")
    if len(item.encode("utf-8", errors="strict")) > 256:
        raise fail(field_path, "path exceeds the supported size")
    if item.startswith("/") or item.startswith("\\"):
        raise fail(field_path, "paths must be repository-relative")
    normalized = item.replace("\\", "/")
    if ".." in normalized.split("/"):
        raise fail(field_path, "paths must not traverse outside the repository")
    return item


def _build_egress(
    value: Any, lines: Mapping[str, int], *, source: str, fail: Any
) -> EgressSection:
    mapping = _mapping_field(value, "advanced.egress", source=source, fail=fail)
    _reject_unknown_fields(
        mapping, ("advanced", "egress"), lines, source=source, supported=EGRESS_FIELDS
    )
    return EgressSection(
        allow_data_egress=_bool_field(
            mapping.get("allow_data_egress"),
            "advanced.egress.allow_data_egress",
            False,
            fail=fail,
        )
    )


def _build_large_changes(
    value: Any, lines: Mapping[str, int], *, source: str, fail: Any
) -> LargeChangeSection:
    mapping = _mapping_field(value, "advanced.large_changes", source=source, fail=fail)
    _reject_unknown_fields(
        mapping,
        ("advanced", "large_changes"),
        lines,
        source=source,
        supported=LARGE_CHANGE_FIELDS,
    )
    return LargeChangeSection(
        orchestrate=_bool_field(
            mapping.get("orchestrate"),
            "advanced.large_changes.orchestrate",
            False,
            fail=fail,
        )
    )


def _build_resources(
    value: Any, lines: Mapping[str, int], *, source: str, fail: Any
) -> ResourceSection:
    mapping = _mapping_field(value, "advanced.resources", source=source, fail=fail)
    _reject_unknown_fields(
        mapping,
        ("advanced", "resources"),
        lines,
        source=source,
        supported=RESOURCE_FIELDS,
    )
    timeout_value = mapping.get("timeout_seconds")
    timeout_seconds: float | None = None
    if timeout_value is not None:
        if isinstance(timeout_value, bool) or not isinstance(
            timeout_value, (int, float)
        ):
            raise fail("advanced.resources.timeout_seconds", "must be a number")
        timeout_seconds = float(timeout_value)
        if timeout_seconds <= 0:
            raise fail(
                "advanced.resources.timeout_seconds", "must be a positive number"
            )
        if timeout_seconds > TIMEOUT_SECONDS_CEILING:
            raise fail(
                "advanced.resources.timeout_seconds",
                f"must not exceed the packaged ceiling {int(TIMEOUT_SECONDS_CEILING)}",
            )
    max_provider_calls: int | None = None
    if mapping.get("max_provider_calls") is not None:
        max_provider_calls = _positive_int_field(
            mapping.get("max_provider_calls"),
            "advanced.resources.max_provider_calls",
            DEFAULT_TOTAL_WORK_MAX_PROVIDER_CALLS,
            ceiling=DEFAULT_TOTAL_WORK_MAX_PROVIDER_CALLS,
            fail=fail,
        )
    return ResourceSection(
        timeout_seconds=timeout_seconds,
        max_provider_calls=max_provider_calls,
    )


def _bounded_echo(value: str) -> str:
    text = value.strip()
    if len(text) <= MAX_ECHO_BYTES:
        return text
    return f"{text[:MAX_ECHO_BYTES]}..."


@dataclass(frozen=True)
class Provenance:
    """Where one resolved value came from."""

    source: str
    detail: str


@dataclass(frozen=True)
class ResolvedInference:
    """The effective, validated inference configuration and its provenance."""

    backend: str
    model: str
    base_url: str
    credential_env: str | None
    credential_required: bool
    credential_present: bool
    runner_kind: str
    inference_location: str
    upstream_provider: str | None
    timeout_seconds: float | None
    provenance: Mapping[str, Provenance]

    def source_of(self, field_name: str) -> Provenance:
        return self.provenance[field_name]


def resolve_inference(
    configuration: ProductConfiguration | None = None,
    *,
    cli_provider: str | None = None,
    cli_model: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ResolvedInference:
    """Resolve the effective backend and model from the documented precedence."""

    configuration = (
        configuration if configuration is not None else default_configuration()
    )
    environment = os.environ if environ is None else environ
    _reject_retired_provider_environment(environment)

    provenance: dict[str, Provenance] = {}
    backend_choice = _first_non_empty(
        (
            (cli_provider, Provenance("command line", "--provider")),
            (
                environment.get("REVIEWSENSEI_PROVIDER"),
                Provenance("environment variable", "REVIEWSENSEI_PROVIDER"),
            ),
            (
                configuration.inference.backend
                if configuration.is_declared("inference.backend")
                else None,
                Provenance(
                    "configuration file", f"{configuration.source} inference.backend"
                ),
            ),
        )
    )
    if backend_choice is None:
        backend = "local-ollama"
        provenance["backend"] = Provenance(
            "packaged default", "packaged default ('local-ollama')"
        )
    else:
        raw_backend, backend_provenance = backend_choice
        backend = _normalize_backend(
            raw_backend, provenance=backend_provenance, configuration=configuration
        )
        provenance["backend"] = backend_provenance
    defaults = backend_defaults(backend)

    model_choice = _first_non_empty(
        (
            (cli_model, Provenance("command line", "--model")),
            (
                environment.get("REVIEWSENSEI_MODEL"),
                Provenance("environment variable", "REVIEWSENSEI_MODEL"),
            ),
            (
                configuration.inference.model
                if configuration.is_declared("inference.model")
                else None,
                Provenance(
                    "configuration file", f"{configuration.source} inference.model"
                ),
            ),
        )
    )
    if model_choice is None:
        model = defaults.model
        provenance["model"] = Provenance(
            "packaged default", f"packaged default for backend '{backend}'"
        )
    else:
        model, model_provenance = model_choice
        provenance["model"] = model_provenance
        _validate_model(
            model,
            backend=backend,
            defaults=defaults,
            provenance=model_provenance,
            configuration=configuration,
        )

    base_url, base_url_provenance = _resolve_endpoint(
        configuration, backend=backend, defaults=defaults
    )
    provenance["base_url"] = base_url_provenance

    credential_env = defaults.credential_env
    configured_credential = configuration.advanced.endpoint.credential_env
    if configured_credential is not None:
        if backend != "openai-compatible":
            raise ConfigurationError(
                "advanced.endpoint.credential_env is only supported for backend "
                f"'openai-compatible'; backend '{backend}' reads "
                f"{defaults.credential_env or 'no credential'}",
                source=configuration.source,
                field="advanced.endpoint.credential_env",
                line=configuration.line_of("advanced.endpoint.credential_env"),
            )
        credential_env = configured_credential
        provenance["credential_env"] = Provenance(
            "configuration file",
            f"{configuration.source} advanced.endpoint.credential_env",
        )
    else:
        provenance["credential_env"] = Provenance(
            "packaged default", f"packaged default for backend '{backend}'"
        )
    credential_present = bool(
        credential_env and environment.get(credential_env, "").strip()
    )
    provenance["runner_kind"] = Provenance(
        "packaged default", f"packaged default for backend '{backend}'"
    )

    upstream_provider = _resolve_upstream(
        configuration,
        backend=backend,
        model=model,
        defaults=defaults,
    )
    timeout_seconds = _resolve_timeout(configuration, defaults=defaults)
    inference_location = (
        "local" if backend == "fixture" or _is_loopback_url(base_url) else "remote"
    )
    return ResolvedInference(
        backend=backend,
        model=model,
        base_url=base_url,
        credential_env=credential_env,
        credential_required=defaults.credential_required,
        credential_present=credential_present,
        runner_kind=defaults.runner_kind,
        inference_location=inference_location,
        upstream_provider=upstream_provider,
        timeout_seconds=timeout_seconds,
        provenance=provenance,
    )


def _first_non_empty(
    candidates: Sequence[tuple[str | None, Provenance]],
) -> tuple[str, Provenance] | None:
    for raw, provenance in candidates:
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise ConfigurationError(
                f"{provenance.detail} must be a string", source=provenance.source
            )
        value = raw.strip()
        if not value:
            continue
        return value, provenance
    return None


def _normalize_backend(
    raw: str,
    *,
    provenance: Provenance,
    configuration: ProductConfiguration,
) -> str:
    candidate = raw.strip().casefold()
    defaults = BACKEND_DEFAULTS.get(candidate)
    if defaults is not None:
        return candidate
    legacy = {
        "ollama": "local-ollama",
        "local": "local-ollama",
        "local_ollama": "local-ollama",
        "cloud": "cloud-ollama",
        "cloud_ollama": "cloud-ollama",
        "ollama-cloud": "cloud-ollama",
        "openai": "openai-compatible",
    }
    from_file = provenance.source == "configuration file"
    field_path = "inference.backend" if from_file else None
    replacement = legacy.get(candidate)
    if replacement is not None:
        remedy = (
            f"use '{replacement}'"
            if from_file
            else f"set inference.backend in {DEFAULT_CONFIG_FILENAME} to "
            f"'{replacement}'"
        )
        message = (
            f"backend '{_bounded_echo(raw)}' from {provenance.detail} is not a "
            f"canonical backend; {remedy}"
        )
    else:
        guidance = (
            ""
            if from_file
            else f"; set inference.backend in {DEFAULT_CONFIG_FILENAME} to one of these"
        )
        message = (
            f"backend '{_bounded_echo(raw)}' from {provenance.detail} is not "
            f"supported; supported backends are "
            f"{', '.join(documented_backend_names())}{guidance}"
        )
    if field_path is None:
        raise ConfigurationError(message, source=provenance.source)
    raise _located(configuration, field_path, message)


def _located(
    configuration: ProductConfiguration,
    field_path: str | None,
    message: str,
) -> ConfigurationError:
    if field_path is None:
        return ConfigurationError(message)
    return ConfigurationError(
        message,
        source=configuration.source,
        field=field_path,
        line=configuration.line_of(field_path),
    )


_MODEL_REQUIREMENTS: Mapping[str, str] = {
    "local-ollama": (
        "local-ollama models are Ollama slugs without a vendor prefix and must "
        "not use the ':cloud' suffix"
    ),
    "cloud-ollama": ("cloud-ollama models are Ollama slugs without a vendor prefix"),
    "openrouter": (
        "openrouter models use a 'vendor/model' slug and must not use the "
        "':cloud' suffix"
    ),
    "openai-compatible": (
        "openai-compatible models are non-empty identifiers such as 'gpt-4o-mini'"
    ),
    "fixture": "fixture models are non-empty identifiers",
}


def _validate_model(
    model: str,
    *,
    backend: str,
    defaults: BackendDefaults,
    provenance: Provenance,
    configuration: ProductConfiguration,
) -> None:
    if len(model.encode("utf-8", errors="strict")) > MAX_SCALAR_BYTES:
        raise _model_error(
            configuration,
            provenance,
            backend,
            defaults,
            "model exceeds the supported size",
        )
    if model.startswith("-"):
        raise _model_error(
            configuration,
            provenance,
            backend,
            defaults,
            "model must not start with '-'",
        )
    if backend == "openrouter":
        if ":cloud" in model:
            raise _model_error(
                configuration,
                provenance,
                backend,
                defaults,
                "an Ollama cloud model cannot be reinterpreted as an openrouter model",
            )
        if not _OPENROUTER_MODEL_PATTERN.fullmatch(model):
            raise _model_error(
                configuration,
                provenance,
                backend,
                defaults,
                "an Ollama model slug cannot be reinterpreted as an openrouter model",
            )
        return
    if backend in {"local-ollama", "cloud-ollama"}:
        if "/" in model:
            raise _model_error(
                configuration,
                provenance,
                backend,
                defaults,
                "an openrouter slug cannot be reinterpreted as an Ollama model",
            )
        if ":cloud" in model and backend == "local-ollama":
            raise _model_error(
                configuration,
                provenance,
                backend,
                defaults,
                "a ':cloud' model requires backend 'cloud-ollama'",
            )
        if not _OLLAMA_MODEL_PATTERN.fullmatch(model):
            raise _model_error(
                configuration,
                provenance,
                backend,
                defaults,
                "model is not a valid Ollama model slug",
            )
        return
    if ":cloud" in model and backend != "cloud-ollama":
        raise _model_error(
            configuration,
            provenance,
            backend,
            defaults,
            "a ':cloud' model requires backend 'cloud-ollama'",
        )


def _model_error(
    configuration: ProductConfiguration,
    provenance: Provenance,
    backend: str,
    defaults: BackendDefaults,
    problem: str,
) -> ConfigurationError:
    from_file = provenance.source == "configuration file"
    remedy = (
        f"Update inference.model in {configuration.source} or set REVIEWSENSEI_MODEL "
        f"to a model valid for '{backend}'."
        if from_file
        else f"Pass --model or set REVIEWSENSEI_MODEL to a model valid for '{backend}'."
    )
    origin = (
        "inference.model from the configuration file"
        if from_file
        else f"model from {provenance.detail}"
    )
    message = (
        f"{origin} is not valid for backend '{backend}': {problem}; "
        f"{_MODEL_REQUIREMENTS.get(backend, '')}, for example '{defaults.model}'. "
        f"{remedy}"
    )
    if from_file:
        return _located(configuration, "inference.model", message)
    return ConfigurationError(message)


def _resolve_endpoint(
    configuration: ProductConfiguration,
    *,
    backend: str,
    defaults: BackendDefaults,
) -> tuple[str, Provenance]:
    endpoint = configuration.advanced.endpoint
    if endpoint.base_url is None:
        return defaults.base_url, Provenance(
            "packaged default", f"packaged default for backend '{backend}'"
        )
    if backend == "openrouter":
        if not is_allowlisted_openrouter_endpoint(endpoint.base_url):
            raise _located(
                configuration,
                "advanced.endpoint.base_url",
                "openrouter keeps its allowlisted packaged endpoint; a custom "
                "endpoint cannot be retained or routing restrictions relaxed",
            )
        return endpoint.base_url, Provenance(
            "configuration file",
            f"{configuration.source} advanced.endpoint.base_url",
        )
    if not endpoint.allow_custom_endpoint:
        raise _located(
            configuration,
            "advanced.endpoint.base_url",
            "advanced.endpoint.allow_custom_endpoint: true is required to select "
            "a custom endpoint; no custom endpoint is enabled implicitly",
        )
    _validate_transport(endpoint.base_url, configuration=configuration)
    return endpoint.base_url, Provenance(
        "configuration file", f"{configuration.source} advanced.endpoint.base_url"
    )


def _validate_transport(url: str, *, configuration: ProductConfiguration) -> None:
    def fail(message: str) -> ConfigurationError:
        return _located(configuration, "advanced.endpoint.base_url", message)

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise fail("advanced.endpoint.base_url must use http or https")
    if parsed.query or parsed.fragment:
        raise fail("advanced.endpoint.base_url must not contain a query or fragment")
    if not parsed.hostname:
        raise fail("advanced.endpoint.base_url must include a host")
    if parsed.username or parsed.password:
        raise fail("advanced.endpoint.base_url must not contain credentials")
    try:
        parsed.port
    except ValueError as exc:
        raise fail("advanced.endpoint.base_url contains an invalid port") from exc
    if parsed.scheme == "http" and not _is_plaintext_transport_allowed(parsed.hostname):
        raise fail(
            "advanced.endpoint.base_url must use https for a non-local host; "
            "plaintext transport is limited to loopback names, private IPv4 "
            "literals, and .local or .internal names"
        )


def _is_plaintext_transport_allowed(hostname: str) -> bool:
    host = hostname.casefold()
    if host in _LOOPBACK_HOSTS:
        return True
    if _is_private_ipv4_literal(host):
        return True
    return host.endswith(".local") or host.endswith(".internal")


def _is_private_ipv4_literal(host: str) -> bool:
    parts = host.split(".")
    if len(parts) != 4 or not all(part.isascii() and part.isdigit() for part in parts):
        return False
    octets = [int(part) for part in parts]
    if any(octet > 255 for octet in octets):
        return False
    if octets[0] in {10, 127}:
        return True
    if octets[0] == 192 and octets[1] == 168:
        return True
    return octets[0] == 172 and 16 <= octets[1] <= 31


def _is_loopback_url(url: str) -> bool:
    if not url:
        return False
    hostname = (urlsplit(url).hostname or "").casefold()
    return hostname in _LOOPBACK_HOSTS or hostname.startswith("127.")


def _resolve_upstream(
    configuration: ProductConfiguration,
    *,
    backend: str,
    model: str,
    defaults: BackendDefaults,
) -> str | None:
    configured = configuration.advanced.routing.upstream_provider
    if configured is not None and backend != "openrouter":
        raise _located(
            configuration,
            "advanced.routing.upstream_provider",
            "advanced.routing.upstream_provider applies only to backend "
            f"'openrouter'; the effective backend is '{backend}'",
        )
    if backend != "openrouter":
        return None
    hosted = published_hosted_openrouter_models()
    if model in hosted:
        documented = _documented_upstream(model)
        if configured is not None and configured != documented:
            raise _located(
                configuration,
                "advanced.routing.upstream_provider",
                f"'{configured}' does not match the documented upstream "
                f"'{documented}' for model '{model}'; routing restrictions cannot "
                "be relaxed",
            )
        return documented
    return configured if configured is not None else defaults.upstream_provider


def _documented_upstream(model: str) -> str:
    return hosted_openrouter_upstream(model)


def _resolve_timeout(
    configuration: ProductConfiguration, *, defaults: BackendDefaults
) -> float | None:
    configured = configuration.advanced.resources.timeout_seconds
    if configured is not None:
        return configured
    return defaults.timeout_seconds


def retired_environment_settings(
    environ: Mapping[str, str], *, include_provider_overrides: bool = True
) -> tuple[str, ...]:
    """Return retired settings present in the environment with non-empty values."""

    candidates: dict[str, str] = dict(RETIRED_BEHAVIOR_ENVIRONMENT_SETTINGS)
    if include_provider_overrides:
        candidates.update(RETIRED_ENVIRONMENT_SETTINGS)
    return tuple(
        sorted(
            name
            for name in candidates
            if isinstance(environ.get(name), str) and environ.get(name, "").strip()
        )
    )


def retired_environment_remedies(
    environ: Mapping[str, str], *, include_provider_overrides: bool = True
) -> tuple[tuple[str, str], ...]:
    """Return retired settings and their replacements, sorted by setting name."""

    candidates: dict[str, str] = dict(RETIRED_BEHAVIOR_ENVIRONMENT_SETTINGS)
    if include_provider_overrides:
        candidates.update(RETIRED_ENVIRONMENT_SETTINGS)
    return tuple(
        (name, candidates[name])
        for name in sorted(candidates)
        if isinstance(environ.get(name), str) and environ.get(name, "").strip()
    )


def _reject_retired_provider_environment(environ: Mapping[str, str]) -> None:
    for name, replacement in RETIRED_ENVIRONMENT_SETTINGS.items():
        value = environ.get(name)
        if isinstance(value, str) and value.strip():
            raise ConfigurationError(
                f"{name} is retired and has no effect; {replacement}",
                source="environment",
            )


@dataclass(frozen=True)
class CustomizationDirectories:
    """Selected conventional customization directories, or packaged defaults."""

    stages: Path | None
    categories: Path | None

    def selected(self) -> tuple[tuple[str, Path], ...]:
        entries = []
        if self.stages is not None:
            entries.append(("stages", self.stages))
        if self.categories is not None:
            entries.append(("categories", self.categories))
        return tuple(entries)


def customization_directories(
    configuration: ProductConfiguration | None = None,
) -> CustomizationDirectories:
    """Return the conventional customization directories beside the config root."""

    configuration = (
        configuration if configuration is not None else default_configuration()
    )
    base = configuration.root / CONVENTIONAL_DIRECTORY_NAME
    return CustomizationDirectories(
        stages=_select_customization_directory(base / STAGES_DIRECTORY_NAME),
        categories=_select_customization_directory(base / CATEGORIES_DIRECTORY_NAME),
    )


def _select_customization_directory(path: Path) -> Path | None:
    label = f"{CONVENTIONAL_DIRECTORY_NAME}/{path.name}"
    if path.is_symlink():
        raise ConfigurationError(f"{label} must not be a symbolic link")
    if not path.exists():
        return None
    if not path.is_dir():
        raise ConfigurationError(f"{label} must be a directory")
    files = [
        entry for entry in sorted(path.iterdir()) if not entry.name.startswith(".")
    ]
    if not files:
        raise ConfigurationError(
            f"{label} is present but contains no configuration; remove the "
            "directory to use packaged defaults"
        )
    for entry in files:
        if entry.is_symlink() or not entry.is_file():
            raise ConfigurationError(f"{label}/{entry.name} must be a regular file")
        if entry.suffix != ".json":
            raise ConfigurationError(
                f"{label}/{entry.name} is not supported; only .json files are read"
            )
    return path


def render_configuration(
    configuration: ProductConfiguration,
    resolved: ResolvedInference,
    *,
    explain: bool = True,
    customization: CustomizationDirectories | None = None,
) -> str:
    """Render the effective configuration, optionally with provenance."""

    customization = (
        customization
        if customization is not None
        else customization_directories(configuration)
    )
    lines = [
        f"configuration: {configuration.source or 'none (packaged defaults)'}"
        f" (schema {configuration.schema})",
        f"configuration root: {configuration.root}",
        "effective inference:",
    ]
    inference_rows = (
        ("backend", resolved.backend),
        ("model", resolved.model),
        ("base_url", resolved.base_url or "none"),
        (
            "credential reference",
            f"{resolved.credential_env or 'none'}"
            f"{' (present)' if resolved.credential_present else ' (absent)'}",
        ),
        (
            "execution",
            f"{resolved.runner_kind} runner, {resolved.inference_location} inference",
        ),
    )
    for label, value in inference_rows:
        lines.append(
            f"  {label}: {value}{_render_provenance(resolved, label, explain)}"
        )
    if resolved.upstream_provider is not None:
        provenance = Provenance("packaged default", "documented routing policy")
        if configuration.advanced.routing.upstream_provider is not None:
            provenance = Provenance(
                "configuration file",
                f"{configuration.source} advanced.routing.upstream_provider",
            )
        lines.append(
            f"  routing upstream: {resolved.upstream_provider}"
            f"{_format_provenance(provenance, explain)}"
        )
    if resolved.timeout_seconds is not None:
        provenance = Provenance(
            "packaged default", f"packaged default for backend '{resolved.backend}'"
        )
        if configuration.advanced.resources.timeout_seconds is not None:
            provenance = Provenance(
                "configuration file",
                f"{configuration.source} advanced.resources.timeout_seconds",
            )
        lines.append(
            f"  timeout: {resolved.timeout_seconds:g}s"
            f"{_format_provenance(provenance, explain)}"
        )
    lines.append("github:")
    for field_name in GITHUB_FIELDS:
        value = getattr(configuration.github, field_name)
        lines.append(
            f"  {field_name}: {_render_value(value)}"
            f"{_yaml_provenance(configuration, f'github.{field_name}', explain)}"
        )
    advanced_rows = _advanced_rows(configuration)
    if advanced_rows:
        lines.append("advanced:")
        lines.extend(f"  {row}" for row in advanced_rows)
    else:
        lines.append("advanced: no overrides (all advanced capabilities are off)")
    if explain:
        lines.extend(_omitted_field_lines(configuration))
    for label, path in customization.selected():
        lines.append(f"customization: {label} from {path}")
    if not customization.selected():
        lines.append(
            f"customization: packaged defaults "
            f"({CONVENTIONAL_DIRECTORY_NAME}/{STAGES_DIRECTORY_NAME}, "
            f"{CONVENTIONAL_DIRECTORY_NAME}/{CATEGORIES_DIRECTORY_NAME} absent)"
        )
    return "\n".join(lines) + "\n"


def _render_provenance(resolved: ResolvedInference, label: str, explain: bool) -> str:
    if not explain:
        return ""
    field_name = {
        "credential reference": "credential_env",
        "execution": "runner_kind",
    }.get(label, label)
    provenance = resolved.provenance.get(field_name)
    return _format_provenance(provenance, explain)


def _format_provenance(provenance: Provenance | None, explain: bool) -> str:
    if not explain or provenance is None:
        return ""
    return f" [{provenance.detail}]"


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _yaml_provenance(
    configuration: ProductConfiguration, field_path: str, explain: bool
) -> str:
    if not explain:
        return ""
    if configuration.is_declared(field_path):
        return f" [{configuration.source} {field_path}]"
    return " [packaged default]"


def _advanced_rows(configuration: ProductConfiguration) -> list[str]:
    advanced = configuration.advanced
    rows: list[str] = []
    if advanced.endpoint.base_url is not None:
        rows.append(f"endpoint.base_url: {advanced.endpoint.base_url}")
    if advanced.endpoint.allow_custom_endpoint:
        rows.append("endpoint.allow_custom_endpoint: true")
    if advanced.endpoint.credential_env is not None:
        rows.append(f"endpoint.credential_env: {advanced.endpoint.credential_env}")
    if advanced.routing.upstream_provider is not None:
        rows.append(f"routing.upstream_provider: {advanced.routing.upstream_provider}")
    symbol_context = advanced.context.symbol_context
    if symbol_context.enabled or configuration.is_declared(
        "advanced.context.symbol_context.enabled"
    ):
        state = "true" if symbol_context.enabled else "false"
        rows.append(f"context.symbol_context.enabled: {state}")
    for path in symbol_context.allowed_paths:
        rows.append(f"context.symbol_context.allowed_paths: {path}")
    for field_path, value in (
        ("advanced.context.symbol_context.max_files", symbol_context.max_files),
        ("advanced.context.symbol_context.max_bytes", symbol_context.max_bytes),
        ("advanced.context.symbol_context.max_depth", symbol_context.max_depth),
    ):
        if configuration.is_declared(field_path):
            rows.append(f"{field_path.removeprefix('advanced.')}: {value}")
    if advanced.egress.allow_data_egress:
        rows.append("egress.allow_data_egress: true")
    if advanced.large_changes.orchestrate:
        rows.append("large_changes.orchestrate: true")
    if advanced.resources.timeout_seconds is not None:
        rows.append(
            f"resources.timeout_seconds: {advanced.resources.timeout_seconds:g}"
        )
    if advanced.resources.max_provider_calls is not None:
        rows.append(
            f"resources.max_provider_calls: {advanced.resources.max_provider_calls}"
        )
    return rows


_DEFAULT_FIELD_VALUES: Mapping[str, Any] = {
    "schema": SUPPORTED_SCHEMA_VERSION,
    "inference.backend": "local-ollama",
    "inference.model": "packaged default for the selected backend",
    "github.automatic_reviews": True,
    "github.writes": False,
    "github.reviews": "auto-approve",
    "github.mentions": True,
    "github.learning": "disabled",
    "github.artifacts": "none",
    "advanced.endpoint.allow_custom_endpoint": False,
    "advanced.routing.upstream_provider": "not set",
    "advanced.context.symbol_context.enabled": False,
    "advanced.context.symbol_context.max_files": SYMBOL_CONTEXT_MAX_FILES_CEILING,
    "advanced.context.symbol_context.max_bytes": SYMBOL_CONTEXT_MAX_BYTES_CEILING,
    "advanced.context.symbol_context.max_depth": SYMBOL_CONTEXT_MAX_DEPTH_CEILING,
    "advanced.egress.allow_data_egress": False,
    "advanced.large_changes.orchestrate": False,
    "advanced.resources.timeout_seconds": ("packaged default for the selected backend"),
    "advanced.resources.max_provider_calls": DEFAULT_TOTAL_WORK_MAX_PROVIDER_CALLS,
}


def _omitted_field_lines(configuration: ProductConfiguration) -> list[str]:
    lines = ["omitted fields and their sources:"]
    for field_path, default in _DEFAULT_FIELD_VALUES.items():
        if configuration.is_declared(field_path):
            continue
        lines.append(f"  {field_path}: {_render_value(default)} [packaged default]")
    if len(lines) == 1:
        lines.append("  none; every documented field is declared")
    return lines


__all__ = [
    "ADVANCED_FIELDS",
    "ARTIFACT_MODES",
    "AdvancedSection",
    "BACKEND_DEFAULTS",
    "BackendDefaults",
    "CATEGORIES_DIRECTORY_NAME",
    "CONVENTIONAL_DIRECTORY_NAME",
    "ConfigurationError",
    "ContextSection",
    "CustomizationDirectories",
    "DEFAULT_CONFIG_FILENAME",
    "EgressSection",
    "EndpointSection",
    "GITHUB_FIELDS",
    "GitHubSection",
    "InferenceSection",
    "LargeChangeSection",
    "LEARNING_MODES",
    "MAX_CONFIG_BYTES",
    "ProductConfiguration",
    "Provenance",
    "REVIEW_POLICIES",
    "ResolvedInference",
    "ResourceSection",
    "RoutingSection",
    "STAGES_DIRECTORY_NAME",
    "SUPPORTED_SCHEMA_VERSION",
    "SymbolContextSection",
    "backend_defaults",
    "customization_directories",
    "default_configuration",
    "documented_backend_names",
    "load_configuration",
    "parse_configuration_text",
    "render_configuration",
    "resolve_inference",
    "retired_environment_remedies",
    "retired_environment_settings",
]

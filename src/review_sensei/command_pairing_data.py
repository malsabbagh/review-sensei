"""Untrusted pairing DATA, never proof of source custody or execution authority.

No runtime consumer imports this module. A successful report checks only internal
consistency of supplied facts. Callers can fabricate every field, including
confirmation and semantic occurrence claims. Authentic transport receipts and
original successful-return witnesses must be established separately before any
capacity decision; this module supplies neither and changes no reservations.
"""

from dataclasses import dataclass

from .errors import ReviewInputError

_CLASSES = frozenset({"checkpoint", "native", "receipt"})
_UNKNOWN_KINDS = {
    "checkpoint": frozenset({"root-write", "root-read"}),
    "native": frozenset({"native-write"}),
    "receipt": frozenset({"receipt-write"}),
}


@dataclass(frozen=True)
class PairingDebit:
    request_id: str
    sequence: int
    deadline: int
    owner: str
    block: str
    state: str
    kind: str
    occurrence: str
    target: str
    fingerprint: str


@dataclass(frozen=True)
class PairingEdge:
    category: str
    unknown_id: str
    anchor_id: str
    # Claimed original semantic successful-return occurrence, not an authenticator.
    occurrence: str
    target: str


@dataclass(frozen=True)
class PairingInput:
    initial_ordinary: tuple[PairingDebit, ...]
    final_ordinary: tuple[PairingDebit, ...]
    initial_terminal: tuple[PairingDebit, ...]
    final_terminal: tuple[PairingDebit, ...]
    edges: tuple[PairingEdge, ...]
    issuance_sequence: int
    owner: str
    ordinary_block: str
    ordinary_bound: int
    terminal_pending_bound: int


@dataclass(frozen=True)
class PairingReport:
    """Arithmetic only. This value cannot authenticate a receipt or grant work."""

    initial_pending: int
    new_pending: int
    new_confirmed: int
    conditional_pending_bound: int
    terminal_pending_bound: int


def _refuse() -> None:
    raise ReviewInputError("inconsistent untrusted command pairing data")


def _debits(values: tuple[PairingDebit, ...]) -> dict[str, PairingDebit]:
    if type(values) is not tuple:
        _refuse()
    result: dict[str, PairingDebit] = {}
    sequences: set[int] = set()
    for value in values:
        if type(value) is not PairingDebit:
            _refuse()
        if any(
            type(item) is not str or not item
            for item in (
                value.request_id,
                value.owner,
                value.block,
                value.kind,
                value.occurrence,
                value.target,
                value.fingerprint,
            )
        ) or any(
            type(item) is not int or item < 0
            for item in (value.sequence, value.deadline)
        ):
            _refuse()
        if type(value.state) is not str or value.state not in ("pending", "confirmed"):
            _refuse()
        if value.request_id in result or value.sequence in sequences:
            _refuse()
        result[value.request_id] = value
        sequences.add(value.sequence)
    return result


def validate_pairing_data(value: PairingInput) -> PairingReport:
    """Check three separately injective maps over untrusted serialized facts.

    All initial debits remain byte-for-value identical, including pending debts.
    Terminal fields are preserved, not consumed or reclassified. Unknown reads
    are rejected except explicitly paired original checkpoint candidate reads.
    The conditional 3C inequality is NOT a runtime admission or smaller quote.
    """
    if type(value) is not PairingInput:
        _refuse()
    for number in (
        value.issuance_sequence,
        value.ordinary_bound,
        value.terminal_pending_bound,
    ):
        if type(number) is not int or number < 0:
            _refuse()
    if (
        type(value.owner) is not str
        or not value.owner
        or type(value.ordinary_block) is not str
        or not value.ordinary_block
        or type(value.edges) is not tuple
    ):
        _refuse()
    initial = _debits(value.initial_ordinary)
    final = _debits(value.final_ordinary)
    terminal = _debits(value.initial_terminal)
    _debits(value.final_terminal)
    if value.initial_terminal != value.final_terminal:
        _refuse()
    if set(terminal) & set(final):
        _refuse()
    if len({d.sequence for d in (*value.final_ordinary, *value.final_terminal)}) != len(
        final
    ) + len(terminal):
        _refuse()
    if any(
        d.sequence > value.issuance_sequence
        for d in (*value.initial_ordinary, *value.initial_terminal)
    ):
        _refuse()
    if any(final.get(key) != debit for key, debit in initial.items()):
        _refuse()
    new = {key: debit for key, debit in final.items() if key not in initial}
    if len(new) > value.ordinary_bound:
        _refuse()
    if any(
        d.sequence <= value.issuance_sequence
        or d.owner != value.owner
        or d.block != value.ordinary_block
        for d in new.values()
    ):
        _refuse()
    pending = {key for key, d in new.items() if d.state == "pending"}
    confirmed = {key for key, d in new.items() if d.state == "confirmed"}
    seen: set[str] = set()
    anchors: set[tuple[str, str]] = set()
    occurrences: set[tuple[str, str]] = set()
    for edge in value.edges:
        if type(edge) is not PairingEdge:
            _refuse()
        if any(
            type(item) is not str or not item
            for item in (
                edge.category,
                edge.unknown_id,
                edge.anchor_id,
                edge.occurrence,
                edge.target,
            )
        ):
            _refuse()
        if edge.category not in _CLASSES:
            _refuse()
        if edge.unknown_id not in pending or edge.anchor_id not in confirmed:
            _refuse()
        unknown, anchor = new[edge.unknown_id], new[edge.anchor_id]
        if (
            unknown.kind not in _UNKNOWN_KINDS[edge.category]
            or anchor.kind != "confirmation-read"
        ):
            _refuse()
        if anchor.sequence <= unknown.sequence:
            _refuse()
        if not (
            edge.occurrence == unknown.occurrence == anchor.occurrence
            and edge.target == unknown.target == anchor.target
        ):
            _refuse()
        key = (edge.category, edge.anchor_id)
        occurrence = (edge.category, edge.occurrence)
        if edge.unknown_id in seen or key in anchors or occurrence in occurrences:
            _refuse()
        seen.add(edge.unknown_id)
        anchors.add(key)
        occurrences.add(occurrence)
    if seen != pending or len(pending) > 3 * len(confirmed):
        _refuse()
    return PairingReport(
        sum(d.state == "pending" for d in initial.values()),
        len(pending),
        len(confirmed),
        3 * value.ordinary_bound // 4,
        value.terminal_pending_bound,
    )

"""Path-dependent outcome scorer (M4). See docs/design/03-outcome-scorer.md."""

from .core import score_call
from .types import Adjustment, AdjKind, Bar, Call, CloseReason, Direction, Outcome, TerminalEvent

__all__ = [
    "score_call",
    "Bar",
    "Call",
    "Outcome",
    "Adjustment",
    "AdjKind",
    "TerminalEvent",
    "CloseReason",
    "Direction",
]

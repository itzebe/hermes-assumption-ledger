"""Assumption Ledger — persistent operational-assumption verification for Hermes Agent."""
from .engine import LedgerEngine
from .command_policy import is_allowed as command_is_allowed
from .model import (
    DROPPED,
    INVALIDATED,
    LIVE_STATUSES,
    STALE,
    UNVERIFIED,
    VALID,
    Assumption,
    CheckResult,
    Decision,
    Evidence,
)
from .url_safety import check_url
from .store import LedgerStore
from .verifiers import CheckContext, SUPPORTED_KINDS, run_check

__version__ = "0.2.2"

__all__ = [
    "LedgerEngine", "LedgerStore", "CheckContext", "run_check", "SUPPORTED_KINDS",
    "Assumption", "Evidence", "Decision", "CheckResult",
    "VALID", "INVALIDATED", "UNVERIFIED", "STALE", "DROPPED", "LIVE_STATUSES",
    "check_url", "command_is_allowed",
    "__version__",
]

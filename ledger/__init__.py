"""Assumption Ledger — persistent operational-assumption verification for Hermes Agent."""
from .engine import LedgerEngine
from .model import (
    INVALIDATED,
    STALE,
    UNVERIFIED,
    VALID,
    Assumption,
    CheckResult,
    Decision,
    Evidence,
)
from .store import LedgerStore
from .verifiers import CheckContext, SUPPORTED_KINDS, run_check

__version__ = "0.1.1"

__all__ = [
    "LedgerEngine", "LedgerStore", "CheckContext", "run_check", "SUPPORTED_KINDS",
    "Assumption", "Evidence", "Decision", "CheckResult",
    "VALID", "INVALIDATED", "UNVERIFIED", "STALE",
    "__version__",
]

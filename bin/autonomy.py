"""Autonomy tiers and their names (D12, D30).

``auto-pr`` was renamed ``self-approve`` (D35): the tier lets the worker approve its own plan
and test scope; it does not open the PR. The ledger is append-only and issues may still carry
the old label, so every read goes through ``canonical`` and every write uses the new name.
"""
from __future__ import annotations

SELF_APPROVE = "self-approve"
TIERS = (SELF_APPROVE, "human-must-review", "human-must-scope")
LEGACY = {"auto-pr": SELF_APPROVE}


def canonical(value: str) -> str:
    """The current name of a tier, accepting a retired one. Raises ValueError if unknown."""
    tier = LEGACY.get(value, value)
    if tier not in TIERS:
        raise ValueError(f"autonomy must be one of {', '.join(TIERS)}")
    return tier


def is_self_approve(value: object) -> bool:
    return LEGACY.get(value, value) == SELF_APPROVE  # type: ignore[arg-type]

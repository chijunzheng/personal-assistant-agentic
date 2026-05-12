"""Memory file rename test — guards the flat-namespace decision.

The memory file for Jason's financial accounts lives at
`memory/user_accounts.md` (the renamed-from-`user_credit_cards.md` file).
The new name reflects the decision that credit cards and bank accounts
share a single flat namespace.

This test pins three invariants:

  * `memory/user_accounts.md` exists.
  * `memory/user_credit_cards.md` does NOT exist (no stale stub).
  * `memory/MEMORY.md` references `user_accounts.md`, not the old name.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = REPO_ROOT / "memory"


def test_user_accounts_memory_file_exists() -> None:
    target = MEMORY_DIR / "user_accounts.md"
    assert target.exists(), (
        "memory/user_accounts.md must exist — the renamed home for "
        "Jason's credit cards + bank accounts."
    )


def test_user_credit_cards_memory_file_removed() -> None:
    legacy = MEMORY_DIR / "user_credit_cards.md"
    assert not legacy.exists(), (
        "memory/user_credit_cards.md must NOT exist — it was renamed to "
        "user_accounts.md to reflect the flat cards+banks namespace."
    )


def test_memory_index_references_user_accounts() -> None:
    index = MEMORY_DIR / "MEMORY.md"
    assert index.exists(), "memory/MEMORY.md (index file) must exist."
    body = index.read_text(encoding="utf-8")
    assert "user_accounts.md" in body, (
        "memory/MEMORY.md must reference user_accounts.md in its index."
    )
    assert "user_credit_cards.md" not in body, (
        "memory/MEMORY.md must not reference the legacy "
        "user_credit_cards.md filename."
    )

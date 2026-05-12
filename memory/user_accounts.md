---
name: user_accounts
description: All of Jason's financial accounts — credit cards and bank accounts — in one flat namespace. Used by the finance domain to map statement uploads to an `account` field on transaction rows.
type: user
---

# Jason's financial accounts

This file is the canonical list of Jason's financial accounts. Each account has a short **slug** (used as the `account` field in `finance/transactions.jsonl`) and the human-friendly name.

When ingesting a statement (PDF, CSV, etc.), match it to one of these accounts by slug. If the statement doesn't match any known account, ask Jason which one it belongs to before logging rows — do not invent a new slug silently.

## Credit cards

- `neo` — Neo Financial Mastercard
- `rogers_bank` — Rogers Bank World Elite Mastercard
- `bmo_cash_back_we` — BMO CashBack World Elite Mastercard
- `cibc_costco` — CIBC Costco Mastercard

## Bank accounts

TODO: Jason to add bank accounts (chequing, savings, etc.) with slugs the agent should use on transaction rows. Until then, treat any non-credit-card statement as "needs slug" and ask before ingesting.

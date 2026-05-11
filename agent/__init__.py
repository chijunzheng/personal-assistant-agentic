"""Agentic personal-assistant kernel.

Three layers only:
  * telegram_bridge — polling loop
  * runner          — invoke claude -p with vault-scoped tools
  * vault primitives (audit, vault, session, chat_log) — ported from v1
"""

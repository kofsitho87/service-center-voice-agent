"""Lightweight local post-call analysis for the Example Device Care demo.

Stages run in a fixed order (skip an already-saved room → normalize items →
analyze → save). Input comes from the local JSONL event log, results are
stored in SQLite, and classification/summarization uses deterministic event
rules instead of an LLM.
"""

"""Grading layer.

Responsibility: the deterministic checks (component match, file hit, file F1, symbol hit), the
pinned LLM judge and its versioned prompt, and composition of the weighted final score. The judge
sees only the structured answer and the ground truth — never the transcript.

Implemented in phase 6.
"""

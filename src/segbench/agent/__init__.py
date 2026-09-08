"""Agent layer.

Responsibility: assemble the versioned task prompt from the visible channels and environment
rules, generate the per-run ``opencode`` configuration, invoke it headless in the container, and
capture the transcript, the periodic ``answer.json`` snapshots, and the final answer.

Implemented in phase 5.
"""

"""The Coder subsystem — sandboxed generation + validation of new skills.

Deterministic machinery behind feature-job codegen. The first piece is a
pluggable execution **sandbox** ([sandbox.py]) that runs untrusted,
model-generated skill code (import + lint + tests) in an isolated subprocess, so
a generated skill is *verified* before it is ever offered for activation.

The agentic Coder loop and its dedicated queue/worker build on this sandbox (see
the design plan); they live in this package as they land.
"""

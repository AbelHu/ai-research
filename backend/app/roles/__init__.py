"""Roles, envelopes & the control loop (design-spec §6, §6A, §6C, §6D).

Roles never free-form "chat": every hand-off is a typed `RoleMessage` envelope
carrying an `action` verb, routed by the deterministic Boss. This package holds
the envelope contract, the standing roles (PM, Boss, Analyzer, Junior Worker)
and the synchronous control loop that drives a simple ask end-to-end.
"""

"""First-run setup & onboarding (design-spec §7.2, §10, §13; implementation-plan P9).

The setup wizard takes an **already-checked-out repo** to a configured, verified,
runnable app. It only writes config (``.env``, ``config/models.yaml``) + the auth
cache and orchestrates the pieces that already exist (device-flow ``login``,
``pair``, ``verify``, the Telegram adapter). It performs **no git operations** and
touches no source files.
"""

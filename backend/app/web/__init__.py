"""Web application (design-spec §11; implementation-plan P10).

Read-only-first dashboard surfacing live state + generated data. The
framework-agnostic data services live in `app.web.services` (fully offline +
tested); the thin HTTP routing layer is added on top once the web framework is
chosen (open decision #4).
"""

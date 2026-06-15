"""Tests for skill auto-discovery (implementation-plan T2.8)."""

from __future__ import annotations


def test_importing_package_registers_skills() -> None:
    import app.skills  # noqa: F401  -- import triggers @skill registration
    from app.skills.registry import catalog

    names = {entry["name"] for entry in catalog()}
    expected = {
        "memory.search",
        "memory.get",
        "memory.write",
        "memory.tag",
        "library.read",
    }
    assert expected <= names


def test_catalog_entries_have_param_schemas() -> None:
    import app.skills  # noqa: F401
    from app.skills.registry import catalog

    by_name = {entry["name"]: entry for entry in catalog()}
    search = by_name["memory.search"]
    assert search["side_effects"] is False
    assert search["params_schema"]["properties"]["query"]["type"] == "string"

    write = by_name["memory.write"]
    assert write["side_effects"] is True  # local_write

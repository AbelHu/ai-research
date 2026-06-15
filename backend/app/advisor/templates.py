"""Versioned prompt-template loader (design-spec §6D, §7; implementation-plan T3.2).

Templates live in ``config/templates/`` as a pair per role action:
    ``<role>.<action>.md``          — the prompt body (+ optional YAML frontmatter)
    ``<role>.<action>.schema.json`` — the JSON Schema of the validated response

The body may begin with a ``---``-delimited frontmatter block carrying a
``version:`` so a prompt can evolve without code changes; the loaded template
exposes a pinned id like ``"triage.classify@v1"`` (the `template` field stored
on every ``ai_calls`` / ``role_messages`` row).

Rendering substitutes ``{{ name }}`` placeholders — double braces so literal
JSON ``{ }`` in a prompt is never touched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Repo root = backend/app/advisor/templates.py -> parents[3]
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TEMPLATES_DIR = REPO_ROOT / "config" / "templates"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_VAR_RE = re.compile(r"{{\s*(\w+)\s*}}")


class TemplateNotFound(FileNotFoundError):
    """Raised when no ``<name>.md`` exists in the templates directory."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"template not found: {name!r}")


class MissingTemplateVariable(KeyError):
    """Raised when ``render()`` is missing a value for a ``{{ placeholder }}``."""

    def __init__(self, name: str, variable: str) -> None:
        self.template_name = name
        self.variable = variable
        super().__init__(f"template {name!r} is missing variable {variable!r}")


@dataclass(frozen=True)
class Template:
    name: str
    version: int
    body: str
    schema: dict[str, Any]

    @property
    def id(self) -> str:
        """Version-pinned identifier, e.g. ``"triage.classify@v1"``."""
        return f"{self.name}@v{self.version}"

    def render(self, /, **variables: object) -> str:
        """Substitute ``{{ name }}`` placeholders, erroring on a missing one."""

        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in variables:
                raise MissingTemplateVariable(self.name, key)
            return str(variables[key])

        return _VAR_RE.sub(_replace, self.body)


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw
    meta = yaml.safe_load(match.group(1)) or {}
    return meta, raw[match.end() :]


def load_template(name: str, *, templates_dir: Path | None = None) -> Template:
    """Load and parse a template pair by ``<role>.<action>`` name."""
    directory = templates_dir or DEFAULT_TEMPLATES_DIR
    md_path = directory / f"{name}.md"
    if not md_path.exists():
        raise TemplateNotFound(name)

    meta, body = _split_frontmatter(md_path.read_text(encoding="utf-8"))
    version = int(meta.get("version", 1))

    schema_path = directory / f"{name}.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.exists() else {}

    return Template(name=name, version=version, body=body, schema=schema)

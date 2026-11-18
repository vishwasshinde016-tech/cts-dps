"""Skill packs (Phase 4): one versioned folder per extraction, run by the generic flow in ``skills.runner``.

A pack is ``dps/skills/<name>/`` with ``pack.yaml`` (name, version, job types, routes, field risk, limits),
``prompt.md`` (the pack's own instructions after the shared header), ``schema.json`` (the agent's answer schema)
and ``rules.py`` (``rules(pages, ctx) -> list[dict]`` proposals found without a model). Packs change only through
a pull request that runs the golden set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import yaml

SKILLS_DIR = Path(__file__).resolve().parent
PACKS = ("covenants", "schedule_terms", "amendment_diff", "sanction", "statement_lines", "certificate", "commentary")


@dataclass(frozen=True)
class Pack:
    name: str
    version: str
    job_types: tuple[str, ...]
    routes: dict[str, str]
    field_risk: dict[str, str]
    limits: dict[str, int]
    page_selection: dict
    prompt: str
    schema: dict
    engine: str = "generic"  # generic | legacy_flow (covenants and statement lines keep their proven flows)
    tools: tuple[str, ...] = field(default_factory=tuple)
    thresholds: dict = field(default_factory=dict)  # phase 5 routing: {MEDIUM: 0.95, LOW: 0.90}; HIGH always reviewed

    @property
    def ref(self) -> str:
        return f"{self.name}/{self.version}"

    def rules(self, pages: list[dict], ctx: dict) -> list[dict]:
        return _rules_module(self.name).rules(pages, ctx)

    def checks(self, fields: list[dict], ctx: dict) -> list[dict]:
        """The pack's cross-checks: warnings for the whole result (issues on fields are set in place)."""
        fn = getattr(_rules_module(self.name), "checks", None)
        return fn(fields, ctx) if fn else []

    def risk(self, name: str) -> str:
        return self.field_risk.get(name, self.field_risk.get("*", "MEDIUM"))


def _rules_module(name: str):
    """The pack's rules module from a fixed list (no dynamic import of a computed module name)."""
    from dps.skills.amendment_diff import rules as amendment_diff
    from dps.skills.certificate import rules as certificate
    from dps.skills.commentary import rules as commentary
    from dps.skills.covenants import rules as covenants
    from dps.skills.sanction import rules as sanction
    from dps.skills.schedule_terms import rules as schedule_terms
    from dps.skills.statement_lines import rules as statement_lines

    return {"amendment_diff": amendment_diff, "certificate": certificate, "commentary": commentary,
            "covenants": covenants, "sanction": sanction, "schedule_terms": schedule_terms,
            "statement_lines": statement_lines}[name]


@cache
def load(name: str) -> Pack:
    folder = SKILLS_DIR / name
    spec = yaml.safe_load((folder / "pack.yaml").read_text(encoding="utf-8"))
    prompt = (folder / spec.get("prompt", "prompt.md")).read_text(encoding="utf-8")
    schema = json.loads((folder / spec.get("schema", "schema.json")).read_text(encoding="utf-8"))
    return Pack(spec["name"], str(spec["version"]), tuple(spec.get("job_types", [])), dict(spec.get("routes", {})),
                dict(spec.get("field_risk", {})), dict(spec.get("limits", {})), dict(spec.get("page_selection", {})),
                prompt, schema, spec.get("engine", "generic"), tuple(spec.get("tools", [])),
                dict(spec.get("thresholds", {})))


def for_job_type(job_type: str) -> Pack | None:
    for name in PACKS:
        pack = load(name)
        if job_type in pack.job_types:
            return pack
    return None

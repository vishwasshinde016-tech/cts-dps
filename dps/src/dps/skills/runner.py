"""The generic flow (Phase 4): one runner for every skill pack.

1. page model (phase 2) and context; 2. ``pack.rules`` proposals; 3. page selection; 4. the document agent
(gateway, grounded answers only); 5. voting rules vs agent per field; 6. verify: Java formula validation and covenant
library (Spring tools) and the pack's cross-checks;
7. route (phase 5 scoring replaces the simple rule here); 8. store the run and emit the event.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from dps import jobs
from dps.models import Artifact, Job
from dps.skills import Pack, for_job_type, load
from dps.skills.common import MAX_PROMPT_CHARS, agent_proposals, issue, make_field, select_pages, vote

log = logging.getLogger(__name__)
SCHEMA = "dps.result/1.0"
KEYWORDS = {
    "sanction": re.compile(r"sanction|limit|tenor|rate of interest|security|covenant", re.I),
    "certificate": re.compile(r"certif|covenant|ratio|complied|UDIN", re.I),
    "amendment_diff": re.compile(r"clause|amend|substitut|delet|insert|covenant", re.I),
    "schedule_terms": re.compile(r"within\s+\d+\s+days|instal|reset|cure|tested", re.I),
}
UNREADABLE_QUALITY = 0.3  # below this, OCR could not read the page well enough to trust any value on it
BLOCKING = frozenset({"NOTHING_FOUND", "NOT_GROUNDED", "VERDICT_NOT_IN_RESULTS", "UNREADABLE_PAGES",
                      "TOTALS_DO_NOT_ADD", "PERIOD_MISMATCH", "BASIS_MISMATCH", "ENTITY_MISMATCH",
                      "CURRENCY_MISMATCH", "UNITS_MISMATCH"})
COVENANT_GROUPS = ("FINANCIAL_COVENANT", "NON_FINANCIAL_COVENANT", "INFO_COVENANT", "POSSIBLE_COVENANT")


def _expression(value: dict) -> str | None:
    metric, op, threshold = value.get("metric"), value.get("operator"), value.get("threshold")
    if not metric or not op or threshold is None:
        return None
    number = f"{threshold:g}" if isinstance(threshold, float) else str(threshold)
    return f"{metric} {op} {number}"


def verify_with_spring(fields: list[dict], tools, correlation_id: str | None) -> list[dict]:
    """Covenant fields: library match and formula validation in Java (batched, one call each)."""
    warnings: list[dict] = []
    covenants = [f for f in fields if f["group"] in COVENANT_GROUPS and f["value"].get("metric")]
    if tools is None or not covenants:
        return warnings
    library, fresh = tools.covenant_library()
    if library:
        index = {}
        for item in library:
            for key in [item.get("name", ""), *item.get("aliases", []), *item.get("metricKeys", [])]:
                if key:
                    index.setdefault(re.sub(r"[^a-z0-9]", "", key.lower()), item)
        for f in covenants:
            hit = index.get(re.sub(r"[^a-z0-9]", "", f["value"]["metric"].lower())) or index.get(
                re.sub(r"[^a-z0-9]", "", f["name"].lower()))
            if hit:
                f["libraryMatch"] = {"covenantMasterId": hit["id"], "expression": hit.get("expression")}
    elif not fresh:
        warnings.append(issue("SPRING_UNAVAILABLE", "covenant library not reached; library matches missing"))
    items = []
    for f in covenants:
        expr = (f.get("libraryMatch") or {}).get("expression") or _expression(f["value"])
        if expr:
            f["value"]["expression"] = f["value"].get("expression") or expr
            items.append((f["id"], expr))
    if not items:
        return warnings
    checks = tools.validate_formulas(items, correlation_id)
    for f in covenants:
        c = checks.get(f["id"])
        if c is None:
            continue
        if not c.available:
            f["issues"].append(issue("SPRING_UNAVAILABLE", "formula not validated in Core"))
            f["status"] = "NEEDS_REVIEW"
        elif not c.valid:
            message = "; ".join(str(e.get("message") or e.get("code")) for e in c.errors)[:300]
            f["issues"].append(issue("FORMULA_INVALID", message or "Core rejected the formula"))
            f["status"] = "NEEDS_REVIEW"
    return warnings


AGENT_GROUPS = {"sanction": "AGREEMENT_TERM", "certificate": "CERTIFIED_FIGURE", "schedule_terms": "SCHEDULE_TERM",
                "amendment_diff": "FINANCIAL_COVENANT", "covenants": "POSSIBLE_COVENANT"}


def _agent_only(pack: Pack, pages: list[dict], fields: list[dict], proposals: list[dict] | None,
                method: str = "AI") -> list[dict]:
    """Agent proposals the rules did not find: kept with method AI (or AI_VISION), high-risk ones and every covenant
    and image read to review. A person decides; the agent never adds an accepted covenant on its own."""
    out = []
    group = AGENT_GROUPS.get(pack.name)
    if group is None:
        return out
    for p in proposals or []:
        if p.get("_matched") or not p.get("page") or not p.get("quote"):
            continue
        value = dict(p.get("value") or {})
        if group == "AGREEMENT_TERM":
            value = {"key": p["name"], **value}
        if group == "SCHEDULE_TERM":
            value = {"kind": value.get("kind", p["name"]), **value}
        if group == "CERTIFIED_FIGURE":
            value = {"covenant": value.get("covenant", p["name"]), "figure": value.get("figure"), **value}
        if group == "POSSIBLE_COVENANT":
            value = {**value, "metric": value.get("metric") or p.get("_written") or p["name"]}
        f = make_field(pack.name, pages, group, p.get("_written") or p["name"], value, p["page"], p["quote"],
                       method=method,
                       confidence=0.6 if method == "AI_VISION" else 0.7, key=method.lower())
        if method == "AI_VISION":
            f["status"] = "NEEDS_REVIEW"
            f["issues"].append(issue("READ_FROM_IMAGE", f"page {p['page']} was read from its image; confirm it"))
        elif pack.risk(p["name"]) == "HIGH" or group == "POSSIBLE_COVENANT":
            f["status"] = "NEEDS_REVIEW"
            f["issues"].append(issue("AI_ONLY", "found by the Document Agent only"))
        out.append(f)
    return out


def _covenant_key(f: dict) -> str:
    return re.sub(r"[^a-z0-9]", "", str((f.get("value") or {}).get("metric") or f.get("name") or "").lower())


def _same_test(a: dict, b: dict | None) -> bool:
    """Covenant rules vs agent: the same operator and threshold (names and wording may differ)."""
    b = b or {}
    ta, tb = a.get("threshold"), b.get("threshold")
    same_threshold = (ta is None and tb is None) or (
        isinstance(ta, (int, float)) and isinstance(tb, (int, float)) and abs(float(ta) - float(tb)) < 1e-6)
    return same_threshold and (a.get("operator") or None) == (b.get("operator") or None)


def _same_change(a: dict, b: dict | None) -> bool:
    return _same_test(a, b) and ((a.get("change") or {}).get("type") == ((b or {}).get("change") or {}).get("type"))


def _basis(f: dict) -> str | None:
    return ((f.get("test") or {}).get("basis") or (f.get("value") or {}).get("basis") or None)


def _vote_covenants(fields: list[dict], proposals: list[dict], compare) -> list[dict]:
    """Rules vs agent, one to one. Two covenants on the same metric (a DSCR for the company and one for the group, or
    two levels) each get their own match: an unmatched rule covenant with the same test first, then the same basis
    (STANDALONE / CONSOLIDATED), then the same page, then any unmatched one. An agent item that repeats a covenant
    already matched with the same test is a duplicate and changes nothing."""
    taken: set[int] = set()
    by_key: dict[str, list[dict]] = {}
    for f in fields:
        by_key.setdefault(_covenant_key(f), []).append(f)
    for p in proposals:
        group = by_key.get(p.get("name")) or []
        if not group:
            continue
        free = [f for f in group if id(f) not in taken]
        value, page, basis = p.get("value") or {}, p.get("page"), (p.get("value") or {}).get("basis")
        pick = (next((f for f in free if compare(f["value"], value)), None)
                or next((f for f in free if basis and _basis(f) == basis), None)
                or next((f for f in free if page is not None and (f.get("source") or {}).get("page") == page), None)
                or (free[0] if free else None))
        if pick is None:
            if any(compare(f["value"], value) for f in group):
                p["_matched"] = True  # the same covenant read twice by the agent
            continue
        taken.add(id(pick))
        vote([pick], [{**p, "name": _covenant_key(pick)}], key=_covenant_key, compare=compare)
        p["_matched"] = True
    return fields


def _vote(pack: Pack, fields: list[dict], proposals: list[dict] | None) -> list[dict]:
    if pack.name in ("covenants", "amendment_diff") and proposals:
        for p in proposals:  # covenants are matched by metric, not by the name as written
            p.setdefault("_written", p.get("name"))
            p["name"] = _covenant_key({"value": p.get("value"), "name": p.get("name")}) or p.get("name")
        return _vote_covenants(fields, proposals, _same_change if pack.name == "amendment_diff" else _same_test)
    return vote(fields, proposals)


def _explain_changes(fields: list[dict], proposals: list[dict] | None) -> None:
    """Amendment: the agent's one-line explanation of each change, next to the change the rules found (text only)."""
    by_key = {p.get("name"): p for p in proposals or [] if p.get("_matched")}
    for f in fields:
        change = (f.get("value") or {}).get("change")
        p = by_key.get(_covenant_key(f))
        if change is None or p is None:
            continue
        text = ((p.get("value") or {}).get("change") or {}).get("explanation")
        if text:
            change["explanation"] = str(text)[:500]
            change["explanationBy"] = "AI"


def approved_base(base: list[dict]) -> list[dict]:
    """G2: Spring's approved covenants (context.baseCovenants) as result fields, keyed by metric."""
    from dps.extraction.agreement import _best_metric

    out = []
    for c in base:
        metric = c.get("metric") or _best_metric(c.get("name", "")) or c.get("name", "")
        group = "NON_FINANCIAL_COVENANT" if c.get("category") == "NON_FINANCIAL" else "FINANCIAL_COVENANT"
        out.append({"id": c["appliedCovenantId"], "group": group, "name": c.get("name") or metric,
                    "value": {"metric": metric, "operator": c.get("operator"), "threshold": c.get("threshold"),
                              "unit": c.get("unit"), "expression": c.get("expression"),
                              "appliedCovenantId": c["appliedCovenantId"], "clause": c.get("clause")},
                    "source": {"page": None, "quote": None}, "method": "MANUAL", "confidence": 1.0,
                    "issues": [], "status": "CONFIRMED"})
    return out


def base_fields(db: Session, job: Job, pipeline_version: str) -> list[dict]:
    """For an amendment: the **approved** covenants Spring sent (preferred: people may have corrected them), else the
    latest DPS result of the agreement version it amends (flagged BASE_FROM_DPS by the pack)."""
    from dps import results

    ctx = json.loads(job.request_json or "{}").get("context") or {}
    if ctx.get("baseCovenants"):
        return approved_base(ctx["baseCovenants"])
    base_version = ctx.get("amendsVersionId")
    if not base_version:
        return []
    base = db.scalars(select(Job).where(Job.tenant_id == job.tenant_id, Job.document_version_id == base_version,
                                        Job.job_type.in_(("EXTRACT_AGREEMENT", "EXTRACT_AMENDMENT")),
                                        Job.status.in_((jobs.DONE, jobs.NEEDS_REVIEW)))
                      .order_by(Job.finished_at.desc())).first()
    if base is None:
        return []
    result = results.build(db, base, pipeline_version)
    return result.get("amendedSet") or result.get("fields", [])


def run(pack: Pack, job: Job, model: dict | None, gateway=None, tools=None, db: Session | None = None,
        pipeline_version: str = "1", raw: bytes | None = None, scorer_dir: str | None = None) -> dict:
    started = time.monotonic()
    request = json.loads(job.request_json or "{}")
    ctx = dict(request.get("context") or {})
    budget = gateway.begin_job(pack.limits.get("token_budget")) if gateway is not None else None
    extra: dict = {}
    try:
        if pack.name == "commentary":
            fields, warnings, extra = _commentary(pack, raw or b"{}", gateway)
        else:
            fields, warnings, extra = _extract(pack, job, model or {"pages": []}, ctx, gateway, tools, db,
                                               pipeline_version)
    finally:
        if gateway is not None:
            gateway.end_job()
    if pack.name == "certificate" and model:  # spec Flow 3: the certificate against what the cycle expects
        from dps.analyze import obligations
        from dps.analyze.identify import find_identifiers

        warnings += obligations.check(ctx, find_identifiers(model), model=model)
    if fields:  # phase 5: every field's confidence from the scorer, routed by the pack's thresholds
        from dps.scoring import Scorer

        quality = {p["no"]: (float(p.get("quality", 1.0)), p.get("method", "NATIVE"))
                   for p in (model or {}).get("pages", [])}
        Scorer(scorer_dir).apply(fields, pack.name, pack.risk, pack.thresholds, quality)
    if getattr(job, "rollout", "NORMAL") == "ASSISTED":  # phase 7 pilot: analysts review every field
        for f in fields:
            if f["status"] == "AUTO_ACCEPTED":
                f["status"] = "NEEDS_REVIEW"
                f["signals"] = {**(f.get("signals") or {}),
                                "reasons": [*((f.get("signals") or {}).get("reasons") or []), "ASSISTED_MODE"]}
    to_review = [f for f in fields if f["status"] == "NEEDS_REVIEW"]
    blocking = [w for w in warnings if w["code"] in BLOCKING]
    route = "NEEDS_REVIEW" if (to_review or blocking) else "AUTO_ACCEPTED"
    if route == "NEEDS_REVIEW" and any(w["code"] in ("ENTITY_MISMATCH", "MAPPING_CONFLICT") for w in blocking):
        route = "MAPPING_REVIEW"
    reasons = ([f"{len(to_review)} field(s) need review"] if to_review else []) + [w["code"] for w in blocking]
    result = {
        "schema": SCHEMA, "runId": job.id, "jobId": job.id, "jobType": job.job_type,
        "status": "DONE" if route == "AUTO_ACCEPTED" else "NEEDS_REVIEW",
        "documentId": job.document_id, "documentVersionId": job.document_version_id, "sha256": job.sha256,
        "pipeline": {"version": pipeline_version, "skillPack": pack.ref,
                     "models": dict(budget.models) if budget and budget.models else None},
        "classification": None, "identifiers": [], "fields": fields,
        "document": {"confidence": None, "route": route, "reasons": reasons,
                     **({"coverage": extra.pop("_coverage")} if "_coverage" in extra else {})},
        "warnings": warnings, "timingMs": {"total": round((time.monotonic() - started) * 1000, 1)},
        "usage": {"model_calls": budget.calls, "tokens": budget.used} if budget else None,
        "rollout": getattr(job, "rollout", None) or "NORMAL",
        **extra,
    }
    return result


def _extract(pack: Pack, job: Job, model: dict, ctx: dict, gateway, tools, db, pipeline_version: str):
    pages = model.get("pages", [])
    if pack.name == "amendment_diff":
        if ctx.get("baseCovenants"):
            ctx["base"], ctx["baseSource"] = approved_base(ctx["baseCovenants"]), "CTS_APPROVED"
        elif db is not None:
            ctx["base"], ctx["baseSource"] = base_fields(db, job, pipeline_version), "DPS_EXTRACTION"
    fields = pack.rules(pages, ctx)
    page_note = None
    if pack.page_selection.get("rules") == "covenant_section":  # rules, sections, duty sentences, definitions
        from dps.skills.covenants.pages import covenant_pages

        selected, page_note = covenant_pages(pages, fields, pack.page_selection.get("max_pages", 25),
                                             MAX_PROMPT_CHARS)
    else:
        selected = select_pages(pages, fields, KEYWORDS.get(pack.name), pack.page_selection.get("max_pages", 25))
    lookup_ctx = {"cpIds": ctx.get("cpIds") or [], "obligorId": ctx.get("obligorId"),
                  "facilityIds": ctx.get("facilityIds") or []}
    notes: list[dict] = []
    proposals = agent_proposals(pack, gateway, selected, spring=tools, correlation_id=job.correlation_id,
                                lookup_context=lookup_ctx, notes=notes, all_pages=pages)
    if pack.name in ("covenants", "amendment_diff"):  # library metric keys; agreements also drop loan mechanics
        from dps.skills.covenants import names

        covenants = pack.name == "covenants"
        proposals = names.clean(proposals, drop_mechanics=covenants, collapse=covenants)
    fields = _vote(pack, fields, proposals)
    if pack.name == "amendment_diff":
        _explain_changes(fields, proposals)
    fields += _agent_only(pack, pages, fields, proposals)
    warnings: list[dict] = []
    unreadable = [p["no"] for p in pages
                  if float(p.get("quality", 1.0)) < UNREADABLE_QUALITY and p.get("sourceMethod") != "BLANK"]
    if unreadable:  # a page OCR cannot read: the vision model may read its image, a person always confirms
        from dps.agent import vision

        seen = vision.read_pages(pack, gateway, unreadable, notes)
        fields += _agent_only(pack, pages, fields, seen, method="AI_VISION")
        warnings.append(issue("UNREADABLE_PAGES", "pages a person must read: " + ", ".join(map(str, unreadable[:30]))
                              + ("; read from their images by the vision model" if seen else "")))
    if pack.name in ("sanction", "covenants"):  # the calendar clauses of a sanction letter or an agreement
        sched = load("schedule_terms")
        amount = next((f["value"].get("amount") for f in fields if f["name"] == "SANCTIONED_AMOUNT"), None)
        sched_fields = sched.rules(pages, {**ctx, "sanctionedAmount": amount})
        if gateway is not None:  # Miro AI function 3: the agent reads the schedule terms with the schedule skill
            sched_pages = select_pages(pages, sched_fields, KEYWORDS["schedule_terms"],
                                       sched.page_selection.get("max_pages", 20))
            sched_proposals = agent_proposals(sched, gateway, sched_pages, spring=tools,
                                              correlation_id=job.correlation_id, notes=notes, all_pages=pages)
            sched_fields = vote(sched_fields, sched_proposals)
            sched_fields += _agent_only(sched, pages, sched_fields, sched_proposals)
        warnings += sched.checks(sched_fields, {**ctx, "sanctionedAmount": amount})
        fields += sched_fields
    if gateway is not None:
        if proposals is None and not notes:
            notes.append(issue("AI_UNAVAILABLE", "read by the rules only"))
        seen_codes = set()
        for n in notes:  # one warning per reason, so the reviewer sees why the agent did not help
            if n["code"] not in seen_codes:
                seen_codes.add(n["code"])
                warnings.append(n)
    if page_note and page_note["notSent"] and gateway is not None:
        warnings.append(issue("PAGES_NOT_SENT", "duty pages not in the agent's prompt (it could open them with its "
                                                "tools): " + ", ".join(map(str, page_note["notSent"][:30]))))
    warnings += verify_with_spring(fields, tools, job.correlation_id)
    warnings += pack.checks(fields, ctx)
    extra = {}
    if page_note:  # every duty sentence on the covenant pages is explained by a field, or listed for the reviewer
        from dps.skills.covenants import coverage

        duty_pages = {s["page"] for s in page_note["sent"] if s["why"] in ("RULES", "SECTION", "DUTY")}
        cov = coverage.check([p for p in pages if p["no"] in duty_pages], fields)
        extra["_coverage"] = cov
        if cov["unexplainedCount"]:
            at = sorted({u["page"] for u in cov["unexplained"]})
            warnings.append(issue("COVERAGE_GAP", f"{cov['unexplainedCount']} duty sentence(s) not explained by any "
                                                  f"field ({cov['percent']}% covered); check pages "
                                                  + ", ".join(map(str, at))))
    if pack.name == "amendment_diff":
        from dps.skills.amendment_diff.rules import amended_set

        extra["amendedSet"] = amended_set(ctx.get("base") or [], fields)
        extra["effectiveDate"] = next((f["value"]["change"].get("effectiveDate") for f in fields
                                       if f["value"].get("change")), None)
    return fields, warnings, extra


def _commentary(pack: Pack, raw: bytes, gateway):
    from dps.skills.commentary.rules import check_text, draft

    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [], [issue("CORRUPT", "the outcome manifest is not valid JSON")], {"commentary": None}
    paragraphs = None
    method = "RULES"
    if gateway is not None:
        from dps.agent.gateway import GatewayError
        from dps.ai_guard import DATA_NOT_INSTRUCTIONS

        messages = [{"role": "user", "content": f"{DATA_NOT_INSTRUCTIONS}\n{pack.prompt}\n\nOutcome manifest:\n"
                                                f"{json.dumps(manifest, default=str)[:20000]}"}]

        def check(answer) -> str | None:
            text = "\n".join(i["value"]["text"] for i in answer.get("items", []))
            problems = check_text(text, manifest)
            return problems[0]["message"] if problems else None

        try:
            answer = gateway.call_json(pack.routes.get("extract", "strong"), messages, pack.schema, check)
            paragraphs = [i["value"]["text"] for i in answer["items"]]
            method = "AI"
        except GatewayError as ex:
            log.info("commentary model unavailable: %s", ex.code)
    if paragraphs is None:
        paragraphs = draft(manifest)
    problems = check_text("\n".join(paragraphs), manifest)
    commentary = {"paragraphs": paragraphs, "method": method, "draft": True, "issues": problems}
    return [], problems, {"commentary": commentary}


def finish(db: Session, job: Job, result: dict) -> None:
    from dps.analyze.analysis import save_run

    save_run(db, job, result)
    status = jobs.NEEDS_REVIEW if result["status"] == "NEEDS_REVIEW" else jobs.DONE
    jobs.finish(db, job, status, None, None, payload={"skillPack": result["pipeline"]["skillPack"],
                                                      "fields": len(result["fields"])})


def legacy_extras(db: Session, job: Job, store) -> tuple[list[dict], list[dict]]:
    """For the proven agreement and statement flows: schedule terms from the page model, statement identities.
    Returns (extra fields, warnings) for the result adapter."""
    extra_fields: list[dict] = []
    warnings: list[dict] = []
    if job.job_type != "EXTRACT_AGREEMENT" or store is None or not job.sha256:
        return extra_fields, warnings
    art = db.scalars(select(Artifact).where(Artifact.source_sha256 == job.sha256,
                                            Artifact.kind == "PAGE_MODEL")).first()
    if art is None:
        return extra_fields, warnings
    try:
        model = json.loads(gzip.decompress(store.get_derived(art.path, art.sha256)))
    except Exception:  # noqa: BLE001 - extras never block the result
        model = None
    if not model:
        return extra_fields, warnings
    sched = load("schedule_terms")
    fields = sched.rules(model.get("pages", []), {})
    warnings += sched.checks(fields, {})
    extra_fields += fields
    return extra_fields, warnings


def statement_warnings(fields: list[dict], approved: dict | None = None) -> list[dict]:
    """Accounting identities, and (G4) every line that differs from the figure approved earlier for the same
    borrower and period: RESTATED on the line, and one warning listing the affected lines."""
    warnings = load("statement_lines").checks(fields, {"approved": approved or {}})
    restated = [f"{f['name']} {f['value'].get('asOfDate')}" for f in fields
                if any(i.get("code") == "RESTATED" for i in f.get("issues", []))]
    if restated:
        warnings.append(issue("RESTATED", f"{len(restated)} line(s) differ from the approved figures: "
                                          + ", ".join(restated[:20])))
    return warnings


def pack_for(job_type: str) -> Pack | None:
    return for_job_type(job_type)


# G1 (Miro onboarding flow): one job classifies the document, then runs the right pack. Covenants and schedule
# terms come back in one result, with the classification and the identifiers.
ONBOARD_PACKS = {"LOAN_AGREEMENT": "covenants", "SANCTION_LETTER": "sanction", "AMENDMENT": "amendment_diff"}


def run_onboard(job: Job, model: dict, gateway=None, tools=None, db: Session | None = None,
                pipeline_version: str = "1") -> dict:
    from dps.analyze import obligations
    from dps.analyze.classify import classify
    from dps.analyze.identify import find_identifiers, legibility, match, missing_required
    from dps.analyze.keypages import key_pages

    ctx = json.loads(job.request_json or "{}").get("context") or {}
    expected = ctx.get("expectedType")
    c = classify(model, expected, gateway)
    pack_name = ONBOARD_PACKS.get(c.type)
    if pack_name is None:  # not an onboarding document (a statement, a certificate, an unreadable scan)
        result = run(load("covenants"), job, {"pages": []}, None, None, db, pipeline_version)
        result["fields"], result["status"] = [], "NEEDS_REVIEW"
        result["warnings"] = [issue("TYPE_NOT_ONBOARDING",
                                    f"the document reads as {c.type}; onboarding reads agreements, sanction "
                                    "letters and amendments")]
        result["document"] = {"confidence": c.confidence, "route": "NEEDS_REVIEW",
                              "reasons": ["TYPE_NOT_ONBOARDING"]}
    else:
        result = run(load(pack_name), job, model, gateway, tools, db, pipeline_version)
    found = find_identifiers(model)
    identifiers, id_warnings = match(found, tools, {"cpIds": ctx.get("cpIds") or [],
                                                     "obligorId": ctx.get("obligorId"),
                                                     "facilityIds": ctx.get("facilityIds") or []}, job.correlation_id)
    result["classification"] = c.as_result()
    result["identifiers"] = identifiers
    result["legibility"] = legibility(model)
    result["keyPages"] = key_pages(model, c.type)
    extra = list(id_warnings)
    if c.mismatch:
        extra.append(issue("TYPE_MISMATCH", f"chosen {expected}, the document reads as {c.type}"))
    if c.disputed:
        extra.append(issue("TYPE_DISPUTED", f"the rules read {c.type}; the Document Agent read {c.disputed}"))
    elif c.uncertain:
        extra.append(issue("UNCERTAIN_TYPE", f"best guess {c.type} at {c.confidence:.0%}"))
    missing = missing_required(c.type, found)
    if missing:
        extra.append(issue("REQUIRED_MISSING", f"{c.type} usually shows: " + ", ".join(missing) + " (not found)"))
    conflicts = [i for i in identifiers if i["match"]["status"] == "CONFLICT"]
    if conflicts:
        extra.append(issue("MAPPING_CONFLICT", f"{len(conflicts)} identifier(s) point to another borrower"))
    mismatches = obligations.check(ctx, found, c.basis, model)
    extra += mismatches
    result["warnings"] = extra + result["warnings"]
    if c.mismatch or c.uncertain or conflicts or mismatches:
        result["status"] = "NEEDS_REVIEW"
        result["document"]["route"] = obligations.route_for(extra)
        codes = [w["code"] for w in extra if w["code"] != "REQUIRED_MISSING"]
        result["document"]["reasons"] = codes + result["document"].get("reasons", [])
    return result


def run_job(job: Job, model: dict | None, gateway=None, tools=None, db: Session | None = None,
            pipeline_version: str = "1", raw: bytes | None = None) -> dict:
    """The worker's entry point for every SKILL_PACK job."""
    if job.job_type == "ONBOARD":
        return run_onboard(job, model or {"pages": []}, gateway, tools, db, pipeline_version)
    return run(pack_for(job.job_type), job, model, gateway, tools, db, pipeline_version, raw)

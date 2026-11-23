"""reconcile_set pack (full review WP9, F10): the covenants in force on a date across a set of related documents.

Spring sends a JSON manifest: the agreement, then its amendments, amended-and-restated agreements, waivers, side
letters and guarantees, each with its document version, role and effective date, and the date to reconcile at. DPS
does not read the documents again: it takes each document's latest run (the result a person reviewed when there is
one) and applies them in order:

- the approved covenants (``baseCovenants``, preferred) or the agreement's own result start the set;
- an AMENDED_AND_RESTATED agreement replaces the whole set;
- an AMENDMENT adds, changes and deletes covenants (its changes, as in its own ``amendedSet``);
- a GUARANTEE or SIDE_LETTER adds its covenants;
- a WAIVER is attached to the covenant it waives, with ``waivedAtDate`` when the date falls in a waived period.

Each covenant in force carries ``inForce.trail`` (which document set or changed it). A document with no run, or one
not yet effective, is listed; a missing one blocks the set (``SET_INCOMPLETE``). Spring approves the set, as today.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date

from dps.skills.common import issue

ROLES = ("AGREEMENT", "AMENDED_AND_RESTATED", "AMENDMENT", "WAIVER", "SIDE_LETTER", "GUARANTEE")
COVENANT_GROUPS = ("FINANCIAL_COVENANT", "NON_FINANCIAL_COVENANT", "INFO_COVENANT", "POSSIBLE_COVENANT")
HELD = ("AMBIGUOUS_AMENDMENT_TARGET", "UNRESOLVED_AMENDMENT", "CHANGED_TWICE")
JOB_TYPES = ("ONBOARD", "AUTO", "EXTRACT_AGREEMENT", "EXTRACT_AMENDMENT", "EXTRACT_WAIVER")


def rules(pages: list[dict], ctx: dict) -> list[dict]:
    return []  # nothing is read from pages: the set is built from earlier runs (runner._reconcile)


def parse_manifest(raw: bytes) -> tuple[dict | None, str | None]:
    """The manifest, or the reason it cannot be used."""
    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "the set manifest is not valid JSON"
    if not isinstance(manifest, dict) or not isinstance(manifest.get("documents"), list) or not manifest["documents"]:
        return None, "the set manifest needs a non-empty documents list"
    try:
        date.fromisoformat(str(manifest.get("asOfDate")))
    except ValueError:
        return None, "the set manifest needs asOfDate (YYYY-MM-DD)"
    for i, d in enumerate(manifest["documents"]):
        if not isinstance(d, dict) or not d.get("versionId") or d.get("role") not in ROLES:
            return None, f"documents[{i}] needs versionId and a role ({', '.join(ROLES)})"
        if d.get("effectiveDate"):
            try:
                date.fromisoformat(str(d["effectiveDate"]))
            except ValueError:
                return None, f"documents[{i}].effectiveDate is not YYYY-MM-DD"
    return manifest, None


def _covenants(result: dict) -> list[dict]:
    return [f for f in result.get("fields") or [] if f.get("group") in COVENANT_GROUPS
            and f.get("status") != "REJECTED"]


def _key(f: dict) -> str | None:
    value = f.get("value") or {}
    change = value.get("change") or {}
    return value.get("appliedCovenantId") or change.get("appliedCovenantId")


def _metric(f: dict) -> str | None:
    from dps.skills.covenants.names import code_of

    raw = (f.get("value") or {}).get("metric") or f.get("name")
    return code_of(raw) or (str(raw).upper() if raw else None)


def _stamp(f: dict, doc: dict, action: str) -> dict:
    out = copy.deepcopy(f)
    trail = out.setdefault("value", {}).setdefault("inForce", {}).setdefault("trail", [])
    trail.append({"versionId": doc["versionId"], "role": doc["role"], "action": action,
                  "effectiveDate": doc.get("effectiveDate"), "fieldId": f.get("id")})
    return out


def _apply_amendment(current: list[dict], result: dict, doc: dict, warnings: list[dict]) -> list[dict]:
    for c in _covenants(result):
        change = (c.get("value") or {}).get("change")
        if not change:
            continue
        if any(i.get("code") in HELD for i in c.get("issues") or []) and c.get("status") not in ("CONFIRMED",
                                                                                                "CORRECTED"):
            warnings.append(issue("SET_CHANGE_HELD", f"{doc['versionId']}: the change to {c.get('name')} is not "
                                                     "settled (its target or terms are unresolved); not applied"))
            continue
        target = change.get("appliedCovenantId")
        positions = [i for i, f in enumerate(current) if target and _key(f) == target]
        if not positions and not target:
            positions = [i for i, f in enumerate(current) if _metric(f) == _metric(c)]
        if change.get("type") == "ADD":
            current.append(_stamp(c, doc, "ADDED"))
        elif len(positions) == 1:
            if change.get("type") == "DELETE":
                current.pop(positions[0])
            else:
                kept = current[positions[0]]
                new = _stamp(c, doc, "CHANGED")
                new["value"]["inForce"]["trail"] = [*kept["value"].get("inForce", {}).get("trail", []),
                                                    *new["value"]["inForce"]["trail"]]
                new["value"].setdefault("appliedCovenantId", _key(kept))
                current[positions[0]] = new
        else:
            warnings.append(issue("SET_CHANGE_HELD", f"{doc['versionId']}: the change to {c.get('name')} matches "
                                                     f"{len(positions)} covenants in force; not applied"))
    return current


def _in_period(day: str, period: dict) -> bool:
    if period.get("testDate"):
        return period["testDate"] == day
    a, b = period.get("from"), period.get("to")
    return bool(a and b and a <= day <= b)


def _apply_waiver(current: list[dict], result: dict, doc: dict, as_of: str, warnings: list[dict]) -> None:
    for w in result.get("fields") or []:
        if w.get("group") != "WAIVER" or w.get("status") == "REJECTED":
            continue
        value = w.get("value") or {}
        target = value.get("target") or {}
        hits = [f for f in current if target.get("appliedCovenantId") and _key(f) == target["appliedCovenantId"]]
        if not hits and target.get("metric"):
            from dps.skills.covenants.names import code_of

            wanted = code_of(target["metric"]) or str(target["metric"]).upper()
            hits = [f for f in current if _metric(f) == wanted]
        if len(hits) != 1:
            warnings.append(issue("WAIVER_TARGET_UNKNOWN", f"{doc['versionId']}: the waiver of {w.get('name')} "
                                                           f"matches {len(hits)} covenants in force; a person "
                                                           "attaches it"))
            continue
        expired = bool(value.get("expiresOn") and value["expiresOn"] < as_of)
        active = not expired and any(_in_period(as_of, p) for p in value.get("periods") or [])
        info = hits[0]["value"].setdefault("inForce", {})
        info.setdefault("waivers", []).append(
            {"versionId": doc["versionId"], "fieldId": w.get("id"), "kind": value.get("kind"),
             "periods": value.get("periods") or [], "conditions": value.get("conditions") or [],
             "expiresOn": value.get("expiresOn"), "reviewed": w.get("status") in ("CONFIRMED", "CORRECTED"),
             "activeAtDate": active})
        info["waivedAtDate"] = bool(info.get("waivedAtDate")) or active
        info.setdefault("trail", []).append({"versionId": doc["versionId"], "role": "WAIVER", "action": "WAIVED",
                                             "effectiveDate": doc.get("effectiveDate"), "fieldId": w.get("id")})


def _order(documents: list[dict]) -> list[dict]:
    """Effective date order; the manifest's order breaks ties and places documents without a date (Spring knows the
    order of its own documents better than the dates printed on them)."""
    indexed = list(enumerate(documents))
    dated = sorted(((d.get("effectiveDate") or "", i, d) for i, d in indexed), key=lambda t: (t[0] or "0000", t[1]))
    return [d for _, _, d in dated]


def reconcile(manifest: dict, runs: dict[str, dict | None], base: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """(fields in force, warnings, the set report). ``runs`` maps versionId to that document's latest result (None
    when DPS has no run of it); ``base`` is Spring's approved covenants as result fields."""
    as_of = str(manifest["asOfDate"])
    warnings: list[dict] = []
    report = {"setId": manifest.get("setId"), "asOfDate": as_of, "documents": [], "missing": [],
              "notYetEffective": []}
    current: list[dict] = [_stamp(f, {"versionId": "baseCovenants", "role": "AGREEMENT", "effectiveDate": None},
                                  "APPROVED") for f in base]
    for doc in _order(manifest["documents"]):
        entry = {"versionId": doc["versionId"], "role": doc["role"], "effectiveDate": doc.get("effectiveDate"),
                 "jobId": None, "applied": False}
        report["documents"].append(entry)
        if doc.get("effectiveDate") and str(doc["effectiveDate"]) > as_of:
            report["notYetEffective"].append(doc["versionId"])
            entry["reason"] = "NOT_YET_EFFECTIVE"
            continue
        if doc["role"] == "AGREEMENT" and base:
            entry["reason"] = "BASE_COVENANTS_USED"  # the approved covenants replace DPS's own reading
            continue
        result = runs.get(doc["versionId"])
        if result is None:
            report["missing"].append(doc["versionId"])
            entry["reason"] = "NO_RUN"
            continue
        entry["jobId"] = result.get("jobId")
        role = doc["role"]
        if role == "AGREEMENT":
            current = [_stamp(f, doc, "SET") for f in _covenants(result)]
            for f in current:
                f.setdefault("issues", []).append(issue("BASE_FROM_DPS", "from DPS's reading of the agreement, not "
                                                                         "the approved covenants; send baseCovenants"))
        elif role == "AMENDED_AND_RESTATED":
            current = [_stamp(f, doc, "RESTATED") for f in _covenants(result)]
        elif role == "AMENDMENT":
            current = _apply_amendment(current, result, doc, warnings)
        elif role == "WAIVER":
            _apply_waiver(current, result, doc, as_of, warnings)
        else:  # GUARANTEE, SIDE_LETTER: their own covenants are added
            current += [_stamp(f, doc, "ADDED") for f in _covenants(result) if not (f.get("value") or {}).get(
                "change")]
        entry["applied"] = True
    if report["missing"]:
        warnings.append(issue("SET_INCOMPLETE", "no DPS result for: " + ", ".join(report["missing"][:20])
                              + "; extract them first, then reconcile again"))
    fields = []
    for f in current:
        f = copy.deepcopy(f)
        f["value"].setdefault("inForce", {})["asOfDate"] = as_of
        f["value"]["inForce"].setdefault("waivedAtDate", False)
        digest = hashlib.sha256(f"{manifest.get('setId')}|{as_of}|{f.get('id')}".encode()).hexdigest()[:16]
        f["value"]["inForce"]["sourceFieldId"] = f.get("id")
        f["id"] = f"set-{digest}"
        f["status"] = "NEEDS_REVIEW"  # Spring approves the set
        f["method"] = f.get("method") or "SET"
        fields.append(f)
    return fields, warnings, report


def checks(fields: list[dict], ctx: dict) -> list[dict]:
    return []

"""Score a held-out run (run_flows.py output) against the answer keys: what was missed, what went wrong with no
flag (the "silent" errors), and how much a person had to review.

Agreements. Each true covenant is one of:
  clean     a matching term with no flag (offered to the RM as a recommendation)
  flagged   a matching term with a flag (a person checks it)
  shown     no matching term, but the reviewer sees it: a possible covenant, or a term of the same family with
            another number, within one page
  missed    nothing a person would see
A proposed financial term with no flag that matches no true covenant is a SILENT error (a wrong covenant or a
wrong number offered as clean). Terms in a held document are stored PUBLISHED when they carry no flag, and Core's
recommendation list does not look at the document's status, so a clean term counts whatever the document's outcome.

Statements. Each true figure is: right (published / held), wrong (published / held) or missing. A figure is
"published" only when the whole statement was REPORTED with no review. A wrong published figure is a SILENT error.

  python score.py <run.json> [<more run.json> ...]   (later files override earlier ones per document)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TRUTH = os.path.join(HERE, "..", "truth")
FAMILY = {"DSCR": "DSCR", "ICR": "ICR", "FIXED_CHARGE_COVERAGE_RATIO": "FCCR", "DEBT_TO_EBITDA": "LEVERAGE",
          "NET_LEVERAGE_RATIO": "LEVERAGE", "DEBT_TO_NET_WORTH": "DEBT_TO_NW", "DEBT_TO_EQUITY": "DEBT_TO_NW",
          "CURRENT_RATIO": "CURRENT_RATIO", "QUICK_RATIO": "QUICK_RATIO", "MIN_TANGIBLE_NET_WORTH": "MIN_NW",
          "MIN_NET_WORTH": "MIN_NW", "MINIMUM_LIQUIDITY": "LIQUIDITY", "EBITDA_TO_EMI": "DSCR",
          "LOAN_TO_VALUE": "LTV", "MIN_EBITDA": "MIN_EBITDA"}
SCORED_FAMILIES = set(FAMILY.values())
ABS_KEYS = {"CAPEX", "INTEREST_PAID", "TAX"}
US_UNIT = {"million": 0.1, "thousand": 0.0001}


def family(metric: str) -> str:
    return FAMILY.get((metric or "").split(":")[0], metric)


def is_fin(p: dict) -> bool:
    return p["category"] == "FINANCIAL" and p["threshold"] is not None


def matches(p: dict, c: dict) -> bool:
    if c["threshold"] is None or c["family"] not in SCORED_FAMILIES or not is_fin(p):
        return False
    if family(p["metric_raw"]) != c["family"]:
        return False
    if ("min" if (p["operator"] or "").startswith(">") else "max") != c["dir"]:
        return False
    return any(abs(p["threshold"] - t) <= max(0.011, 0.01 * abs(t)) for t in [c["threshold"], *c.get("alts", [])])


def near(p: dict, c: dict) -> bool:
    return p.get("page") is not None and c.get("page") is not None and abs(p["page"] - c["page"]) <= 1


def agreements(run: dict) -> dict:
    truth = json.load(open(os.path.join(TRUTH, "truth_agreements.json")))
    excluded, not_scored = truth["excluded"], truth.get("not_scored", {})
    count = {"docs": 0, "held": 0, "covenants": 0, "clean": 0, "flagged": 0, "shown": 0, "missed": 0,
             "proposed": 0, "proposed_flagged": 0, "silent_wrong": 0, "flagged_wrong": 0, "not_read": 0,
             "not_read_covenants": 0}
    detail = {}
    for doc, want in truth["covenants"].items():
        if doc in excluded or doc not in run:
            continue
        r = run[doc]
        props = r.get("proposals", [])
        if r["outcome"] in ("HEAVY", "CRASH", "FAILED"):
            # Handed to the heavy pool (or failed) with no final reading: a job a person sees waiting, not scored.
            count["not_read"] += 1
            count["not_read_covenants"] += len(want)
            detail[doc] = {"outcome": r["outcome"], "covenants": [], "silent_wrong": []}
            continue
        count["docs"] += 1
        count["held"] += r["outcome"] != "PUBLISHED"
        rows = []
        for c in want:
            count["covenants"] += 1
            hit = [p for p in props if matches(p, c)]
            if any(not p["issues"] for p in hit):
                state = "clean"
            elif hit:
                state = "flagged"
            elif any(near(p, c) and (p["category"] == "CANDIDATE" or family(p["metric_raw"]) == c["family"]
                                     or c["family"] not in SCORED_FAMILIES or c["threshold"] is None)
                     for p in props):
                state = "shown"
            else:
                state = "missed"
            count[state] += 1
            rows.append((c["family"], c["threshold"], c["page"], state))
        wrong = []
        for p in props:
            if p["category"] not in ("FINANCIAL", "CANDIDATE"):
                continue
            count["proposed"] += 1
            count["proposed_flagged"] += bool(p["issues"])
            if not is_fin(p) or any(matches(p, c) for c in want):
                continue
            if doc in not_scored and family(p["metric_raw"]) == "LEVERAGE":
                continue  # the covenant whose levels are an image: not judged
            if p["issues"]:
                count["flagged_wrong"] += 1
            else:
                count["silent_wrong"] += 1
                wrong.append((p["metric_raw"], p["operator"], p["threshold"], p["page"]))
        detail[doc] = {"outcome": r["outcome"], "covenants": rows, "silent_wrong": wrong}
    return {"count": count, "detail": detail}


def same(v: float, t: float, key: str, factor: float) -> bool:
    if key in ABS_KEYS:
        v, t = abs(v), abs(t)
    return abs(v - t) <= max(abs(t) * 0.001, 1.01 * factor)


def statements(run: dict) -> dict:
    us = json.load(open(os.path.join(TRUTH, "truth_us.json")))
    inlk = json.load(open(os.path.join(TRUTH, "truth_statements_inlk.json")))["docs"]
    keys = {}
    for doc, t in us.items():
        f = US_UNIT[t["unit"]]
        keys[doc] = ({k: v for k, v in t["values"].items()}, f)
    for doc, t in inlk.items():
        if t.get("no_figures"):
            keys[doc] = ({}, 1.0)
        else:
            f = t["to_crore"]
            keys[doc] = ({k: [x * f for x in v] for k, v in t["values"].items()}, f)
    count = {"docs": 0, "held": 0, "values": 0, "right_published": 0, "right_held": 0, "wrong_published": 0,
             "wrong_held": 0, "missing": 0, "no_figure_docs_published": 0, "not_read": 0,
             "ai_confirmed_right": 0, "ai_confirmed_wrong": 0, "ai_questioned_a_wrong_figure": 0,
             "ai_questioned_a_right_figure": 0, "ai_suggested_right": 0, "ai_suggested_wrong": 0}
    detail = {}
    for doc, (want, factor) in keys.items():
        if doc not in run:
            continue
        r = run[doc]
        if r["outcome"] in ("HEAVY", "CRASH", "FAILED"):
            count["not_read"] += 1
            continue
        published = r["outcome"] == "REPORTED"
        count["docs"] += 1
        count["held"] += not published
        got = {v["key"]: v["value"] for v in r.get("values", [])}
        if not want and published and got:
            count["no_figure_docs_published"] += 1
        rows = {}
        for key, accepted in want.items():
            count["values"] += 1
            if key not in got:
                state = "missing"
            elif any(same(got[key], t, key, factor) for t in accepted):
                state = "right_published" if published else "right_held"
            else:
                state = "wrong_published" if published else "wrong_held"
            count[state] += 1
            rows[key] = (state, got.get(key), accepted[0])
        detail[doc] = {"outcome": r["outcome"], "unit": r.get("unit"), "issues": r.get("issues"), "values": rows}
        # The second reader (--ai): figures it confirmed, figures it read differently, lines it suggested.
        for key in r.get("confirmed") or []:
            if key in want:
                ok = any(same(got[key], t, key, factor) for t in want[key]) if key in got else False
                count["ai_confirmed_right" if ok else "ai_confirmed_wrong"] += 1
        for issue in r.get("issues") or []:
            if issue.startswith("SECOND_READER_DIFFERS"):
                key = issue.split(":")[1].split()[0]
                if key in want and key in got:
                    ok = any(same(got[key], t, key, factor) for t in want[key])
                    count["ai_questioned_a_wrong_figure" if not ok else "ai_questioned_a_right_figure"] += 1
        for s in r.get("likely") or []:
            if s.get("source") == "second reader" and s["key"] in want:
                ok = any(same(s["value"], t, s["key"], factor) for t in want[s["key"]])
                count["ai_suggested_right" if ok else "ai_suggested_wrong"] += 1
    return {"count": count, "detail": detail}


def ai_agreements(run: dict) -> dict:
    """With --ai: how the AI reader's confirmations, questions and additions compare with the answer key."""
    truth = json.load(open(os.path.join(TRUTH, "truth_agreements.json")))
    out: dict[str, int] = {}
    for doc, want in truth["covenants"].items():
        r = run.get(doc)
        if doc in truth["excluded"] or not r or not r.get("ai") or r["outcome"] in ("HEAVY", "CRASH", "FAILED"):
            continue
        for p in r.get("proposals", []):
            if not is_fin(p):
                continue
            right = any(matches(p, c) for c in want)
            codes = [i.split(":")[0] for i in p["issues"]]
            if p["method"] == "REGEX+AI":
                out["confirmed_by_both_" + ("right" if right else "wrong")] = out.get(
                    "confirmed_by_both_" + ("right" if right else "wrong"), 0) + 1
            if p["method"] == "AI":
                k = "ai_only_" + ("right" if right else "wrong")
                out[k] = out.get(k, 0) + 1
            if "AI_READ_DIFFERENT" in codes:
                k = "ai_questioned_" + ("a_right_term" if right else "a_wrong_term")
                out[k] = out.get(k, 0) + 1
            if "AI_DID_NOT_CONFIRM" in codes:
                k = "ai_did_not_confirm_" + ("a_right_term" if right else "a_wrong_term")
                out[k] = out.get(k, 0) + 1
    return out


def main(paths: list[str]) -> None:
    run = {}
    for p in paths:
        run.update(json.load(open(p)))
    a, s = agreements(run), statements(run)
    ca, cs = a["count"], s["count"]
    print("AGREEMENTS", json.dumps(ca))
    print(f"  recall seen by a person (clean+flagged+shown): {ca['clean'] + ca['flagged'] + ca['shown']}/{ca['covenants']}"
          f"  missed: {ca['missed']}  silent wrong terms: {ca['silent_wrong']}  docs held: {ca['held']}/{ca['docs']}")
    for doc, d in a["detail"].items():
        print(f"  {doc} {d['outcome']:13} " + "; ".join(f"{f} {t} p{pg} {st}" for f, t, pg, st in d["covenants"])
              + (f"  SILENT: {d['silent_wrong']}" if d["silent_wrong"] else ""))
    ai = ai_agreements(run)
    if ai:
        print("  AI READER", json.dumps(ai))
    print("STATEMENTS", json.dumps(cs))
    pub = cs["right_published"] + cs["wrong_published"]
    print(f"  published values: {pub}, wrong among them (silent): {cs['wrong_published']}; held docs: "
          f"{cs['held']}/{cs['docs']}; missing: {cs['missing']}/{cs['values']}")
    for doc, d in s["detail"].items():
        bad = {k: v for k, v in d["values"].items() if not v[0].startswith("right")}
        print(f"  {doc} {d['outcome']:12} unit={d['unit']} issues={[i.split(':')[0] for i in d['issues'] or []]}"
              f" bad={bad}")
    json.dump({"agreements": a, "statements": s}, open(paths[-1].replace(".json", "") + ".scores.json", "w"),
              indent=1, default=str)


if __name__ == "__main__":
    main(sys.argv[1:])

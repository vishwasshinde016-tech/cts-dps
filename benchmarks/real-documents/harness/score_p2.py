"""Phase 2 agreements: rules alone vs rules + grounded AI vote, on the 45 labelled agreements."""
import json
import sys

FAMILY = {"DSCR": "DSCR", "ICR": "ICR", "FIXED_CHARGE_COVERAGE_RATIO": "FCCR", "DEBT_TO_EBITDA": "LEVERAGE",
          "NET_LEVERAGE_RATIO": "LEVERAGE", "DEBT_TO_NET_WORTH": "DEBT_TO_NW", "DEBT_TO_EQUITY": "DEBT_TO_NW",
          "CURRENT_RATIO": "CURRENT_RATIO", "QUICK_RATIO": "QUICK_RATIO", "MIN_TANGIBLE_NET_WORTH": "MIN_NW",
          "MIN_NET_WORTH": "MIN_NW", "MINIMUM_LIQUIDITY": "LIQUIDITY", "EBITDA_TO_EMI": "DSCR",
          "LOAN_TO_VALUE": "LTV", "MIN_EBITDA": "MIN_EBITDA"}
truth = json.load(open(sys.argv[2] if len(sys.argv) > 2 else "truth_ag.json"))
cov, excluded = truth["covenants"], set(truth["excluded"])
res = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "r_p2_ag.json"))


def match(t, c):
    return (FAMILY.get(t["metric"], t["metric"]) == c["family"]
            and ("min" if (t["operator"] or "").startswith(">") else "max") == c["dir"]
            and t["threshold"] is not None and abs(t["threshold"] - c["threshold"]) <= max(0.011, 0.01 * c["threshold"]))


def score(key, select):
    found = false = total = 0
    for doc, want in cov.items():
        if doc in excluded or doc not in res or key not in res[doc]:
            continue
        terms = [t for t in res[doc][key] if select(t)]
        total += len(want)
        found += sum(1 for c in want if any(match(t, c) for t in terms))
        false += sum(1 for t in terms if not any(match(t, c) for c in want))
    return found, total, false


def fin(t):
    return t["category"] == "FINANCIAL" and t["threshold"] is not None


rows = [("Rules alone", "before", fin), ("Rules + AI (all proposals)", "after", fin),
        ("Confirmed by both (auto-publish quality)", "after", lambda t: fin(t) and t["method"] == "REGEX+AI"),
        ("Rules only, not confirmed", "after", lambda t: fin(t) and t["method"] == "REGEX"),
        ("Added by AI only", "after", lambda t: fin(t) and t["method"] == "AI")]
print(f"{'set':42} {'found':>6} {'of':>4} {'recall':>7} {'false':>6} {'precision':>9}")
for name, key, sel in rows:
    f, n, x = score(key, sel)
    prec = f"{100 * (n and f) / max(1, f + x):.1f}%" if (f + x) else "-"
    print(f"{name:42} {f:>6} {n:>4} {100 * f / max(1, n):6.1f}% {x:>6} {prec:>9}")

# AI_READ_DIFFERENT: did the AI question rule terms that were wrong?
right_q = wrong_q = 0
for doc, want in cov.items():
    if doc in excluded or doc not in res or "after" not in res[doc]:
        continue
    for t in res[doc]["after"]:
        if fin(t) and any(i.startswith("AI_READ_DIFFERENT") for i in t["issues"]):
            if any(match(t, c) for c in want):
                right_q += 1
            else:
                wrong_q += 1
dropped = sum(r.get("dropped") or 0 for r in res.values())
ai_total = sum(len(r.get("ai", [])) for r in res.values())
cands_before = sum(1 for r in res.values() for t in r["before"] if t["category"] == "CANDIDATE")
cands_after = sum(1 for r in res.values() for t in r.get("after", r["before"]) if t["category"] == "CANDIDATE")
errors = [d for d, r in res.items() if "error" in r]
print(f"\nAI answers kept after grounding: {ai_total}; dropped (quote or number not on the page): {dropped}")
print(f"AI questioned a rules covenant: {right_q + wrong_q} times; the rules were wrong in {wrong_q}, right in {right_q}")
print(f"possible-covenant rows: {cands_before} before, {cands_after} after; errors: {errors}")
print(f"seconds per agreement: {sum(r['seconds'] for r in res.values()) / max(1, len(res)):.1f}")

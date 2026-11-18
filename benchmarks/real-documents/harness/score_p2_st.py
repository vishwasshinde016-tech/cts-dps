"""Phase 2 statements: what the grounded second reader confirms, flags and suggests, against hand and XBRL truth."""
import json
import sys

res = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "r_p2_st.json"))
hand = {d: v for d, v in json.load(open("adjudicated.json")).items() if not d.startswith("_")}
us = {d: v["values"] for d, v in json.load(open("truth_us.json")).items()}
CHECKED = {"REVENUE", "PBT", "PAT", "INTEREST_PAID", "DEPRECIATION", "TOTAL_ASSETS", "NET_WORTH", "CURRENT_ASSETS",
           "CURRENT_LIABILITIES", "LT_DEBT", "ST_DEBT", "CASH", "CFO", "INVENTORY", "RECEIVABLES"}


def near(a, b):
    return abs(a - b) <= max(0.01, 0.005 * abs(b))


def ok(v, t):
    return any(near(v, c) for c in (t if isinstance(t, list) else [t]))


for name, truths in (("India / Sri Lanka, hand-checked", hand), ("US 10-K, XBRL", us)):
    conf_right = conf_wrong = flag_wrong = flag_right = silent_wrong = right_alone = 0
    sugg_right = sugg_wrong = readings = grounded = 0
    for d, t in truths.items():
        r = res.get(d)
        if not r or "error" in r:
            continue
        readings += len(r.get("readings", []))
        grounded += sum(1 for x in r.get("readings", []) if x["grounded"])
        flagged = {i.split(":")[1].split()[0] for i in r.get("issues", []) if i.startswith("SECOND_READER_DIFFERS")}
        for k, truth in t.items():
            if truth is None or k not in CHECKED:
                continue
            v = r["values"].get(k)
            if v is None:
                s = next((x for x in r.get("suggestions", []) if x["key"] == k), None)
                if s:
                    sugg_right += ok(s["value"], truth)
                    sugg_wrong += not ok(s["value"], truth)
                continue
            good = ok(v, truth)
            right_alone += good
            if k in r.get("confirmed", []):
                conf_right += good
                conf_wrong += not good
            elif k in flagged:
                flag_wrong += not good
                flag_right += good
            elif not good:
                silent_wrong += 1
    print(f"\n{name}")
    print(f"  model answers: {readings}, on the page: {grounded} ({100 * grounded / max(1, readings):.0f}%)")
    print(f"  confirmed by both: {conf_right + conf_wrong} values, wrong among them: {conf_wrong}")
    print(f"  sent to review as different: {flag_right + flag_wrong} ({flag_wrong} really wrong, {flag_right} were right)")
    print(f"  wrong and neither confirmed nor flagged (reader alone): {silent_wrong}")
    print(f"  suggestions for missing values: {sugg_right} right, {sugg_wrong} wrong")

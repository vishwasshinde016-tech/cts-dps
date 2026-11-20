"""Phase 5 held-out set: documents DPS has never been tuned on.

Agreements: credit and loan agreements filed on EDGAR as EX-10 exhibits, from companies not in the first benchmark
(real-documents/sources.tsv), filed in 2025 or 2026, not amendments. US statements: the latest 10-K of companies not
in the first benchmark, with the XBRL company facts that become the answer key.

Same rules as the first benchmark: SEC's public endpoints only, an honest User-Agent, a pause between calls, no
retries against a refusal. The files are written under <out> and are not committed (third-party copyright).

  python fetch.py <out> agreements 20
  python fetch.py <out> statements
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

UA = "cts-dps-benchmark research@cts-dps.local"
HERE = os.path.dirname(os.path.abspath(__file__))
FIRST = os.path.join(HERE, "..", "..", "real-documents", "sources.tsv")
FIRST_10K = {18230, 315189, 73309, 106640, 46080, 63276, 793952, 48465, 21076, 89800, 9389, 1370946, 931015, 91440,
             55067}
QUERIES = ['"financial covenants" "credit agreement" "leverage ratio"',
           '"financial covenants" "loan agreement" "debt service coverage ratio"',
           '"interest coverage ratio" "credit agreement" "financial covenant"',
           '"fixed charge coverage ratio" "credit agreement" "financial covenants"',
           '"tangible net worth" "loan agreement" "financial covenants"',
           '"minimum liquidity" "loan agreement" "financial covenants"',
           '"net leverage ratio" "credit agreement" "financial covenant"',
           '"loan to value" "loan agreement" "debt service coverage"',
           '"debt service coverage ratio" "term loan agreement"',
           '"consolidated interest coverage ratio" "revolving credit agreement"',
           '"total leverage ratio" "credit agreement" "covenants"',
           '"current ratio" "loan agreement" "financial covenants"']
# Mid-size US manufacturers and consumer companies, none in the first benchmark.
TEN_K = {"HU01": 47111, "HU02": 55785, "HU03": 1069202, "HU04": 737758, "HU05": 58492, "HU06": 14930}


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    time.sleep(0.35)
    return data


def first_ciks() -> set[str]:
    text = open(FIRST, encoding="utf-8").read()
    return set(re.findall(r"/edgar/data/(\d+)/", text)) | {str(c) for c in FIRST_10K}


def agreements(out: str, want: int) -> None:
    os.makedirs(out, exist_ok=True)
    idx = os.path.join(out, "index.json")
    chosen = json.load(open(idx)) if os.path.exists(idx) else {}
    seen = first_ciks() | {v["url"].split("/data/")[1].split("/")[0] for v in chosen.values()}
    for q in QUERIES:
        for start in range(0, 300, 100):
            if len(chosen) >= want:
                break
            url = ("https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode(
                {"q": q, "dateRange": "custom", "startdt": "2025-01-01", "enddt": "2026-10-31", "from": start}))
            for h in json.loads(get(url))["hits"]["hits"]:
                s = h["_source"]
                ftype, desc = s.get("file_type", ""), (s.get("file_description") or "").lower()
                adsh, fname = h["_id"].split(":")
                cik = s["ciks"][0].lstrip("0")
                name = (desc + " " + fname).lower()
                if not ftype.startswith("EX-10") or cik in seen or "amend" in name:
                    continue
                if not ("credit" in name or "loan" in name):
                    continue
                link = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{fname}"
                try:
                    data = get(link)
                except Exception as ex:  # noqa: BLE001
                    print("skip", link, type(ex).__name__)
                    continue
                seen.add(cik)
                doc_id = f"HA{len(chosen) + 1:02d}"
                ext = ".pdf" if data.startswith(b"%PDF") else ".html"
                open(os.path.join(out, doc_id + ext), "wb").write(data)
                chosen[doc_id] = {"company": s["display_names"][0], "file_date": s["file_date"], "url": link,
                                  "file_type": ftype, "description": s.get("file_description"), "bytes": len(data)}
                print(doc_id, s["file_date"], s["display_names"][0][:50], ftype, len(data), flush=True)
                if len(chosen) >= want:
                    break
    json.dump(chosen, open(idx, "w"), indent=1)


def statements(out: str) -> None:
    os.makedirs(out, exist_ok=True)
    index = {}
    for doc_id, cik in TEN_K.items():
        try:
            sub = json.loads(get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))
            recent = sub["filings"]["recent"]
            i = recent["form"].index("10-K")
            acc, prim = recent["accessionNumber"][i].replace("-", ""), recent["primaryDocument"][i]
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{prim}"
            open(os.path.join(out, doc_id + ".html"), "wb").write(get(url))
            facts = get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
            open(os.path.join(out, doc_id + ".facts.json"), "wb").write(facts)
            index[doc_id] = {"cik": cik, "name": sub["name"], "url": url, "accession": recent["accessionNumber"][i],
                             "report_date": recent["reportDate"][i]}
            print(doc_id, sub["name"], recent["reportDate"][i], flush=True)
        except Exception as ex:  # noqa: BLE001
            print(doc_id, "failed", type(ex).__name__, str(ex)[:80], flush=True)
    json.dump(index, open(os.path.join(out, "index.json"), "w"), indent=1)


if __name__ == "__main__":
    if sys.argv[2] == "agreements":
        agreements(sys.argv[1], int(sys.argv[3]))
    else:
        statements(sys.argv[1])

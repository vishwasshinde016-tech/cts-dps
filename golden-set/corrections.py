"""Export accepted reviewer corrections as local field-level regression cases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def accepted_cases(items: list[dict]) -> list[dict]:
    cases = []
    for item in items:
        if item.get("status") != "ACCEPTED":
            continue
        payload = item.get("payload") or {}
        action = payload.get("action")
        if action not in ("ADD", "CORRECT", "REJECT"):
            continue
        field = payload.get("before" if action == "REJECT" else "after") or {}
        source = field.get("source") or {}
        if not (field.get("group") and field.get("name") and source.get("page") and source.get("quote")):
            continue
        cases.append({"candidateId": item["id"], "documentVersionId": payload.get("versionId"),
                      "jobType": item["jobType"], "present": action != "REJECT", "group": field["group"],
                      "name": field["name"], "value": field.get("value"), "page": source["page"],
                      "quote": source["quote"]})
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True, help="Saved approved ops candidate response")
    parser.add_argument("--output", type=Path, required=True, help="New file in the approved local test corpus")
    args = parser.parse_args()
    cases = accepted_cases(json.loads(args.candidates.read_bytes())["items"])
    with args.output.open("x", encoding="utf-8") as output:
        json.dump({"cases": cases}, output, ensure_ascii=False, indent=2)
    print(f"Exported {len(cases)} accepted corrections with page evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

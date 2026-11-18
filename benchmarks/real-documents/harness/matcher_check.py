"""Meaning matcher on real statement labels: for lines the catalog maps by exact wording, how often does the
matcher alone (no exact wording) name the same line, and how often does it name a wrong one with confidence?"""
import json
import sys
from collections import Counter

from dps.extraction.similarity import nearest
from dps.extraction.statement import match_key

res = json.load(open(sys.argv[1]))
same = wrong = unsure = 0
wrong_pairs = Counter()
seen = set()
for d, ms in res.items():
    for row in ms.get("NEW", {}).get("raw", []):
        label = row[0]
        key = match_key(label)
        if key is None or label in seen:
            continue
        seen.add(label)
        got, score = nearest(label)
        if score < 0.6:
            unsure += 1
        elif got == key:
            same += 1
        else:
            wrong += 1
            wrong_pairs[(key, got)] += 1
n = same + wrong + unsure
print(f"distinct catalog-mapped labels: {n}; matcher names the same line {same} ({100 * same / n:.1f}%), "
      f"another line {wrong} ({100 * wrong / n:.1f}%), unsure (<0.6) {unsure} ({100 * unsure / n:.1f}%)")
print("most common disagreements:", wrong_pairs.most_common(8))

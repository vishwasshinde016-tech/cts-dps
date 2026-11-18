# Covenant taxonomy

The one list of covenant types DPS uses to name every covenant it extracts (decision 18 Nov 2026). Every covenant
field in a result carries `value.code` and `value.category` from here, next to the older DPS name in `value.metric`,
and every result carries `pipeline.taxonomyVersion`.

| File | What |
| --- | --- |
| `default_taxonomy.yaml` | The bank's taxonomy, copied as given (92 types; categories financial, affirmative, negative, reporting, event_of_default). Do not edit it for DPS needs: use an overlay. |
| `dps_overlay.yaml` | DPS's own overlay: 6 extra types + `NEG_DISPOSAL_CAP`, alias fixes, and `dps_keys` mapping the rules' 32 older metric names to codes. |
| `__init__.py` | Loader: merges the files, checks them, matches names to codes. |

Load order: `default_taxonomy.yaml`, `dps_overlay.yaml`, then any YAML file (or folder of them) in
`DPS_COVENANT_TAXONOMY_PATH`. The version is the files' `version` values joined with `+` (today `2026.11.0+dps.1`).

## How to add or change a type

1. Add an entry to `dps_overlay.yaml` (or a bank overlay file), and raise its `version` (for example `dps.2`):
   - a **new code** adds a type: `{code: FIN_NEW_RATIO, category: financial, name: ..., description: ..., aliases: [...], keywords: [...], quantitative: true, default_operator: ">=", unit: ratio, frequency: quarterly}`;
   - an **existing code** patches it: `aliases`, `keywords` and `dps_keys` are added to what is there;
     `replace_aliases: true` replaces the aliases instead; other keys replace the old value;
   - `enabled: false` switches a type off.
2. Run the checks: `python -m pytest tests/test_covenant_taxonomy.py tests/test_covenant_names.py tests/test_recall.py`.
3. Run the golden-set gate from `dps/`:
   `python ../golden-set/predict.py --out ../golden-predictions` then
   `python ../golden-set/evaluate.py --predictions ../golden-predictions --statuses SYNTHETIC` (must pass).
4. Open a pull request. Taxonomy changes go in only through a PR with the gate passed.

## Checks at load

The worker refuses to start when: a code is not UPPER_SNAKE; a category is unknown; `default_operator` is not one of
`>= > <= < =`; `unit` is not one of `ratio multiple percent amount days count`; one alias is on two types of the same
category; one DPS key is mapped to two codes. An alias shared by two categories (Change of Control is a negative
covenant and an event of default) goes to the covenant category and is listed in `notes`.

## Matching rules

- Aliases of one or two letters ("CR", "DP") are never matched.
- An upper-case acronym of up to 6 letters ("DSCR", "TOL") matches only as a whole upper-case word.
- A one-word alias ("Insurance") matches only a name that is exactly that word.
- Longer aliases match inside a name, the longest first. Category order when two match: financial, negative,
  affirmative, reporting, event_of_default.

## Where it is used

| Code | Use |
| --- | --- |
| `skills/covenants/parts.py` | The covenants skill reads in 4 parts (financial, undertakings, reporting, events of default); each part's prompt lists only its codes. |
| `skills/covenants/names.py` | Agent names become a DPS key or a taxonomy code; unknown names become OTHER with the name as written. |
| `skills/covenants/codes.py` | `stamp`: code and category on every covenant field; a missing operator, unit or test frequency is filled from the taxonomy default, listed in `value.defaults`, flagged `TAXONOMY_DEFAULT` and sent to review. `event_fields`: events of default as `EVENT_OF_DEFAULT` fields, always reviewed. |
| `skills/covenants/coverage.py` | Coverage families (one reporting family; guarantees with indebtedness ...) and the last way to tell a duty sentence's kind. |
| `skills/runner.py` | Vote keyed by taxonomy code (the rules' DEBT_TO_NET_WORTH and the agent's FIN_DEBT_TO_EQUITY are one covenant). |

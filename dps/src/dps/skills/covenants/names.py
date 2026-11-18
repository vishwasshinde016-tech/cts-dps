"""The covenants skill's agent answers, cleaned before the vote.

Live run on Azure OpenAI (18 Nov 2026): the agent found every labelled covenant but also (1) returned loan mechanics
(drawdown and prepayment notice, payment default, cure period) that the schedule_terms skill already reads, and
(2) split one clause into several items with names of its own ("RESTRICTION_ON_CHARGED_ASSET_DISPOSAL_...") that
Spring's covenant library does not know. Here every proposal gets a library metric key (or OTHER, with the name as
written kept for the reviewer), loan mechanics are dropped, and a non-financial key is proposed once per clause
area (the same page or the next: a list of reporting duties under one clause is one item; the same kind of duty far
away in the agreement stays a separate item).
"""

from __future__ import annotations

import re

from dps.extraction.agreement import FINANCIAL_ALIASES, NON_FINANCIAL_RULES

OTHER = "OTHER"
# Security covenants the rules also read (extraction/agreement.py _SECURITY_RULES): a threshold, so voted like the
# financial ones. Live run 18 Nov 2026: without them the agent's security covenants became OTHER and stood apart
# from the rules' identical ones.
SECURITY: dict[str, list[str]] = {
    "SECURITY_COVER": ["SECURITY COVER", "ASSET COVER", "COLLATERAL COVER", "SECURITY COVERAGE"],
    "MIN_COLLATERAL_VALUE": ["MINIMUM COLLATERAL VALUE", "COLLATERAL VALUE", "VALUE OF THE COLLATERAL",
                             "VALUE OF THE SECURITY"],
    "ASSET_DISPOSAL_CAP": ["ASSET DISPOSAL CAP", "DISPOSAL CAP", "ASSET DISPOSAL LIMIT", "DISPOSAL LIMIT"],
}
FINANCIAL = frozenset(FINANCIAL_ALIASES) | frozenset(SECURITY)
# Undertakings of Indian loan agreements the rules have no rule for (live run, 18 Nov 2026): without a key they were
# OTHER, so Spring could not map them and the coverage check could not match them by kind.
EXTRA_NON_FINANCIAL: dict[str, str] = {
    "END_USE": r"END_USE|USE_OF_PROCEEDS|USE_OF_FUNDS|PURPOSE|PROCEEDS|UTILI[SZ]ATION_OF_(?:THE_)?FUNDS",
    "ESCROW_ACCOUNT": r"ESCROW|TRUST_AND_RETENTION|TRA_ACCOUNT|COLLECTION_ACCOUNT|DESIGNATED_ACCOUNT",
    "SUBORDINATION": r"SUBORDINAT|PROMOTER_LOANS?|UNSECURED_LOANS?_FROM_PROMOTERS",
    "SECURITY_CREATION": r"SECURITY_CREATION|CREATE_SECURITY|CREATION_OF_(?:SECURITY|MORTGAGE|CHARGE)|"
                         r"PERFECTION|PERFECT_SECURITY",
    "MATERIAL_CONTRACTS": r"MATERIAL_CONTRACTS?|PROJECT_DOCUMENTS?|MATERIAL_AGREEMENTS?",
}
NON_FINANCIAL = tuple(dict.fromkeys([*(rule[0] for rule in NON_FINANCIAL_RULES), *EXTRA_NON_FINANCIAL]))
KNOWN = FINANCIAL | frozenset(NON_FINANCIAL)

# A name the agent made up, to the library key it means. Checked in this order: the first match wins, so the
# reporting words come before "INSURANCE" ("evidence of tax payment, insurance validity" is an information duty)
# and disposal before pledge ("disposal of charged assets" is a disposal restriction).
_NON_FINANCIAL_WORDS: list[tuple[str, str]] = [
    ("COMPLIANCE_CERTIFICATE", r"COMPLIANCE_CERTIFICATE|OFFICER_CERTIFICATE"),
    ("REPORTING_COVENANT", r"REPORTING|FINANCIAL_STATEMENTS?|ANNUAL_ACCOUNTS|MANAGEMENT_ACCOUNTS|INFORMATION"
                           r"|TAX_FILINGS?|EVIDENCE|NOTICE|DELIVER"),
    # the five newer kinds after reporting ("evidence of end use" is an information duty) and before the broader ones
    # ("subordination of promoter loans" is not a borrowing restriction, "creation of mortgage" not a negative pledge)
    *EXTRA_NON_FINANCIAL.items(),
    ("ASSET_DISPOSAL_RESTRICTION", r"DISPOS|SALE_OF_ASSETS?|TRANSFER_OF_ASSETS?|ASSET_SALE"),
    ("DEBT_INCURRANCE_RESTRICTION", r"BORROWING|INDEBTEDNESS|ADDITIONAL_DEBT|DEBT_INCUR|GUARANTEE"),
    ("NEGATIVE_PLEDGE", r"NEGATIVE_PLEDGE|LIEN|ENCUMBRANCE|SECURITY_INTEREST"),
    ("CHANGE_OF_CONTROL", r"CONTROL|SHAREHOLDING|OWNERSHIP"),
    ("INSURANCE_MAINTENANCE", r"INSURANCE"),
    ("BOOKS_AND_RECORDS", r"BOOKS|RECORDS|INSPECTION|AUDIT_ACCESS"),
    ("REGULATORY_AND_TAX_COMPLIANCE", r"COMPLIANCE_WITH_LAWS?|APPLICABLE_LAWS?|TAX_PAYMENT|STATUTORY|LICEN[CS]E"),
]
_NON_FINANCIAL_RE = [(key, re.compile(rf"(?:^|_)(?:{words})", re.I)) for key, words in _NON_FINANCIAL_WORDS]

# Loan mechanics, not covenants: the schedule_terms skill reads them (drawdown, prepayment, repayment, interest,
# default and cure windows, fees).
_MECHANICS = re.compile(
    r"(?:^|_)(?:DRAW_?DOWN|UTILI[SZ]ATION|PREPAY|REPAYMENT|INSTAL+MENT|EMI_PAYMENT|INTEREST_(?:PAYMENT|RATE|RESET)"
    r"|EVENTS?_OF_DEFAULT|PAYMENT_(?:DEFAULT|OBLIGATION|GRACE)|DEFAULT_INTEREST|CURE|GRACE|FEES?|ACCELERAT"
    r"|TENOR|AVAILABILITY_PERIOD|COMMITMENT_PERIOD)", re.I)

_ALIASES = sorted(((alias.upper(), key) for key, names in [*FINANCIAL_ALIASES.items(), *SECURITY.items()]
                   for alias in names),
                  key=lambda a: -len(a[0]))


def _token(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(text or "").upper()).strip("_")


def canonical(metric: str | None, name: str | None = None) -> str:
    """The library key for the agent's metric (or name as written); OTHER when none fits."""
    for raw in (metric, name):
        token = _token(raw)
        if not token:
            continue
        if token in KNOWN:
            return token
        spaced = f" {token.replace('_', ' ')} "
        for alias, key in _ALIASES:  # the longest alias first: "DEBT TO NET WORTH" before "NET WORTH"
            if f" {re.sub(r'[^A-Z0-9]+', ' ', alias).strip()} " in spaced:
                return key
        for key, pattern in _NON_FINANCIAL_RE:
            if pattern.search(token):
                return key
    return OTHER


def is_mechanics(metric: str | None, name: str | None = None) -> bool:
    """A loan mechanic (drawdown, prepayment, default, cure ...) rather than a covenant."""
    return any(_MECHANICS.search(_token(raw)) for raw in (metric, name) if raw)


def clean(proposals: list[dict] | None, drop_mechanics: bool = True, collapse: bool = True) -> list[dict] | None:
    """Library keys on every proposal; loan mechanics dropped; one proposal per non-financial key."""
    if proposals is None:
        return None
    out: list[dict] = []
    seen: dict[str, list[int | None]] = {}
    for p in proposals:
        value = dict(p.get("value") or {})
        written = p.get("_written") or p.get("name") or value.get("metric")
        key = canonical(value.get("metric"), p.get("name"))
        # Before the name mapping decides: "PREPAYMENT_NOTICE" is a loan mechanic, not a reporting duty. A newer kind
        # is never a mechanic ("utilisation of proceeds" is END_USE).
        known_metric = _token(value.get("metric")) in KNOWN or key in EXTRA_NON_FINANCIAL
        if drop_mechanics and key not in FINANCIAL and not known_metric \
                and is_mechanics(value.get("metric"), p.get("name")):
            continue
        if collapse and key in NON_FINANCIAL:
            page = p.get("page") if isinstance(p.get("page"), int) else None
            pages = seen.setdefault(key, [])
            if any(q is None or page is None or abs(q - page) <= 1 for q in pages):
                continue
            pages.append(page)
        value["metric"] = key
        if key == OTHER:
            value["nameAsWritten"] = str(written or "")[:120]
        out.append({**p, "value": value, "_written": written})
    return out

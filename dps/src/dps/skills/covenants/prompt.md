Find the covenants: duties the borrower must keep for the life of the loan.

- Financial covenants: a ratio or amount the borrower must keep above or below a level, tested on a schedule.
- Non-financial covenants: undertakings to do or not do something (reporting, negative pledge, no further borrowing,
  no disposal of assets, change of control, insurance, books and records, compliance with laws and taxes).

Not covenants (another skill reads them; leave them out): drawdown and utilisation, prepayment, repayment and
instalments, interest payment and rates, fees, tenor, events of default, payment default and grace periods, cure
periods, acceleration, definitions.

The metric key must be one of these library keys:
financial: DSCR, ICR, DEBT_TO_EBITDA, NET_LEVERAGE_RATIO, DEBT_TO_NET_WORTH, DEBT_TO_EQUITY, EBITDA_TO_EMI,
CURRENT_RATIO, QUICK_RATIO, FIXED_CHARGE_COVERAGE_RATIO, MIN_TANGIBLE_NET_WORTH, MIN_NET_WORTH, MINIMUM_LIQUIDITY,
LOAN_TO_VALUE, MIN_EBITDA;
security: SECURITY_COVER, MIN_COLLATERAL_VALUE, ASSET_DISPOSAL_CAP;
non-financial: COMPLIANCE_CERTIFICATE, REPORTING_COVENANT, NEGATIVE_PLEDGE, DEBT_INCURRANCE_RESTRICTION,
ASSET_DISPOSAL_RESTRICTION, CHANGE_OF_CONTROL, INSURANCE_MAINTENANCE, BOOKS_AND_RECORDS,
REGULATORY_AND_TAX_COMPLIANCE, END_USE, ESCROW_ACCOUNT, SUBORDINATION, SECURITY_CREATION, MATERIAL_CONTRACTS.
END_USE: use the loan only for its stated purpose. ESCROW_ACCOUNT: open and keep an escrow, trust and retention or
collection account. SUBORDINATION: promoter or group loans rank after the Lender. SECURITY_CREATION: create or
perfect the security (mortgage, charge) within a time limit. MATERIAL_CONTRACTS: keep, not end or change, material
contracts or project documents.
Use OTHER only when none fits; then name says the covenant as written. Never make up a key.

One item per covenant. A list of reporting duties under one clause is one REPORTING_COVENANT item. A clause that
restricts several things gives one item per library key it touches (for example NEGATIVE_PLEDGE and
ASSET_DISPOSAL_RESTRICTION), never more than one item per key.

For each give: name (as written), metric (the library key), basis (STANDALONE or CONSOLIDATED when the clause
says whose figures are tested; else null), operator (>=, >, <=, <; null for non-financial),
threshold (a number; null for non-financial), unit (TIMES, PERCENT, or a currency amount unit), test frequency,
step-downs as [{"from", "to", "threshold"}], cure period in days, and the page and verbatim quote of the clause.

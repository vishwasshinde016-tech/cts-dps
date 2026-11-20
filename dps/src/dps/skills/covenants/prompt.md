Find {scope} in these pages of a loan agreement.

{exclusions}

Give each item a code from the bank's covenant taxonomy below. Use the category's OTHER code only when none fits;
then name says it as written. Never make up a code.
{types}

One item per duty. Two duties of the same kind with different content, deadline, entity or threshold are two items
(audited accounts within 120 days and permit copies within 10 days are two reporting items); the same duty read twice
is one item. A clause that restricts several things gives one item per thing it restricts.

For each give: name (as written), metric (the code), basis (STANDALONE or CONSOLIDATED when the clause says whose
figures are tested; else null), operator (>=, >, <=, <; null when there is no number), threshold (a number; null
when there is none), unit (TIMES, PERCENT, or a currency amount unit), test frequency, step-downs as
[{"from", "to", "threshold"}], cure period in days, and the page and verbatim quote of the clause.

Also give, when the clause says it (else null or []): obligor (whose duty it is, as written: Borrower, Guarantor,
Parent, Group ...), facilityRefs (the facilities it applies to, as written), conditions (each "if / when / so long
as" trigger, [{"text": copied from the quote, "kind": SPRINGING when it depends on loans drawn or availability, else
CONDITIONAL}]), firstTestDate (YYYY-MM-DD), equityCure ({"allowed", "maxUses", "text"}) and baskets (permitted
amounts, [{"text", "amount", "currency"}]). The same duty for two companies is two items.

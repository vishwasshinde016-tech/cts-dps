# The architect's documents (for the demo)

These are the real, public loan and credit agreements from the list the architect gave. They are used in `docs/cts-dps/demo-walkthrough.md`. They are kept here so the demo needs no internet. The documents are for evaluation only.

- The SEC exhibits were HTML pages; they were printed to PDF, because DPS reads PDF, Word and Excel.
- `dps/scripts/fetch_public_samples.py` downloads the same list again and writes into this folder.

## The files

| File | Flow it shows | Source (the architect's list) |
|---|---|---|
| [us_ex10_1_credit_agreement_2020.pdf](us_ex10_1_credit_agreement_2020.pdf) | **Onboarding (the demo file)**: a 100-page US credit agreement | https://www.sec.gov/Archives/edgar/data/866787/000114036120008175/ex10_1.htm |
| [us_southcross_amendment_2018.pdf](us_southcross_amendment_2018.pdf) | **Amendment (the demo file)**: the Sixth Amendment to a revolving credit agreement | https://www.sec.gov/Archives/edgar/data/0001547638/000154763818000049/a2018q210-qex101.htm |
| [us_holly_amendment_2010.pdf](us_holly_amendment_2010.pdf) | Amendment: the First Amendment to a restated credit agreement | https://www.sec.gov/Archives/edgar/data/0000048039/000095012310047852/d72895exv10w1.htm |
| [us_core_labs_credit_agreement_2022.pdf](us_core_labs_credit_agreement_2022.pdf) | Onboarding: a 139-page US credit agreement | https://www.sec.gov/Archives/edgar/data/1000229/000095017022013124/clb-ex10_1.htm |
| [us_meet_group_credit_agreement_2019.pdf](us_meet_group_credit_agreement_2019.pdf) | Onboarding: a US credit agreement with a pricing grid | https://www.sec.gov/Archives/edgar/data/1078099/000107809919000099/exhibit101-themeetgroupxcr.htm |
| [uk_dwf_senior_facilities_agreement.pdf](uk_dwf_senior_facilities_agreement.pdf) | Onboarding: a 396-page UK LMA senior facilities agreement | https://dwfgroup.com/-/media/dwf-global-site/pdfs/investors/public-statement/financing-documents/senior-facilities-agreement.pdf |
| [in_au_emerging_enterprises_facility.pdf](in_au_emerging_enterprises_facility.pdf) | Onboarding: an Indian bank's facility agreement form | https://www.au.bank.in/pdfs/au-forms/loan-agreements/emerging-enterprises-facility-agreement-emerging-enterprises-facility-agreementpdf.pdf |
| [in_idfc_first_bbg_term_loan_agreement.pdf](in_idfc_first_bbg_term_loan_agreement.pdf) | Onboarding: an Indian bank's term loan form | https://www.idfcfirst.bank.in/content/dam/idfcfirstbank/pdf/loans/BBG-Term-Loan-Agreement.pdf |
| [in_idfc_first_composite_business_loan.pdf](in_idfc_first_composite_business_loan.pdf) | Onboarding: an Indian bank's composite business loan form | https://www.idfcfirst.bank.in/content/dam/idfcfirstbank/pdf/loans/Composite-Business-loan-Agreement-Term-loan.pdf |

## Not available on 19 Nov 2026

Four links on the list could not be downloaded:

| Document | Reason |
|---|---|
| Workspace facility agreement (2022) | the link returns "not found" |
| Gresham House senior facilities agreement (2023) | the link returns "not found" |
| DCB construction finance agreement | the link returns "not found" |
| South Indian Bank credit arrangement letter | the site's security certificate fails |

## Flows the list does not cover

The list has no financial statements, sanction letters or compliance certificates.
- For the **assessment** flow, the walkthrough uses our labelled statement [`golden-set/documents/FS-012.pdf`](../golden-set/documents/FS-012.pdf).
- There is **no sanction-letter or compliance-certificate file** for a demo yet.

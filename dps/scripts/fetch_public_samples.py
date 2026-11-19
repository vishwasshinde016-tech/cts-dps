"""Download the architect's list of real, public loan and credit agreements for the demo (needs internet).

    python scripts/fetch_public_samples.py              # -> samples/ (next to this dps/ folder's scripts)
    python scripts/fetch_public_samples.py D:\\demo     # another folder
    python scripts/demo_onboard.py samples/uk_workspace_facility_agreement_2022.pdf

The list is the one the architect gave (United States SEC EDGAR, United Kingdom LMA-style facility agreements,
Indian banks' standard loan agreement forms). Documents are for evaluation only: check each source's terms. They are
third-party documents, so ``samples/`` is ignored by Git and never committed.

SEC EDGAR asks automated clients to send a descriptive User-Agent with a contact address. Set DPS_SEC_USER_AGENT
(or COVX_SEC_USER_AGENT), for example "MyBank Covenant Research ops@mybank.example".

DPS reads PDF, Word and Excel. SEC exhibits are HTML pages, so each one is also printed to PDF with headless Microsoft
Edge or Google Chrome when one is installed (both are on a normal Windows laptop). Without a browser the .htm is kept
and the script says so.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

# (file name, URL, what it is, which DPS flow it demonstrates)
SOURCES = [
    # --- United States: SEC EDGAR credit agreements / amendments ---------------------------------------
    ("us_core_labs_credit_agreement_2022.htm",
     "https://www.sec.gov/Archives/edgar/data/1000229/000095017022013124/clb-ex10_1.htm",
     "US credit agreement (Core Laboratories, 2022)", "ONBOARD"),
    ("us_meet_group_credit_agreement_2019.htm",
     "https://www.sec.gov/Archives/edgar/data/1078099/000107809919000099/exhibit101-themeetgroupxcr.htm",
     "US credit agreement (The Meet Group, 2019)", "ONBOARD"),
    ("us_ex10_1_credit_agreement_2020.htm",
     "https://www.sec.gov/Archives/edgar/data/866787/000114036120008175/ex10_1.htm",
     "US credit agreement (2020)", "ONBOARD"),
    ("us_holly_amendment_2010.htm",
     "https://www.sec.gov/Archives/edgar/data/0000048039/000095012310047852/d72895exv10w1.htm",
     "US credit agreement amendment (Holly, 2010)", "EXTRACT_AMENDMENT"),
    ("us_southcross_amendment_2018.htm",
     "https://www.sec.gov/Archives/edgar/data/0001547638/000154763818000049/a2018q210-qex101.htm",
     "US credit agreement amendment (Southcross, 2018)", "EXTRACT_AMENDMENT"),
    # --- United Kingdom / LMA-based facility agreements published by listed companies -------------------
    ("uk_workspace_facility_agreement_2022.pdf",
     "https://www.workspace.co.uk/getmedia/a0487b57-85c2-4555-a26f-a6bd40c04972/35-Facility-Agreement-(2-March-2022).PDF",
     "UK facility agreement (Workspace, 2022)", "ONBOARD"),
    ("uk_gresham_house_senior_facilities_2023.pdf",
     "https://greshamhouse.com/wp-content/uploads/2023/11/23.1-Senior-Facilities-Agreement-Ares-Management-Limited-dated-27-November-2023.pdf",
     "UK senior facilities agreement (Gresham House, 2023)", "ONBOARD"),
    ("uk_dwf_senior_facilities_agreement.pdf",
     "https://dwfgroup.com/-/media/dwf-global-site/pdfs/investors/public-statement/financing-documents/senior-facilities-agreement.pdf",
     "UK senior facilities agreement (DWF)", "ONBOARD"),
    # --- India: banks' published standard loan agreement forms -------------------------------------------
    ("in_idfc_first_bbg_term_loan_agreement.pdf",
     "https://www.idfcfirst.bank.in/content/dam/idfcfirstbank/pdf/loans/BBG-Term-Loan-Agreement.pdf",
     "India term loan agreement form (IDFC FIRST)", "ONBOARD"),
    ("in_idfc_first_composite_business_loan.pdf",
     "https://www.idfcfirst.bank.in/content/dam/idfcfirstbank/pdf/loans/Composite-Business-loan-Agreement-Term-loan.pdf",
     "India composite business loan agreement form (IDFC FIRST)", "ONBOARD"),
    ("in_dcb_construction_finance_agreement.pdf",
     "https://www.dcbbank.com/upload/pdf/Construction-Finance-6-Loan-Agreement-Multi-Facility-Agreement.pdf",
     "India construction finance multi-facility agreement form (DCB)", "ONBOARD"),
    ("in_au_emerging_enterprises_facility.pdf",
     "https://www.au.bank.in/pdfs/au-forms/loan-agreements/emerging-enterprises-facility-agreement-emerging-enterprises-facility-agreementpdf.pdf",
     "India emerging enterprises facility agreement form (AU)", "ONBOARD"),
    ("in_south_indian_bank_credit_arrangement_letter.pdf",
     "https://southindianbank.com/UserFiles/file/Credit_Arrangement_Letter.pdf",
     "India credit arrangement (sanction) letter (South Indian Bank)", "ONBOARD"),
]

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> str | None:
    for path in BROWSERS:
        if Path(path).exists():
            return path
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def html_to_pdf(browser: str, html: Path, pdf: Path) -> bool:
    """Print an HTML page to PDF with a headless browser; True when the PDF was written."""
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
           f"--print-to-pdf={pdf.resolve()}", html.resolve().as_uri()]
    try:
        subprocess.run(cmd, check=False, timeout=180, capture_output=True)  # noqa: S603 - fixed local browser
    except (OSError, subprocess.TimeoutExpired):
        return False
    return pdf.exists() and pdf.stat().st_size > 1000


def main(out_dir: str | None = None) -> int:
    out = Path(out_dir) if out_dir else Path(__file__).resolve().parents[1] / "samples"
    out.mkdir(parents=True, exist_ok=True)
    ua = os.environ.get("DPS_SEC_USER_AGENT") or os.environ.get("COVX_SEC_USER_AGENT") or \
        "CTS-DPS-evaluation research@cts-dps.local"
    browser = find_browser()
    failed = 0
    for name, url, what, flow in SOURCES:
        dest = out / name
        if not dest.exists():
            try:
                if not url.startswith("https://"):  # only the fixed https list above, never file:// or other schemes
                    raise ValueError("not an https URL")
                r = httpx.get(url, headers={"User-Agent": ua}, timeout=90, follow_redirects=True)
                r.raise_for_status()
                dest.write_bytes(r.content)
                time.sleep(0.4)  # be polite to SEC and the banks' sites
            except Exception as exc:  # noqa: BLE001 - report and carry on with the next file
                failed += 1
                print(f"FAIL {name}: {exc}", file=sys.stderr)
                continue
        line = f"ok   {name:<52} {dest.stat().st_size // 1024:>6} KB  {flow:<17} {what}"
        if dest.suffix == ".htm":
            pdf = dest.with_suffix(".pdf")
            if pdf.exists() or (browser and html_to_pdf(browser, dest, pdf)):
                line += f"  -> {pdf.name}"
            else:
                line += "  (no Edge/Chrome found: open the .htm and print it to PDF)"
        print(line)
    print(f"\nfiles in {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))

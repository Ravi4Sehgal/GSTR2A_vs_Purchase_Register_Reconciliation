# GSTR-2A vs Purchase Register Reconciliation

An AI-powered Streamlit web app that automates GST reconciliation between GSTR-2A (supplier-filed data from the GST portal) and a company's internal Purchase Register — a task typically done manually in Excel by tax teams.

## What It Does

Upload two CSV files (GSTR-2A and Purchase Register) and the app:
- Matches invoices across both files on 6 fields
- Classifies each invoice into one of 7 reconciliation statuses
- Uses **Google Gemini AI** to explain every mismatch in plain English
- Exports a color-coded Excel report with 3 sheets

## Reconciliation Statuses

| Status | Meaning | ITC Risk |
|---|---|---|
| ✅ Match – Exact Match | All 6 fields match perfectly | None |
| 🔵 Match – Match with Tolerance | Taxable value within ₹1,000 or GST amount within ₹50 | Low |
| 🟡 Match – Fuzzy Match | Invoice number differs only in separators (`/`, `-`, `_`, spaces, etc.) | Low |
| 🟠 Probable Match | Fuzzy invoice number match but date or other field differs | Medium |
| 🔴 Mismatch | Place of Supply mismatch or significant value difference | High |
| 🟣 Addition in PR | Invoice in Purchase Register but missing from GSTR-2A | High — ITC at risk |
| 🩷 Addition in 2A | Invoice in GSTR-2A but missing from Purchase Register | Review required |

## Matching Logic

- **Invoice Number**: Fuzzy match by stripping `/`, `\`, `-`, `_`, `*`, `%`, `#`, `@`, and spaces before comparing
- **Invoice Date**: Normalized across formats — `DD-MM-YYYY`, `DD/MM/YYYY`, `YYYY-MM-DD`, `DD-Mon-YYYY`
- **Supplier GSTIN**: Exact match
- **Taxable Value**: Tolerance of ±₹1,000
- **GST Amount**: Tolerance of ±₹50
- **Place of Supply**: Hard mismatch — no tolerance applied

## Tech Stack

| Component | Technology |
|---|---|
| UI | Streamlit |
| AI Explanations | Google Gemini (`gemini-2.5-flash-lite`) |
| Data Processing | pandas |
| Excel Export | openpyxl |
| Language | Python 3 |

## Sample Output

The exported Excel report contains 3 sheets:
1. **Reconciliation Report** — All invoices with status and AI explanation, color-coded by status
2. **Summary** — Count per status with ITC risk level
3. **Exceptions** — Only mismatched/unmatched invoices for quick review

## How to Run

### Prerequisites
```bash
pip install streamlit pandas openpyxl google-genai
```

### Setup
1. Get a Gemini API key from [Google AI Studio](https://aistudio.google.com/)
2. Set the environment variable:
   ```bash
   set GEMINI_API_KEY=your_api_key_here   # Windows
   export GEMINI_API_KEY=your_api_key_here  # Mac/Linux
   ```

### Run
```bash
python -m streamlit run gstr2a_recon_app.py
```

Open `http://localhost:8501` in your browser, upload the two CSV files, and click **Run Reconciliation**.

## Sample Data

Sample files are included to test the app:
- `gstr2a.csv` — 22 rows with deliberate mismatches (fuzzy invoice numbers, date format differences, Place of Supply mismatches, additions)
- `purchase_register.csv` — 20 rows as the internal register

## CSV Format

Both files must have these columns (case-sensitive):

```
invoice_number, invoice_date, supplier_gstin, supplier_name, taxable_value, gst_amount, place_of_supply
```

## Author

**Ravi Sehgal** | Chartered Accountant & Finance Systems PM  
[LinkedIn](https://www.linkedin.com/in/ravisehgal) · [GitHub](https://github.com/Ravi4Sehgal)

import streamlit as st
import pandas as pd
import re
import io
import os
from google import genai

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="GSTR-2A Reconciliation", page_icon="🧾", layout="wide")
st.title("🧾 GSTR-2A vs Purchase Register Reconciliation")
st.caption("Upload both files to run AI-powered reconciliation with mismatch explanations")

# ── Gemini client ─────────────────────────────────────────────────────────────
@st.cache_resource
def get_gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY", "YOUR_KEY_HERE")
    return genai.Client(api_key=api_key)

# ── Helper: normalize invoice number ─────────────────────────────────────────
STRIP_CHARS = re.compile(r"[/\-_*%#@ ]")

def normalize_inv(inv: str) -> str:
    if pd.isna(inv):
        return ""
    return STRIP_CHARS.sub("", str(inv)).upper().strip()

# ── Helper: normalize date to DD-MM-YYYY ─────────────────────────────────────
def normalize_date(d) -> str:
    if pd.isna(d):
        return ""
    d = str(d).strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y"):
        try:
            from datetime import datetime
            return datetime.strptime(d, fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return d  # return as-is if no format matched

# ── Matching logic ────────────────────────────────────────────────────────────
TAXABLE_TOL = 1000
GST_TOL = 50

def get_recon_status(pr_row, gst_row):
    """
    Compare one PR row against one matched GSTR-2A row.
    Returns: (status_string, mismatch_fields_list)
    """
    mismatches = []

    # Normalize dates
    pr_date  = normalize_date(pr_row["invoice_date"])
    gst_date = normalize_date(gst_row["invoice_date"])

    # Normalize invoice numbers
    pr_inv  = normalize_inv(pr_row["invoice_number"])
    gst_inv = normalize_inv(gst_row["invoice_number"])

    exact_inv   = str(pr_row["invoice_number"]).strip().upper() == str(gst_row["invoice_number"]).strip().upper()
    fuzzy_inv   = pr_inv == gst_inv          # after stripping special chars
    probable_inv = fuzzy_inv                 # same logic, different tier based on other fields

    date_match   = pr_date == gst_date
    gstin_match  = str(pr_row["supplier_gstin"]).strip() == str(gst_row["supplier_gstin"]).strip()
    pos_match    = str(pr_row["place_of_supply"]).strip() == str(gst_row["place_of_supply"]).strip()

    taxable_diff = abs(float(pr_row["taxable_value"]) - float(gst_row["taxable_value"]))
    gst_diff     = abs(float(pr_row["gst_amount"])    - float(gst_row["gst_amount"]))
    taxable_tol  = taxable_diff <= TAXABLE_TOL
    gst_tol      = gst_diff     <= GST_TOL
    amount_exact = taxable_diff == 0 and gst_diff == 0

    if not date_match:   mismatches.append(f"Date: PR={pr_date} vs 2A={gst_date}")
    if not gstin_match:  mismatches.append(f"GSTIN: PR={pr_row['supplier_gstin']} vs 2A={gst_row['supplier_gstin']}")
    if not pos_match:    mismatches.append(f"Place of Supply: PR={pr_row['place_of_supply']} vs 2A={gst_row['place_of_supply']}")
    if taxable_diff > 0: mismatches.append(f"Taxable Value: PR=₹{pr_row['taxable_value']} vs 2A=₹{gst_row['taxable_value']} (diff ₹{taxable_diff:.2f})")
    if gst_diff > 0:     mismatches.append(f"GST Amount: PR=₹{pr_row['gst_amount']} vs 2A=₹{gst_row['gst_amount']} (diff ₹{gst_diff:.2f})")

    # ── Tier 1: Exact Match ───────────────────────────────────────────────────
    if exact_inv and not mismatches:
        return "Match - Exact Match", []

    # ── Tier 2: Match with Tolerance ─────────────────────────────────────────
    if exact_inv and gstin_match and date_match and pos_match and taxable_tol and gst_tol and not (amount_exact):
        return "Match - Match with Tolerance", mismatches

    # ── Tier 3: Fuzzy Match ───────────────────────────────────────────────────
    # Invoice# matches after cleaning, date format difference or minor special char
    inv_had_special = not exact_inv and fuzzy_inv
    if fuzzy_inv and gstin_match and pos_match and taxable_tol and gst_tol:
        if inv_had_special or not date_match:
            return "Match - Fuzzy Match", mismatches

    # ── Place of Supply hard mismatch ─────────────────────────────────────────
    if not pos_match:
        return "Mismatch", mismatches

    # ── Tier 4: Probable Match ────────────────────────────────────────────────
    # Invoice# matches after removing special chars (already covered in fuzzy above)
    # Catches cases where amounts differ too much for tolerance
    if fuzzy_inv and not exact_inv:
        return "Probable Match", mismatches

    # ── Tier 5: Mismatch ─────────────────────────────────────────────────────
    return "Mismatch", mismatches


# ── AI explanation ────────────────────────────────────────────────────────────
EXPLAIN_PROMPT = """
You are a GST compliance analyst. Given a GSTR-2A reconciliation result, write ONE clear sentence
explaining the issue in plain English for a finance manager.

Details:
- Invoice Number (PR): {pr_inv}
- Invoice Number (2A): {gst_inv}
- Supplier: {supplier}
- Reconciliation Status: {status}
- Mismatched Fields: {mismatches}

One sentence only. Be specific with field names and values. Start with the invoice number.
"""

def explain_mismatch(client, row: dict) -> str:
    if row["recon_status"] == "Match - Exact Match":
        return "All fields match exactly."
    prompt = EXPLAIN_PROMPT.format(
        pr_inv=row.get("pr_invoice_number", ""),
        gst_inv=row.get("gst_invoice_number", ""),
        supplier=row.get("supplier_name", ""),
        status=row["recon_status"],
        mismatches=row.get("mismatch_details", "")
    )
    response = client.models.generate_content(model="gemini-3.5-flash-lite", contents=prompt)
    return response.text.strip()


# ── Main reconciliation engine ────────────────────────────────────────────────
def reconcile(pr_df: pd.DataFrame, gst_df: pd.DataFrame, client) -> pd.DataFrame:
    results = []
    matched_gst_idx = set()

    pr_df  = pr_df.copy()
    gst_df = gst_df.copy()

    # Index GSTR-2A by normalized invoice number for fast lookup
    gst_df["_norm_inv"] = gst_df["invoice_number"].apply(normalize_inv)
    pr_df["_norm_inv"]  = pr_df["invoice_number"].apply(normalize_inv)

    for _, pr in pr_df.iterrows():
        pr_norm = pr["_norm_inv"]

        # Try exact match first, then normalized match
        gst_exact = gst_df[gst_df["invoice_number"].str.strip().str.upper() == str(pr["invoice_number"]).strip().upper()]
        gst_fuzzy  = gst_df[gst_df["_norm_inv"] == pr_norm]

        matched_gst = gst_exact if not gst_exact.empty else gst_fuzzy

        if matched_gst.empty:
            # Addition in PR
            results.append({
                "pr_invoice_number":  pr["invoice_number"],
                "gst_invoice_number": None,
                "invoice_date_pr":    pr["invoice_date"],
                "invoice_date_2a":    None,
                "supplier_name":      pr["supplier_name"],
                "supplier_gstin_pr":  pr["supplier_gstin"],
                "supplier_gstin_2a":  None,
                "taxable_value_pr":   pr["taxable_value"],
                "taxable_value_2a":   None,
                "gst_amount_pr":      pr["gst_amount"],
                "gst_amount_2a":      None,
                "place_of_supply_pr": pr["place_of_supply"],
                "place_of_supply_2a": None,
                "recon_status":       "Addition in PR",
                "mismatch_details":   "Invoice exists in Purchase Register but not found in GSTR-2A",
                "ai_explanation":     ""
            })
            continue

        gst = matched_gst.iloc[0]
        matched_gst_idx.add(gst.name)

        status, mismatch_fields = get_recon_status(pr, gst)

        row = {
            "pr_invoice_number":  pr["invoice_number"],
            "gst_invoice_number": gst["invoice_number"],
            "invoice_date_pr":    pr["invoice_date"],
            "invoice_date_2a":    gst["invoice_date"],
            "supplier_name":      pr["supplier_name"],
            "supplier_gstin_pr":  pr["supplier_gstin"],
            "supplier_gstin_2a":  gst["supplier_gstin"],
            "taxable_value_pr":   pr["taxable_value"],
            "taxable_value_2a":   gst["taxable_value"],
            "gst_amount_pr":      pr["gst_amount"],
            "gst_amount_2a":      gst["gst_amount"],
            "place_of_supply_pr": pr["place_of_supply"],
            "place_of_supply_2a": gst["place_of_supply"],
            "recon_status":       status,
            "mismatch_details":   "; ".join(mismatch_fields),
            "ai_explanation":     ""
        }
        results.append(row)

    # Addition in 2A — GSTR-2A rows not matched to any PR row
    for idx, gst in gst_df.iterrows():
        if idx not in matched_gst_idx:
            results.append({
                "pr_invoice_number":  None,
                "gst_invoice_number": gst["invoice_number"],
                "invoice_date_pr":    None,
                "invoice_date_2a":    gst["invoice_date"],
                "supplier_name":      gst["supplier_name"],
                "supplier_gstin_pr":  None,
                "supplier_gstin_2a":  gst["supplier_gstin"],
                "taxable_value_pr":   None,
                "taxable_value_2a":   gst["taxable_value"],
                "gst_amount_pr":      None,
                "gst_amount_2a":      gst["gst_amount"],
                "place_of_supply_pr": None,
                "place_of_supply_2a": gst["place_of_supply"],
                "recon_status":       "Addition in 2A",
                "mismatch_details":   "Invoice exists in GSTR-2A but not found in Purchase Register",
                "ai_explanation":     ""
            })

    result_df = pd.DataFrame(results)

    # Generate AI explanations for non-exact matches
    non_exact = result_df["recon_status"] != "Match - Exact Match"
    total = non_exact.sum()
    if total > 0:
        progress = st.progress(0, text="Generating AI explanations...")
        for i, idx in enumerate(result_df[non_exact].index):
            result_df.at[idx, "ai_explanation"] = explain_mismatch(client, result_df.loc[idx].to_dict())
            progress.progress((i + 1) / total, text=f"AI explanations: {i+1}/{total}")
        progress.empty()

    # Exact matches get a simple note
    result_df.loc[result_df["recon_status"] == "Match - Exact Match", "ai_explanation"] = "All fields match exactly."

    return result_df


# ── Status color map ──────────────────────────────────────────────────────────
STATUS_COLORS = {
    "Match - Exact Match":          "#d4edda",
    "Match - Match with Tolerance": "#cce5ff",
    "Match - Fuzzy Match":          "#fff3cd",
    "Probable Match":               "#fde8d8",
    "Mismatch":                     "#f8d7da",
    "Addition in PR":               "#e2d9f3",
    "Addition in 2A":               "#fddde6",
}

def color_row(row):
    color = STATUS_COLORS.get(row["recon_status"], "#ffffff")
    return [f"background-color: {color}"] * len(row)


# ── Excel export ──────────────────────────────────────────────────────────────
def to_excel(df: pd.DataFrame) -> bytes:
    import openpyxl
    from openpyxl.styles import PatternFill, Font, Alignment
    from openpyxl.utils import get_column_letter

    COLOR_MAP = {
        "Match - Exact Match":          "D4EDDA",
        "Match - Match with Tolerance": "CCE5FF",
        "Match - Fuzzy Match":          "FFF3CD",
        "Probable Match":               "FDE8D8",
        "Mismatch":                     "F8D7DA",
        "Addition in PR":               "E2D9F3",
        "Addition in 2A":               "FDDDE6",
    }

    wb = openpyxl.Workbook()

    # ── Sheet 1: Full Reconciliation ──────────────────────────────────────────
    ws1 = wb.active
    ws1.title = "Reconciliation Report"
    headers = list(df.columns)
    ws1.append(headers)

    # Header row styling
    for col_num, header in enumerate(headers, 1):
        cell = ws1.cell(row=1, column=col_num)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2F4F4F")
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    for _, row in df.iterrows():
        ws1.append([row.get(h) for h in headers])

    # Color rows by status
    for row_num in range(2, len(df) + 2):
        status = ws1.cell(row=row_num, column=headers.index("recon_status") + 1).value
        fill_color = COLOR_MAP.get(status, "FFFFFF")
        fill = PatternFill("solid", fgColor=fill_color)
        for col_num in range(1, len(headers) + 1):
            ws1.cell(row=row_num, column=col_num).fill = fill

    # Auto-width columns
    for col_num, col in enumerate(ws1.columns, 1):
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws1.column_dimensions[get_column_letter(col_num)].width = min(max_len + 4, 40)

    # ── Sheet 2: Summary ──────────────────────────────────────────────────────
    ws2 = wb.create_sheet("Summary")
    summary = df["recon_status"].value_counts().reset_index()
    summary.columns = ["Status", "Count"]
    summary["ITC Risk"] = summary["Status"].map({
        "Match - Exact Match":          "None",
        "Match - Match with Tolerance": "Low",
        "Match - Fuzzy Match":          "Low",
        "Probable Match":               "Medium",
        "Mismatch":                     "High",
        "Addition in PR":               "High — ITC at risk",
        "Addition in 2A":               "Review required",
    })
    ws2.append(["Status", "Count", "ITC Risk"])
    for _, row in summary.iterrows():
        ws2.append([row["Status"], row["Count"], row["ITC Risk"]])

    # ── Sheet 3: Exceptions only ──────────────────────────────────────────────
    ws3 = wb.create_sheet("Exceptions")
    exceptions = df[df["recon_status"].isin(["Mismatch", "Addition in PR", "Addition in 2A", "Probable Match"])]
    exc_headers = ["pr_invoice_number", "gst_invoice_number", "supplier_name",
                   "recon_status", "mismatch_details", "ai_explanation"]
    ws3.append(exc_headers)
    for _, row in exceptions.iterrows():
        ws3.append([row.get(h) for h in exc_headers])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ── Streamlit UI ──────────────────────────────────────────────────────────────
st.markdown("---")
col1, col2 = st.columns(2)
with col1:
    gst_file = st.file_uploader("📂 Upload GSTR-2A file (CSV)", type=["csv"], key="gst")
with col2:
    pr_file  = st.file_uploader("📂 Upload Purchase Register (CSV)", type=["csv"], key="pr")

api_key = st.text_input("🔑 Gemini API Key", type="password",
                         value=os.environ.get("GEMINI_API_KEY", ""),
                         placeholder="Paste your Gemini API key here")

run_btn = st.button("▶ Run Reconciliation", type="primary", disabled=not (gst_file and pr_file and api_key))

if run_btn:
    os.environ["GEMINI_API_KEY"] = api_key
    client = genai.Client(api_key=api_key)

    with st.spinner("Reading files..."):
        gst_df = pd.read_csv(gst_file)
        pr_df  = pd.read_csv(pr_file)

    st.success(f"✓ GSTR-2A: {len(gst_df)} rows | Purchase Register: {len(pr_df)} rows")

    with st.spinner("Running reconciliation..."):
        result_df = reconcile(pr_df, gst_df, client)

    # ── Summary metrics ───────────────────────────────────────────────────────
    st.markdown("### 📊 Reconciliation Summary")
    status_counts = result_df["recon_status"].value_counts()

    m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
    m1.metric("✅ Exact Match",     status_counts.get("Match - Exact Match", 0))
    m2.metric("🔵 With Tolerance",  status_counts.get("Match - Match with Tolerance", 0))
    m3.metric("🟡 Fuzzy Match",     status_counts.get("Match - Fuzzy Match", 0))
    m4.metric("🟠 Probable Match",  status_counts.get("Probable Match", 0))
    m5.metric("🔴 Mismatch",        status_counts.get("Mismatch", 0))
    m6.metric("🟣 Addition in PR",  status_counts.get("Addition in PR", 0))
    m7.metric("🩷 Addition in 2A",  status_counts.get("Addition in 2A", 0))

    # ── Filter ────────────────────────────────────────────────────────────────
    st.markdown("### 📋 Detailed Results")
    filter_status = st.multiselect(
        "Filter by status:",
        options=result_df["recon_status"].unique().tolist(),
        default=result_df["recon_status"].unique().tolist()
    )
    filtered_df = result_df[result_df["recon_status"].isin(filter_status)]

    st.dataframe(
        filtered_df.style.apply(color_row, axis=1),
        use_container_width=True,
        height=500
    )

    # ── Download ──────────────────────────────────────────────────────────────
    st.markdown("### 📥 Download Report")
    excel_bytes = to_excel(result_df)
    st.download_button(
        label="⬇ Download Reconciliation Report (Excel)",
        data=excel_bytes,
        file_name="gstr2a_reconciliation_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    st.success("✓ Reconciliation complete!")

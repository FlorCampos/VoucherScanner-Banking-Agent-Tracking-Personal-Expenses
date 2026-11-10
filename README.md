# 📸 Voucher Scanner

Streamlit app that captures or uploads a photo of a voucher/receipt, extracts its line items with **Gemini 2.5 Flash** (vision + structured outputs), and automatically logs them to a **Google Sheets** spreadsheet, one row per product.

## Architecture (Phase 1)

```
Image (camera / upload)
        │
        ▼
Gemini 2.5 Flash  ──  response_schema = Voucher (Pydantic)
        │
        ▼
Voucher (validated object: merchant, date, payment, list of items)
        │
        ▼
One row per product
        │
        ▼
Google Sheets (gspread + google-auth)
```

- **OCR/Extraction**: `google-genai`, model `gemini-2.5-flash`, with `response_mime_type="application/json"` and `response_schema=Voucher`. The model is forced to return JSON that satisfies the `Voucher` Pydantic schema; if it doesn't, `response.parsed` is `None` and the app treats it as an explicit error (no manual text parsing).
- **Persistence**: `gspread.service_account_from_dict(...)`, authenticated with `google-auth` (not `oauth2client`, which is deprecated).
- **Secrets**: managed exclusively via `st.secrets` (Streamlit secrets), never files on disk.

## Spreadsheet columns

Each scanned voucher is shown as JSON (one object per product) and appended to the worksheet with one row per product, in this column order (the app does not write a header row):

| # | Column | Notes |
|---|---|---|
| 1 | Merchant | Same value on every row of the voucher |
| 2 | Product | Exactly as printed on the voucher (not translated), with quantity when shown |
| 3 | Price | Before discount |
| 4 | Discount | `0` when the product had no discount |
| 5 | Price after discount | Price minus discount, computed by the app |
| 6 | Purchase date | `DD/MM/YYYY` |
| 7 | Month | In English, e.g. `January` |
| 8 | Tax | Tax for that product |
| 9 | Category | Per product |
| 10 | Payment method | `cash`, `credit`, `debit` or `other` |
| 11 | Card brand | `Visa`, `Mastercard` or `Other`; empty for cash |
| 12 | Card number | Last 4 digits after 12 asterisks, e.g. `************1234`; empty for cash |

## Local setup

1. Create and activate a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   # to run tests:
   pip install -r requirements-dev.txt
   ```
3. Create `.streamlit/secrets.toml` (this file is in `.gitignore`, never committed). You can store `GOOGLE_CREDENTIALS` either as a TOML table or as a raw JSON string:
   ```toml
   GEMINI_KEY = "your-gemini-api-key"

   [GOOGLE_CREDENTIALS]
   type = "service_account"
   project_id = "..."
   private_key = """-----BEGIN PRIVATE KEY-----
   ...
   -----END PRIVATE KEY-----"""
   client_email = "..."
   token_uri = "https://oauth2.googleapis.com/token"
   # ...remaining fields from the downloaded JSON key
   ```
   - `GEMINI_KEY`: API key from [Google AI Studio](https://aistudio.google.com/).
   - `GOOGLE_CREDENTIALS`: the full Google Cloud service account JSON, with **Editor** access to the target spreadsheet (share the sheet with the service account's `client_email`). Paste the downloaded JSON file's values as-is — avoid retyping the `private_key` by hand, since a single dropped or altered character breaks the PEM.
4. Run the app:
   ```bash
   streamlit run app.py
   ```

## Tests

```bash
pytest tests/ -v
```

Covers the `Voucher` schema (required fields/type validation), the row building and card masking (`build_sheet_rows`, `mask_card_number`), and the `extract_voucher_data` and `save_to_google_sheets` functions (mocking the Gemini and Sheets clients, no real API calls).

## Security

- No secret lives in the repository. `credentials.json` and `.streamlit/secrets.toml` are excluded via `.gitignore`.
- If a service account key is ever exposed on disk or in a commit, **rotate it immediately** from Google Cloud Console (IAM → Service Accounts → Keys) — don't wait for confirmation that it was actually leaked.

## Roadmap (upcoming phases)

- **Phase 2 — Robustness**: retries with backoff for transient Gemini API errors; image size/validation limits before sending to the model; rate-limit handling.
- **Phase 3 — Observability**: structured (JSON) logging sent to an external system (e.g. Cloud Logging) instead of just `st.error`; extraction success-rate metrics.
- **Phase 4 — Data scalability**: migrate from Google Sheets to a database (Postgres/SQLite) behind a service layer, keeping Sheets as an optional view/export.
- **Phase 5 — Multi-user**: user authentication in the app and per-user sheet/record separation.

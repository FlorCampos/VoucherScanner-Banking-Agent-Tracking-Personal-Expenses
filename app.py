import json
import logging
import re

import gspread
import streamlit as st
from PIL import Image
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from gspread.exceptions import APIError as GspreadAPIError
from gspread.exceptions import SpreadsheetNotFound, WorksheetNotFound
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voucher_scanner")

GEMINI_MODEL = "gemini-2.5-flash"
SHEET_NAME = "2.- MONTHLY BUDGET"
WORKSHEET_NAME = "Micro-expenses-2026"
SHEETS_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

PROMPT = (
    "Analyze this image of a purchase voucher/receipt and extract the "
    "information requested in the schema. If a field does not appear in "
    "the image, use your best estimate or a reasonable default value."
)


class Voucher(BaseModel):
    merchant: str = Field(description="Name of the merchant or establishment")
    purchase_date: str = Field(description="Voucher date in DD/MM/YYYY format")
    month_name: str = Field(description="Month name in English, e.g. January")
    total_amount: float = Field(description="Total amount paid")
    tax_amount: float = Field(default=0.0, description="Tax amount (VAT/sales tax), if applicable")
    suggested_category: str = Field(
        description="Suggested expense category, e.g. Food, Transport, Other"
    )


def get_gemini_client() -> genai.Client:
    return genai.Client(api_key=st.secrets["GEMINI_KEY"])


_PEM_HEADER = "-----BEGIN PRIVATE KEY-----"
_PEM_FOOTER = "-----END PRIVATE KEY-----"
_PEM_BLOCK_RE = re.compile(
    re.escape(_PEM_HEADER) + r"(.*?)" + re.escape(_PEM_FOOTER), re.DOTALL
)


def _normalize_private_key(private_key: str) -> str:
    """Rebuild a canonical PEM from a key that may have been mangled when
    pasted into secrets.toml (lost line breaks, literal "\\n", CRLF,
    indentation whitespace, etc.)."""
    key = private_key.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    match = _PEM_BLOCK_RE.search(key)
    if not match:
        return key
    body = "".join(match.group(1).split())
    wrapped_body = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return f"{_PEM_HEADER}\n{wrapped_body}\n{_PEM_FOOTER}\n"


def get_sheets_client() -> gspread.Client:
    raw_creds = st.secrets["GOOGLE_CREDENTIALS"]
    if isinstance(raw_creds, str):
        creds_dict = json.loads(raw_creds)
    else:
        creds_dict = dict(raw_creds)

    private_key = creds_dict.get("private_key")
    if isinstance(private_key, str):
        creds_dict["private_key"] = _normalize_private_key(private_key)

    return gspread.service_account_from_dict(creds_dict, scopes=SHEETS_SCOPES)


def extract_voucher_data(client: genai.Client, img: Image.Image) -> Voucher:
    """Call Gemini requesting a structured output validated against the Voucher schema."""
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[PROMPT, img],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Voucher,
        ),
    )
    if response.parsed is None:
        raise ValueError("The AI did not return valid JSON matching the expected schema.")
    return response.parsed


def save_to_google_sheets(gc: gspread.Client, voucher: Voucher) -> None:
    sh = gc.open(SHEET_NAME).worksheet(WORKSHEET_NAME)
    row = [
        voucher.merchant,
        voucher.purchase_date,
        voucher.month_name,
        voucher.total_amount,
        voucher.tax_amount,
        voucher.suggested_category,
    ]
    sh.append_row(row)


def main() -> None:
    st.set_page_config(page_title="Voucher Scanner Pro", page_icon="📸")
    st.title("📸 Voucher Scanner Pro")
    st.write("Capture or upload your voucher to save it to your budget.")

    if "scan_counter" not in st.session_state:
        st.session_state.scan_counter = 0

    tab1, tab2 = st.tabs(["📸 Camera", "📁 Upload File"])
    with tab1:
        camera_photo = st.camera_input(
            "Take a photo", key=f"camera_{st.session_state.scan_counter}"
        )
    with tab2:
        uploaded_photo = st.file_uploader("Select an image", type=["jpg", "jpeg", "png"])

    photo = camera_photo or uploaded_photo
    if not photo:
        return

    try:
        img = Image.open(photo)
    except Exception:
        logger.exception("Uploaded file is not a valid image")
        st.error("⚠️ The file does not appear to be a valid image. Upload a JPG or PNG.")
        return

    st.image(img, caption="Image ready to process", use_container_width=True)

    if not st.button("🚀 Process and Save"):
        return

    with st.spinner("The AI is reading the voucher..."):
        try:
            gemini_client = get_gemini_client()
            voucher = extract_voucher_data(gemini_client, img)
        except genai_errors.APIError as exc:
            logger.exception("Gemini API call failed")
            st.error(f"⚠️ Error communicating with Gemini: {exc}")
            return
        except ValueError as exc:
            logger.exception("Gemini response did not match the schema")
            st.error(f"⚠️ {exc} Try again with a clearer, sharper photo.")
            return
        except KeyError:
            logger.exception("Missing GEMINI_KEY secret")
            st.error("⚠️ The Gemini API key is not configured in the app secrets.")
            return
        except Exception:
            logger.exception("Unexpected error calling Gemini")
            st.error("⚠️ An unexpected error occurred while reading the voucher. Please try again.")
            return

        st.json(voucher.model_dump())

        try:
            sheets_client = get_sheets_client()
            save_to_google_sheets(sheets_client, voucher)
        except SpreadsheetNotFound:
            logger.exception("Spreadsheet not found")
            st.error(
                f"⚠️ Spreadsheet '{SHEET_NAME}' was not found. Check the name "
                "and that the service account has access."
            )
            return
        except WorksheetNotFound:
            logger.exception("Worksheet not found")
            st.error(f"⚠️ Worksheet '{WORKSHEET_NAME}' was not found in '{SHEET_NAME}'.")
            return
        except GspreadAPIError as exc:
            logger.exception("Google Sheets API call failed")
            st.error(f"⚠️ Error saving to Google Sheets: {exc}")
            return
        except (json.JSONDecodeError, KeyError):
            logger.exception("Google credentials misconfigured in st.secrets")
            st.error(
                "⚠️ Google Sheets credentials are not configured correctly "
                "in the app secrets."
            )
            return
        except Exception:
            logger.exception("Unexpected error saving to Google Sheets")
            st.error("⚠️ An unexpected error occurred while saving to Google Sheets.")
            return

        st.success("✅ Data saved to Google Sheets successfully!")
        st.balloons()
        if st.button("🔄 Scan another voucher"):
            st.session_state.scan_counter += 1
            st.rerun()


if __name__ == "__main__":
    main()

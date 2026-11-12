import json
import logging
import re
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import Enum

import gspread
import streamlit as st
from PIL import Image, ImageOps
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
# One spreadsheet row is written per product, in this column order.
SHEET_COLUMNS = [
    "Merchant",
    "Product",
    "Price",
    "Discount",
    "Price after discount",
    "Tax",
    "Final price",
    "Purchase date",
    "Month",
    "Category",
    "Payment method",
    "Card brand",
    "Card number",
]
CARD_MASK_LENGTH = 12
CENT = Decimal("0.01")
DATE_FORMAT = "%d/%m/%Y"
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

PROMPT = (
    "Analyze this image of a purchase voucher/receipt and extract the "
    "information requested in the schema, with one entry in `items` per "
    "product purchased. If a field does not appear in the image, use your "
    "best estimate or a reasonable default value, unless the field's "
    "description says otherwise."
)


class PaymentMethod(str, Enum):
    CASH = "cash"
    CREDIT = "credit"
    DEBIT = "debit"
    OTHER = "other"


class CardBrand(str, Enum):
    VISA = "Visa"
    MASTERCARD = "Mastercard"
    OTHER = "Other"


class VoucherItem(BaseModel):
    product: str = Field(
        description=(
            "Product name exactly as printed on the voucher: same language, wording "
            "and abbreviations, without translating or rewriting it. Include its "
            "quantity or size when the voucher shows one"
        )
    )
    price_after_discount: float = Field(
        description=(
            "Amount charged for this product before tax and after any discount, i.e. "
            "the amount that counts toward the voucher subtotal. When a savings line "
            "printed below the product is only informational (e.g. 'RABAIS SUR LE PRIX "
            "COURANT: $0.77' under a product printed at $1.22), the printed price is "
            "already discounted, so use it as is ($1.22). Subtract a discount only "
            "when the voucher itself subtracts it as a separate negative amount. The "
            "amounts of all items must add up to the voucher subtotal"
        )
    )
    discount: float = Field(
        default=0.0,
        description=(
            "Amount saved on this product, as a positive number, taken from the "
            "savings or discount line printed with it. Report it even when that line "
            "is only informational and the printed price is already discounted (e.g. "
            "'RABAIS SUR LE PRIX COURANT: $0.77' means a discount of 0.77). 0 only if "
            "the voucher shows no savings for this product"
        ),
    )
    taxable: bool = Field(
        description=(
            "True only when the voucher marks this product as taxed, usually with a "
            "tax code printed next to its price (e.g. 'FP', 'F', 'P', 'T', 'TX'). False "
            "when the product has no such mark. If the voucher charges tax but marks "
            "no product, set true for the products that are normally taxed"
        )
    )
    category: str = Field(
        description="Expense category for this product, e.g. Food, Household, Health, Other"
    )


class TaxLine(BaseModel):
    name: str = Field(description="Tax name as printed on the voucher, e.g. TPS, TVQ, GST, HST")
    rate_percent: float | None = Field(
        default=None,
        description=(
            "Tax rate percentage printed on the voucher for this tax, e.g. 5 for "
            "'TPS (5%)' or 9.975 for 'TVQ (9.975%)'; null if the voucher does not "
            "print a rate. Never guess the rate"
        ),
    )
    amount: float = Field(description="Amount of this tax as printed on the voucher")


class Voucher(BaseModel):
    merchant: str = Field(
        description=(
            "Full merchant name exactly as printed at the top of the voucher, "
            "including the branch or store name and not only the brand. Do not "
            "shorten, translate or rewrite it"
        )
    )
    purchase_date: str = Field(description="Purchase date in DD/MM/YYYY format")
    month_name: str = Field(
        description="Month of purchase_date as its English name, e.g. January"
    )
    items: list[VoucherItem] = Field(
        description=(
            "One entry per product purchased. Do not create entries for discounts, "
            "taxes, subtotals or totals"
        )
    )
    taxes: list[TaxLine] = Field(
        description=(
            "One entry per tax line printed on the voucher (e.g. TPS and TVQ); empty "
            "if the voucher charges no tax"
        )
    )
    payment_method: PaymentMethod = Field(
        description="How the purchase was paid: cash, credit, debit or other"
    )
    card_brand: CardBrand | None = Field(
        default=None,
        description="Card network when paid by card; null if not paid by card or not shown",
    )
    card_last4: str | None = Field(
        default=None,
        description=(
            "Last 4 digits of the card exactly as printed on the voucher; null if "
            "not paid by card or not shown. Never guess these digits"
        ),
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
            temperature=0,
            response_mime_type="application/json",
            response_schema=Voucher,
        ),
    )
    if response.parsed is None:
        raise ValueError("The AI did not return valid JSON matching the expected schema.")
    if not response.parsed.items:
        raise ValueError("No products were found on the voucher.")
    return response.parsed


def mask_card_number(last4: str | None) -> str:
    """Format the card's last 4 digits as printed on vouchers: ************1234."""
    digits = re.sub(r"\D", "", last4 or "")
    if len(digits) < 4:
        return ""
    return "*" * CARD_MASK_LENGTH + digits[-4:]


def allocate_cents(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Split `total` in proportion to `weights` into whole cents that add up exactly to it."""
    weight_sum = sum(weights)
    if total <= 0 or weight_sum <= 0:
        return [Decimal("0.00")] * len(weights)
    exact = [total * weight / weight_sum for weight in weights]
    parts = [share.quantize(CENT, rounding=ROUND_DOWN) for share in exact]
    leftover_cents = int((total - sum(parts)) / CENT)
    by_largest_remainder = sorted(
        range(len(weights)), key=lambda i: exact[i] - parts[i], reverse=True
    )
    for i in by_largest_remainder[:leftover_cents]:
        parts[i] += CENT
    return parts


def compute_item_taxes(voucher: Voucher) -> list[float]:
    """Tax per product, using the rates printed on the voucher.

    Each tax is computed the way the cash register does it (rate applied to the
    total of the taxed products, rounded to the cent) and then split across those
    products, so the per-product taxes add up to the voucher's tax amounts. A tax
    line without a printed rate falls back to its printed amount.
    """
    taxable_prices = [
        Decimal(str(item.price_after_discount)) if item.taxable else Decimal(0)
        for item in voucher.items
    ]
    taxable_base = sum(taxable_prices)
    item_taxes = [Decimal("0.00")] * len(voucher.items)
    for tax in voucher.taxes:
        if tax.rate_percent is not None:
            tax_total = taxable_base * Decimal(str(tax.rate_percent)) / 100
        else:
            tax_total = Decimal(str(tax.amount))
        tax_total = tax_total.quantize(CENT, rounding=ROUND_HALF_UP)
        for i, share in enumerate(allocate_cents(tax_total, taxable_prices)):
            item_taxes[i] += share
    return [float(tax) for tax in item_taxes]


def resolve_purchase_date(raw_date: str, today: date) -> date | None:
    """Parse a DD/MM/YYYY date, fixing day/month mix-ups.

    Vouchers print dates in different orders (e.g. '11/05/2026' may mean 5
    November), so when day and month can be swapped this keeps the reading
    closest to `today` that is not in the future. Returns None if `raw_date` is
    not a valid DD/MM/YYYY date.
    """
    try:
        parsed = datetime.strptime(raw_date.strip(), DATE_FORMAT).date()
    except ValueError:
        return None
    candidates = [parsed]
    if parsed.day <= 12 and parsed.day != parsed.month:
        candidates.append(parsed.replace(day=parsed.month, month=parsed.day))
    not_in_future = [candidate for candidate in candidates if candidate <= today]
    return min(not_in_future or candidates, key=lambda candidate: abs((today - candidate).days))


def build_sheet_rows(voucher: Voucher, today: date | None = None) -> list[list[str | float]]:
    """Build one spreadsheet row per product, in SHEET_COLUMNS order."""
    paid_in_cash = voucher.payment_method is PaymentMethod.CASH
    card_brand = voucher.card_brand.value if voucher.card_brand and not paid_in_cash else ""
    card_number = "" if paid_in_cash else mask_card_number(voucher.card_last4)

    purchase_date, month_name = voucher.purchase_date, voucher.month_name
    resolved_date = resolve_purchase_date(voucher.purchase_date, today or date.today())
    if resolved_date:
        purchase_date = resolved_date.strftime(DATE_FORMAT)
        month_name = MONTH_NAMES[resolved_date.month - 1]

    rows = []
    for item, tax in zip(voucher.items, compute_item_taxes(voucher)):
        discount = abs(item.discount)
        rows.append(
            [
                voucher.merchant,
                item.product,
                round(item.price_after_discount + discount, 2),
                discount,
                item.price_after_discount,
                tax,
                round(item.price_after_discount + tax, 2),
                purchase_date,
                month_name,
                item.category,
                voucher.payment_method.value,
                card_brand,
                card_number,
            ]
        )
    return rows


def save_to_google_sheets(gc: gspread.Client, rows: list[list[str | float]]) -> None:
    sh = gc.open(SHEET_NAME).worksheet(WORKSHEET_NAME)
    sh.append_rows(rows)


def load_image(photo) -> Image.Image:
    """Open the photo upright, applying the orientation the phone stored in its EXIF data."""
    return ImageOps.exif_transpose(Image.open(photo))


def start_new_scan() -> None:
    """Clear the uploaded photo by giving the uploader a new key."""
    st.session_state.scan_counter += 1


def main() -> None:
    st.set_page_config(page_title="Voucher Scanner Pro", page_icon="📸")
    st.title("📸 Voucher Scanner Pro")
    st.write("Take or upload a photo of your voucher to save it to your budget.")

    if "scan_counter" not in st.session_state:
        st.session_state.scan_counter = 0

    photo = st.file_uploader(
        "Take or select a photo",
        type=["jpg", "jpeg", "png"],
        key=f"voucher_photo_{st.session_state.scan_counter}",
    )
    if not photo:
        return

    try:
        img = load_image(photo)
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

        rows = build_sheet_rows(voucher)
        st.json([dict(zip(SHEET_COLUMNS, row)) for row in rows])

        try:
            sheets_client = get_sheets_client()
            save_to_google_sheets(sheets_client, rows)
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
        st.button("🔄 Scan another voucher", on_click=start_new_scan)


if __name__ == "__main__":
    main()

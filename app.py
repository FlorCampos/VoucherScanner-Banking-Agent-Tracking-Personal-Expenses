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
from gspread.utils import absolute_range_name, rowcol_to_a1
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
# Background colors that alternate between consecutive vouchers in the sheet.
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
LIGHT_BLUE = {"red": 221 / 255, "green": 235 / 255, "blue": 247 / 255}
RED = {"red": 0.8, "green": 0.0, "blue": 0.0}
# The voucher total goes in the column right after SHEET_COLUMNS, once per voucher.
VOUCHER_TOTAL_COLUMN = len(SHEET_COLUMNS) + 1
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
MULTI_PHOTO_NOTE = (
    "The images are photos of different parts of the same voucher and may "
    "overlap. Treat them as one voucher. A product line that is visible in more "
    "than one photo is the same line and must be listed only once, but identical "
    "lines that the voucher prints separately are separate products and must "
    "each be listed."
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
    tax_code: str | None = Field(
        default=None,
        description=(
            "Tax code printed on this product's line, usually right after its price, "
            "copied exactly as printed (e.g. 'FP', 'F', 'P', 'T', 'TX'). null when "
            "nothing is printed after the price; most grocery products have none. Copy "
            "what is printed, do not infer it from the kind of product. Check your "
            "answer: the prices of the products that have a tax code, multiplied by "
            "the tax rates, must give the tax amounts printed on the voucher"
        ),
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
            "taxes, subtotals, totals or zero-amount lines that are not products (e.g. "
            "promotions or loyalty stamps)"
        )
    )
    taxes: list[TaxLine] = Field(
        description=(
            "One entry per tax line printed on the voucher (e.g. TPS and TVQ); empty "
            "if the voucher charges no tax"
        )
    )
    total_amount: float = Field(
        description=(
            "Final total charged on the voucher, taxes included, as printed next to "
            "the total (e.g. 'TOTAL'). Never compute it yourself"
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


def extract_voucher_data(client: genai.Client, images: list[Image.Image]) -> Voucher:
    """Call Gemini requesting a structured output validated against the Voucher schema.

    `images` holds one or more photos of the same voucher.
    """
    prompt = f"{PROMPT} {MULTI_PHOTO_NOTE}" if len(images) > 1 else PROMPT
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[prompt, *images],
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=Voucher,
        ),
    )
    voucher = response.parsed
    if voucher is None:
        raise ValueError("The AI did not return valid JSON matching the expected schema.")
    # Zero-amount lines (promotions, loyalty stamps) are not purchases.
    voucher.items = [
        item for item in voucher.items if item.price_after_discount or item.discount
    ]
    if not voucher.items:
        raise ValueError("No products were found on the voucher.")
    return voucher


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

    The taxed products are the ones with a tax code printed on their line. If the
    voucher charges tax but prints no codes at all (e.g. a restaurant bill), every
    product is treated as taxed.
    """
    taxed = [bool((item.tax_code or "").strip()) for item in voucher.items]
    if not any(taxed) and any(tax.amount > 0 for tax in voucher.taxes):
        taxed = [True] * len(voucher.items)
    taxable_prices = [
        Decimal(str(item.price_after_discount)) if is_taxed else Decimal(0)
        for item, is_taxed in zip(voucher.items, taxed)
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


def sum_column(rows: list[list[str | float]], column: str) -> float:
    """Add up one numeric column of the rows (e.g. 'Final price'), exact to the cent."""
    index = SHEET_COLUMNS.index(column)
    return float(sum(Decimal(str(row[index])) for row in rows))


def totals_match(rows_total: float, voucher_total: float) -> bool:
    """True when both amounts are the same to the cent."""
    return round(rows_total * 100) == round(voucher_total * 100)


def parse_row_span(updated_range: str) -> tuple[int, int]:
    """First and last row number of an A1 range such as "'Sheet'!A115:M124"."""
    cells = updated_range.rsplit("!", 1)[-1].replace("$", "")
    match = re.fullmatch(r"[A-Z]+(\d+)(?::[A-Z]+(\d+))?", cells)
    if not match:
        raise ValueError(f"Unexpected range: {updated_range!r}")
    first_row = int(match.group(1))
    return first_row, int(match.group(2) or first_row)


def is_same_color(color: dict, other: dict) -> bool:
    """Compare two Sheets API colors; a missing channel means 0."""
    return all(
        abs(color.get(channel, 0.0) - other.get(channel, 0.0)) <= 0.01
        for channel in ("red", "green", "blue")
    )


def next_band_color(previous_color: dict | None) -> dict:
    """Color for a new voucher: the opposite of the voucher above it."""
    if previous_color is not None and is_same_color(previous_color, LIGHT_BLUE):
        return WHITE
    return LIGHT_BLUE


def read_row_color(worksheet: gspread.Worksheet, row: int) -> dict | None:
    """Background color of column A in `row`, or None if it cannot be read."""
    if row < 1:
        return None
    metadata = worksheet.spreadsheet.fetch_sheet_metadata(
        params={
            "includeGridData": "true",
            "ranges": absolute_range_name(worksheet.title, f"A{row}"),
            "fields": "sheets.data.rowData.values.effectiveFormat.backgroundColor",
        }
    )
    try:
        cell = metadata["sheets"][0]["data"][0]["rowData"][0]["values"][0]
        return cell["effectiveFormat"]["backgroundColor"]
    except (KeyError, IndexError, TypeError):
        return None


def format_voucher_block(
    worksheet: gspread.Worksheet, first_row: int, last_row: int, total_matches: bool = True
) -> None:
    """Format a voucher's rows as one block.

    The rows get the background color the voucher above does not have, and the
    voucher total becomes a single centered cell spanning all of them. A total
    that does not match the sum of the rows is shown in bold red.
    """
    color = next_band_color(read_row_color(worksheet, first_row - 1))
    total_top = rowcol_to_a1(first_row, VOUCHER_TOTAL_COLUMN)
    total_bottom = rowcol_to_a1(last_row, VOUCHER_TOTAL_COLUMN)
    total_cells = f"{total_top}:{total_bottom}"
    if last_row > first_row:
        worksheet.merge_cells(total_cells)

    total_format: dict = {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}
    if not total_matches:
        total_format["textFormat"] = {"bold": True, "foregroundColor": RED}
    worksheet.batch_format(
        [
            {"range": f"A{first_row}:{total_bottom}", "format": {"backgroundColor": color}},
            {"range": total_cells, "format": total_format},
        ]
    )


def save_to_google_sheets(
    gc: gspread.Client,
    rows: list[list[str | float]],
    voucher_total: float,
    total_matches: bool = True,
) -> bool:
    """Append the voucher's rows with its total and format them as one block.

    The total printed on the voucher is written once, next to the first row, and
    then merged across the voucher's rows. Consecutive vouchers alternate between
    two background colors so it is easy to see where each one starts and ends.
    Returns False when the rows were saved but could not be formatted.
    """
    sh = gc.open(SHEET_NAME).worksheet(WORKSHEET_NAME)
    sheet_rows = [
        [*row, voucher_total if index == 0 else ""] for index, row in enumerate(rows)
    ]
    response = sh.append_rows(sheet_rows)
    try:
        first_row, last_row = parse_row_span(response["updates"]["updatedRange"])
        format_voucher_block(sh, first_row, last_row, total_matches)
    except Exception:
        logger.exception("Rows were saved but could not be formatted")
        return False
    return True


def load_image(photo) -> Image.Image:
    """Open the photo upright, applying the orientation the phone stored in its EXIF data."""
    return ImageOps.exif_transpose(Image.open(photo))


def start_new_scan() -> None:
    """Clear the uploaded photo by giving the uploader a new key."""
    st.session_state.scan_counter += 1


def main() -> None:
    st.set_page_config(page_title="Voucher Scanner Pro", page_icon="📸")
    st.title("📸 Voucher Scanner Pro")
    st.write("Take or upload photos of your voucher to save it to your budget.")

    if "scan_counter" not in st.session_state:
        st.session_state.scan_counter = 0

    photos = st.file_uploader(
        "Take or select one or more photos of the same voucher",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        key=f"voucher_photo_{st.session_state.scan_counter}",
        help="For a long voucher, add several close-up photos that overlap a little.",
    )
    if not photos:
        return

    try:
        images = [load_image(photo) for photo in photos]
    except Exception:
        logger.exception("Uploaded file is not a valid image")
        st.error("⚠️ A file does not appear to be a valid image. Upload JPG or PNG photos.")
        return

    for number, img in enumerate(images, start=1):
        st.image(img, caption=f"Photo {number} of {len(images)}", width="stretch")

    if not st.button("🚀 Process and Save"):
        return

    with st.spinner("The AI is reading the voucher..."):
        try:
            gemini_client = get_gemini_client()
            voucher = extract_voucher_data(gemini_client, images)
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

        rows_total = sum_column(rows, "Final price")
        total_matches = totals_match(rows_total, voucher.total_amount)
        if total_matches:
            st.caption(f"✅ The rows add up to the voucher total: {rows_total:.2f}")
        else:
            rows_tax = sum_column(rows, "Tax")
            voucher_tax = sum(tax.amount for tax in voucher.taxes)
            logger.warning(
                "Rows total %.2f differs from voucher total %.2f (tax %.2f vs %.2f)",
                rows_total,
                voucher.total_amount,
                rows_tax,
                voucher_tax,
            )
            st.warning(
                f"⚠️ The rows add up to {rows_total:.2f} but the voucher total is "
                f"{voucher.total_amount:.2f}. Tax in the rows: {rows_tax:.2f}; tax "
                f"printed on the voucher: {voucher_tax:.2f}. A product may be missing, "
                "duplicated or misread, or wrongly marked as taxed. Review the rows "
                "saved in the sheet."
            )

        try:
            sheets_client = get_sheets_client()
            rows_formatted = save_to_google_sheets(
                sheets_client, rows, voucher.total_amount, total_matches
            )
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
        if not rows_formatted:
            st.warning(
                "⚠️ The rows were saved, but their format (color and merged total) could "
                "not be applied."
            )
        st.balloons()
        st.button("🔄 Scan another voucher", on_click=start_new_scan)


if __name__ == "__main__":
    main()

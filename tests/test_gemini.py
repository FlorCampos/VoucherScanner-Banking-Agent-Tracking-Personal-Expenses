import io
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from PIL import Image
from pydantic import ValidationError

from app import (
    GEMINI_MODEL,
    LIGHT_BLUE,
    MULTI_PHOTO_NOTE,
    PROMPT,
    RED,
    SHEET_COLUMNS,
    SHEET_NAME,
    WHITE,
    WORKSHEET_NAME,
    CardBrand,
    PaymentMethod,
    TaxLine,
    Voucher,
    VoucherItem,
    _normalize_private_key,
    allocate_cents,
    build_sheet_rows,
    compute_item_taxes,
    extract_voucher_data,
    format_voucher_block,
    load_image,
    mask_card_number,
    next_band_color,
    parse_row_span,
    read_row_color,
    resolve_purchase_date,
    save_to_google_sheets,
    sum_column,
    totals_match,
)

TODAY = date(2026, 11, 11)


@pytest.fixture(scope="module")
def sample_pem() -> str:
    """Throwaway PEM generated in memory, never a real credential."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def quebec_taxes(gst_amount: float = 0.0, qst_amount: float = 0.0) -> list[TaxLine]:
    return [
        TaxLine(name="TPS", rate_percent=5, amount=gst_amount),
        TaxLine(name="TVQ", rate_percent=9.975, amount=qst_amount),
    ]


def make_voucher(**overrides) -> Voucher:
    payload = {
        "merchant": "Supermercado Lider",
        "purchase_date": "25/10/2026",
        "month_name": "October",
        "items": [
            VoucherItem(
                product="1 bottle of 500 mL almond milk",
                price_after_discount=3.99,
                discount=1.0,
                tax_code=None,
                category="Food",
            ),
            VoucherItem(
                product="1 pack of paper towels",
                price_after_discount=8.0,
                tax_code="FP",
                category="Household",
            ),
        ],
        "taxes": quebec_taxes(gst_amount=0.4, qst_amount=0.8),
        "total_amount": 13.19,
        "payment_method": PaymentMethod.CREDIT,
        "card_brand": CardBrand.VISA,
        "card_last4": "4417",
    }
    payload.update(overrides)
    return Voucher(**payload)


def make_iga_voucher() -> Voucher:
    """Voucher with already-discounted prices, two taxed products and TPS/TVQ rates."""
    return make_voucher(
        items=[
            VoucherItem(
                product="KITKAT SINGLE 4 BAT",
                price_after_discount=1.22,
                discount=0.77,
                tax_code="FP",
                category="Food",
            ),
            VoucherItem(
                product="KITKAT SINGLE 4 BAT",
                price_after_discount=1.22,
                discount=0.77,
                tax_code="FP",
                category="Food",
            ),
            VoucherItem(
                product="COMP OEUFS BL GROS",
                price_after_discount=3.49,
                discount=0.7,
                tax_code=None,
                category="Food",
            ),
            VoucherItem(
                product="PRIM POIT POULET DES",
                price_after_discount=12.05,
                tax_code=None,
                category="Food",
            ),
        ],
        taxes=quebec_taxes(gst_amount=0.12, qst_amount=0.24),
        total_amount=18.34,
    )


@pytest.mark.parametrize(
    "mangle",
    [
        lambda pem: pem,
        lambda pem: pem.replace("\n", "\\n"),
        lambda pem: pem.replace("\n", ""),
        lambda pem: pem.replace("\n", "\r\n"),
        lambda pem: pem.replace("\n", "\n   "),
    ],
    ids=["intact", "literal \\n", "no line breaks", "CRLF", "indented"],
)
def test_normalize_private_key_recovers_valid_pem(sample_pem, mangle):
    mangled = mangle(sample_pem)
    normalized = _normalize_private_key(mangled)
    serialization.load_pem_private_key(normalized.encode(), password=None)


def test_voucher_schema_accepts_valid_payload():
    voucher = make_voucher()
    assert len(voucher.items) == 2
    assert voucher.items[0].discount == 1.0
    assert voucher.items[1].discount == 0.0
    assert voucher.items[1].tax_code == "FP"
    assert [tax.rate_percent for tax in voucher.taxes] == [5, 9.975]


def test_voucher_schema_parses_json_payload():
    voucher = Voucher.model_validate(
        {
            "merchant": "Farmacia Cruz Verde",
            "purchase_date": "01/01/2026",
            "month_name": "January",
            "items": [
                {
                    "product": "1 box of bandages",
                    "price_after_discount": 3.5,
                    "tax_code": "T",
                    "category": "Health",
                }
            ],
            "taxes": [{"name": "TAX", "rate_percent": None, "amount": 0.52}],
            "total_amount": 4.02,
            "payment_method": "debit",
            "card_brand": "Mastercard",
            "card_last4": "0021",
        }
    )
    assert voucher.payment_method is PaymentMethod.DEBIT
    assert voucher.card_brand is CardBrand.MASTERCARD
    assert voucher.taxes[0].rate_percent is None


def test_voucher_schema_card_fields_are_optional():
    voucher = make_voucher(payment_method="cash", card_brand=None, card_last4=None)
    assert voucher.card_brand is None
    assert voucher.card_last4 is None


def test_voucher_schema_rejects_missing_required_fields():
    with pytest.raises(ValidationError):
        Voucher(purchase_date="12/03/2026", month_name="March")


def test_voucher_schema_rejects_unknown_payment_method():
    with pytest.raises(ValidationError):
        make_voucher(payment_method="crypto")


@pytest.mark.parametrize(
    ("last4", "expected"),
    [
        ("4417", "************4417"),
        ("**** **** **** 4417", "************4417"),
        ("12", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_mask_card_number(last4, expected):
    assert mask_card_number(last4) == expected


@pytest.mark.parametrize(
    ("raw_date", "expected"),
    [
        ("25/10/2026", date(2026, 10, 25)),
        ("11/05/2026", date(2026, 11, 5)),
        ("05/11/2026", date(2026, 11, 5)),
        ("11/11/2026", date(2026, 11, 11)),
        ("03/12/2026", date(2026, 3, 12)),
        (" 09/11/2026 ", date(2026, 11, 9)),
        ("31/02/2026", None),
        ("2026-11-05", None),
        ("", None),
    ],
    ids=[
        "unambiguous",
        "month-first print becomes recent date",
        "already correct stays",
        "same day and month",
        "future reading is discarded",
        "surrounding spaces",
        "impossible date",
        "other format",
        "empty",
    ],
)
def test_resolve_purchase_date(raw_date, expected):
    assert resolve_purchase_date(raw_date, TODAY) == expected


def test_build_sheet_rows_fixes_swapped_day_and_month():
    voucher = make_voucher(purchase_date="11/05/2026", month_name="May")

    row = dict(zip(SHEET_COLUMNS, build_sheet_rows(voucher, today=TODAY)[0]))

    assert row["Purchase date"] == "05/11/2026"
    assert row["Month"] == "November"


def test_build_sheet_rows_keeps_extracted_date_when_it_cannot_be_parsed():
    voucher = make_voucher(purchase_date="2026-11-05", month_name="November")

    row = dict(zip(SHEET_COLUMNS, build_sheet_rows(voucher, today=TODAY)[0]))

    assert row["Purchase date"] == "2026-11-05"
    assert row["Month"] == "November"


def test_load_image_applies_phone_orientation():
    """A photo stored sideways with EXIF orientation 6 must come out upright."""
    exif = Image.Exif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, format="JPEG", exif=exif)
    buffer.seek(0)

    img = load_image(buffer)

    assert img.size == (20, 40)


def test_load_image_leaves_upright_photos_unchanged():
    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, format="PNG")
    buffer.seek(0)

    assert load_image(buffer).size == (40, 20)


def test_allocate_cents_adds_up_exactly_to_the_total():
    parts = allocate_cents(Decimal("0.02"), [Decimal("0.10")] * 3)

    assert parts == [Decimal("0.01"), Decimal("0.01"), Decimal("0.00")]
    assert sum(parts) == Decimal("0.02")


def test_allocate_cents_gives_nothing_to_zero_weights():
    parts = allocate_cents(Decimal("0.36"), [Decimal("1.22"), Decimal(0), Decimal("1.22")])

    assert parts == [Decimal("0.18"), Decimal("0.00"), Decimal("0.18")]


def test_compute_item_taxes_uses_printed_rates_on_taxed_products_only():
    taxes = compute_item_taxes(make_iga_voucher())

    assert taxes == [0.18, 0.18, 0.0, 0.0]
    assert round(sum(taxes), 2) == 0.36


def test_compute_item_taxes_matches_register_rounding_across_products():
    """Three 0.10 products at 5%: the register charges 0.02 in total, not 3 x 0.01."""
    items = [
        VoucherItem(product=f"CANDY {n}", price_after_discount=0.1, tax_code="FP", category="Food")
        for n in range(3)
    ]
    voucher = make_voucher(items=items, taxes=[TaxLine(name="GST", rate_percent=5, amount=0.02)])

    taxes = compute_item_taxes(voucher)

    assert taxes == [0.01, 0.01, 0.0]


def test_compute_item_taxes_falls_back_to_printed_amount_without_rate():
    voucher = make_iga_voucher()
    voucher.taxes = [TaxLine(name="TAX", amount=0.36)]

    assert compute_item_taxes(voucher) == [0.18, 0.18, 0.0, 0.0]


def test_compute_item_taxes_treats_every_product_as_taxed_when_no_codes_are_printed():
    """A restaurant bill charges tax but prints no per-product tax codes."""
    items = [
        VoucherItem(product="SOUP", price_after_discount=10.0, category="Food"),
        VoucherItem(product="PASTA", price_after_discount=20.0, category="Food"),
    ]
    voucher = make_voucher(items=items, taxes=quebec_taxes(gst_amount=1.5, qst_amount=2.99))

    assert compute_item_taxes(voucher) == [1.5, 2.99]


def test_compute_item_taxes_ignores_blank_tax_codes():
    voucher = make_iga_voucher()
    voucher.items[2].tax_code = "  "

    assert compute_item_taxes(voucher) == [0.18, 0.18, 0.0, 0.0]


def test_compute_item_taxes_is_zero_when_voucher_has_no_taxes():
    assert compute_item_taxes(make_voucher(taxes=[])) == [0.0, 0.0]


def test_build_sheet_rows_returns_one_row_per_product():
    rows = build_sheet_rows(make_voucher())

    assert rows == [
        [
            "Supermercado Lider",
            "1 bottle of 500 mL almond milk",
            4.99,
            1.0,
            3.99,
            0.0,
            3.99,
            "25/10/2026",
            "October",
            "Food",
            "credit",
            "Visa",
            "************4417",
        ],
        [
            "Supermercado Lider",
            "1 pack of paper towels",
            8.0,
            0.0,
            8.0,
            1.2,
            9.2,
            "25/10/2026",
            "October",
            "Household",
            "credit",
            "Visa",
            "************4417",
        ],
    ]
    assert all(len(row) == len(SHEET_COLUMNS) for row in rows)


def test_build_sheet_rows_leaves_card_columns_empty_for_cash():
    rows = build_sheet_rows(make_voucher(payment_method="cash"))

    for row in rows:
        assert row[-3:] == ["cash", "", ""]


def test_build_sheet_rows_treats_negative_discount_as_positive():
    item = VoucherItem(
        product="500 g of grapes",
        price_after_discount=3.5,
        discount=-1.5,
        tax_code=None,
        category="Food",
    )

    row = build_sheet_rows(make_voucher(items=[item]))[0]

    assert row[2] == 5.0
    assert row[3] == 1.5
    assert row[4] == 3.5


def test_build_sheet_rows_final_price_adds_tax_to_discounted_price():
    item = VoucherItem(
        product="DETERGENT 1L",
        price_after_discount=8.0,
        discount=2.0,
        tax_code="FP",
        category="Household",
    )

    row = dict(zip(SHEET_COLUMNS, build_sheet_rows(make_voucher(items=[item]))[0]))

    assert row["Price"] == 10.0
    assert row["Price after discount"] == 8.0
    assert row["Tax"] == 1.2
    assert row["Final price"] == 9.2


def test_build_sheet_rows_match_voucher_with_already_discounted_prices():
    """Printed prices already include the discount; rows must add up to the voucher."""
    rows = [dict(zip(SHEET_COLUMNS, row)) for row in build_sheet_rows(make_iga_voucher())]

    assert rows[0]["Price"] == 1.99
    assert rows[0]["Discount"] == 0.77
    assert rows[0]["Price after discount"] == 1.22
    assert rows[0]["Tax"] == 0.18
    assert rows[0]["Final price"] == 1.4
    assert round(sum(row["Discount"] for row in rows), 2) == 2.24
    assert round(sum(row["Price after discount"] for row in rows), 2) == 17.98
    assert round(sum(row["Final price"] for row in rows), 2) == 18.34


def test_extract_voucher_data_returns_parsed_voucher():
    fake_voucher = make_voucher()
    fake_response = MagicMock(parsed=fake_voucher)
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response

    result = extract_voucher_data(fake_client, ["fake-image"])

    fake_client.models.generate_content.assert_called_once()
    _, kwargs = fake_client.models.generate_content.call_args
    assert kwargs["model"] == GEMINI_MODEL
    assert kwargs["contents"] == [PROMPT, "fake-image"]
    assert kwargs["config"].temperature == 0
    assert kwargs["config"].response_mime_type == "application/json"
    assert kwargs["config"].response_schema is Voucher
    assert result == fake_voucher


def test_extract_voucher_data_sends_all_photos_with_the_multi_photo_note():
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = MagicMock(parsed=make_voucher())

    extract_voucher_data(fake_client, ["top-photo", "bottom-photo"])

    _, kwargs = fake_client.models.generate_content.call_args
    assert kwargs["contents"] == [f"{PROMPT} {MULTI_PHOTO_NOTE}", "top-photo", "bottom-photo"]


def test_extract_voucher_data_raises_when_parsing_fails():
    fake_response = MagicMock(parsed=None)
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response

    with pytest.raises(ValueError):
        extract_voucher_data(fake_client, ["fake-image"])


def test_extract_voucher_data_drops_zero_amount_lines():
    voucher = make_iga_voucher()
    voucher.items.append(
        VoucherItem(product="TIMBRE", price_after_discount=0.0, category="Other")
    )
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = MagicMock(parsed=voucher)

    result = extract_voucher_data(fake_client, ["fake-image"])

    assert [item.product for item in result.items] == [
        "KITKAT SINGLE 4 BAT",
        "KITKAT SINGLE 4 BAT",
        "COMP OEUFS BL GROS",
        "PRIM POIT POULET DES",
    ]


def test_extract_voucher_data_raises_when_only_zero_amount_lines_found():
    zero_line = VoucherItem(product="TIMBRE", price_after_discount=0.0, category="Other")
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = MagicMock(
        parsed=make_voucher(items=[zero_line])
    )

    with pytest.raises(ValueError):
        extract_voucher_data(fake_client, ["fake-image"])


def test_extract_voucher_data_raises_when_no_products_found():
    fake_response = MagicMock(parsed=make_voucher(items=[]))
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response

    with pytest.raises(ValueError):
        extract_voucher_data(fake_client, ["fake-image"])


def test_rows_add_up_to_the_voucher_total():
    voucher = make_iga_voucher()

    rows = build_sheet_rows(voucher)
    rows_total = sum_column(rows, "Final price")

    assert rows_total == 18.34
    assert sum_column(rows, "Tax") == 0.36
    assert totals_match(rows_total, voucher.total_amount)


def test_totals_do_not_match_when_a_product_is_duplicated():
    voucher = make_iga_voucher()
    voucher.items.append(voucher.items[-1])

    rows_total = sum_column(build_sheet_rows(voucher), "Final price")

    assert rows_total == 30.39
    assert not totals_match(rows_total, voucher.total_amount)


@pytest.mark.parametrize(
    ("rows_total", "voucher_total", "expected"),
    [(18.34, 18.34, True), (0.1 + 0.2, 0.3, True), (18.35, 18.34, False), (18.33, 18.34, False)],
)
def test_totals_match_compares_to_the_cent(rows_total, voucher_total, expected):
    assert totals_match(rows_total, voucher_total) is expected


def cell_metadata(color: dict | None) -> dict:
    """Sheets API response for one cell's background color."""
    cell = {} if color is None else {"effectiveFormat": {"backgroundColor": color}}
    return {"sheets": [{"data": [{"rowData": [{"values": [cell]}]}]}]}


def make_worksheet(previous_color: dict | None = None, updated_range: str | None = None):
    worksheet = MagicMock()
    worksheet.title = WORKSHEET_NAME
    worksheet.spreadsheet.fetch_sheet_metadata.return_value = cell_metadata(previous_color)
    worksheet.append_rows.return_value = {"updates": {"updatedRange": updated_range}}
    return worksheet


@pytest.mark.parametrize(
    ("updated_range", "expected"),
    [
        ("'Micro-expenses-2026'!A115:M124", (115, 124)),
        ("'Micro-expenses-2026'!A7:M7", (7, 7)),
        ("Sheet1!A2", (2, 2)),
        ("'Micro-expenses-2026'!$A$115:$M$124", (115, 124)),
    ],
)
def test_parse_row_span(updated_range, expected):
    assert parse_row_span(updated_range) == expected


def test_parse_row_span_rejects_unexpected_ranges():
    with pytest.raises(ValueError):
        parse_row_span("'Micro-expenses-2026'!A:M")


@pytest.mark.parametrize(
    ("previous_color", "expected"),
    [
        (None, LIGHT_BLUE),
        (WHITE, LIGHT_BLUE),
        ({}, LIGHT_BLUE),
        ({"red": 1, "green": 0.9}, LIGHT_BLUE),
        (LIGHT_BLUE, WHITE),
        ({"red": 0.8666667, "green": 0.92156863, "blue": 0.96862745}, WHITE),
    ],
    ids=["unknown", "white", "black", "other color", "light blue", "light blue as returned"],
)
def test_next_band_color_alternates_with_the_voucher_above(previous_color, expected):
    assert next_band_color(previous_color) == expected


def test_read_row_color_asks_only_for_the_cell_background():
    worksheet = make_worksheet(previous_color=LIGHT_BLUE)

    assert read_row_color(worksheet, 114) == LIGHT_BLUE
    worksheet.spreadsheet.fetch_sheet_metadata.assert_called_once_with(
        params={
            "includeGridData": "true",
            "ranges": "'Micro-expenses-2026'!A114",
            "fields": "sheets.data.rowData.values.effectiveFormat.backgroundColor",
        }
    )


def test_read_row_color_returns_none_when_there_is_no_format_or_no_row_above():
    assert read_row_color(make_worksheet(previous_color=None), 114) is None
    assert read_row_color(make_worksheet(previous_color=LIGHT_BLUE), 0) is None


CENTERED = {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}


def test_format_voucher_block_colors_the_rows_and_merges_the_total():
    worksheet = make_worksheet(previous_color=WHITE)

    format_voucher_block(worksheet, 115, 124)

    worksheet.merge_cells.assert_called_once_with("N115:N124")
    worksheet.batch_format.assert_called_once_with(
        [
            {"range": "A115:N124", "format": {"backgroundColor": LIGHT_BLUE}},
            {"range": "N115:N124", "format": CENTERED},
        ]
    )


def test_format_voucher_block_uses_white_after_a_light_blue_voucher():
    worksheet = make_worksheet(previous_color=LIGHT_BLUE)

    format_voucher_block(worksheet, 125, 128)

    formats = worksheet.batch_format.call_args[0][0]
    assert formats[0] == {"range": "A125:N128", "format": {"backgroundColor": WHITE}}


def test_format_voucher_block_does_not_merge_a_single_row_voucher():
    worksheet = make_worksheet(previous_color=WHITE)

    format_voucher_block(worksheet, 130, 130)

    worksheet.merge_cells.assert_not_called()
    worksheet.batch_format.assert_called_once_with(
        [
            {"range": "A130:N130", "format": {"backgroundColor": LIGHT_BLUE}},
            {"range": "N130:N130", "format": CENTERED},
        ]
    )


def test_format_voucher_block_marks_a_total_that_does_not_match_in_bold_red():
    worksheet = make_worksheet(previous_color=WHITE)

    format_voucher_block(worksheet, 115, 118, total_matches=False)

    formats = worksheet.batch_format.call_args[0][0]
    assert formats[1] == {
        "range": "N115:N118",
        "format": {**CENTERED, "textFormat": {"bold": True, "foregroundColor": RED}},
    }


def test_save_to_google_sheets_writes_the_total_once_and_formats_the_block():
    rows = build_sheet_rows(make_voucher())
    worksheet = make_worksheet(
        previous_color=WHITE, updated_range="'Micro-expenses-2026'!A115:N116"
    )
    fake_gc = MagicMock()
    fake_gc.open.return_value.worksheet.return_value = worksheet

    formatted = save_to_google_sheets(fake_gc, rows, voucher_total=13.19)

    fake_gc.open.assert_called_once_with(SHEET_NAME)
    fake_gc.open.return_value.worksheet.assert_called_once_with(WORKSHEET_NAME)
    worksheet.append_rows.assert_called_once_with([[*rows[0], 13.19], [*rows[1], ""]])
    worksheet.merge_cells.assert_called_once_with("N115:N116")
    worksheet.batch_format.assert_called_once()
    assert formatted is True


def test_save_to_google_sheets_does_not_change_the_rows_it_receives():
    rows = build_sheet_rows(make_voucher())
    worksheet = make_worksheet(
        previous_color=WHITE, updated_range="'Micro-expenses-2026'!A115:N116"
    )
    fake_gc = MagicMock()
    fake_gc.open.return_value.worksheet.return_value = worksheet

    save_to_google_sheets(fake_gc, rows, voucher_total=13.19)

    assert all(len(row) == len(SHEET_COLUMNS) for row in rows)


def test_save_to_google_sheets_passes_a_total_mismatch_to_the_formatting():
    rows = build_sheet_rows(make_voucher())
    worksheet = make_worksheet(
        previous_color=WHITE, updated_range="'Micro-expenses-2026'!A115:N116"
    )
    fake_gc = MagicMock()
    fake_gc.open.return_value.worksheet.return_value = worksheet

    save_to_google_sheets(fake_gc, rows, voucher_total=99.0, total_matches=False)

    total_format = worksheet.batch_format.call_args[0][0][1]["format"]
    assert total_format["textFormat"] == {"bold": True, "foregroundColor": RED}


def test_save_to_google_sheets_keeps_the_rows_when_formatting_fails():
    rows = build_sheet_rows(make_voucher())
    worksheet = make_worksheet(
        previous_color=WHITE, updated_range="'Micro-expenses-2026'!A115:N116"
    )
    worksheet.merge_cells.side_effect = RuntimeError("formatting not allowed")
    fake_gc = MagicMock()
    fake_gc.open.return_value.worksheet.return_value = worksheet

    formatted = save_to_google_sheets(fake_gc, rows, voucher_total=13.19)

    worksheet.append_rows.assert_called_once()
    assert formatted is False


def test_save_to_google_sheets_does_not_hide_errors_when_saving_fails():
    worksheet = make_worksheet()
    worksheet.append_rows.side_effect = RuntimeError("quota exceeded")
    fake_gc = MagicMock()
    fake_gc.open.return_value.worksheet.return_value = worksheet

    with pytest.raises(RuntimeError):
        save_to_google_sheets(fake_gc, build_sheet_rows(make_voucher()), voucher_total=13.19)

    worksheet.merge_cells.assert_not_called()
    worksheet.batch_format.assert_not_called()

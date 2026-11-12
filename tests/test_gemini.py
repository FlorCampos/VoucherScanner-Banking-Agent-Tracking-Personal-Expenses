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
    PROMPT,
    SHEET_COLUMNS,
    SHEET_NAME,
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
    load_image,
    mask_card_number,
    resolve_purchase_date,
    save_to_google_sheets,
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
                taxable=False,
                category="Food",
            ),
            VoucherItem(
                product="1 pack of paper towels",
                price_after_discount=8.0,
                taxable=True,
                category="Household",
            ),
        ],
        "taxes": quebec_taxes(gst_amount=0.4, qst_amount=0.8),
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
                taxable=True,
                category="Food",
            ),
            VoucherItem(
                product="KITKAT SINGLE 4 BAT",
                price_after_discount=1.22,
                discount=0.77,
                taxable=True,
                category="Food",
            ),
            VoucherItem(
                product="COMP OEUFS BL GROS",
                price_after_discount=3.49,
                discount=0.7,
                taxable=False,
                category="Food",
            ),
            VoucherItem(
                product="PRIM POIT POULET DES",
                price_after_discount=12.05,
                taxable=False,
                category="Food",
            ),
        ],
        taxes=quebec_taxes(gst_amount=0.12, qst_amount=0.24),
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
    assert voucher.items[1].taxable is True
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
                    "taxable": True,
                    "category": "Health",
                }
            ],
            "taxes": [{"name": "TAX", "rate_percent": None, "amount": 0.52}],
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
        VoucherItem(product=f"CANDY {n}", price_after_discount=0.1, taxable=True, category="Food")
        for n in range(3)
    ]
    voucher = make_voucher(items=items, taxes=[TaxLine(name="GST", rate_percent=5, amount=0.02)])

    taxes = compute_item_taxes(voucher)

    assert taxes == [0.01, 0.01, 0.0]


def test_compute_item_taxes_falls_back_to_printed_amount_without_rate():
    voucher = make_iga_voucher()
    voucher.taxes = [TaxLine(name="TAX", amount=0.36)]

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
        taxable=False,
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
        taxable=True,
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

    result = extract_voucher_data(fake_client, img="fake-image")

    fake_client.models.generate_content.assert_called_once()
    _, kwargs = fake_client.models.generate_content.call_args
    assert kwargs["model"] == GEMINI_MODEL
    assert kwargs["contents"][0] == PROMPT
    assert kwargs["contents"][1] == "fake-image"
    assert kwargs["config"].temperature == 0
    assert kwargs["config"].response_mime_type == "application/json"
    assert kwargs["config"].response_schema is Voucher
    assert result == fake_voucher


def test_extract_voucher_data_raises_when_parsing_fails():
    fake_response = MagicMock(parsed=None)
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response

    with pytest.raises(ValueError):
        extract_voucher_data(fake_client, img="fake-image")


def test_extract_voucher_data_raises_when_no_products_found():
    fake_response = MagicMock(parsed=make_voucher(items=[]))
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response

    with pytest.raises(ValueError):
        extract_voucher_data(fake_client, img="fake-image")


def test_save_to_google_sheets_appends_all_rows():
    rows = build_sheet_rows(make_voucher())
    fake_gc = MagicMock()

    save_to_google_sheets(fake_gc, rows)

    fake_gc.open.assert_called_once_with(SHEET_NAME)
    fake_gc.open.return_value.worksheet.assert_called_once_with(WORKSHEET_NAME)
    fake_gc.open.return_value.worksheet.return_value.append_rows.assert_called_once_with(rows)

from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from app import GEMINI_MODEL, Voucher, _normalize_private_key, extract_voucher_data


@pytest.fixture(scope="module")
def sample_pem() -> str:
    """Throwaway PEM generated in memory, never a real credential."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


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
    voucher = Voucher(
        merchant="Supermercado Lider",
        purchase_date="12/03/2026",
        month_name="March",
        total_amount=15990.0,
        tax_amount=2550.0,
        suggested_category="Comida",
    )
    assert voucher.total_amount == 15990.0
    assert voucher.tax_amount == 2550.0


def test_voucher_schema_rejects_missing_required_fields():
    with pytest.raises(ValidationError):
        Voucher(purchase_date="12/03/2026", total_amount=100.0)


def test_extract_voucher_data_returns_parsed_voucher():
    fake_voucher = Voucher(
        merchant="Farmacia Cruz Verde",
        purchase_date="01/01/2026",
        month_name="January",
        total_amount=4990.0,
        tax_amount=0.0,
        suggested_category="Salud",
    )
    fake_response = MagicMock(parsed=fake_voucher)
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response

    result = extract_voucher_data(fake_client, img="fake-image")

    fake_client.models.generate_content.assert_called_once()
    _, kwargs = fake_client.models.generate_content.call_args
    assert kwargs["model"] == GEMINI_MODEL
    assert kwargs["config"].response_mime_type == "application/json"
    assert kwargs["config"].response_schema is Voucher
    assert result == fake_voucher


def test_extract_voucher_data_raises_when_parsing_fails():
    fake_response = MagicMock(parsed=None)
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response

    with pytest.raises(ValueError):
        extract_voucher_data(fake_client, img="fake-image")

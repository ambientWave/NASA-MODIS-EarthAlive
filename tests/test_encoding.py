from __future__ import annotations

import base64

import pytest

from app.utils.encoding import encode_bytes_base64, encode_file_base64


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-image-payload"


def test_encode_bytes_base64_round_trips():
    encoded = encode_bytes_base64(PNG_BYTES)
    assert isinstance(encoded, str)
    assert base64.b64decode(encoded) == PNG_BYTES


def test_encode_file_base64_reads_binary_file(tmp_path):
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(PNG_BYTES)

    encoded = encode_file_base64(image_path)

    assert base64.b64decode(encoded) == PNG_BYTES


def test_encode_file_base64_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        encode_file_base64(tmp_path / "missing.png", "plot")

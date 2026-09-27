import pytest
import os
import zipfile
import tempfile
from pathlib import Path
from cryptography.fernet import Fernet

from app.core.config import settings
from app.core.security import encrypt_session_string, decrypt_session_string, safe_extract_zip
from app.services.proxy_service import parse_proxy_line
from app.services.inviter_service import calculate_delay
from app.services.spambot_service import classify_spambot_reply

@pytest.fixture(autouse=True)
def setup_encryption_key():
    test_key = Fernet.generate_key().decode()
    settings.ENCRYPTION_KEY = test_key
    yield

def test_fernet_session_encryption():
    raw_session = "1BVtsOKUBu2x...test_string_session...xyz=="
    encrypted = encrypt_session_string(raw_session)
    assert encrypted != raw_session
    decrypted = decrypt_session_string(encrypted)
    assert decrypted == raw_session

def test_safe_zip_extraction():
    with tempfile.TemporaryDirectory() as temp_dir:
        archive_path = Path(temp_dir) / "test.zip"
        target_dir = Path(temp_dir) / "extracted"

        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("tdata/key_datas", b"sample_binary_data")

        safe_extract_zip(archive_path, target_dir)
        extracted_file = target_dir / "tdata" / "key_datas"
        assert extracted_file.exists()
        assert extracted_file.read_bytes() == b"sample_binary_data"

def test_safe_zip_traversal_blocked():
    with tempfile.TemporaryDirectory() as temp_dir:
        archive_path = Path(temp_dir) / "traversal.zip"
        target_dir = Path(temp_dir) / "extracted"

        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("../evil.txt", b"malicious_data")

        with pytest.raises(zipfile.BadZipFile, match="Illegal path in archive"):
            safe_extract_zip(archive_path, target_dir)

def test_safe_zip_symlink_blocked():
    with tempfile.TemporaryDirectory() as temp_dir:
        archive_path = Path(temp_dir) / "symlink.zip"
        target_dir = Path(temp_dir) / "extracted"

        with zipfile.ZipFile(archive_path, "w") as zf:
            zinfo = zipfile.ZipInfo("symlink_target")
            zinfo.create_system = 3  # Unix
            zinfo.external_attr = 0o120777 << 16  # Symlink mode
            zf.writestr(zinfo, "/etc/passwd")

        with pytest.raises(zipfile.BadZipFile, match="contains symlinks"):
            safe_extract_zip(archive_path, target_dir)

def test_safe_zip_size_limit():
    with tempfile.TemporaryDirectory() as temp_dir:
        archive_path = Path(temp_dir) / "bomb.zip"
        target_dir = Path(temp_dir) / "extracted"

        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("huge.txt", b"A" * 1000)

        with pytest.raises(zipfile.BadZipFile, match="exceeds maximum allowed"):
            safe_extract_zip(archive_path, target_dir, max_uncompressed_bytes=500)

def test_proxy_parser_variations():
    socks_standard = parse_proxy_line("192.168.1.1:1080:user:pass")
    assert socks_standard == ("192.168.1.1", 1080, "user", "pass", "socks5")

    http_standard = parse_proxy_line("http://proxyadmin:secret123@10.0.0.1:8080")
    assert http_standard == ("10.0.0.1", 8080, "proxyadmin", "secret123", "http")

    socks_no_auth = parse_proxy_line("172.16.0.5:9050")
    assert socks_no_auth == ("172.16.0.5", 9050, None, None, "socks5")

    invalid_line = parse_proxy_line("not_a_valid_proxy")
    assert invalid_line is None

def test_speed_delay_calculations():
    for _ in range(20):
        cautious_delay = calculate_delay("cautious")
        assert cautious_delay >= 20.0
        fast_delay = calculate_delay("fast")
        assert fast_delay >= 7.0

def test_spambot_classification():
    ok_reply = "Good news, no limits are currently applied to your account. You're free as a bird!"
    status, desc = classify_spambot_reply(ok_reply)
    assert status == "active"

    limited_reply = "Unfortunately, some limitations were placed on your account due to reports."
    status, desc = classify_spambot_reply(limited_reply)
    assert status == "spambot"

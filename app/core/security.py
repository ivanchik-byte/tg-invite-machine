import os
import zipfile
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from app.core.config import settings

def get_cipher() -> Fernet:
    key = settings.ENCRYPTION_KEY
    if not key:
        raise ValueError("ENCRYPTION_KEY is not configured in settings")
    if isinstance(key, str):
        key = key.encode("utf-8")
    return Fernet(key)

def encrypt_session_string(session_string: str) -> str:
    cipher = get_cipher()
    token = cipher.encrypt(session_string.encode("utf-8"))
    return token.decode("utf-8")

def decrypt_session_string(encrypted_token: str) -> str:
    cipher = get_cipher()
    try:
        decrypted_bytes = cipher.decrypt(encrypted_token.encode("utf-8"))
        return decrypted_bytes.decode("utf-8")
    except InvalidToken:
        raise ValueError("Failed to decrypt session: invalid or corrupted encryption key")

def safe_extract_zip(archive_path: Path, destination_dir: Path, max_uncompressed_bytes: int = 150 * 1024 * 1024) -> None:
    destination_real = destination_dir.resolve()
    total_written = 0

    with zipfile.ZipFile(archive_path, "r") as archive:
        for entry in archive.infolist():
            if (entry.external_attr >> 16) & 0o120000 == 0o120000:
                raise zipfile.BadZipFile("Archive contains symlinks which are forbidden")

            entry_path = (destination_real / entry.filename).resolve()
            if not entry_path.is_relative_to(destination_real):
                raise zipfile.BadZipFile(f"Illegal path in archive: {entry.filename}")

            if entry.is_dir():
                entry_path.mkdir(parents=True, exist_ok=True)
            else:
                entry_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry, "r") as source_stream, open(entry_path, "wb") as target_file:
                    while chunk := source_stream.read(65536):
                        total_written += len(chunk)
                        if total_written > max_uncompressed_bytes:
                            raise zipfile.BadZipFile("Archive exceeds maximum allowed uncompressed size")
                        target_file.write(chunk)

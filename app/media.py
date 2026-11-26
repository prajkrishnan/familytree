"""Avatar processing: validate, strip metadata, re-encode, resize."""
from __future__ import annotations

import io
import secrets
from pathlib import Path

from PIL import Image, ImageOps

from .config import Settings
from .util import now

MAX_DIM = 512
THUMB_DIM = 160
WEBP_QUALITY = 82


class ImageError(ValueError):
    pass


def _extract(upload_bytes: bytes) -> tuple[bytes, bytes]:
    try:
        probe = Image.open(io.BytesIO(upload_bytes))
        probe.verify()
        image = Image.open(io.BytesIO(upload_bytes))
        image = ImageOps.exif_transpose(image)
    except Exception as exc:  # noqa: BLE001 - any decoder failure is a bad upload
        raise ImageError("That file is not a valid image.") from exc

    if image.width < 32 or image.height < 32:
        raise ImageError("Image is too small (minimum 32×32).")
    if image.width * image.height > 40_000_000:
        raise ImageError("Image dimensions are too large.")

    if image.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        converted = image.convert("RGBA")
        background.paste(converted, mask=converted.split()[-1])
        image = background
    else:
        image = image.convert("RGB")

    image.thumbnail((MAX_DIM, MAX_DIM), Image.Resampling.LANCZOS)
    full_io = io.BytesIO()
    image.save(full_io, format="WEBP", quality=WEBP_QUALITY, method=4)

    thumb = image.copy()
    thumb.thumbnail((THUMB_DIM, THUMB_DIM), Image.Resampling.LANCZOS)
    thumb_io = io.BytesIO()
    thumb.save(thumb_io, format="WEBP", quality=WEBP_QUALITY, method=4)
    return full_io.getvalue(), thumb_io.getvalue()


def process_upload(upload, settings: Settings) -> tuple[bytes, bytes]:
    data = upload.file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise ImageError(f"Image is larger than {settings.max_upload_mb} MB.")
    if len(data) < 64:
        raise ImageError("That file is empty or unreadable.")
    return _extract(data)


def new_avatar_key(person_id: str) -> str:
    return f"{person_id}-{secrets.token_hex(4)}"


def save_pending(upload, settings: Settings) -> str:
    full, thumb = process_upload(upload, settings)
    key = new_avatar_key("pending")
    (settings.pending_dir / f"{key}.webp").write_bytes(full)
    (settings.pending_dir / f"{key}_t.webp").write_bytes(thumb)
    return key


def save_direct(upload, settings: Settings, person_id: str) -> str:
    full, thumb = process_upload(upload, settings)
    key = new_avatar_key(person_id)
    (settings.avatar_dir / f"{key}.webp").write_bytes(full)
    (settings.avatar_dir / f"{key}_t.webp").write_bytes(thumb)
    return key


def commit_pending(settings: Settings, pending_key: str) -> str:
    """Move a pending upload into the avatar store. Returns the final key."""
    if not pending_key or not _safe_key(pending_key):
        raise ImageError("Invalid pending image reference.")
    src_full = settings.pending_dir / f"{pending_key}.webp"
    if not src_full.exists():
        raise ImageError("The pending photo has expired. Please upload it again.")
    key = new_avatar_key("avatar")
    src_full.replace(settings.avatar_dir / f"{key}.webp")
    src_thumb = settings.pending_dir / f"{pending_key}_t.webp"
    if src_thumb.exists():
        src_thumb.replace(settings.avatar_dir / f"{key}_t.webp")
    return key


def discard_pending(settings: Settings, pending_key: str) -> None:
    if not pending_key or not _safe_key(pending_key):
        return
    for suffix in (".webp", "_t.webp"):
        try:
            (settings.pending_dir / f"{pending_key}{suffix}").unlink()
        except OSError:
            pass


def delete_avatar(settings: Settings, key: str | None) -> None:
    if not key or not _safe_key(key):
        return
    for suffix in (".webp", "_t.webp"):
        try:
            (settings.avatar_dir / f"{key}{suffix}").unlink()
        except OSError:
            pass


def avatar_file(settings: Settings, key: str, thumb: bool = False) -> Path | None:
    if not key or not _safe_key(key):
        return None
    path = settings.avatar_dir / (f"{key}_t.webp" if thumb else f"{key}.webp")
    return path if path.exists() else None


def _safe_key(key: str) -> bool:
    return bool(key) and "/" not in key and "\\" not in key and ".." not in key and len(key) < 120


def cleanup_pending(settings: Settings, older_than_seconds: int = 7 * 86400) -> None:
    cutoff = now() - older_than_seconds
    try:
        for path in settings.pending_dir.glob("*.webp"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        pass

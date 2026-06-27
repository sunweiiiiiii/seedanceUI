from __future__ import annotations

import mimetypes
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse


MEDIA_MIME_TYPES = {
    "image/jpeg": ("image", 12 * 1024 * 1024),
    "image/png": ("image", 12 * 1024 * 1024),
    "image/webp": ("image", 12 * 1024 * 1024),
    "video/mp4": ("video", 300 * 1024 * 1024),
    "video/quicktime": ("video", 300 * 1024 * 1024),
    "video/webm": ("video", 300 * 1024 * 1024),
    "audio/mpeg": ("audio", 80 * 1024 * 1024),
    "audio/mp3": ("audio", 80 * 1024 * 1024),
    "audio/wav": ("audio", 80 * 1024 * 1024),
    "audio/x-wav": ("audio", 80 * 1024 * 1024),
    "audio/mp4": ("audio", 80 * 1024 * 1024),
    "audio/x-m4a": ("audio", 80 * 1024 * 1024),
    "audio/aac": ("audio", 80 * 1024 * 1024),
    "audio/ogg": ("audio", 80 * 1024 * 1024),
}


class TOSUploadError(RuntimeError):
    pass


@dataclass
class TOSConfig:
    access_key: str
    secret_key: str
    bucket: str
    region: str = "cn-beijing"
    endpoint: str = "tos-cn-beijing.volces.com"
    prefix: str = "seedance-references"
    url_mode: str = "signed"
    signed_expires: int = 86400
    public_base_url: str = ""


def normalize_media(filename: str, content_type: str, data: bytes) -> tuple[str, str, bytes]:
    provided_mime = (content_type or "").split(";", 1)[0].strip().lower()
    guessed_mime = (mimetypes.guess_type(filename)[0] or "").lower()
    mime = guessed_mime if provided_mime in {"", "application/octet-stream"} else provided_mime
    mime = mime or guessed_mime or "application/octet-stream"
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime == "audio/x-m4a":
        mime = "audio/mp4"
    if mime not in MEDIA_MIME_TYPES:
        raise TOSUploadError("仅支持 JPEG/PNG/WebP 图片、MP4/MOV/WebM 视频、MP3/WAV/M4A/AAC/OGG 音频。")
    kind, max_bytes = MEDIA_MIME_TYPES[mime]
    if len(data) > max_bytes:
        limits = {"image": "12 MB", "video": "300 MB", "audio": "80 MB"}
        raise TOSUploadError(f"{kind} 文件不能超过 {limits[kind]}。")
    return kind, mime, data


def _endpoint_host(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint:
        return "tos-cn-beijing.volces.com"
    parsed = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
    return parsed.netloc or parsed.path


def _clean_part(value: str, fallback: str) -> str:
    value = re.sub(r"[^\w\-.]+", "-", value, flags=re.UNICODE).strip("-_.")
    return value[:64] or fallback


def build_object_key(prefix: str, filename: str, content_type: str, kind: str = "media") -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".ogg"}:
        suffix = mimetypes.guess_extension(content_type) or ".bin"
        if suffix == ".jpe":
            suffix = ".jpg"
    stem = _clean_part(Path(filename).stem, kind)
    clean_prefix = "/".join(_clean_part(part, "refs") for part in prefix.split("/") if part.strip())
    dated = datetime.now().strftime("%Y%m%d")
    stamp = datetime.now().strftime("%H%M%S")
    key = f"{kind}/{dated}/{stamp}_{secrets.token_hex(4)}_{stem}{suffix}"
    return f"{clean_prefix}/{key}" if clean_prefix else key


def public_object_url(config: TOSConfig, key: str) -> str:
    if config.public_base_url.strip():
        return f"{config.public_base_url.rstrip('/')}/{quote(key, safe='/')}"
    host = _endpoint_host(config.endpoint)
    return f"https://{config.bucket}.{host}/{quote(key, safe='/')}"


def upload_media(config: TOSConfig, filename: str, content_type: str, data: bytes) -> dict[str, Any]:
    if not config.access_key or not config.secret_key:
        raise TOSUploadError("请配置 TOS Access Key ID 和 Secret Access Key。")
    if not config.bucket:
        raise TOSUploadError("请配置 TOS Bucket。")
    if config.url_mode not in {"signed", "public"}:
        raise TOSUploadError("TOS URL 模式只能是 signed 或 public。")

    try:
        import tos
        from tos import enum
    except ImportError as exc:
        raise TOSUploadError("缺少 TOS SDK，请先运行：python -m pip install -r requirements.txt") from exc

    kind, content_type, data = normalize_media(filename, content_type, data)
    key = build_object_key(config.prefix, filename, content_type, kind)
    client = tos.TosClientV2(
        config.access_key,
        config.secret_key,
        _endpoint_host(config.endpoint),
        config.region,
        auto_recognize_content_type=True,
    )
    acl = enum.ACLType.ACL_Public_Read if config.url_mode == "public" else None
    try:
        output = client.put_object(
            config.bucket,
            key,
            content=data,
            content_length=len(data),
            content_type=content_type,
            acl=acl,
        )
        if config.url_mode == "signed":
            signed = client.pre_signed_url(
                enum.HttpMethodType.Http_Method_Get,
                config.bucket,
                key,
                expires=config.signed_expires,
            )
            url = signed.signed_url
        else:
            url = public_object_url(config, key)
    except Exception as exc:
        raise TOSUploadError(f"TOS 上传失败：{exc}") from exc

    return {
        "bucket": config.bucket,
        "key": key,
        "url": url,
        "url_mode": config.url_mode,
        "etag": getattr(output, "etag", ""),
        "size": len(data),
        "content_type": content_type,
        "kind": kind,
    }


def upload_image(config: TOSConfig, filename: str, content_type: str, data: bytes) -> dict[str, Any]:
    return upload_media(config, filename, content_type, data)

from __future__ import annotations

import base64
import mimetypes
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse


IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 12 * 1024 * 1024


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


def parse_image_data(data_url: str, content_type: str = "") -> tuple[str, bytes]:
    data_url = data_url.strip()
    mime = content_type.strip().lower()
    payload = data_url
    match = re.match(r"^data:([^;,]+)?;base64,(.+)$", data_url, flags=re.DOTALL)
    if match:
        mime = (match.group(1) or mime).lower()
        payload = match.group(2)
    try:
        data = base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise TOSUploadError("图片数据不是有效的 base64。") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise TOSUploadError("图片不能超过 12 MB。")
    if mime not in IMAGE_MIME_TYPES:
        raise TOSUploadError("仅支持 JPEG、PNG、WebP 图片。")
    return mime, data


def _endpoint_host(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint:
        return "tos-cn-beijing.volces.com"
    parsed = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
    return parsed.netloc or parsed.path


def _clean_part(value: str, fallback: str) -> str:
    value = re.sub(r"[^\w\-.]+", "-", value, flags=re.UNICODE).strip("-_.")
    return value[:64] or fallback


def build_object_key(prefix: str, filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = mimetypes.guess_extension(content_type) or ".jpg"
        if suffix == ".jpe":
            suffix = ".jpg"
    stem = _clean_part(Path(filename).stem, "image")
    clean_prefix = "/".join(_clean_part(part, "refs") for part in prefix.split("/") if part.strip())
    dated = datetime.now().strftime("%Y%m%d")
    stamp = datetime.now().strftime("%H%M%S")
    key = f"{dated}/{stamp}_{secrets.token_hex(4)}_{stem}{suffix}"
    return f"{clean_prefix}/{key}" if clean_prefix else key


def public_object_url(config: TOSConfig, key: str) -> str:
    if config.public_base_url.strip():
        return f"{config.public_base_url.rstrip('/')}/{quote(key, safe='/')}"
    host = _endpoint_host(config.endpoint)
    return f"https://{config.bucket}.{host}/{quote(key, safe='/')}"


def upload_image(config: TOSConfig, filename: str, content_type: str, data: bytes) -> dict[str, Any]:
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

    key = build_object_key(config.prefix, filename, content_type)
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
    }

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ark_seedance import create_generation_task, download_file, get_generation_task
from tos_uploader import TOSConfig, TOSUploadError, upload_media


ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
STATIC = ROOT / "static"
CONFIG_PATH = ROOT / ".seedance_config.json"
HOST = os.environ.get("SEEDANCE_UI_HOST") or os.environ.get("JIMENG_UI_HOST") or "127.0.0.1"
PORT = int(os.environ.get("SEEDANCE_UI_PORT") or os.environ.get("JIMENG_UI_PORT") or "7860")


MODEL_CATALOG: dict[str, dict[str, Any]] = {
    "seedance_2": {
        "label": "Doubao Seedance 2.0",
        "model_id": "doubao-seedance-2-0-260128",
        "description": "标准质量，支持 480p / 720p / 1080p / 4k",
        "resolutions": ["480p", "720p", "1080p", "4k"],
    },
    "seedance_2_fast": {
        "label": "Doubao Seedance 2.0 Fast",
        "model_id": "doubao-seedance-2-0-fast-260128",
        "description": "快速版本，支持 480p / 720p",
        "resolutions": ["480p", "720p"],
    },
}

MODEL_SOURCE = (
    "模型 ID 来自火山方舟 Seedance 2.0 官方 API/SDK 文档；"
    "当前界面使用固定白名单，不是通过实时模型列表接口拉取。"
)
RATIOS = {"adaptive", "16:9", "4:3", "1:1", "3:4", "9:16", "21:9"}
RESOLUTIONS = {"480p", "720p", "1080p", "4k"}


@dataclass
class Job:
    id: str
    model_key: str
    prompt: str
    status: str = "created"
    message: str = "已创建"
    task_id: str | None = None
    video_url: str | None = None
    last_frame_url: str | None = None
    output: str | None = None
    last_frame_output: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    request_body: dict[str, Any] | None = None
    response: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        model = MODEL_CATALOG.get(self.model_key, {})
        data["model"] = model.get("label", self.model_key)
        data["model_id"] = model.get("model_id", self.model_key)
        data["created_at_text"] = datetime.fromtimestamp(self.created_at).strftime("%Y-%m-%d %H:%M:%S")
        if self.output:
            data["download_path"] = "/outputs/" + Path(self.output).name
        if self.last_frame_output:
            data["last_frame_path"] = "/outputs/" + Path(self.last_frame_output).name
        return data


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
memory_credentials: dict[str, str] = {}
memory_storage: dict[str, str] = {}


def read_config() -> dict[str, str]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {
        "api_key": str(data.get("api_key") or data.get("ark_api_key") or "").strip(),
        "tos_access_key": str(data.get("tos_access_key") or data.get("tos_ak") or "").strip(),
        "tos_secret_key": str(data.get("tos_secret_key") or data.get("tos_sk") or "").strip(),
        "tos_bucket": str(data.get("tos_bucket") or "").strip(),
        "tos_region": str(data.get("tos_region") or "cn-beijing").strip(),
        "tos_endpoint": str(data.get("tos_endpoint") or "tos-cn-beijing.volces.com").strip(),
        "tos_prefix": str(data.get("tos_prefix") or "seedance-references").strip(),
        "tos_url_mode": str(data.get("tos_url_mode") or "signed").strip(),
        "tos_signed_expires": str(data.get("tos_signed_expires") or "86400").strip(),
        "tos_public_base_url": str(data.get("tos_public_base_url") or "").strip(),
    }


def write_config(data: dict[str, str]) -> None:
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_api_config(api_key: str) -> None:
    cfg = read_config()
    cfg["api_key"] = api_key
    write_config(cfg)


def write_storage_config(storage: dict[str, str]) -> None:
    cfg = read_config()
    cfg.update(storage)
    write_config(cfg)


def mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return "*" * len(value)
    return value[:7] + "*" * (len(value) - 12) + value[-5:]


def credential_status() -> dict[str, Any]:
    cfg = read_config()
    env_key = os.environ.get("ARK_API_KEY") or os.environ.get("VOLC_ARK_API_KEY")
    api_key = env_key or memory_credentials.get("api_key") or cfg.get("api_key", "")
    source = "none"
    if env_key:
        source = "env"
    elif memory_credentials.get("api_key"):
        source = "session"
    elif cfg.get("api_key"):
        source = "local"
    return {"configured": bool(api_key), "api_key": mask_key(api_key), "source": source}


def storage_status() -> dict[str, Any]:
    cfg = read_config()
    env_ak = os.environ.get("TOS_ACCESSKEY") or os.environ.get("VOLC_ACCESSKEY")
    env_sk = os.environ.get("TOS_SECRETKEY") or os.environ.get("VOLC_SECRETKEY")
    access_key = env_ak or memory_storage.get("tos_access_key") or cfg.get("tos_access_key", "")
    secret_key = env_sk or memory_storage.get("tos_secret_key") or cfg.get("tos_secret_key", "")
    bucket = os.environ.get("TOS_BUCKET") or memory_storage.get("tos_bucket") or cfg.get("tos_bucket", "")
    source = "none"
    if env_ak and env_sk and bucket:
        source = "env"
    elif memory_storage:
        source = "session"
    elif cfg.get("tos_access_key") and cfg.get("tos_secret_key") and cfg.get("tos_bucket"):
        source = "local"
    return {
        "configured": bool(access_key and secret_key and bucket),
        "source": source,
        "access_key": mask_key(access_key),
        "secret_key": mask_key(secret_key),
        "bucket": bucket,
        "region": os.environ.get("TOS_REGION") or memory_storage.get("tos_region") or cfg.get("tos_region", "cn-beijing"),
        "endpoint": os.environ.get("TOS_ENDPOINT") or memory_storage.get("tos_endpoint") or cfg.get("tos_endpoint", "tos-cn-beijing.volces.com"),
        "prefix": os.environ.get("TOS_PREFIX") or memory_storage.get("tos_prefix") or cfg.get("tos_prefix", "seedance-references"),
        "url_mode": os.environ.get("TOS_URL_MODE") or memory_storage.get("tos_url_mode") or cfg.get("tos_url_mode", "signed"),
        "signed_expires": os.environ.get("TOS_SIGNED_EXPIRES")
        or memory_storage.get("tos_signed_expires")
        or cfg.get("tos_signed_expires", "86400"),
        "public_base_url": os.environ.get("TOS_PUBLIC_BASE_URL")
        or memory_storage.get("tos_public_base_url")
        or cfg.get("tos_public_base_url", ""),
    }


def resolve_storage_config(payload: dict[str, Any] | None = None) -> TOSConfig:
    payload = payload or {}
    cfg = read_config()

    def value(name: str, env_name: str = "") -> str:
        return (
            str(payload.get(name) or "").strip()
            or (os.environ.get(env_name) if env_name else "")
            or memory_storage.get(name)
            or cfg.get(name, "")
        )

    access_key = value("tos_access_key", "TOS_ACCESSKEY") or os.environ.get("VOLC_ACCESSKEY", "")
    secret_key = value("tos_secret_key", "TOS_SECRETKEY") or os.environ.get("VOLC_SECRETKEY", "")
    bucket = value("tos_bucket", "TOS_BUCKET")
    region = value("tos_region", "TOS_REGION") or "cn-beijing"
    endpoint = value("tos_endpoint", "TOS_ENDPOINT") or "tos-cn-beijing.volces.com"
    prefix = value("tos_prefix", "TOS_PREFIX") or "seedance-references"
    url_mode = value("tos_url_mode", "TOS_URL_MODE") or "signed"
    public_base_url = value("tos_public_base_url", "TOS_PUBLIC_BASE_URL")
    try:
        signed_expires = int(value("tos_signed_expires", "TOS_SIGNED_EXPIRES") or 86400)
    except ValueError as exc:
        raise ValueError("TOS 预签名有效期必须是数字。") from exc
    if signed_expires < 60 or signed_expires > 604800:
        raise ValueError("TOS 预签名有效期建议设置在 60-604800 秒之间。")
    return TOSConfig(
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        region=region,
        endpoint=endpoint,
        prefix=prefix,
        url_mode=url_mode,
        signed_expires=signed_expires,
        public_base_url=public_base_url,
    )


def resolve_api_key(payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    cfg = read_config()
    api_key = (
        str(payload.get("api_key") or "").strip()
        or os.environ.get("ARK_API_KEY")
        or os.environ.get("VOLC_ARK_API_KEY")
        or memory_credentials.get("api_key")
        or cfg.get("api_key", "")
    )
    if not api_key:
        raise ValueError("请先配置 Ark API Key。")
    return api_key


def clean_filename(text: str) -> str:
    text = re.sub(r"[^\w\-.]+", "_", text, flags=re.UNICODE).strip("_")
    return text[:72] or "seedance_video"


def text_list(payload: dict[str, Any], key: str) -> list[str]:
    raw = payload.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(item).strip() for item in raw if str(item).strip()]


def build_content(payload: dict[str, Any]) -> list[dict[str, Any]]:
    prompt = str(payload.get("prompt") or "").strip()
    image_urls = text_list(payload, "image_urls")
    video_url = str(payload.get("video_url") or "").strip()
    audio_url = str(payload.get("audio_url") or "").strip()
    if not prompt and not (image_urls or video_url or audio_url):
        raise ValueError("请填写提示词或至少提供一个参考素材 URL。")
    if len(image_urls) > 9:
        raise ValueError("参考图片最多 9 张。")

    content: list[dict[str, Any]] = []
    if prompt:
        content.append({"type": "text", "text": prompt})
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}, "role": "reference_image"})
    if video_url:
        content.append({"type": "video_url", "video_url": {"url": video_url}, "role": "reference_video"})
    if audio_url:
        content.append({"type": "audio_url", "audio_url": {"url": audio_url}, "role": "reference_audio"})
    return content


def build_body(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    model_key = str(payload.get("model") or "seedance_2")
    model = MODEL_CATALOG.get(model_key)
    if not model:
        raise ValueError("未知模型。")

    duration = int(payload.get("duration") or 5)
    if duration != -1 and not 4 <= duration <= 15:
        raise ValueError("Seedance 2.0 的 duration 需为 4-15 秒，或 -1。")

    ratio = str(payload.get("ratio") or "16:9")
    if ratio not in RATIOS:
        raise ValueError("比例参数不正确。")

    resolution = str(payload.get("resolution") or "1080p")
    if resolution not in RESOLUTIONS:
        raise ValueError("分辨率参数不正确。")
    if resolution not in model["resolutions"]:
        raise ValueError(f"{model['label']} 不支持 {resolution}。")

    seed = int(payload.get("seed") if str(payload.get("seed", "")).strip() else -1)
    if seed < -1 or seed > 2**32 - 1:
        raise ValueError("Seed 取值范围为 -1 到 2^32-1。")

    body: dict[str, Any] = {
        "model": model["model_id"],
        "content": build_content(payload),
        "duration": duration,
        "ratio": ratio,
        "resolution": resolution,
        "seed": seed,
        "watermark": bool(payload.get("watermark")),
        "generate_audio": bool(payload.get("generate_audio")),
        "return_last_frame": bool(payload.get("return_last_frame")),
    }

    safety_identifier = str(payload.get("safety_identifier") or "").strip()
    if safety_identifier:
        if len(safety_identifier) > 64:
            raise ValueError("safety_identifier 长度不能超过 64。")
        body["safety_identifier"] = safety_identifier

    if bool(payload.get("web_search")):
        body["tools"] = [{"type": "web_search"}]

    expires = str(payload.get("execution_expires_after") or "").strip()
    if expires:
        seconds = int(expires)
        if not 3600 <= seconds <= 259200:
            raise ValueError("execution_expires_after 取值范围为 3600-259200 秒。")
        body["execution_expires_after"] = seconds

    return model_key, body


def status_label(status: str) -> str:
    return {
        "created": "已创建",
        "submitting": "提交任务中",
        "queued": "排队中",
        "running": "生成中",
        "succeeded": "已完成",
        "failed": "任务失败",
        "expired": "任务过期",
        "cancelled": "已取消",
        "error": "失败",
    }.get(status, status)


def run_job(job_id: str, body: dict[str, Any], api_key: str) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.status = "submitting"
        job.message = status_label("submitting")
        job.request_body = body
        job.updated_at = time.time()
    try:
        task_id, create_response = create_generation_task(body, api_key)
        with jobs_lock:
            job = jobs[job_id]
            job.task_id = task_id
            job.status = "queued"
            job.message = status_label("queued")
            job.response = create_response
            job.updated_at = time.time()

        result: dict[str, Any] | None = None
        for _ in range(360):
            result = get_generation_task(task_id, api_key)
            status = str(result.get("status") or "unknown")
            error = result.get("error") or {}
            with jobs_lock:
                job = jobs[job_id]
                job.status = status
                job.message = error.get("message") if status == "failed" and isinstance(error, dict) else status_label(status)
                job.response = result
                job.updated_at = time.time()
            if status == "succeeded":
                content = result.get("content") or {}
                video_url = str(content.get("video_url") or "").strip()
                if not video_url:
                    raise RuntimeError("任务完成但没有返回 video_url。")
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output = OUTPUTS / f"{stamp}_{clean_filename(str(body.get('model')))}_{task_id}.mp4"
                download_file(video_url, output)

                last_frame_url = str(content.get("last_frame_url") or "").strip()
                last_frame_output: Path | None = None
                if last_frame_url:
                    last_frame_output = output.with_suffix(".last_frame.png")
                    download_file(last_frame_url, last_frame_output)

                meta_path = output.with_suffix(".json")
                meta_path.write_text(
                    json.dumps(
                        {"task_id": task_id, "request": body, "create_response": create_response, "result": result},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                with jobs_lock:
                    job = jobs[job_id]
                    job.video_url = video_url
                    job.last_frame_url = last_frame_url or None
                    job.output = str(output)
                    job.last_frame_output = str(last_frame_output) if last_frame_output else None
                    job.message = "已完成，视频已保存到本机"
                    job.updated_at = time.time()
                return
            if status in {"failed", "expired", "cancelled"}:
                raise RuntimeError(job.message)
            time.sleep(5)
        raise TimeoutError("任务轮询超时。")
    except Exception as exc:
        with jobs_lock:
            job = jobs[job_id]
            job.status = "error"
            job.message = str(exc)
            job.updated_at = time.time()


def history() -> list[dict[str, Any]]:
    OUTPUTS.mkdir(exist_ok=True)
    items = []
    for file in sorted(OUTPUTS.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = file.stat()
        items.append(
            {
                "name": file.name,
                "url": "/outputs/" + file.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return items[:30]


def _parse_header_params(value: str) -> dict[str, str]:
    parts = [part.strip() for part in value.split(";")]
    params: dict[str, str] = {"": parts[0].lower() if parts else ""}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        params[key.strip().lower()] = raw.strip().strip('"')
    return params


def parse_multipart(content_type: str, body: bytes) -> dict[str, Any]:
    params = _parse_header_params(content_type)
    boundary = params.get("boundary", "")
    if not boundary:
        raise ValueError("multipart 请求缺少 boundary。")
    marker = b"--" + boundary.encode("utf-8")
    payload: dict[str, Any] = {}
    for part in body.split(marker):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].rstrip(b"\r\n")
        header_blob, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers: dict[str, str] = {}
        for line in header_blob.decode("utf-8", errors="replace").split("\r\n"):
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            headers[key.strip().lower()] = raw.strip()
        disposition = _parse_header_params(headers.get("content-disposition", ""))
        name = disposition.get("name", "")
        if not name:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        filename = disposition.get("filename")
        if filename:
            payload[name] = {
                "file_name": Path(filename).name,
                "content_type": headers.get("content-type", ""),
                "data": data,
            }
        else:
            payload[name] = data.decode("utf-8", errors="replace")
    return payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_html(INDEX_HTML)
        elif path == "/api/config":
            self.send_json(credential_status())
        elif path == "/api/storage/config":
            self.send_json(storage_status())
        elif path == "/api/models":
            self.send_json({"models": MODEL_CATALOG, "source": MODEL_SOURCE, "ratios": sorted(RATIOS)})
        elif path == "/api/history":
            self.send_json({"items": history()})
        elif path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with jobs_lock:
                job = jobs.get(job_id)
                data = job.as_dict() if job else None
            if not data:
                self.send_error_json(HTTPStatus.NOT_FOUND, "任务不存在。")
            else:
                self.send_json(data)
        elif path.startswith("/outputs/"):
            self.send_output(path)
        elif path.startswith("/static/"):
            self.send_static(path)
        else:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self.read_payload(max_bytes=360 * 1024 * 1024 if parsed.path == "/api/upload-reference" else 256 * 1024)
            if parsed.path == "/api/config":
                self.save_config(payload)
            elif parsed.path == "/api/storage/config":
                self.save_storage_config(payload)
            elif parsed.path == "/api/upload-reference":
                self.upload_reference(payload)
            elif parsed.path == "/api/generate":
                self.generate(payload)
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def save_config(self, payload: dict[str, Any]) -> None:
        api_key = str(payload.get("api_key") or "").strip()
        if not api_key:
            raise ValueError("请填写 Ark API Key。")
        memory_credentials["api_key"] = api_key
        if bool(payload.get("save")):
            write_api_config(api_key)
        self.send_json(credential_status())

    def save_storage_config(self, payload: dict[str, Any]) -> None:
        storage = resolve_storage_config(payload)
        memory_storage.update(
            {
                "tos_access_key": storage.access_key,
                "tos_secret_key": storage.secret_key,
                "tos_bucket": storage.bucket,
                "tos_region": storage.region,
                "tos_endpoint": storage.endpoint,
                "tos_prefix": storage.prefix,
                "tos_url_mode": storage.url_mode,
                "tos_signed_expires": str(storage.signed_expires),
                "tos_public_base_url": storage.public_base_url,
            }
        )
        if bool(payload.get("save")):
            write_storage_config(memory_storage)
        self.send_json(storage_status())

    def upload_reference(self, payload: dict[str, Any]) -> None:
        file_payload = payload.get("file")
        if not isinstance(file_payload, dict) or not file_payload.get("data"):
            raise ValueError("请选择要上传的图片、视频或音频。")
        filename = str(file_payload.get("file_name") or payload.get("file_name") or "reference.bin").strip()
        content_type = str(file_payload.get("content_type") or payload.get("content_type") or "").strip()
        storage_payload = payload.get("storage") if isinstance(payload.get("storage"), dict) else payload
        storage = resolve_storage_config(storage_payload)
        result = upload_media(storage, filename, content_type, file_payload["data"])
        self.send_json(result)

    def generate(self, payload: dict[str, Any]) -> None:
        api_key = resolve_api_key(payload)
        model_key, body = build_body(payload)
        job_id = secrets.token_urlsafe(10)
        job = Job(id=job_id, model_key=model_key, prompt=str(payload.get("prompt") or ""))
        with jobs_lock:
            jobs[job_id] = job
        thread = threading.Thread(target=run_job, args=(job_id, body, api_key), daemon=True)
        thread.start()
        self.send_json(job.as_dict())

    def read_payload(self, max_bytes: int = 256 * 1024) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > max_bytes:
            raise ValueError("请求过大。图片不超过 12 MB，视频不超过 300 MB，音频不超过 80 MB。")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        content_type = self.headers.get("Content-Type", "")
        if content_type.lower().startswith("multipart/form-data"):
            return parse_multipart(content_type, raw)
        return json.loads(raw.decode("utf-8"))

    def send_output(self, path: str) -> None:
        name = unquote(path.removeprefix("/outputs/"))
        file = (OUTPUTS / name).resolve()
        if OUTPUTS.resolve() not in file.parents or not file.exists():
            self.send_error_json(HTTPStatus.NOT_FOUND, "文件不存在。")
            return
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        data = file.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_static(self, path: str) -> None:
        name = unquote(path.removeprefix("/static/"))
        file = (STATIC / name).resolve()
        static_root = STATIC.resolve()
        if static_root not in file.parents or not file.is_file():
            self.send_error_json(HTTPStatus.NOT_FOUND, "文件不存在。")
            return
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        data = file.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"error": message}, status=status)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>SHIMEI Video Studio</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101115;
      --panel: rgba(20, 22, 29, .76);
      --panel-strong: rgba(24, 27, 36, .92);
      --line: rgba(198, 232, 255, .18);
      --text: #f6f7fb;
      --muted: #a8b0be;
      --cyan: #34d6ff;
      --lime: #b7ff5a;
      --pink: #ff4fb8;
      --amber: #ffc44d;
      --danger: #ff6b6b;
      --shadow: 0 24px 80px rgba(0, 0, 0, .38);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font: 14px/1.45 "Inter", "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      letter-spacing: 0;
      background:
        linear-gradient(135deg, rgba(52, 214, 255, .12), transparent 32%),
        linear-gradient(315deg, rgba(255, 79, 184, .11), transparent 34%),
        linear-gradient(180deg, #101115 0%, #17151d 48%, #11151a 100%);
      overflow-x: hidden;
      isolation: isolate;
    }
    body::before, body::after {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      z-index: -1;
    }
    body::before {
      background:
        linear-gradient(90deg, rgba(52, 214, 255, .08) 1px, transparent 1px),
        linear-gradient(0deg, rgba(183, 255, 90, .055) 1px, transparent 1px),
        repeating-linear-gradient(118deg, transparent 0 88px, rgba(255, 196, 77, .13) 89px, transparent 92px);
      background-size: 52px 52px, 52px 52px, 280px 280px;
      mask-image: linear-gradient(180deg, #000, transparent 86%);
    }
    body::after {
      background: linear-gradient(180deg, rgba(255,255,255,.07), transparent 26%);
    }
    button, input, textarea, select { font: inherit; letter-spacing: 0; }
    .shell {
      width: min(1160px, calc(100vw - 28px));
      margin: 0 auto;
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr auto;
      gap: 18px;
      padding: 18px 0 22px;
    }
    .nav {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-height: 44px;
    }
    .brand {
      display: inline-flex;
      align-items: center;
      gap: 12px;
      font-weight: 760;
      font-size: 16px;
    }
    .brand-logo {
      width: 118px;
      height: auto;
      display: block;
      filter: drop-shadow(0 0 20px rgba(48, 84, 218, .28));
    }
    .brand-name {
      color: rgba(246, 247, 251, .9);
      white-space: nowrap;
    }
    .pill-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .pill {
      min-height: 30px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: rgba(255,255,255,.06);
      font-size: 12px;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--danger);
      box-shadow: 0 0 0 4px rgba(255, 107, 107, .13);
    }
    .dot.ok {
      background: var(--lime);
      box-shadow: 0 0 0 4px rgba(183, 255, 90, .12);
    }
    .hero {
      display: grid;
      align-content: center;
      gap: 18px;
      padding: 16px 0 6px;
    }
    .headline {
      text-align: center;
      display: grid;
      gap: 8px;
    }
    h1 {
      margin: 0;
      font-size: clamp(34px, 7vw, 86px);
      line-height: .96;
      font-weight: 820;
    }
    .accent {
      background: linear-gradient(100deg, #fff, var(--cyan) 35%, var(--lime) 62%, var(--pink));
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
    }
    .subtitle {
      color: var(--muted);
      font-size: 15px;
    }
    .composer {
      width: min(920px, 100%);
      margin: 0 auto;
      border: 1px solid rgba(255,255,255,.18);
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(255,255,255,.11), rgba(255,255,255,.065));
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
      overflow: hidden;
      position: relative;
    }
    .composer textarea {
      width: 100%;
      min-height: 190px;
      resize: vertical;
      border: 0;
      outline: none;
      color: var(--text);
      background: transparent;
      padding: 18px;
      font-size: 15px;
    }
    .composer textarea::placeholder { color: #7f8795; }
    .controls {
      display: grid;
      gap: 12px;
      padding: 0 14px 14px;
    }
    .params, .actions, .asset-grid {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .select, .input, .btn, .file-btn {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 10px;
      color: var(--text);
      background: rgba(8, 10, 14, .52);
      outline: none;
    }
    .select, .input {
      padding: 0 10px;
    }
    .input.small { width: 72px; }
    .input.url { min-width: min(360px, 100%); flex: 1; }
    .btn, .file-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 0 12px;
      cursor: pointer;
      text-decoration: none;
    }
    .btn.primary {
      min-width: 126px;
      border-color: rgba(52, 214, 255, .58);
      background: linear-gradient(135deg, var(--cyan), var(--pink));
      font-weight: 760;
      color: #080a0f;
    }
    .btn:disabled { opacity: .56; cursor: not-allowed; }
    .mention-trigger {
      min-width: 38px;
      font-weight: 800;
      color: var(--cyan);
    }
    .mention-menu {
      position: absolute;
      left: 18px;
      top: 66px;
      z-index: 20;
      width: min(330px, calc(100% - 36px));
      max-height: 285px;
      overflow: auto;
      padding: 8px;
      border: 1px solid rgba(52, 214, 255, .28);
      border-radius: 12px;
      background: rgba(12, 14, 20, .96);
      box-shadow: 0 20px 70px rgba(0, 0, 0, .42);
    }
    .mention-item {
      width: 100%;
      min-height: 52px;
      border: 0;
      border-radius: 10px;
      padding: 8px 10px;
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 8px;
      align-items: center;
      color: var(--text);
      background: transparent;
      text-align: left;
      cursor: pointer;
    }
    .mention-item:hover, .mention-item.active {
      background: rgba(52, 214, 255, .12);
    }
    .mention-item:disabled {
      cursor: not-allowed;
      opacity: .45;
    }
    .mention-token {
      min-width: 52px;
      min-height: 28px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 999px;
      color: #071017;
      background: linear-gradient(135deg, var(--cyan), var(--lime));
      font-size: 12px;
      font-weight: 800;
    }
    .mention-meta {
      min-width: 0;
      display: grid;
      gap: 2px;
      overflow-wrap: anywhere;
    }
    .mention-name {
      font-size: 13px;
      font-weight: 760;
    }
    .mention-url {
      color: var(--muted);
      font-size: 12px;
    }
    .asset-panel {
      display: grid;
      gap: 8px;
      padding: 10px;
      border: 1px solid rgba(255,255,255,.12);
      border-radius: 12px;
      background: rgba(0,0,0,.18);
    }
    .asset-title {
      display: flex;
      justify-content: space-between;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }
    .asset-grid {
      align-items: stretch;
    }
    .asset {
      min-width: min(210px, 100%);
      flex: 1;
      display: grid;
      gap: 7px;
    }
    .asset label, .field label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }
    .upload-line {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 7px;
    }
    input[type="file"] {
      min-width: 0;
      padding: 7px;
    }
    .status {
      min-height: 18px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .toggles {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
    }
    .toggles input { width: auto; margin-right: 6px; }
    .actions {
      justify-content: space-between;
      border-top: 1px solid rgba(255,255,255,.12);
      padding-top: 12px;
    }
    .panel-row {
      width: min(920px, 100%);
      margin: 0 auto;
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(280px, 360px);
      gap: 12px;
    }
    .panel {
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--panel);
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .panel-inner {
      padding: 13px;
      display: grid;
      gap: 10px;
    }
    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      color: var(--muted);
      font-weight: 700;
      font-size: 13px;
    }
    video, .last-frame {
      width: 100%;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: #05070a;
      display: block;
    }
    video { aspect-ratio: 16 / 9; }
    .jobbox {
      display: grid;
      gap: 8px;
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .progress {
      height: 7px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(255,255,255,.12);
    }
    .bar {
      height: 100%;
      width: 0;
      background: linear-gradient(90deg, var(--cyan), var(--lime), var(--pink));
      transition: width .25s ease;
    }
    details {
      width: min(920px, 100%);
      margin: 0 auto;
      border: 1px solid rgba(255,255,255,.14);
      border-radius: 14px;
      background: rgba(10, 12, 18, .52);
      overflow: hidden;
    }
    summary {
      min-height: 44px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 0 14px;
      cursor: pointer;
      color: var(--muted);
      font-weight: 700;
    }
    .settings {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      padding: 0 14px 14px;
    }
    .field { display: grid; gap: 6px; min-width: 0; }
    .field input, .field select {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 0 10px;
      color: var(--text);
      background: rgba(8, 10, 14, .52);
      outline: none;
    }
    .history {
      display: grid;
      gap: 8px;
      max-height: 260px;
      overflow: auto;
    }
    .history a {
      display: grid;
      gap: 3px;
      padding: 9px;
      border: 1px solid rgba(255,255,255,.12);
      border-radius: 10px;
      color: var(--text);
      text-decoration: none;
      background: rgba(255,255,255,.05);
      overflow-wrap: anywhere;
    }
    .muted { color: var(--muted); }
    .small { font-size: 12px; }
    .hidden { display: none !important; }
    @media (max-width: 880px) {
      .panel-row { grid-template-columns: 1fr; }
      .settings { grid-template-columns: 1fr; }
      .actions { align-items: stretch; }
      .btn.primary { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header class="nav">
      <div class="brand"><img class="brand-logo" src="/static/logo.png" alt="SHIMEI" /><span class="brand-name">Video Studio</span></div>
      <div class="pill-row">
        <span class="pill"><span id="keyDot" class="dot"></span><span id="keyStatus">Ark 未配置</span></span>
        <span class="pill"><span id="storageDot" class="dot"></span><span id="storageStatus">TOS 未配置</span></span>
      </div>
    </header>

    <main class="hero">
      <section class="headline">
        <h1><span class="accent">一键生成电影级视频</span></h1>
        <div class="subtitle">输入 Prompt，上传参考图 / 视频 / 音频，自动提交到 Doubao Seedance 2.0</div>
      </section>

      <section class="composer">
        <textarea id="prompt" spellcheck="false" placeholder="使用 @ 快速引用参考内容，例如：@图片1 模仿 @视频1 的动作，音色参考 @音频1。"></textarea>
        <div id="mentionMenu" class="mention-menu hidden"></div>
        <div class="controls">
          <div class="params">
            <select id="model" class="select"></select>
            <select id="resolution" class="select">
              <option>1080p</option>
              <option>4k</option>
              <option>720p</option>
              <option>480p</option>
            </select>
            <select id="ratio" class="select">
              <option>16:9</option>
              <option>9:16</option>
              <option>1:1</option>
              <option>adaptive</option>
              <option>4:3</option>
              <option>3:4</option>
              <option>21:9</option>
            </select>
            <input id="duration" class="input small" type="number" min="4" max="15" value="5" title="时长" />
            <input id="seed" class="input small" type="number" value="-1" title="Seed" />
            <button id="mentionButton" class="btn mention-trigger" type="button" title="引用素材">@</button>
          </div>

          <div class="asset-panel">
            <div class="asset-title"><span>参考素材</span><span>使用 @图片1 / @视频1 / @音频1 引用</span></div>
            <div class="asset-grid">
              <div class="asset">
                <label for="imageFile1">图片1</label>
                <input id="imageUrl1" class="input url" placeholder="图片 URL 或上传后自动填入" />
                <div class="upload-line">
                  <input id="imageFile1" type="file" accept="image/png,image/jpeg,image/webp" />
                  <button type="button" class="btn" data-upload-kind="image" data-upload-slot="1">上传</button>
                </div>
                <div id="imageUpload1" class="status"></div>
              </div>
              <div class="asset">
                <label for="imageFile2">图片2</label>
                <input id="imageUrl2" class="input url" placeholder="图片 URL 或上传后自动填入" />
                <div class="upload-line">
                  <input id="imageFile2" type="file" accept="image/png,image/jpeg,image/webp" />
                  <button type="button" class="btn" data-upload-kind="image" data-upload-slot="2">上传</button>
                </div>
                <div id="imageUpload2" class="status"></div>
              </div>
              <div class="asset">
                <label for="imageFile3">图片3</label>
                <input id="imageUrl3" class="input url" placeholder="图片 URL 或上传后自动填入" />
                <div class="upload-line">
                  <input id="imageFile3" type="file" accept="image/png,image/jpeg,image/webp" />
                  <button type="button" class="btn" data-upload-kind="image" data-upload-slot="3">上传</button>
                </div>
                <div id="imageUpload3" class="status"></div>
              </div>
              <div class="asset">
                <label for="imageFile4">图片4</label>
                <input id="imageUrl4" class="input url" placeholder="图片 URL 或上传后自动填入" />
                <div class="upload-line">
                  <input id="imageFile4" type="file" accept="image/png,image/jpeg,image/webp" />
                  <button type="button" class="btn" data-upload-kind="image" data-upload-slot="4">上传</button>
                </div>
                <div id="imageUpload4" class="status"></div>
              </div>
              <div class="asset">
                <label for="videoFile">视频1</label>
                <input id="videoUrl" class="input url" placeholder="参考视频 URL 或上传后自动填入" />
                <div class="upload-line">
                  <input id="videoFile" type="file" accept="video/mp4,video/quicktime,video/webm" />
                  <button type="button" class="btn" data-upload-kind="video">上传</button>
                </div>
                <div id="videoUpload" class="status"></div>
              </div>
              <div class="asset">
                <label for="audioFile">音频1</label>
                <input id="audioUrl" class="input url" placeholder="参考音频 URL 或上传后自动填入" />
                <div class="upload-line">
                  <input id="audioFile" type="file" accept="audio/mpeg,audio/mp3,audio/wav,audio/x-wav,audio/mp4,audio/aac,audio/ogg" />
                  <button type="button" class="btn" data-upload-kind="audio">上传</button>
                </div>
                <div id="audioUpload" class="status"></div>
              </div>
            </div>
          </div>

          <div class="toggles">
            <label><input id="generateAudio" type="checkbox" />生成音频</label>
            <label><input id="returnLastFrame" type="checkbox" />返回尾帧</label>
            <label><input id="watermark" type="checkbox" />水印</label>
            <label><input id="webSearch" type="checkbox" />联网搜索</label>
          </div>

          <div class="actions">
            <span id="charCount" class="muted small">0 字</span>
            <div class="pill-row">
              <button id="clearForm" class="btn">清空</button>
              <button id="generate" class="btn primary">生成视频</button>
            </div>
          </div>
        </div>
      </section>

      <div class="panel-row">
        <section class="panel">
          <div class="panel-inner">
            <div class="panel-head"><span>生成结果</span><span id="taskCaption">等待任务</span></div>
            <video id="video" controls class="hidden"></video>
            <img id="lastFrame" class="last-frame hidden" alt="" />
            <div id="job" class="jobbox">
              <strong>未开始</strong>
              <span class="small">提交后会自动轮询并下载到 outputs/</span>
              <div class="progress"><div class="bar"></div></div>
            </div>
            <a id="download" class="btn primary hidden" download>下载 MP4</a>
          </div>
        </section>
        <section class="panel">
          <div class="panel-inner">
            <div class="panel-head"><span>历史</span><button id="refreshHistory" class="btn">刷新</button></div>
            <div id="history" class="history"></div>
          </div>
        </section>
      </div>

      <details>
        <summary><span>高级配置</span><span id="configHint">默认读取本机配置</span></summary>
        <div class="settings">
          <div class="field">
            <label for="apiKey">Ark API Key</label>
            <input id="apiKey" type="password" placeholder="默认读取本机配置" />
          </div>
          <div class="field">
            <label for="tosBucket">TOS Bucket</label>
            <input id="tosBucket" value="miles" />
          </div>
          <div class="field">
            <label for="tosPrefix">TOS 前缀</label>
            <input id="tosPrefix" value="seedance-references" />
          </div>
          <div class="field">
            <label for="tosAccessKey">TOS Access Key ID</label>
            <input id="tosAccessKey" placeholder="默认读取本机配置" />
          </div>
          <div class="field">
            <label for="tosSecretKey">TOS Secret Access Key</label>
            <input id="tosSecretKey" type="password" placeholder="默认读取本机配置" />
          </div>
          <div class="field">
            <label for="tosRegion">TOS Region</label>
            <input id="tosRegion" value="cn-beijing" />
          </div>
          <div class="field">
            <label for="tosEndpoint">TOS Endpoint</label>
            <input id="tosEndpoint" value="tos-cn-beijing.volces.com" />
          </div>
          <div class="field">
            <label for="tosUrlMode">URL 模式</label>
            <select id="tosUrlMode"><option value="signed">预签名 URL</option><option value="public">公开 URL</option></select>
          </div>
          <div class="field">
            <label for="tosSignedExpires">预签名有效期</label>
            <input id="tosSignedExpires" type="number" value="86400" />
          </div>
          <div class="field">
            <label for="tosPublicBase">公开 Base URL</label>
            <input id="tosPublicBase" placeholder="可选" />
          </div>
          <div class="field">
            <label for="safetyIdentifier">Safety Identifier</label>
            <input id="safetyIdentifier" maxlength="64" placeholder="可选" />
          </div>
          <div class="field">
            <label>&nbsp;</label>
            <button id="saveAllConfig" class="btn">保存本机配置</button>
          </div>
        </div>
      </details>
    </main>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    let models = {};
    let currentJob = null;

    async function api(path, options = {}) {
      const headers = options.body instanceof FormData ? (options.headers || {}) : {"Content-Type": "application/json", ...(options.headers || {})};
      const res = await fetch(path, {...options, headers});
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "请求失败");
      return data;
    }

    function storagePayload() {
      return {
        tos_access_key: $("tosAccessKey").value.trim(),
        tos_secret_key: $("tosSecretKey").value.trim(),
        tos_bucket: $("tosBucket").value.trim(),
        tos_region: $("tosRegion").value.trim(),
        tos_endpoint: $("tosEndpoint").value.trim(),
        tos_prefix: $("tosPrefix").value.trim(),
        tos_url_mode: $("tosUrlMode").value,
        tos_signed_expires: $("tosSignedExpires").value.trim(),
        tos_public_base_url: $("tosPublicBase").value.trim()
      };
    }

    function renderConfig(data) {
      $("keyDot").classList.toggle("ok", !!data.configured);
      $("keyStatus").textContent = data.configured ? "Ark 已配置" : "Ark 未配置";
      if (data.api_key) $("apiKey").placeholder = data.api_key;
    }

    function renderStorageConfig(data) {
      $("storageDot").classList.toggle("ok", !!data.configured);
      $("storageStatus").textContent = data.configured ? `TOS ${data.bucket || "已配置"}` : "TOS 未配置";
      if (data.access_key) $("tosAccessKey").placeholder = data.access_key;
      if (data.secret_key) $("tosSecretKey").placeholder = data.secret_key;
      const mapping = {bucket:"tosBucket", region:"tosRegion", endpoint:"tosEndpoint", prefix:"tosPrefix", signed_expires:"tosSignedExpires", public_base_url:"tosPublicBase"};
      Object.entries(mapping).forEach(([key, id]) => { if (data[key] && !$(id).value) $(id).value = data[key]; });
      if (data.url_mode) $("tosUrlMode").value = data.url_mode;
    }

    function renderModels(data) {
      models = data.models;
      $("model").innerHTML = Object.entries(models).map(([key, model]) => `<option value="${key}">${model.label}</option>`).join("");
      updateModelFields();
    }

    function updateModelFields() {
      const model = models[$("model").value] || {};
      [...$("resolution").options].forEach(option => {
        option.disabled = model.resolutions && !model.resolutions.includes(option.value);
      });
      if (model.resolutions && !model.resolutions.includes($("resolution").value)) {
        $("resolution").value = model.resolutions.includes("1080p") ? "1080p" : model.resolutions[model.resolutions.length - 1];
      }
    }

    function progressFor(status) {
      return {created:8, submitting:18, queued:36, running:68, succeeded:100, failed:100, expired:100, cancelled:100, error:100}[status] || 48;
    }

    function resetJobView() {
      $("video").classList.add("hidden");
      $("video").removeAttribute("src");
      $("lastFrame").classList.add("hidden");
      $("lastFrame").removeAttribute("src");
      $("download").classList.add("hidden");
      $("download").removeAttribute("href");
    }

    function renderJob(job) {
      const pct = progressFor(job.status);
      $("taskCaption").textContent = job.task_id ? `Task ${job.task_id}` : (job.model || "等待任务");
      $("job").innerHTML = `
        <strong>${job.message || job.status}</strong>
        <span class="small">${job.model_id || job.model || ""}</span>
        ${job.task_id ? `<span class="small">task_id: ${job.task_id}</span>` : ""}
        <div class="progress"><div class="bar" style="width:${pct}%"></div></div>
      `;
      if (job.status === "succeeded" && job.download_path) {
        $("video").src = job.download_path;
        $("video").classList.remove("hidden");
        $("download").href = job.download_path;
        $("download").classList.remove("hidden");
        if (job.last_frame_path) {
          $("lastFrame").src = job.last_frame_path;
          $("lastFrame").classList.remove("hidden");
        }
        loadHistory();
      }
      if (job.status === "error" || job.status === "failed") {
        $("job").innerHTML += `<span class="small" style="color:var(--danger)">${job.message}</span>`;
      }
    }

    async function pollJob(id) {
      currentJob = id;
      while (currentJob === id) {
        const job = await api(`/api/jobs/${id}`);
        renderJob(job);
        if (["succeeded", "failed", "expired", "cancelled", "error"].includes(job.status)) break;
        await new Promise(resolve => setTimeout(resolve, 2500));
      }
      $("generate").disabled = false;
    }

    async function loadHistory() {
      const data = await api("/api/history");
      $("history").innerHTML = data.items.map(item => `
        <a href="${item.url}" target="_blank">
          <strong>${item.name}</strong>
          <span class="muted small">${(item.size / 1024 / 1024).toFixed(2)} MB · ${item.modified}</span>
        </a>
      `).join("") || `<span class="muted small">暂无历史</span>`;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }

    function referenceItems() {
      return [
        {kind:"image", token:"@图片1", label:"图片1", urlId:"imageUrl1", statusId:"imageUpload1"},
        {kind:"image", token:"@图片2", label:"图片2", urlId:"imageUrl2", statusId:"imageUpload2"},
        {kind:"image", token:"@图片3", label:"图片3", urlId:"imageUrl3", statusId:"imageUpload3"},
        {kind:"image", token:"@图片4", label:"图片4", urlId:"imageUrl4", statusId:"imageUpload4"},
        {kind:"video", token:"@视频1", label:"视频1", urlId:"videoUrl", statusId:"videoUpload"},
        {kind:"audio", token:"@音频1", label:"音频1", urlId:"audioUrl", statusId:"audioUpload"}
      ].map(item => ({...item, url: () => $(item.urlId).value.trim()}));
    }

    function shortUrl(url) {
      if (!url) return "未填写 URL，选择后可先插入占位引用";
      return url.replace(/^https?:\/\//, "").slice(0, 58);
    }

    function updateReferenceState() {
      referenceItems().forEach(item => {
        const status = $(item.statusId);
        const text = status.textContent.trim();
        if (item.url()) {
          if (!text || text.startsWith("可引用")) status.textContent = `可引用 ${item.token}`;
        } else if (text.startsWith("可引用")) {
          status.textContent = "";
        }
      });
    }

    function mentionContext() {
      const input = $("prompt");
      const pos = input.selectionStart;
      const before = input.value.slice(0, pos);
      const at = before.lastIndexOf("@");
      if (at < 0) return null;
      const query = before.slice(at + 1);
      if (/[\s，。；：、,.!?]/.test(query)) return null;
      return {start: at, end: pos, query};
    }

    function hideMentionMenu() {
      $("mentionMenu").classList.add("hidden");
      $("mentionMenu").innerHTML = "";
    }

    function showMentionMenu(query = "") {
      const normalized = query.trim().toLowerCase();
      const items = referenceItems().filter(item => {
        const haystack = `${item.token} ${item.label} ${item.kind}`.toLowerCase();
        return !normalized || haystack.includes(normalized);
      });
      $("mentionMenu").innerHTML = items.map(item => `
        <button type="button" class="mention-item" data-token="${escapeHtml(item.token)}">
          <span class="mention-token">${escapeHtml(item.token)}</span>
          <span class="mention-meta">
            <span class="mention-name">${escapeHtml(item.label)}</span>
            <span class="mention-url">${escapeHtml(shortUrl(item.url()))}</span>
          </span>
        </button>
      `).join("") || `<div class="mention-item"><span class="mention-meta"><span class="mention-name">没有匹配的素材</span></span></div>`;
      $("mentionMenu").classList.remove("hidden");
      document.querySelectorAll("[data-token]").forEach(button => {
        button.addEventListener("mousedown", event => {
          event.preventDefault();
          insertMention(button.dataset.token);
        });
      });
    }

    function insertMention(token) {
      const input = $("prompt");
      const ctx = mentionContext();
      const start = ctx ? ctx.start : input.selectionStart;
      const end = ctx ? ctx.end : input.selectionEnd;
      const prefix = input.value.slice(0, start);
      const suffix = input.value.slice(end);
      const spacer = suffix.startsWith(" ") || suffix === "" ? "" : " ";
      input.value = `${prefix}${token}${spacer}${suffix}`;
      const cursor = prefix.length + token.length + spacer.length;
      input.focus();
      input.setSelectionRange(cursor, cursor);
      $("charCount").textContent = `${input.value.length} 字`;
      hideMentionMenu();
    }

    function selectedReferences() {
      updateReferenceState();
      const text = $("prompt").value;
      const items = referenceItems();
      const filled = items.filter(item => item.url());
      const mentioned = items.filter(item => text.includes(item.token));
      if (filled.length && !mentioned.length) {
        throw new Error("已填写参考素材，请在 Prompt 中用 @ 引用，例如 @图片1、@视频1 或 @音频1。");
      }
      const missing = mentioned.filter(item => !item.url());
      if (missing.length) {
        throw new Error(`${missing.map(item => item.token).join("、")} 还没有上传或填写 URL。`);
      }
      return mentioned;
    }

    async function uploadReference(kind, slot = "") {
      const ids = kind === "image" ? {file:`imageFile${slot}`, url:`imageUrl${slot}`, status:`imageUpload${slot}`} :
        kind === "video" ? {file:"videoFile", url:"videoUrl", status:"videoUpload"} :
        {file:"audioFile", url:"audioUrl", status:"audioUpload"};
      const fileInput = $(ids.file);
      const status = $(ids.status);
      const file = fileInput.files[0];
      if (!file) {
        status.textContent = `请选择${kind === "image" ? "图片" : kind === "video" ? "视频" : "音频"}`;
        return;
      }
      const button = document.querySelector(`[data-upload-kind="${kind}"]${slot ? `[data-upload-slot="${slot}"]` : ""}`);
      button.disabled = true;
      status.textContent = "上传中...";
      try {
        const form = new FormData();
        form.append("file", file);
        Object.entries(storagePayload()).forEach(([key, value]) => form.append(key, value));
        const data = await api("/api/upload-reference", {method: "POST", body: form});
        $(ids.url).value = data.url;
        const label = kind === "image" ? `图片${slot}` : kind === "video" ? "视频1" : "音频1";
        const token = kind === "image" ? `@图片${slot}` : kind === "video" ? "@视频1" : "@音频1";
        status.textContent = `已上传：${label} · 可引用 ${token} · ${(data.size / 1024 / 1024).toFixed(2)} MB`;
        updateReferenceState();
      } catch (err) {
        status.textContent = err.message;
      } finally {
        button.disabled = false;
      }
    }

    async function generate() {
      $("generate").disabled = true;
      resetJobView();
      let refs = [];
      try {
        refs = selectedReferences();
      } catch (err) {
        $("generate").disabled = false;
        $("job").innerHTML = `<strong style="color:var(--danger)">引用素材缺失</strong><span class="small">${err.message}</span>`;
        return;
      }
      const payload = {
        api_key: $("apiKey").value.trim(),
        model: $("model").value,
        prompt: $("prompt").value.trim(),
        duration: Number($("duration").value || 5),
        ratio: $("ratio").value,
        resolution: $("resolution").value,
        seed: Number($("seed").value || -1),
        generate_audio: $("generateAudio").checked,
        return_last_frame: $("returnLastFrame").checked,
        watermark: $("watermark").checked,
        web_search: $("webSearch").checked,
        safety_identifier: $("safetyIdentifier").value.trim(),
        image_urls: refs.filter(item => item.kind === "image").map(item => item.url()),
        video_url: (refs.find(item => item.kind === "video") || {url: () => ""}).url(),
        audio_url: (refs.find(item => item.kind === "audio") || {url: () => ""}).url()
      };
      try {
        const job = await api("/api/generate", {method: "POST", body: JSON.stringify(payload)});
        renderJob(job);
        pollJob(job.id);
      } catch (err) {
        $("generate").disabled = false;
        $("job").innerHTML = `<strong style="color:var(--danger)">提交失败</strong><span class="small">${err.message}</span>`;
      }
    }

    async function saveAllConfig() {
      if ($("apiKey").value.trim()) {
        await api("/api/config", {method:"POST", body: JSON.stringify({api_key: $("apiKey").value.trim(), save: true})});
        $("apiKey").value = "";
      }
      const storage = await api("/api/storage/config", {method:"POST", body: JSON.stringify({...storagePayload(), save: true})});
      renderStorageConfig(storage);
      renderConfig(await api("/api/config"));
    }

    $("model").addEventListener("change", updateModelFields);
    $("generate").addEventListener("click", generate);
    $("mentionButton").addEventListener("click", () => {
      $("prompt").focus();
      showMentionMenu("");
    });
    $("clearForm").addEventListener("click", () => {
      $("prompt").value = "";
      ["imageUrl1","imageUrl2","imageUrl3","imageUrl4","videoUrl","audioUrl"].forEach(id => $(id).value = "");
      ["imageFile1","imageFile2","imageFile3","imageFile4","videoFile","audioFile"].forEach(id => $(id).value = "");
      ["imageUpload1","imageUpload2","imageUpload3","imageUpload4","videoUpload","audioUpload"].forEach(id => $(id).textContent = "");
      $("charCount").textContent = "0 字";
      hideMentionMenu();
    });
    $("prompt").addEventListener("input", () => {
      $("charCount").textContent = `${$("prompt").value.length} 字`;
      const ctx = mentionContext();
      if (ctx) showMentionMenu(ctx.query);
      else hideMentionMenu();
    });
    $("prompt").addEventListener("click", () => {
      const ctx = mentionContext();
      if (ctx) showMentionMenu(ctx.query);
    });
    $("prompt").addEventListener("keydown", event => {
      if (event.key === "Escape") hideMentionMenu();
    });
    document.addEventListener("click", event => {
      if (!event.target.closest("#mentionMenu") && event.target !== $("mentionButton") && event.target !== $("prompt")) {
        hideMentionMenu();
      }
    });
    $("refreshHistory").addEventListener("click", loadHistory);
    $("saveAllConfig").addEventListener("click", () => saveAllConfig().catch(err => alert(err.message)));
    document.querySelectorAll("[data-upload-kind]").forEach(button => {
      button.addEventListener("click", () => uploadReference(button.dataset.uploadKind, button.dataset.uploadSlot || ""));
    });
    ["imageUrl1","imageUrl2","imageUrl3","imageUrl4","videoUrl","audioUrl"].forEach(id => {
      $(id).addEventListener("input", updateReferenceState);
    });

    Promise.all([
      api("/api/config").then(renderConfig),
      api("/api/storage/config").then(renderStorageConfig),
      api("/api/models").then(renderModels),
      loadHistory()
    ]);
  </script>
</body>
</html>
"""


def main() -> None:
    OUTPUTS.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"SHIMEI Video Studio running at http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

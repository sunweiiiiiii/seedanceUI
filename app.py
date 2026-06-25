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


ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
CONFIG_PATH = ROOT / ".seedance_config.json"
HOST = "127.0.0.1"
PORT = int(os.environ.get("SEEDANCE_UI_PORT") or os.environ.get("JIMENG_UI_PORT") or "7860")


MODEL_CATALOG: dict[str, dict[str, Any]] = {
    "seedance_2": {
        "label": "Doubao Seedance 2.0",
        "model_id": "doubao-seedance-2-0-260128",
        "description": "标准质量，支持 480p / 720p / 1080p",
        "resolutions": ["480p", "720p", "1080p"],
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
RESOLUTIONS = {"480p", "720p", "1080p"}


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


def read_config() -> dict[str, str]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {"api_key": str(data.get("api_key") or data.get("ark_api_key") or "").strip()}


def write_config(api_key: str) -> None:
    CONFIG_PATH.write_text(json.dumps({"api_key": api_key}, ensure_ascii=False, indent=2), encoding="utf-8")


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

    content: list[dict[str, Any]] = []
    if prompt:
        content.append({"type": "text", "text": prompt})
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}, "role": "reference_image"})
    if video_url:
        content.append({"type": "video_url", "video_url": {"url": video_url}, "role": "reference_video"})
    if audio_url:
        content.append({"type": "audio_url", "audio_url": {"url": audio_url}, "role": "reference_audio"})
    if len(content) > 5:
        raise ValueError("Ark content 最多 5 项；包含提示词时最多再放 4 个参考素材。")
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_html(INDEX_HTML)
        elif path == "/api/config":
            self.send_json(credential_status())
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
        else:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/config":
                self.save_config(payload)
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
            write_config(api_key)
        self.send_json(credential_status())

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

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 256 * 1024:
            raise ValueError("请求过大。参考素材请使用公网 URL。")
        raw = self.rfile.read(length)
        if not raw:
            return {}
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
  <title>Seedance 2.0 控制台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #eef4f2;
      --panel: rgba(255,255,255,.86);
      --panel-strong: rgba(255,255,255,.94);
      --soft: rgba(244,248,247,.78);
      --line: rgba(42,76,86,.22);
      --text: #14201f;
      --muted: #60716e;
      --accent: #147d73;
      --accent-2: #315b95;
      --amber: #a86f35;
      --danger: #b43c3c;
      --shadow: 0 24px 70px rgba(19,39,43,.13);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      background:
        linear-gradient(120deg, rgba(20,125,115,.13), transparent 34%),
        linear-gradient(300deg, rgba(49,91,149,.11), transparent 32%),
        var(--bg);
      font: 14px/1.45 "Inter", "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      letter-spacing: 0;
      overflow-x: hidden;
      isolation: isolate;
    }
    body::before, body::after {
      content: "";
      position: fixed;
      inset: 0;
      z-index: -1;
      pointer-events: none;
    }
    body::before {
      background:
        linear-gradient(90deg, rgba(20,125,115,.08) 1px, transparent 1px),
        linear-gradient(0deg, rgba(49,91,149,.06) 1px, transparent 1px),
        repeating-linear-gradient(115deg, transparent 0 86px, rgba(168,111,53,.10) 87px, transparent 90px);
      background-size: 52px 52px, 52px 52px, 260px 260px;
      opacity: .76;
    }
    body::after {
      background: linear-gradient(180deg, rgba(255,255,255,.62), transparent 42%);
    }
    button, input, textarea, select { font: inherit; letter-spacing: 0; }
    .app {
      width: min(1480px, calc(100vw - 32px));
      min-height: calc(100vh - 32px);
      margin: 16px auto;
      display: grid;
      grid-template-columns: 320px minmax(520px, 1fr) 360px;
      gap: 16px;
    }
    .panel {
      min-width: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
    }
    .sidebar, .result {
      align-self: start;
      position: sticky;
      top: 16px;
      max-height: calc(100vh - 32px);
      overflow: auto;
      padding: 16px;
    }
    .main {
      padding: 16px;
      display: grid;
      gap: 14px;
      grid-template-rows: auto auto auto 1fr auto;
    }
    h1, h2, h3 { margin: 0; font-weight: 680; }
    h1 { font-size: 20px; }
    h2 { margin-top: 3px; color: var(--muted); font-size: 12px; font-weight: 560; }
    h3 { font-size: 13px; }
    .stack { display: grid; gap: 12px; }
    .topline { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 30px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(255,255,255,.72);
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #c45a4c;
      box-shadow: 0 0 0 4px rgba(196,90,76,.12);
    }
    .dot.ok {
      background: #17956d;
      box-shadow: 0 0 0 4px rgba(23,149,109,.13);
    }
    details.key-panel {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,.62);
      overflow: hidden;
    }
    details.key-panel summary {
      min-height: 42px;
      padding: 0 12px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      cursor: pointer;
      font-weight: 680;
    }
    .key-panel-body { display: grid; gap: 12px; padding: 0 12px 12px; }
    .summary-copy { margin-left: auto; color: var(--muted); font-size: 12px; font-weight: 520; }
    .field { display: grid; gap: 6px; }
    label { color: var(--muted); font-size: 12px; font-weight: 640; }
    input, textarea, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,.87);
      color: var(--text);
      outline: none;
      padding: 10px 11px;
    }
    textarea {
      min-height: 340px;
      resize: vertical;
    }
    input:focus, textarea:focus, select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(20,125,115,.13);
    }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
    .segmented {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 6px;
    }
    .segmented.two { grid-template-columns: repeat(2, 1fr); }
    .segmented button {
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,.72);
      color: var(--muted);
      cursor: pointer;
    }
    .segmented button.active {
      border-color: var(--accent);
      background: linear-gradient(180deg, rgba(226,242,239,.96), rgba(214,233,230,.92));
      color: var(--accent);
      font-weight: 680;
    }
    .media-box {
      display: grid;
      gap: 12px;
      padding: 12px;
      border: 1px dashed rgba(42,76,86,.28);
      border-radius: 8px;
      background: rgba(255,255,255,.58);
    }
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding-top: 12px;
      border-top: 1px solid var(--line);
    }
    .btn {
      min-height: 40px;
      padding: 0 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,.75);
      color: var(--text);
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }
    .btn.primary {
      min-width: 126px;
      border-color: var(--accent);
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #fff;
      font-weight: 680;
    }
    .btn:disabled { opacity: .58; cursor: not-allowed; }
    .note {
      color: var(--muted);
      font-size: 12px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--soft);
    }
    video {
      width: 100%;
      aspect-ratio: 16 / 9;
      background: #0d1212;
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .last-frame {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      display: block;
    }
    .jobbox {
      min-height: 118px;
      display: grid;
      gap: 8px;
      align-content: start;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: linear-gradient(135deg, rgba(49,91,149,.09), transparent 42%), rgba(244,248,247,.78);
      overflow-wrap: anywhere;
    }
    .progress {
      height: 7px;
      border-radius: 999px;
      background: #dce5e2;
      overflow: hidden;
    }
    .bar {
      height: 100%;
      width: 0;
      background: linear-gradient(90deg, var(--accent), var(--accent-2), var(--amber));
      transition: width .3s ease;
    }
    .history {
      display: grid;
      gap: 8px;
      margin-top: 12px;
    }
    .history a {
      display: grid;
      gap: 3px;
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-strong);
      color: var(--text);
      text-decoration: none;
      overflow-wrap: anywhere;
    }
    .muted { color: var(--muted); }
    .small { font-size: 12px; }
    .hidden { display: none !important; }
    @media (max-width: 1120px) {
      .app { grid-template-columns: 1fr; }
      .sidebar, .result { position: static; max-height: none; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="panel sidebar stack">
      <div class="topline">
        <div>
          <h1>Seedance 2.0</h1>
          <h2>Volcengine Ark</h2>
        </div>
        <span class="status-pill"><span id="keyDot" class="dot"></span><span id="keyStatus">未配置</span></span>
      </div>

      <details class="key-panel" id="keyPanel">
        <summary>
          <span>Key 配置</span>
          <span class="summary-copy">点击展开</span>
        </summary>
        <div class="key-panel-body">
          <div class="field">
            <label for="apiKey">Ark API Key</label>
            <input id="apiKey" type="password" autocomplete="off" spellcheck="false" placeholder="ark-..." />
          </div>
          <div class="field">
            <label for="keyFile">导入 Key 文件</label>
            <input id="keyFile" type="file" accept=".txt,.env,.json,text/plain,application/json" />
          </div>
          <label><input id="saveKey" type="checkbox" style="width:auto;margin-right:6px" />保存到本机配置</label>
          <button id="saveConfig" class="btn">保存配置</button>
        </div>
      </details>

      <div class="stack">
        <h3>模型</h3>
        <div class="field">
          <label for="model">生成模型</label>
          <select id="model"></select>
        </div>
        <div class="note" id="modelSource"></div>
        <div class="grid-2">
          <div class="field">
            <label for="duration">时长</label>
            <input id="duration" type="number" min="4" max="15" value="5" />
          </div>
          <div class="field">
            <label for="resolution">分辨率</label>
            <select id="resolution">
              <option>1080p</option>
              <option>720p</option>
              <option>480p</option>
            </select>
          </div>
        </div>
        <div class="field">
          <label for="ratio">比例</label>
          <select id="ratio">
            <option>16:9</option>
            <option>adaptive</option>
            <option>9:16</option>
            <option>1:1</option>
            <option>4:3</option>
            <option>3:4</option>
            <option>21:9</option>
          </select>
        </div>
      </div>

      <details class="key-panel">
        <summary>
          <span>高级参数</span>
          <span class="summary-copy">Seed / 音频 / 尾帧</span>
        </summary>
        <div class="key-panel-body">
          <div class="field">
            <label for="seed">Seed</label>
            <input id="seed" type="number" value="-1" />
          </div>
          <div class="grid-2">
            <label><input id="generateAudio" type="checkbox" style="width:auto;margin-right:6px" />生成音频</label>
            <label><input id="returnLastFrame" type="checkbox" style="width:auto;margin-right:6px" />返回尾帧</label>
          </div>
          <div class="grid-2">
            <label><input id="watermark" type="checkbox" style="width:auto;margin-right:6px" />添加水印</label>
            <label><input id="webSearch" type="checkbox" style="width:auto;margin-right:6px" />联网搜索</label>
          </div>
          <div class="field">
            <label for="safetyIdentifier">Safety Identifier</label>
            <input id="safetyIdentifier" maxlength="64" placeholder="可选" />
          </div>
        </div>
      </details>
    </aside>

    <main class="panel main">
      <div class="topline">
        <div>
          <h1>生成任务</h1>
          <h2 id="modeHint">文生视频</h2>
        </div>
        <button id="clearForm" class="btn">清空</button>
      </div>

      <div class="segmented" id="modeTabs">
        <button type="button" data-mode="text" class="active">纯文本</button>
        <button type="button" data-mode="image">参考图</button>
        <button type="button" data-mode="media">多模态</button>
      </div>

      <div id="mediaSection" class="media-box hidden">
        <div class="segmented" id="imageIntent">
          <button type="button" data-intent="reference" class="active">参考图</button>
          <button type="button" data-intent="first">首帧</button>
          <button type="button" data-intent="firstTail">首尾帧</button>
        </div>
        <div class="grid-2">
          <div class="field">
            <label id="imageLabel1" for="imageUrl1">参考图 1 URL</label>
            <input id="imageUrl1" placeholder="https://..." />
          </div>
          <div class="field">
            <label id="imageLabel2" for="imageUrl2">参考图 2 URL</label>
            <input id="imageUrl2" placeholder="https://..." />
          </div>
        </div>
        <div class="grid-2" id="extraImageUrls">
          <div class="field">
            <label for="imageUrl3">参考图 3 URL</label>
            <input id="imageUrl3" placeholder="https://..." />
          </div>
          <div class="field">
            <label for="imageUrl4">参考图 4 URL</label>
            <input id="imageUrl4" placeholder="https://..." />
          </div>
        </div>
        <div class="grid-2" id="mediaUrls">
          <div class="field">
            <label for="videoUrl">参考视频 URL</label>
            <input id="videoUrl" placeholder="https://...mp4" />
          </div>
          <div class="field">
            <label for="audioUrl">参考音频 URL</label>
            <input id="audioUrl" placeholder="https://...mp3" />
          </div>
        </div>
        <div class="note">参考素材需要公网可访问 URL；本机文件请先上传到 TOS、OSS 或其他可访问地址。</div>
      </div>

      <div class="field">
        <label for="prompt">Prompt</label>
        <textarea id="prompt" spellcheck="false"></textarea>
      </div>

      <div class="toolbar">
        <span class="muted small" id="charCount">0 字</span>
        <button id="generate" class="btn primary">生成视频</button>
      </div>
    </main>

    <aside class="panel result stack">
      <div class="topline">
        <div>
          <h1>结果</h1>
          <h2 id="taskCaption">等待任务</h2>
        </div>
        <button id="refreshHistory" class="btn">刷新</button>
      </div>
      <video id="video" controls class="hidden"></video>
      <img id="lastFrame" class="last-frame hidden" alt="" />
      <div id="job" class="jobbox">
        <strong>未开始</strong>
        <span class="muted small">配置参数后提交任务</span>
        <div class="progress"><div class="bar"></div></div>
      </div>
      <a id="download" class="btn primary hidden" download>下载 MP4</a>
      <div>
        <h3>历史</h3>
        <div id="history" class="history"></div>
      </div>
    </aside>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    let models = {};
    let currentJob = null;
    let mode = "text";
    let imageIntent = "reference";

    async function api(path, options = {}) {
      const res = await fetch(path, {
        ...options,
        headers: {"Content-Type": "application/json", ...(options.headers || {})}
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "请求失败");
      return data;
    }

    function renderConfig(data) {
      $("keyDot").classList.toggle("ok", !!data.configured);
      $("keyStatus").textContent = data.configured ? (data.source === "env" ? "环境变量" : "已配置") : "未配置";
      if (data.api_key) $("apiKey").placeholder = data.api_key;
    }

    function parseKeyFile(text) {
      const trimmed = text.trim();
      if (!trimmed) throw new Error("Key 文件为空");
      try {
        const data = JSON.parse(trimmed);
        const value = data.api_key || data.ark_api_key || data.ARK_API_KEY || data.VOLC_ARK_API_KEY;
        if (value) return String(value).trim().replace(/^Bearer\s+/i, "");
      } catch (_) {}
      const bearer = trimmed.match(/Bearer\s+([A-Za-z0-9._-]+)/i);
      if (bearer) return bearer[1];
      const direct = trimmed.match(/\b(ark-[A-Za-z0-9._-]{20,})\b/);
      if (direct) return direct[1];
      const lines = trimmed.split(/\r?\n/);
      for (const line of lines) {
        const match = line.trim().match(/^([^:=\s][^:=]*?)\s*[:=]\s*(.+)$/);
        if (!match) continue;
        const key = match[1].trim().toLowerCase().replace(/[\s_\-]/g, "");
        const value = match[2].trim().replace(/^["']|["']$/g, "").replace(/^Bearer\s+/i, "");
        if (["apikey", "arkapikey", "arkkey", "volcarkapikey"].includes(key)) return value;
      }
      throw new Error("没有解析到 Ark API Key");
    }

    function renderModels(data) {
      models = data.models;
      $("model").innerHTML = Object.entries(models).map(([key, model]) =>
        `<option value="${key}">${model.label}</option>`
      ).join("");
      $("modelSource").textContent = data.source || "";
      updateModelFields();
    }

    function updateModelFields() {
      const model = models[$("model").value] || {};
      [...$("resolution").options].forEach(option => {
        option.disabled = model.resolutions && !model.resolutions.includes(option.value);
      });
      if (model.resolutions && !model.resolutions.includes($("resolution").value)) {
        $("resolution").value = model.resolutions[model.resolutions.length - 1];
      }
    }

    function setMode(next) {
      mode = next;
      document.querySelectorAll("#modeTabs button").forEach(btn => btn.classList.toggle("active", btn.dataset.mode === mode));
      $("mediaSection").classList.toggle("hidden", mode === "text");
      $("mediaUrls").classList.toggle("hidden", mode !== "media");
      $("extraImageUrls").classList.toggle("hidden", mode !== "media" || imageIntent !== "reference");
      $("modeHint").textContent = mode === "text" ? "文生视频" : mode === "image" ? "参考图生成" : "多模态参考生成";
      updateImageIntent();
    }

    function updateImageIntent() {
      document.querySelectorAll("#imageIntent button").forEach(btn => btn.classList.toggle("active", btn.dataset.intent === imageIntent));
      const firstTail = imageIntent === "firstTail";
      $("imageLabel1").textContent = imageIntent === "reference" ? "参考图 1 URL" : "首帧图 URL";
      $("imageLabel2").textContent = firstTail ? "尾帧图 URL" : "参考图 2 URL";
      $("extraImageUrls").classList.toggle("hidden", mode !== "media" || imageIntent !== "reference");
    }

    function progressFor(status) {
      return {
        created: 8,
        submitting: 18,
        queued: 36,
        running: 68,
        succeeded: 100,
        failed: 100,
        expired: 100,
        cancelled: 100,
        error: 100
      }[status] || 48;
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
      $("taskCaption").textContent = job.task_id ? `Task ${job.task_id}` : job.model;
      $("job").innerHTML = `
        <strong>${job.message || job.status}</strong>
        <span class="muted small">${job.model_id || job.model || ""}</span>
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

    function imageUrls() {
      if (mode === "text") return [];
      const ids = imageIntent === "firstTail" ? ["imageUrl1", "imageUrl2"] : ["imageUrl1", "imageUrl2", "imageUrl3", "imageUrl4"];
      return ids.map(id => $(id).value.trim()).filter(Boolean);
    }

    async function generate() {
      $("generate").disabled = true;
      resetJobView();
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
        image_urls: imageUrls(),
        video_url: mode === "media" ? $("videoUrl").value.trim() : "",
        audio_url: mode === "media" ? $("audioUrl").value.trim() : "",
        input_mode: mode,
        image_intent: imageIntent
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

    $("saveConfig").addEventListener("click", async () => {
      try {
        const data = await api("/api/config", {
          method: "POST",
          body: JSON.stringify({api_key: $("apiKey").value.trim(), save: $("saveKey").checked})
        });
        $("apiKey").value = "";
        renderConfig(data);
      } catch (err) {
        alert(err.message);
      }
    });
    $("keyFile").addEventListener("change", async (event) => {
      const file = event.target.files[0];
      if (!file) return;
      try {
        $("apiKey").value = parseKeyFile(await file.text());
        $("keyStatus").textContent = "已导入";
        $("keyDot").classList.add("ok");
      } catch (err) {
        alert(err.message);
      } finally {
        event.target.value = "";
      }
    });
    $("model").addEventListener("change", updateModelFields);
    $("modeTabs").addEventListener("click", (event) => {
      const btn = event.target.closest("button");
      if (btn) setMode(btn.dataset.mode);
    });
    $("imageIntent").addEventListener("click", (event) => {
      const btn = event.target.closest("button");
      if (!btn) return;
      imageIntent = btn.dataset.intent;
      updateImageIntent();
    });
    $("generate").addEventListener("click", generate);
    $("refreshHistory").addEventListener("click", loadHistory);
    $("prompt").addEventListener("input", () => $("charCount").textContent = `${$("prompt").value.length} 字`);
    $("clearForm").addEventListener("click", () => {
      $("prompt").value = "";
      ["imageUrl1", "imageUrl2", "imageUrl3", "imageUrl4", "videoUrl", "audioUrl"].forEach(id => $(id).value = "");
      $("charCount").textContent = "0 字";
    });

    Promise.all([
      api("/api/config").then(renderConfig),
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
    print(f"Seedance UI running at http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

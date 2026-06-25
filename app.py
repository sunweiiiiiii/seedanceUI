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

from jimeng_video import call_visual, download_video, submit_task_body


ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
CONFIG_PATH = ROOT / ".jimeng_config.json"
HOST = "127.0.0.1"
PORT = int(os.environ.get("JIMENG_UI_PORT", "7860"))


MODEL_CATALOG: dict[str, dict[str, Any]] = {
    "pro": {
        "label": "3.0 Pro 1080P",
        "req_key": "jimeng_ti2v_v30_pro",
        "mode": "pro",
        "images": "optional_one",
        "aspect": True,
    },
    "t2v_1080": {
        "label": "3.0 1080P 文生",
        "req_key": "jimeng_t2v_v30_1080p",
        "mode": "text",
        "images": "none",
        "aspect": True,
    },
    "t2v_720": {
        "label": "3.0 720P 文生",
        "req_key": "jimeng_t2v_v30",
        "mode": "text",
        "images": "none",
        "aspect": True,
    },
    "first_1080": {
        "label": "3.0 1080P 首帧",
        "req_key": "jimeng_i2v_first_v30_1080",
        "mode": "first",
        "images": "one",
        "aspect": False,
    },
    "first_720": {
        "label": "3.0 720P 首帧",
        "req_key": "jimeng_i2v_first_v30",
        "mode": "first",
        "images": "one",
        "aspect": False,
    },
    "tail_1080": {
        "label": "3.0 1080P 首尾帧",
        "req_key": "jimeng_i2v_first_tail_v30_1080",
        "mode": "tail",
        "images": "two",
        "aspect": False,
    },
    "tail_720": {
        "label": "3.0 720P 首尾帧",
        "req_key": "jimeng_i2v_first_tail_v30",
        "mode": "tail",
        "images": "two",
        "aspect": False,
    },
    "camera_720": {
        "label": "3.0 720P 运镜",
        "req_key": "jimeng_i2v_recamera_v30",
        "mode": "camera",
        "images": "one",
        "aspect": False,
    },
}


CAMERA_TEMPLATES = {
    "hitchcock_dolly_in": "希区柯克推进",
    "hitchcock_dolly_out": "希区柯克拉远",
    "robo_arm": "机械臂",
    "dynamic_orbit": "动感环绕",
    "central_orbit": "中心环绕",
    "crane_push": "起重机",
    "quick_pull_back": "超级拉远",
    "counterclockwise_swivel": "逆时针回旋",
    "clockwise_swivel": "顺时针回旋",
    "handheld": "手持运镜",
    "rapid_push_pull": "快速推拉",
}


@dataclass
class Job:
    id: str
    model_key: str
    prompt: str
    status: str = "created"
    message: str = ""
    task_id: str | None = None
    video_url: str | None = None
    output: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    response: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        data["model"] = MODEL_CATALOG.get(self.model_key, {}).get("label", self.model_key)
        data["created_at_text"] = datetime.fromtimestamp(self.created_at).strftime("%Y-%m-%d %H:%M:%S")
        if self.output:
            data["download_path"] = "/outputs/" + Path(self.output).name
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
    return {
        "access_key": str(data.get("access_key", "")).strip(),
        "secret_key": str(data.get("secret_key", "")).strip(),
    }


def write_config(access_key: str, secret_key: str) -> None:
    CONFIG_PATH.write_text(
        json.dumps({"access_key": access_key, "secret_key": secret_key}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def credential_status() -> dict[str, Any]:
    cfg = read_config()
    access_key = os.environ.get("VOLC_ACCESSKEY") or memory_credentials.get("access_key") or cfg.get("access_key", "")
    secret_key = os.environ.get("VOLC_SECRETKEY") or memory_credentials.get("secret_key") or cfg.get("secret_key", "")
    source = "none"
    if os.environ.get("VOLC_ACCESSKEY") and os.environ.get("VOLC_SECRETKEY"):
        source = "env"
    elif memory_credentials:
        source = "session"
    elif cfg.get("access_key") and cfg.get("secret_key"):
        source = "local"
    return {
        "configured": bool(access_key and secret_key),
        "access_key": mask_key(access_key),
        "source": source,
    }


def resolve_credentials(payload: dict[str, Any] | None = None) -> tuple[str, str]:
    payload = payload or {}
    access_key = str(payload.get("access_key") or "").strip()
    secret_key = str(payload.get("secret_key") or "").strip()
    cfg = read_config()
    access_key = access_key or os.environ.get("VOLC_ACCESSKEY") or memory_credentials.get("access_key") or cfg.get("access_key", "")
    secret_key = secret_key or os.environ.get("VOLC_SECRETKEY") or memory_credentials.get("secret_key") or cfg.get("secret_key", "")
    if not access_key or not secret_key:
        raise ValueError("请先配置 Access Key ID 和 Secret Access Key。")
    return access_key, secret_key


def mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "*" * len(value)
    return value[:6] + "*" * (len(value) - 10) + value[-4:]


def clean_filename(text: str) -> str:
    text = re.sub(r"[^\w\-.]+", "_", text, flags=re.UNICODE).strip("_")
    return text[:80] or "jimeng_video"


def image_payload(payload: dict[str, Any], mode: str) -> dict[str, list[str]]:
    first_b64 = str(payload.get("first_image_base64") or "").strip()
    tail_b64 = str(payload.get("tail_image_base64") or "").strip()
    first_url = str(payload.get("first_image_url") or "").strip()
    tail_url = str(payload.get("tail_image_url") or "").strip()

    if first_b64 or tail_b64:
        values = [v for v in [first_b64, tail_b64] if v]
        if mode in {"first", "camera"} and len(values) != 1:
            raise ValueError("首帧/运镜模型需要 1 张图片。")
        if mode == "tail" and len(values) != 2:
            raise ValueError("首尾帧模型需要 2 张图片。")
        if mode == "pro" and len(values) > 1:
            raise ValueError("3.0 Pro 最多接收 1 张首帧图片。")
        return {"binary_data_base64": values}

    urls = [v for v in [first_url, tail_url] if v]
    if not urls:
        return {}
    if mode in {"first", "camera"} and len(urls) != 1:
        raise ValueError("首帧/运镜模型需要 1 张图片 URL。")
    if mode == "tail" and len(urls) != 2:
        raise ValueError("首尾帧模型需要 2 张图片 URL。")
    if mode == "pro" and len(urls) > 1:
        raise ValueError("3.0 Pro 最多接收 1 张首帧图片。")
    return {"image_urls": urls}


def build_body(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    model_key = str(payload.get("model") or "pro")
    model = MODEL_CATALOG.get(model_key)
    if not model:
        raise ValueError("未知模型。")
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("请填写提示词。")
    frames = int(payload.get("frames") or 121)
    if frames not in {121, 241}:
        raise ValueError("时长只能选择 5 秒或 10 秒。")
    seed = int(payload.get("seed") if str(payload.get("seed", "")).strip() else -1)
    mode = str(model["mode"])
    body: dict[str, Any] = {
        "req_key": model["req_key"],
        "prompt": prompt,
        "seed": seed,
        "frames": frames,
    }
    images = image_payload(payload, mode)
    if model["images"] in {"one", "two"} and not images:
        raise ValueError("这个模型需要图片。")
    body.update(images)
    if model.get("aspect"):
        aspect_ratio = str(payload.get("aspect_ratio") or "16:9")
        if aspect_ratio not in {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9"}:
            raise ValueError("比例参数不正确。")
        body["aspect_ratio"] = aspect_ratio
    if mode == "camera":
        template_id = str(payload.get("template_id") or "crane_push")
        camera_strength = str(payload.get("camera_strength") or "medium")
        if template_id not in CAMERA_TEMPLATES:
            raise ValueError("运镜模板不正确。")
        if camera_strength not in {"weak", "medium", "strong"}:
            raise ValueError("运镜强度不正确。")
        body["template_id"] = template_id
        body["camera_strength"] = camera_strength
    return model_key, body


def run_job(job_id: str, body: dict[str, Any], access_key: str, secret_key: str) -> None:
    req_key = str(body["req_key"])
    with jobs_lock:
        job = jobs[job_id]
        job.status = "submitting"
        job.message = "提交任务中"
        job.updated_at = time.time()
    try:
        task_id = submit_task_body(body, access_key=access_key, secret_key=secret_key)
        with jobs_lock:
            job = jobs[job_id]
            job.status = "generating"
            job.task_id = task_id
            job.message = "生成中"
            job.updated_at = time.time()

        result: dict[str, Any] | None = None
        for _ in range(180):
            result = call_visual(
                "CVSync2AsyncGetResult",
                {"req_key": req_key, "task_id": task_id},
                access_key=access_key,
                secret_key=secret_key,
            )
            if result.get("code") != 10000:
                raise RuntimeError(json.dumps(result, ensure_ascii=False))
            data = result.get("data") or {}
            status = data.get("status") or "unknown"
            with jobs_lock:
                job = jobs[job_id]
                job.status = str(status)
                job.message = status_label(str(status))
                job.response = result
                job.updated_at = time.time()
            if status == "done":
                video_url = data.get("video_url")
                if not video_url:
                    raise RuntimeError("任务完成但没有返回 video_url。")
                output = OUTPUTS / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{clean_filename(req_key)}_{task_id}.mp4"
                download_video(video_url, output)
                meta_path = output.with_suffix(".json")
                meta_path.write_text(
                    json.dumps({"task_id": task_id, "body": body, "response": result}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                with jobs_lock:
                    job = jobs[job_id]
                    job.status = "done"
                    job.message = "已完成"
                    job.video_url = video_url
                    job.output = str(output)
                    job.updated_at = time.time()
                return
            if status in {"not_found", "expired"}:
                raise RuntimeError(f"任务状态异常：{status}")
            time.sleep(5)
        raise TimeoutError("任务轮询超时。")
    except Exception as exc:
        with jobs_lock:
            job = jobs[job_id]
            job.status = "error"
            job.message = str(exc)
            job.updated_at = time.time()


def status_label(status: str) -> str:
    return {
        "created": "已创建",
        "submitting": "提交任务中",
        "in_queue": "排队中",
        "generating": "生成中",
        "done": "已完成",
        "not_found": "任务未找到",
        "expired": "任务已过期",
        "error": "失败",
    }.get(status, status)


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
            self.send_json({"models": MODEL_CATALOG, "camera_templates": CAMERA_TEMPLATES})
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
        access_key = str(payload.get("access_key") or "").strip()
        secret_key = str(payload.get("secret_key") or "").strip()
        if not access_key or not secret_key:
            raise ValueError("请填写 Access Key ID 和 Secret Access Key。")
        memory_credentials["access_key"] = access_key
        memory_credentials["secret_key"] = secret_key
        if bool(payload.get("save")):
            write_config(access_key, secret_key)
        self.send_json(credential_status())

    def generate(self, payload: dict[str, Any]) -> None:
        access_key, secret_key = resolve_credentials(payload)
        model_key, body = build_body(payload)
        job_id = secrets.token_urlsafe(10)
        job = Job(id=job_id, model_key=model_key, prompt=str(payload.get("prompt") or ""))
        with jobs_lock:
            jobs[job_id] = job
        thread = threading.Thread(target=run_job, args=(job_id, body, access_key, secret_key), daemon=True)
        thread.start()
        self.send_json(job.as_dict())

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 18 * 1024 * 1024:
            raise ValueError("请求过大。")
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
  <title>即梦视频生成控制台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8f7;
      --panel: #ffffff;
      --panel-soft: #f1f4f2;
      --line: #d8ded9;
      --text: #17201b;
      --muted: #627067;
      --accent: #256f53;
      --accent-2: #345d86;
      --danger: #b43c3c;
      --shadow: 0 18px 44px rgba(29, 43, 34, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 "Inter", "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      letter-spacing: 0;
    }
    button, input, textarea, select { font: inherit; letter-spacing: 0; }
    .app {
      display: grid;
      grid-template-columns: 300px minmax(520px, 1fr) 360px;
      gap: 16px;
      width: min(1480px, calc(100vw - 32px));
      margin: 16px auto;
      min-height: calc(100vh - 32px);
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      min-width: 0;
    }
    .sidebar, .result {
      padding: 16px;
      align-self: start;
      position: sticky;
      top: 16px;
      max-height: calc(100vh - 32px);
      overflow: auto;
    }
    .main {
      padding: 16px;
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      gap: 14px;
    }
    h1, h2, h3 {
      margin: 0;
      font-weight: 680;
    }
    h1 { font-size: 20px; }
    h2 { font-size: 14px; color: var(--muted); }
    h3 { font-size: 13px; color: var(--muted); text-transform: uppercase; }
    .topline {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--line);
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 30px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: var(--panel-soft);
      white-space: nowrap;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--danger);
    }
    .dot.ok { background: var(--accent); }
    .stack { display: grid; gap: 12px; }
    .field { display: grid; gap: 6px; }
    label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 620;
    }
    input, textarea, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--text);
      outline: none;
      padding: 10px 11px;
    }
    input:focus, textarea:focus, select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(37, 111, 83, .12);
    }
    textarea {
      min-height: 320px;
      resize: vertical;
    }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
    .segmented {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 6px;
    }
    .segmented button {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      color: var(--muted);
      min-height: 40px;
      cursor: pointer;
    }
    .segmented button.active {
      border-color: var(--accent);
      background: #e5f1eb;
      color: var(--accent);
      font-weight: 680;
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
      border: 1px solid var(--line);
      border-radius: 8px;
      min-height: 40px;
      padding: 0 14px;
      background: var(--panel-soft);
      color: var(--text);
      cursor: pointer;
    }
    .btn.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
      min-width: 126px;
      font-weight: 680;
    }
    .btn:disabled {
      opacity: .58;
      cursor: not-allowed;
    }
    .uploader {
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
      padding: 12px;
      border: 1px dashed #b8c2ba;
      border-radius: 8px;
      background: #fbfcfb;
    }
    .thumbs {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .thumb {
      min-height: 108px;
      border: 1px solid var(--line);
      border-radius: 8px;
      display: grid;
      place-items: center;
      overflow: hidden;
      background: var(--panel-soft);
      color: var(--muted);
      text-align: center;
      padding: 8px;
    }
    .thumb img {
      width: 100%;
      height: 108px;
      object-fit: cover;
      display: block;
    }
    video {
      width: 100%;
      aspect-ratio: 16 / 9;
      background: #0e1210;
      border-radius: 8px;
      border: 1px solid var(--line);
    }
    .jobbox {
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      min-height: 118px;
      display: grid;
      gap: 8px;
      align-content: start;
      overflow-wrap: anywhere;
    }
    .progress {
      height: 7px;
      border-radius: 999px;
      background: #dbe2dd;
      overflow: hidden;
    }
    .bar {
      height: 100%;
      width: 20%;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
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
      color: var(--text);
      text-decoration: none;
      background: #fff;
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
          <h1>即梦控制台</h1>
          <h2>Volcengine Visual API</h2>
        </div>
        <span class="status-pill"><span id="keyDot" class="dot"></span><span id="keyStatus">未配置</span></span>
      </div>

      <div class="stack">
        <h3>密钥</h3>
        <div class="field">
          <label for="accessKey">Access Key ID</label>
          <input id="accessKey" autocomplete="off" spellcheck="false" />
        </div>
        <div class="field">
          <label for="secretKey">Secret Access Key</label>
          <input id="secretKey" type="password" autocomplete="off" spellcheck="false" />
        </div>
        <div class="field">
          <label for="keyFile">导入 Key 文件</label>
          <input id="keyFile" type="file" accept=".txt,.env,.json,text/plain,application/json" />
        </div>
        <label><input id="saveKey" type="checkbox" style="width:auto;margin-right:6px" />保存到本机配置</label>
        <button id="saveConfig" class="btn">保存配置</button>
      </div>

      <div class="stack">
        <h3>模型</h3>
        <div class="field">
          <label for="model">生成模型</label>
          <select id="model"></select>
        </div>
        <div class="grid-2">
          <div class="field">
            <label for="frames">时长</label>
            <select id="frames">
              <option value="121">5s</option>
              <option value="241">10s</option>
            </select>
          </div>
          <div class="field" id="aspectField">
            <label for="aspect">比例</label>
            <select id="aspect">
              <option>16:9</option>
              <option>9:16</option>
              <option>1:1</option>
              <option>4:3</option>
              <option>3:4</option>
              <option>21:9</option>
            </select>
          </div>
        </div>
        <div class="field">
          <label for="seed">Seed</label>
          <input id="seed" type="number" value="-1" />
        </div>
      </div>

      <div id="cameraFields" class="stack hidden">
        <h3>运镜</h3>
        <div class="field">
          <label for="template">模板</label>
          <select id="template"></select>
        </div>
        <div class="segmented" id="strengths">
          <button type="button" data-strength="weak">弱</button>
          <button type="button" data-strength="medium" class="active">中</button>
          <button type="button" data-strength="strong">强</button>
        </div>
      </div>
    </aside>

    <main class="panel main">
      <div class="topline">
        <div>
          <h1>生成任务</h1>
          <h2 id="modeHint">提示词生成或首帧图生</h2>
        </div>
        <button id="clearForm" class="btn">清空</button>
      </div>

      <div id="imageSection" class="uploader">
        <div class="grid-2">
          <div class="field">
            <label for="firstFile">首帧图</label>
            <input id="firstFile" type="file" accept="image/png,image/jpeg" />
          </div>
          <div class="field" id="tailFileWrap">
            <label for="tailFile">尾帧图</label>
            <input id="tailFile" type="file" accept="image/png,image/jpeg" />
          </div>
        </div>
        <div class="grid-2">
          <div class="field">
            <label for="firstUrl">首帧 URL</label>
            <input id="firstUrl" placeholder="https://..." />
          </div>
          <div class="field" id="tailUrlWrap">
            <label for="tailUrl">尾帧 URL</label>
            <input id="tailUrl" placeholder="https://..." />
          </div>
        </div>
        <div class="thumbs">
          <div class="thumb" id="firstThumb">首帧预览</div>
          <div class="thumb" id="tailThumb">尾帧预览</div>
        </div>
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
      <div id="job" class="jobbox">
        <strong>未开始</strong>
        <span class="muted small">配置参数后提交任务</span>
        <div class="progress"><div class="bar" style="width:0%"></div></div>
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
    let cameraStrength = "medium";
    let firstBase64 = "";
    let tailBase64 = "";

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
      if (data.access_key) $("accessKey").placeholder = data.access_key;
    }

    function parseKeyFile(text) {
      const trimmed = text.trim();
      if (!trimmed) throw new Error("Key 文件为空");
      try {
        const data = JSON.parse(trimmed);
        const access = data.access_key || data.accessKey || data.AccessKeyID || data.AccessKeyId || data.VOLC_ACCESSKEY;
        const secret = data.secret_key || data.secretKey || data.SecretAccessKey || data.VOLC_SECRETKEY;
        if (access && secret) return {access: String(access).trim(), secret: String(secret).trim()};
      } catch (_) {}
      const lines = trimmed.split(/\r?\n/);
      const values = {};
      for (const line of lines) {
        const clean = line.trim();
        if (!clean || clean.startsWith("#")) continue;
        const match = clean.match(/^([^:=\s][^:=]*?)\s*[:=]\s*(.+)$/);
        if (!match) continue;
        const key = match[1].trim().toLowerCase().replace(/[\s_\-]/g, "");
        const value = match[2].trim().replace(/^["']|["']$/g, "");
        if (["accesskeyid", "accesskey", "ak", "volcaccesskey", "volcaccesskeyid"].includes(key)) {
          values.access = value;
        }
        if (["secretaccesskey", "secretkey", "sk", "volcsecretkey"].includes(key)) {
          values.secret = value;
        }
      }
      if (values.access && values.secret) return values;
      const ak = trimmed.match(/\b(AKLT[A-Za-z0-9+/=_-]{20,})\b/);
      const maybeSecrets = [...trimmed.matchAll(/\b([A-Za-z0-9+/=_-]{32,})\b/g)].map(m => m[1]).filter(v => !ak || v !== ak[1]);
      if (ak && maybeSecrets.length) return {access: ak[1], secret: maybeSecrets[0]};
      throw new Error("没有解析到 AccessKeyID 和 SecretAccessKey");
    }

    function renderModels(data) {
      models = data.models;
      $("model").innerHTML = Object.entries(models).map(([key, model]) =>
        `<option value="${key}">${model.label}</option>`
      ).join("");
      $("template").innerHTML = Object.entries(data.camera_templates).map(([key, label]) =>
        `<option value="${key}">${label}</option>`
      ).join("");
      $("template").value = "crane_push";
      updateModelFields();
    }

    function updateModelFields() {
      const model = models[$("model").value] || {};
      const imageMode = model.images || "none";
      $("aspectField").classList.toggle("hidden", !model.aspect);
      $("cameraFields").classList.toggle("hidden", model.mode !== "camera");
      $("imageSection").classList.toggle("hidden", imageMode === "none");
      const twoImages = imageMode === "two";
      $("tailFileWrap").classList.toggle("hidden", !twoImages);
      $("tailUrlWrap").classList.toggle("hidden", !twoImages);
      $("tailThumb").classList.toggle("hidden", !twoImages);
      $("modeHint").textContent = model.label || "生成任务";
    }

    async function fileToBase64(file, thumbId) {
      if (!file) return "";
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      $(thumbId).innerHTML = `<img src="${dataUrl}" alt="">`;
      return String(dataUrl).split(",")[1] || "";
    }

    function resetJobView() {
      $("video").classList.add("hidden");
      $("video").removeAttribute("src");
      $("download").classList.add("hidden");
      $("download").removeAttribute("href");
    }

    function progressFor(status) {
      return {
        created: 8,
        submitting: 16,
        in_queue: 36,
        generating: 68,
        done: 100,
        error: 100,
        expired: 100,
        not_found: 100
      }[status] || 48;
    }

    function renderJob(job) {
      const pct = progressFor(job.status);
      $("taskCaption").textContent = job.task_id ? `Task ${job.task_id}` : job.model;
      $("job").innerHTML = `
        <strong>${job.message || job.status}</strong>
        <span class="muted small">${job.model || ""}</span>
        ${job.task_id ? `<span class="small">task_id: ${job.task_id}</span>` : ""}
        <div class="progress"><div class="bar" style="width:${pct}%"></div></div>
      `;
      if (job.status === "done" && job.download_path) {
        $("video").src = job.download_path;
        $("video").classList.remove("hidden");
        $("download").href = job.download_path;
        $("download").classList.remove("hidden");
        loadHistory();
      }
      if (job.status === "error") {
        $("job").innerHTML += `<span class="small" style="color:var(--danger)">${job.message}</span>`;
      }
    }

    async function pollJob(id) {
      currentJob = id;
      while (currentJob === id) {
        const job = await api(`/api/jobs/${id}`);
        renderJob(job);
        if (["done", "error", "expired", "not_found"].includes(job.status)) break;
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

    async function generate() {
      $("generate").disabled = true;
      resetJobView();
      const payload = {
        access_key: $("accessKey").value.trim(),
        secret_key: $("secretKey").value.trim(),
        model: $("model").value,
        prompt: $("prompt").value.trim(),
        frames: Number($("frames").value),
        aspect_ratio: $("aspect").value,
        seed: Number($("seed").value || -1),
        first_image_base64: firstBase64,
        tail_image_base64: tailBase64,
        first_image_url: $("firstUrl").value.trim(),
        tail_image_url: $("tailUrl").value.trim(),
        template_id: $("template").value,
        camera_strength: cameraStrength
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
          body: JSON.stringify({
            access_key: $("accessKey").value.trim(),
            secret_key: $("secretKey").value.trim(),
            save: $("saveKey").checked
          })
        });
        $("accessKey").value = "";
        $("secretKey").value = "";
        renderConfig(data);
      } catch (err) {
        alert(err.message);
      }
    });
    $("keyFile").addEventListener("change", async (event) => {
      const file = event.target.files[0];
      if (!file) return;
      try {
        const parsed = parseKeyFile(await file.text());
        $("accessKey").value = parsed.access;
        $("secretKey").value = parsed.secret;
        $("keyStatus").textContent = "已导入";
        $("keyDot").classList.add("ok");
      } catch (err) {
        alert(err.message);
      } finally {
        event.target.value = "";
      }
    });
    $("model").addEventListener("change", updateModelFields);
    $("generate").addEventListener("click", generate);
    $("prompt").addEventListener("input", () => $("charCount").textContent = `${$("prompt").value.length} 字`);
    $("clearForm").addEventListener("click", () => {
      $("prompt").value = "";
      $("firstFile").value = "";
      $("tailFile").value = "";
      $("firstUrl").value = "";
      $("tailUrl").value = "";
      firstBase64 = "";
      tailBase64 = "";
      $("firstThumb").textContent = "首帧预览";
      $("tailThumb").textContent = "尾帧预览";
      $("charCount").textContent = "0 字";
    });
    $("refreshHistory").addEventListener("click", loadHistory);
    $("firstFile").addEventListener("change", async (event) => {
      firstBase64 = await fileToBase64(event.target.files[0], "firstThumb");
    });
    $("tailFile").addEventListener("change", async (event) => {
      tailBase64 = await fileToBase64(event.target.files[0], "tailThumb");
    });
    $("strengths").addEventListener("click", (event) => {
      const btn = event.target.closest("button");
      if (!btn) return;
      cameraStrength = btn.dataset.strength;
      document.querySelectorAll("#strengths button").forEach(el => el.classList.toggle("active", el === btn));
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
    print(f"Jimeng UI running at http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

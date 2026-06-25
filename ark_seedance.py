from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


API_BASE = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"


class ArkAPIError(RuntimeError):
    pass


def _request(method: str, url: str, api_key: str, body: dict[str, Any] | None = None, timeout: int = 60) -> dict[str, Any]:
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        message = details
        try:
            data = json.loads(details)
            message = data.get("message") or data.get("description") or json.dumps(data, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
        raise ArkAPIError(f"HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise ArkAPIError(f"Network error: {exc.reason}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArkAPIError(f"Invalid JSON response: {raw[:500]}") from exc


def create_generation_task(body: dict[str, Any], api_key: str) -> tuple[str, dict[str, Any]]:
    result = _request("POST", API_BASE, api_key, body=body, timeout=90)
    task_id = str(result.get("id") or "").strip()
    if not task_id:
        raise ArkAPIError(f"Create response missing id: {json.dumps(result, ensure_ascii=False)}")
    return task_id, result


def get_generation_task(task_id: str, api_key: str) -> dict[str, Any]:
    quoted = urllib.parse.quote(task_id, safe="")
    return _request("GET", f"{API_BASE}/{quoted}", api_key, timeout=60)


def download_file(url: str, output: Path, timeout: int = 180) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "seedance-ui/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            output.write_bytes(resp.read())
    except urllib.error.URLError as exc:
        raise ArkAPIError(f"Download failed: {exc.reason}") from exc

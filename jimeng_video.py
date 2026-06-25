import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


API_HOST = "visual.volcengineapi.com"
API_ENDPOINT = f"https://{API_HOST}"
API_VERSION = "2022-08-31"
REGION = "cn-north-1"
SERVICE = "cv"
ALGORITHM = "HMAC-SHA256"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _canonical_query(params: dict[str, str]) -> str:
    pairs = []
    for key in sorted(params):
        pairs.append(
            f"{urllib.parse.quote(key, safe='-_.~')}="
            f"{urllib.parse.quote(str(params[key]), safe='-_.~')}"
        )
    return "&".join(pairs)


def _authorization(access_key: str, secret_key: str, action: str, payload: bytes, x_date: str) -> tuple[str, str]:
    date = x_date[:8]
    payload_hash = _sha256_hex(payload)
    query = _canonical_query({"Action": action, "Version": API_VERSION})
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_headers = (
        "content-type:application/json\n"
        f"host:{API_HOST}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
    )
    canonical_request = "\n".join(
        ["POST", "/", query, canonical_headers, signed_headers, payload_hash]
    )
    credential_scope = f"{date}/{REGION}/{SERVICE}/request"
    string_to_sign = "\n".join(
        [ALGORITHM, x_date, credential_scope, _sha256_hex(canonical_request.encode("utf-8"))]
    )
    signing_key = _hmac_sha256(
        _hmac_sha256(_hmac_sha256(_hmac_sha256(secret_key.encode("utf-8"), date), REGION), SERVICE),
        "request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        f"{ALGORITHM} Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return authorization, payload_hash


def call_visual(action: str, body: dict, access_key: str | None = None, secret_key: str | None = None) -> dict:
    access_key = access_key or os.environ.get("VOLC_ACCESSKEY")
    secret_key = secret_key or os.environ.get("VOLC_SECRETKEY")
    if not access_key or not secret_key:
        raise RuntimeError("Set VOLC_ACCESSKEY and VOLC_SECRETKEY in the environment.")

    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    x_date = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    authorization, payload_hash = _authorization(access_key, secret_key, action, payload, x_date)
    url = f"{API_ENDPOINT}?{_canonical_query({'Action': action, 'Version': API_VERSION})}"
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json",
            "Host": API_HOST,
            "X-Date": x_date,
            "X-Content-Sha256": payload_hash,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {details}") from exc


def submit_task_body(body: dict, access_key: str | None = None, secret_key: str | None = None) -> str:
    result = call_visual("CVSync2AsyncSubmitTask", body, access_key=access_key, secret_key=secret_key)
    if result.get("code") != 10000:
        raise RuntimeError(f"submit failed: {json.dumps(result, ensure_ascii=False)}")
    task_id = result.get("data", {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"submit response missing task_id: {json.dumps(result, ensure_ascii=False)}")
    print(f"submitted task_id={task_id}", flush=True)
    return task_id


def submit_task(prompt: str, frames: int, aspect_ratio: str, seed: int) -> str:
    body = {
        "req_key": "jimeng_ti2v_v30_pro",
        "prompt": prompt,
        "seed": seed,
        "frames": frames,
        "aspect_ratio": aspect_ratio,
    }
    return submit_task_body(body)


def poll_result(
    task_id: str,
    interval: int,
    max_attempts: int,
    req_key: str = "jimeng_ti2v_v30_pro",
    access_key: str | None = None,
    secret_key: str | None = None,
) -> str:
    body = {"req_key": req_key, "task_id": task_id}
    for attempt in range(1, max_attempts + 1):
        result = call_visual("CVSync2AsyncGetResult", body, access_key=access_key, secret_key=secret_key)
        if result.get("code") != 10000:
            raise RuntimeError(f"poll failed: {json.dumps(result, ensure_ascii=False)}")
        data = result.get("data") or {}
        status = data.get("status")
        print(f"poll {attempt}/{max_attempts}: status={status}", flush=True)
        if status == "done":
            video_url = data.get("video_url")
            if not video_url:
                raise RuntimeError(f"done response missing video_url: {json.dumps(result, ensure_ascii=False)}")
            return video_url
        if status in {"not_found", "expired"}:
            raise RuntimeError(f"task ended with status={status}: {json.dumps(result, ensure_ascii=False)}")
        time.sleep(interval)
    raise TimeoutError(f"task not finished after {max_attempts} polling attempts")


def download_video(video_url: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(video_url, timeout=120) as resp:
        output.write_bytes(resp.read())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--output", default="outputs/jimeng_3pro_video.mp4")
    parser.add_argument("--frames", type=int, choices=[121, 241], default=121)
    parser.add_argument("--aspect-ratio", choices=["16:9", "4:3", "1:1", "3:4", "9:16", "21:9"], default="16:9")
    parser.add_argument("--seed", type=int, default=-1)
    parser.add_argument("--poll-interval", type=int, default=5)
    parser.add_argument("--max-attempts", type=int, default=180)
    args = parser.parse_args()

    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    task_id = submit_task(prompt, args.frames, args.aspect_ratio, args.seed)
    video_url = poll_result(task_id, args.poll_interval, args.max_attempts)
    output = Path(args.output)
    download_video(video_url, output)
    print(json.dumps({"task_id": task_id, "video_url": video_url, "output": str(output.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

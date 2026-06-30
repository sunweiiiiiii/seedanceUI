from __future__ import annotations

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / ".seedance_config.json"


def env(name: str, fallback: str = "") -> str:
    return os.environ.get(name, fallback).strip()


def main() -> int:
    config = {
        "api_key": env("ARK_API_KEY") or env("VOLC_ARK_API_KEY"),
        "tos_access_key": env("TOS_ACCESSKEY") or env("VOLC_ACCESSKEY"),
        "tos_secret_key": env("TOS_SECRETKEY") or env("VOLC_SECRETKEY"),
        "tos_bucket": env("TOS_BUCKET", "miles"),
        "tos_region": env("TOS_REGION", "cn-beijing"),
        "tos_endpoint": env("TOS_ENDPOINT", "tos-cn-beijing.volces.com"),
        "tos_prefix": env("TOS_PREFIX", "seedance-references"),
        "tos_url_mode": env("TOS_URL_MODE", "signed"),
        "tos_signed_expires": env("TOS_SIGNED_EXPIRES", "86400"),
        "tos_public_base_url": env("TOS_PUBLIC_BASE_URL"),
    }
    missing = [
        name
        for name in ("api_key", "tos_access_key", "tos_secret_key", "tos_bucket")
        if not config[name]
    ]
    if missing:
        print("Missing required values: " + ", ".join(missing))
        return 1
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

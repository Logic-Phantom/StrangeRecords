"""파일 입출력 헬퍼."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def now_iso(tz: str = "Asia/Seoul") -> str:
    return datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")


def new_job_id(tz: str = "Asia/Seoul") -> str:
    return datetime.now(ZoneInfo(tz)).strftime("%Y%m%d-%H%M%S")


def remove_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def download_file(url: str, dest: Path, *, timeout: int = 30, max_mb: int = 80, headers: dict | None = None) -> Path:
    """스트리밍 다운로드. 크기 제한을 넘으면 중단한다."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    limit = max_mb * 1024 * 1024
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=timeout, headers=headers) as resp:
        resp.raise_for_status()
        size = 0
        with open(tmp, "wb") as fp:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                size += len(chunk)
                if size > limit:
                    fp.close()
                    tmp.unlink(missing_ok=True)
                    raise ValueError(f"다운로드 크기 제한 초과({max_mb}MB): {url}")
                fp.write(chunk)
    tmp.replace(dest)
    return dest

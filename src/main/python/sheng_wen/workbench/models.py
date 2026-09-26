from __future__ import annotations
import hashlib, json, threading, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from ..utils.project_root import get_project_root

ROOT = get_project_root() / "models"
MODEL_IDS = ("tiny", "base", "small", "medium", "large-v3")
_jobs = {}
_lock = threading.Lock()


def catalog():
    ROOT.mkdir(exist_ok=True)
    result = []
    from ..config.settings import config

    active = config.whisper.configured_model_path
    paths = list(ROOT.iterdir())
    if active and Path(active).is_dir() and Path(active) not in paths:
        paths.append(Path(active))
    for p in paths:
        if not p.is_dir():
            continue
        required = ["config.json", "model.bin", "tokenizer.json"]
        complete = all(
            (p / f).is_file() and (p / f).stat().st_size > 0 for f in required
        ) and any((p / f).is_file() for f in ("vocabulary.json", "vocabulary.txt"))
        verified = (p / ".verified.json").exists()
        if verified:
            try:
                mark = json.loads((p / ".verified.json").read_text())
                stat = (p / "model.bin").stat()
                verified = (
                    mark.get("size") == stat.st_size
                    and mark.get("mtime_ns") == stat.st_mtime_ns
                )
            except Exception:
                verified = False
        result.append(
            {
                "id": p.name,
                "path": str(p),
                "size": sum(f.stat().st_size for f in p.iterdir() if f.is_file()),
                "complete": complete,
                "verified": verified,
                "active": bool(active and Path(active).resolve() == p.resolve()),
            }
        )
    return {
        "models": result,
        "downloads": [
            {k: v for k, v in j.items() if k != "stop"} for j in _jobs.values()
        ],
    }


def download(name):
    if name not in MODEL_IDS:
        raise ValueError("不支持的模型")
    with _lock:
        if name in _jobs and _jobs[name]["phase"] in ("downloading", "verifying"):
            return _jobs[name]["phase"]
        job = {
            "id": name,
            "phase": "downloading",
            "downloaded": 0,
            "total": 0,
            "speed": 0,
            "error": None,
            "stop": threading.Event(),
        }
        _jobs[name] = job
    threading.Thread(target=_download, args=(name, job), daemon=True).start()
    return "downloading"


def _download(name, job):
    import requests, shutil

    p = ROOT / f"faster-whisper-{name}"
    p.mkdir(parents=True, exist_ok=True)
    try:
        r = requests.get(
            f"https://huggingface.co/api/models/Systran/faster-whisper-{name}?blobs=true",
            timeout=25,
        )
        r.raise_for_status()
        info = r.json()
        files = [
            f
            for f in info["siblings"]
            if f["rfilename"]
            in (
                "config.json",
                "preprocessor_config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.json",
                "vocabulary.txt",
            )
        ]
        job["total"] = sum(f["size"] for f in files)
        begin = time.monotonic()
        for f in files:
            target = p / f["rfilename"]
            size = f["size"]
            sha = f.get("lfs", {}).get("sha256")
            if job["stop"].is_set():
                job["phase"] = "paused"
                return
            if target.is_file() and target.stat().st_size == size:
                with target.open("rb") as inp:
                    valid = (
                        (hashlib.file_digest(inp, "sha256").hexdigest() == sha)
                        if sha
                        else True
                    )
                if valid:
                    job["downloaded"] += size
                    continue
            url = f"https://huggingface.co/Systran/faster-whisper-{name}/resolve/{info['sha']}/{f['rfilename']}"
            if f["rfilename"] != "model.bin":
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                if len(r.content) != size:
                    raise ValueError("文件长度不一致")
                target.write_bytes(r.content)
                job["downloaded"] += size
                continue
            parts = p / ".download-parts"
            parts.mkdir(exist_ok=True)
            chunk = 8 * 1024 * 1024

            def fetch(start):
                end = min(size, start + chunk) - 1
                part = parts / f"{info['sha']}-{start}"
                if part.exists() and part.stat().st_size == end - start + 1:
                    return part
                for retry in range(4):
                    if job["stop"].is_set():
                        return None
                    try:
                        r = requests.get(
                            url + f"?part={start}",
                            headers={"Range": f"bytes={start}-{end}"},
                            timeout=(15, 45),
                        )
                        r.raise_for_status()
                        if r.status_code != 206 or len(r.content) != end - start + 1:
                            raise ValueError("下载服务器未返回正确分段")
                        part.write_bytes(r.content)
                        return part
                    except Exception:
                        if retry == 3:
                            raise
                        time.sleep(1 + retry)

            starts = list(range(0, size, chunk))
            with ThreadPoolExecutor(max_workers=8) as pool:
                for future in as_completed([pool.submit(fetch, s) for s in starts]):
                    part = future.result()
                    if part:
                        job["downloaded"] += part.stat().st_size
                        job["speed"] = job["downloaded"] / max(
                            1, time.monotonic() - begin
                        )
            if job["stop"].is_set():
                job["phase"] = "paused"
                return
            job["phase"] = "verifying"
            tmp = p / "model.bin.partial"
            with tmp.open("wb") as out:
                for start in starts:
                    with (parts / f"{info['sha']}-{start}").open("rb") as inp:
                        shutil.copyfileobj(inp, out)
            with tmp.open("rb") as inp:
                if not sha or hashlib.file_digest(inp, "sha256").hexdigest() != sha:
                    raise ValueError("模型哈希不一致，请重试")
            tmp.replace(target)
            for start in starts:
                (parts / f"{info['sha']}-{start}").unlink()
            job["phase"] = "downloading"
        weight = p / "model.bin"
        stat = weight.stat()
        (p / ".verified.json").write_text(
            json.dumps(
                {
                    "revision": info["sha"],
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            ),
            encoding="utf-8",
        )
        job["phase"] = "complete"
    except Exception as exc:
        job["phase"] = "failed"
        job["error"] = type(exc).__name__ + "：下载失败，可继续重试；已保留完成分段"


def pause(name):
    if name in _jobs:
        _jobs[name]["stop"].set()

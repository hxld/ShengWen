from __future__ import annotations
import re, time
from pathlib import Path
from .store import get_store

ACTIVE = {"PENDING", "DOWNLOADING", "UPLOADING", "TRANSCRIBING", "SUMMARIZING"}


def api_module():
    from .. import api

    return api


def active_workers():
    api = api_module()
    return [
        w
        for w in (
            api.downloader_worker,
            api.file_upload_worker,
            api.transcriber_worker,
            api.llm_worker,
        )
        if w
    ]


async def cancel(task_id):
    api = api_module()
    task = api.db.get_task(task_id)
    if not task:
        raise ValueError("任务不存在")
    get_store().cancel(task_id)
    for worker in active_workers():
        worker.cancel_task(task_id)
    from ..task_updater import update_and_notify

    await update_and_notify(
        task_id,
        {
            "status": "FAILED",
            "error_message": "已取消；原文与已有结果保留，可重试",
            "progress": 0,
        },
    )


async def retry(task_id, force=False):
    api = api_module()
    store = get_store()
    task = api.db.get_task(task_id)
    if not task:
        raise ValueError("任务不存在")
    for worker in active_workers():
        if worker._active_task_id == task_id:
            raise ValueError("旧任务仍在停止，请稍后再重试")
    if not force and task["status"] in ACTIVE:
        raise ValueError("任务正在排队或处理中")
    job = store.job(task_id)
    payload = (
        dict(job["payload"])
        if job
        else {
            "task_id": task_id,
            "video_url": task["video_url"],
            "summary_mode": task.get("summary_mode") or "auto",
        }
    )
    for worker in active_workers():
        worker._cancelled_task_ids.discard(task_id)
    store.cancel(task_id, False)
    if task.get("transcript"):
        path = Path("temp") / f"{task_id}_resume.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text(task["transcript"], encoding="utf-8")
        payload.update(
            intermediate_file_path=str(path),
            output_file=f"temp/{task_id}_resume_summary.md",
        )
        worker = await api.get_llm_worker()
    elif payload.get("video_file") and Path(payload["video_file"]).is_file():
        worker = await api.get_transcriber_worker()
    elif task["video_url"].startswith("file://"):
        from ..utils.media import build_transcriber_payload

        path = api._resolve_file_url_path(task["video_url"])
        if not Path(path).is_file():
            raise ValueError("原始本地文件不存在，请重新导入")
        payload.update(
            build_transcriber_payload(
                task_id, path, summary_mode=payload.get("summary_mode", "auto")
            )
        )
        worker = await api.get_transcriber_worker()
    else:
        worker = await api.get_downloader_worker()
    api.db.update_task(
        task_id, {"status": "PENDING", "error_message": None, "progress": 0}
    )
    await worker.add_task(payload)
    await api.notify_task_update(task_id)
    return api.db.get_task(task_id)


async def initialize():
    api = api_module()
    from .connections import get_connection

    manager = api.config_manager
    cfg = manager.get_raw_config().get("whisper", {})
    secret = cfg.get("bilibili_cookie_string") or cfg.get("bilibili_sessdata")
    if secret:
        connection = get_connection()
        if not connection.data.get("cookies"):
            connection.import_value(secret, "migrated")
        manager.update_section(
            "whisper", {"bilibili_sessdata": "", "bilibili_cookie_string": ""}
        )
        api.transcription_settings_manager._bilibili_sessdata = ""
    # Recovery is explicit for interrupted work; pending durable jobs can safely be requeued.
    for task in api.db.list_tasks():
        if task["status"] == "PENDING":
            try:
                await retry(task["id"], True)
            except Exception:
                api.db.update_task(
                    task["id"],
                    {
                        "status": "FAILED",
                        "error_message": "排队任务恢复失败，请检查来源后重试",
                    },
                )


def archive(task_id, kind, content, meta=None):
    get_store().version(task_id, kind, content, meta)


def export_note(task, directory=None):
    import yaml

    store = get_store()
    directory = directory or store.setting(
        "obsidian_dir", r"D:\study\hxld_obsidian\inbox"
    )
    p = Path(directory)
    if not p.is_absolute():
        raise ValueError("导出目录必须是绝对路径")
    p.mkdir(parents=True, exist_ok=True)
    title = task.get("topic") or task.get("title") or "未命名视频"
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip(" .")[:75] or "未命名视频"
    target = p / f"{safe}-{task['id'][:8]}.md"
    if target.exists():
        target = p / f"{safe}-{task['id'][:8]}-{time.time_ns()}.md"
    meta = {
        "title": title,
        "source": task.get("video_url", ""),
        "author": task.get("author_name") or "",
        "task_id": task["id"],
        "tags": ["视频总结", "AI笔记"],
        "type": "视频笔记",
    }
    text = (
        "---\n"
        + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
        + "---\n\n# "
        + title
        + "\n\n"
        + (task.get("summary") or "")
    )
    with target.open("x", encoding="utf-8") as out:
        out.write(text)
    return {
        "success": True,
        "file_path": str(target),
        "overwritten": False,
        "error": "",
    }


def prepare_rerun(task_id):
    api = api_module()
    store = get_store()
    task = api.db.get_task(task_id)
    if task and task["status"] in ACTIVE:
        raise ValueError("任务正在运行，请先取消或等待完成")
    for w in active_workers():
        if w._active_task_id == task_id:
            raise ValueError("旧任务尚未停止，请稍后重试")
        w._cancelled_task_ids.discard(task_id)
    store.cancel(task_id, False)
    if task:
        store.version(
            task_id, "summary", task.get("summary"), {"reason": "before_rerun"}
        )
        store.version(
            task_id, "transcript", task.get("transcript"), {"reason": "before_rerun"}
        )


def cleanup(before_date=""):
    from datetime import datetime

    cutoff = (
        datetime.strptime(before_date, "%Y-%m-%d")
        .replace(hour=23, minute=59, second=59)
        .timestamp()
        if before_date
        else None
    )
    root = Path("temp").resolve()
    store = get_store()
    api = api_module()
    deleted = 0
    freed = 0
    skipped = 0
    active_ids = {t["id"] for t in api.db.list_tasks() if t["status"] in ACTIVE}
    for p in root.rglob("*"):
        if not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(root):
            continue
        owner = store.artifact_owner(str(p))
        if (
            not owner
            or owner in active_ids
            or any(part in active_ids for part in p.parts)
        ):
            skipped += 1
            continue
        if cutoff and p.stat().st_mtime > cutoff:
            continue
        # Keep checkpoints and subtitle text: they enable recovery and revisions.
        if p.suffix.lower() not in (
            ".mp4",
            ".mkv",
            ".webm",
            ".m4a",
            ".mp3",
            ".wav",
            ".flac",
            ".aac",
            ".ogg",
        ):
            skipped += 1
            continue
        size = p.stat().st_size
        p.unlink()
        deleted += 1
        freed += size
    return {
        "success": True,
        "deleted_files": deleted,
        "freed_size_mb": round(freed / 1048576, 2),
        "skipped_files": skipped,
        "error": "",
    }

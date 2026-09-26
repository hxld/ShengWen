from __future__ import annotations
import asyncio, json
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from .connections import get_connection, attach_cookies
from .runtime import runtime
from .store import get_store
from .transcripts import parse_transcript, plain
from ..worker import TaskCancelledError


def process_bilibili(worker, payload):
    """Resolve every selected part independently, checkpointing completed text."""
    from bilibili_api import video, Credential
    import yt_dlp
    from ..task_updater import update_and_notify

    task_id = payload["task_id"]
    url = payload["video_url"]
    bvid = worker._extract_bvid_from_url(url)
    parts = payload.get("bilibili_parts") or {}
    default_part = max(0, int(parse_qs(urlsplit(url).query).get("p", ["1"])[0]) - 1)
    indices = sorted(set(parts.get("indices") or [default_part]))
    if not indices or any(not isinstance(i, int) or i < 0 for i in indices):
        raise ValueError("分 P 索引无效")
    cookies = get_connection().cookies(payload.get("bilibili_sessdata", ""))
    cred = Credential.from_cookies(cookies)
    obj = video.Video(bvid=bvid, credential=cred)
    info = asyncio.run(obj.get_info())
    pages = info.get("pages", [])
    if any(i >= len(pages) for i in indices):
        raise ValueError("所选分 P 不存在，请重新选择")
    store = get_store()
    directory = Path("temp") / task_id
    directory.mkdir(parents=True, exist_ok=True)
    all_segments = []
    offset = 0
    coverage = []
    source_map = []
    prefer = worker.transcription_settings_manager.get_runtime_state().get(
        "enable_bilibili_subtitle_fetch", True
    )
    for number, index in enumerate(indices):
        if worker.is_task_cancelled(task_id):
            raise TaskCancelledError("任务已取消")
        checkpoint = directory / f"part-{index}.json"
        result = None
        if checkpoint.exists():
            try:
                result = json.loads(checkpoint.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        if result is None:
            worker._submit_coro(
                update_and_notify(
                    task_id,
                    {"status": "DOWNLOADING", "progress": number / len(indices) * 100},
                )
            )
            subtitle = None
            if prefer:
                try:
                    subtitle = asyncio.run(
                        worker._extract_bilibili_subtitle_via_api(
                            url, cookies.get("SESSDATA", ""), index
                        )
                    )
                except Exception:
                    pass
            if subtitle:
                segments = parse_transcript(subtitle["transcript"])
                source = "subtitle"
                duration = subtitle.get("duration") or pages[index]["duration"]
            else:
                part_url = f"https://www.bilibili.com/video/{bvid}/?p={index + 1}"

                def progress(_):
                    if worker.is_task_cancelled(task_id):
                        raise TaskCancelledError("任务已取消")

                opts = {
                    "format": "bestaudio/best",
                    "noplaylist": True,
                    "outtmpl": str(directory / f"p{index}.%(ext)s"),
                    "progress_hooks": [progress],
                    "quiet": True,
                }
                from ..utils.ffmpeg_helper import FFmpegHelper

                opts["ffmpeg_location"] = FFmpegHelper.get_yt_dlp_ffmpeg_location()
                try:
                    filename = download_audio(
                        obj, index, directory, lambda: worker.is_task_cancelled(task_id)
                    )
                except TaskCancelledError:
                    raise
                except Exception:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        attach_cookies(ydl, payload.get("bilibili_sessdata", ""))
                        media = ydl.extract_info(part_url, download=True)
                        filename = ydl.prepare_filename(media)
                        requested = media.get("requested_downloads") or []
                        if requested and requested[0].get("filepath"):
                            filename = requested[0]["filepath"]
                store.artifact(task_id, filename, "download")
                worker._submit_coro(
                    update_and_notify(
                        task_id,
                        {
                            "status": "TRANSCRIBING",
                            "progress": number / len(indices) * 100,
                        },
                    )
                )
                transcript = runtime.transcribe(
                    filename, cancel_check=lambda: worker.is_task_cancelled(task_id)
                )
                segments = [
                    {
                        "start": s["start"],
                        "end": s.get("end", s["start"] + 1),
                        "text": s["text"],
                        "timing_inferred": False,
                    }
                    for s in transcript.segments
                ]
                source = "asr"
                duration = transcript.audio_duration or pages[index]["duration"]
            if not segments:
                raise ValueError(
                    f"P{index + 1} 没有可用文本，任务未完整完成；可重试补齐"
                )
            result = {"segments": segments, "source": source, "duration": duration}
            temp = checkpoint.with_suffix(".tmp")
            temp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            temp.replace(checkpoint)
            store.artifact(task_id, str(checkpoint), "part_checkpoint")
        for s in result["segments"]:
            all_segments.append(
                dict(
                    s,
                    start=s["start"] + offset,
                    end=s["end"] + offset,
                    source_start=s["start"],
                    source_end=s["end"],
                    part=index + 1,
                    source_url=f"https://www.bilibili.com/video/{bvid}/?p={index + 1}",
                    source=result["source"],
                )
            )
        source_map.append(
            {
                "start": offset,
                "end": offset + result["duration"],
                "part": index + 1,
                "url": f"https://www.bilibili.com/video/{bvid}/?p={index + 1}",
            }
        )
        coverage.append(
            {"part": index + 1, "source": result["source"], "complete": True}
        )
        offset += result["duration"]
        store.meta(
            task_id,
            {
                "coverage": coverage,
                "selected_parts": [i + 1 for i in indices],
                "complete": len(coverage) == len(indices),
                "source_map": source_map,
            },
        )
    if worker.is_task_cancelled(task_id):
        raise TaskCancelledError("任务已取消")
    transcript_path = directory / "transcript.txt"
    transcript_path.write_text(plain(all_segments), encoding="utf-8")
    store.segments(task_id, all_segments)
    worker._submit_coro(
        update_and_notify(
            task_id,
            {
                "title": info.get("title"),
                "author_name": info.get("owner", {}).get("name"),
                "author_url": f"https://space.bilibili.com/{info.get('owner', {}).get('mid', '')}",
                "transcript": plain(all_segments),
                "audio_duration": offset,
                "status": "SUMMARIZING",
                "progress": 0,
            },
        )
    )
    next_payload = dict(
        payload,
        intermediate_file_path=str(transcript_path),
        output_file=str(directory / "summary.md"),
    )
    worker._submit_coro(worker.summary_worker.add_task(next_payload))


def download_audio(video, index, directory, cancel_check):
    """Read Bilibili's authenticated playback metadata, then download only audio."""
    import requests
    from urllib.parse import urlsplit

    target = directory / f"p{index}.m4a"
    if target.is_file() and target.stat().st_size:
        return str(target)
    info = asyncio.run(asyncio.wait_for(video.get_download_url(page_index=index), 25))
    streams = (info.get("dash") or {}).get("audio") or []
    if not streams:
        raise ValueError("视频接口没有返回独立音频流，尝试兼容下载器")
    aac = [s for s in streams if "mp4a" in s.get("codecs", "")] or streams
    stream = max(aac, key=lambda s: s.get("bandwidth", 0))
    url = stream.get("baseUrl") or stream.get("base_url")
    if not url or urlsplit(url).scheme not in ("https", "http"):
        raise ValueError("音频地址无效")
    partial = target.with_suffix(".m4a.part")
    marker = target.with_suffix(".source.json")
    identity = urlsplit(url).path
    same = marker.exists() and marker.read_text(encoding="utf-8") == identity
    offset = partial.stat().st_size if same and partial.exists() else 0
    headers = {"Referer": "https://www.bilibili.com/", "User-Agent": "Mozilla/5.0"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    marker.write_text(identity, encoding="utf-8")
    # No login Cookie is forwarded to the CDN: playback URLs carry their own access data.
    with requests.get(url, headers=headers, timeout=(15, 30), stream=True) as response:
        response.raise_for_status()
        append = offset > 0 and response.status_code == 206
        if append and not response.headers.get("Content-Range", "").startswith(
            f"bytes {offset}-"
        ):
            raise ValueError("续传响应不匹配，已保留下载片段")
        written = 0
        with partial.open("ab" if append else "wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if cancel_check():
                    raise TaskCancelledError("任务已取消，音频下载可续传")
                if chunk:
                    output.write(chunk)
                    written += len(chunk)
        length = response.headers.get("Content-Length")
        if length and written != int(length):
            raise ValueError("音频未下载完整，请重试")
    if not partial.stat().st_size:
        raise ValueError("音频文件为空")
    partial.replace(target)
    return str(target)

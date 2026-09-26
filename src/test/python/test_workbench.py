from __future__ import annotations
import asyncio, json, os, tempfile, unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime
from fastapi.testclient import TestClient
from src.main.python.sheng_wen import api
from src.main.python.sheng_wen.db import TaskDB
from src.main.python.sheng_wen.config.settings import JSONConfigManager
from src.main.python.sheng_wen.workbench import store, connections
from src.main.python.sheng_wen.workbench.store import Store
from src.main.python.sheng_wen.workbench.connections import Connection, parse_cookies
from src.main.python.sheng_wen.workbench.runtime import LazyTranscriber
from src.main.python.sheng_wen.workbench.transcripts import (
    parse_subtitles,
    plain,
    export_subtitles,
)
from src.main.python.sheng_wen.workbench.service import export_note

SRT = "1\n00:00:01,000 --> 00:00:03,000\n缓存可以减少重复请求\n\n2\n00:00:05,000 --> 00:00:07,000\n过期后重新获取数据\n"


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_store = store._store
        self.old_connection = connections._connection
        store._store = Store(self.root / "workbench.sqlite")
        connections._connection = Connection(self.root / "connection.bin")
        self.db = TaskDB(
            sqlite_path=str(self.root / "tasks.sqlite"),
            file_path=str(self.root / "old.json"),
        )
        self.patches = [
            patch.object(api, "db", self.db),
            patch("src.main.python.sheng_wen.db.db", self.db),
        ]
        for p in self.patches:
            p.start()

        async def local_app(scope, receive, send):
            scope = dict(scope, client=("127.0.0.1", 50000))
            await api.app(scope, receive, send)

        self.client = TestClient(local_app, base_url="http://localhost")
        self.db.save_task(
            "sample",
            {
                "id": "sample",
                "video_url": "https://www.bilibili.com/video/BV123",
                "status": "COMPLETED",
                "created_at": datetime.utcnow(),
                "title": "缓存课程",
                "transcript": plain(parse_subtitles(SRT)),
                "summary": "原总结",
            },
        )

    def tearDown(self):
        self.client.close()
        for p in self.patches:
            p.stop()
        self.db.engine.dispose()
        store._store = self.old_store
        connections._connection = self.old_connection
        self.tmp.cleanup()

    def test_encrypted_credential_roundtrip_and_no_response_secret(self):
        value = "FAKE_SESSDATA_FOR_TEST_12345"
        response = self.client.post(
            "/workbench/connection/import",
            json={"value": "SESSDATA=" + value + "; bili_jct=fake"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(value, response.text)
        self.assertNotIn(value.encode(), connections._connection.path.read_bytes())
        self.assertEqual(
            Connection(connections._connection.path).cookies()["SESSDATA"], value
        )

    def test_clear_stops_environment_fallback(self):
        with patch.dict(os.environ, {"BILIBILI_SESSDATA": "ENV_TEST"}):
            self.assertTrue(connections._connection.cookies())
            self.client.delete("/workbench/connection")
            self.assertFalse(connections._connection.cookies())

    def test_cookie_formats_and_domain_scope(self):
        jar = "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tfake\n.evil.com\tTRUE\t/\tTRUE\t0\tbili_jct\tevil"
        self.assertEqual(parse_cookies(jar), {"SESSDATA": "fake"})
        with self.assertRaises(ValueError):
            parse_cookies("bad=value")

    def test_disallowed_browser_origin(self):
        r = self.client.post(
            "/workbench/connection/verify", headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(r.status_code, 403)

    def test_invalid_json_preserved(self):
        p = self.root / "config/settings.json"
        p.parent.mkdir()
        p.write_text("{BROKEN", encoding="utf-8")
        with self.assertRaises(ValueError):
            JSONConfigManager(str(p))
        self.assertEqual(p.read_text(), "{BROKEN")

    def test_durable_payload_excludes_secrets(self):
        store._store.record(
            "sample",
            "LLMWorker",
            {
                "task_id": "sample",
                "intermediate_file_path": "missing",
                "bilibili_sessdata": "hidden",
                "api_key": "hidden",
                "template": "meeting",
            },
        )
        job = Store(store._store.path).job("sample")
        self.assertEqual(job["stage"], "LLMWorker")
        self.assertNotIn("hidden", json.dumps(job))

    def test_pagination_omits_bodies_and_searches_full_text(self):
        for i in range(6):
            self.db.save_task(
                str(i),
                {
                    "id": str(i),
                    "video_url": "file://test.mp3",
                    "status": "COMPLETED",
                    "created_at": datetime.utcnow(),
                    "transcript": "distinct-keyword",
                    "summary": "x" * 10000,
                },
            )
        r = self.client.get(
            "/workbench/tasks",
            params={"q": "distinct-keyword", "limit": 2, "offset": 2},
        )
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["total"], 6)
        self.assertEqual(len(d["items"]), 2)
        self.assertNotIn("summary", d["items"][0])
        self.assertNotIn("transcript", d["items"][0])

    def test_subtitle_roundtrip(self):
        segments = parse_subtitles(SRT)
        self.assertEqual(parse_subtitles(export_subtitles(segments, "vtt")), segments)
        self.assertEqual(segments[0]["start"], 1)

    def test_edit_versions_and_stale_summary(self):
        r = self.client.put(
            "/workbench/tasks/sample/transcript", json={"text": SRT, "format": "srt"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        d = self.client.get("/workbench/tasks/sample/details").json()
        self.assertTrue(d["meta"]["summary_stale"])
        self.assertEqual(len(d["segments"]), 2)
        self.assertGreaterEqual(len(d["versions"]), 1)

    def test_edit_rejects_active_task(self):
        self.db.update_task("sample", {"status": "SUMMARIZING"})
        r = self.client.put(
            "/workbench/tasks/sample/transcript", json={"text": SRT, "format": "srt"}
        )
        self.assertEqual(r.status_code, 400)

    def test_cancel_preserves_transcript_and_blocks_late_results(self):
        store._store.record("sample", "LLMWorker", {"task_id": "sample"})
        self.db.update_task("sample", {"status": "SUMMARIZING"})
        with patch(
            "src.main.python.sheng_wen.workbench.service.active_workers",
            return_value=[],
        ):
            r = self.client.post("/workbench/tasks/sample/cancel")
        self.assertEqual(r.status_code, 200, r.text)
        from src.main.python.sheng_wen.task_updater import update_and_notify

        asyncio.run(
            update_and_notify("sample", {"status": "COMPLETED", "summary": "late"})
        )
        self.assertEqual(self.db.get_task("sample")["status"], "FAILED")
        self.assertEqual(self.db.get_task("sample")["summary"], "原总结")

    def test_export_never_overwrites_and_yaml_is_valid(self):
        import yaml

        t = self.db.get_task("sample")
        t["title"] = '特殊 "标题": 测试'
        a = export_note(t, str(self.root / "notes"))
        Path(a["file_path"]).write_text("MANUAL", encoding="utf-8")
        b = export_note(t, str(self.root / "notes"))
        self.assertNotEqual(a["file_path"], b["file_path"])
        self.assertEqual(Path(a["file_path"]).read_text(), "MANUAL")
        doc = Path(b["file_path"]).read_text(encoding="utf-8")
        self.assertEqual(yaml.safe_load(doc.split("---")[1])["title"], t["title"])

    def test_collection_filter(self):
        self.client.put(
            "/workbench/tasks/sample/meta", json={"collection": "课程A", "tags": "缓存"}
        )
        self.assertEqual(
            self.client.get("/workbench/tasks?collection=课程A").json()["total"], 1
        )

    def test_question_without_evidence_does_not_call_llm(self):
        with patch.object(api, "get_llm_worker", new_callable=AsyncMock) as mocked:
            r = self.client.post(
                "/workbench/tasks/sample/ask", json={"question": "恐龙为什么灭绝"}
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["sources"])
        mocked.assert_not_called()

    def test_answer_citation_validation(self):
        async def respond(messages, resp_callback, stream):
            resp_callback("有缓存 [片段999]")

        fake = MagicMock()
        fake._llm_client.response = respond
        with patch.object(
            api, "get_llm_worker", new_callable=AsyncMock, return_value=fake
        ):
            r = self.client.post(
                "/workbench/tasks/sample/ask", json={"question": "缓存为什么有用"}
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("无法生成引用可靠", r.json()["answer"])

    def test_lazy_model_no_load_until_transcribe_and_reuse(self):
        instance = MagicMock()
        instance.transcribe.return_value = "result"
        lazy = LazyTranscriber()
        with (
            patch.object(
                api.transcription_settings_manager,
                "build_transcriber_kwargs",
                return_value={
                    "model_size_or_path": "fake-local-model",
                    "device": "cpu",
                },
            ),
            patch(
                "src.main.python.sheng_wen.transcriber.transcriber.get_transcriber",
                return_value=instance,
            ) as create,
        ):
            create.assert_not_called()
            self.assertEqual(lazy.transcribe("fake"), "result")
            lazy.transcribe("fake2")
            create.assert_called_once()

    def test_factory_does_not_load_asr(self):
        from src.main.python.sheng_wen.transcriber.transcriber_worker import (
            TranscriberWorker,
        )

        with (
            patch.object(api, "downloader_worker", None),
            patch.object(api, "transcriber_worker", None),
            patch.object(
                api, "get_llm_worker", new_callable=AsyncMock, return_value=MagicMock()
            ),
            patch.object(TranscriberWorker, "start"),
            patch(
                "src.main.python.sheng_wen.downloader.video_downloader_worker.VideoDownloaderWorker.start"
            ),
            patch(
                "src.main.python.sheng_wen.transcriber.transcriber.get_transcriber"
            ) as create,
        ):
            worker = asyncio.run(api.get_downloader_worker())
            self.assertIsNotNone(worker)
            create.assert_not_called()

    def test_multipart_fills_missing_subtitle_with_asr(self):
        from types import SimpleNamespace
        from src.main.python.sheng_wen.workbench.bilibili import process_bilibili

        worker = MagicMock()
        worker._extract_bvid_from_url.return_value = "BV123"
        worker.is_task_cancelled.return_value = False
        worker.transcription_settings_manager.get_runtime_state.return_value = {
            "enable_bilibili_subtitle_fetch": True
        }
        worker._extract_bilibili_subtitle_via_api = AsyncMock(
            side_effect=[{"transcript": "000000第一部分", "duration": 30}, None]
        )
        worker._submit_coro = lambda coro: asyncio.run(coro)
        worker.summary_worker.add_task = AsyncMock()
        video = MagicMock()
        video.get_info = AsyncMock(
            return_value={
                "title": "课程",
                "pages": [{"duration": 30}, {"duration": 30}],
            }
        )
        media = self.root / "audio.mp3"
        media.write_bytes(b"fake")
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.extract_info.return_value = {}
        ydl.prepare_filename.return_value = str(media)
        old = os.getcwd()
        try:
            os.chdir(self.root)
            with (
                patch("bilibili_api.video.Video", return_value=video),
                patch("yt_dlp.YoutubeDL", return_value=ydl),
                patch(
                    "src.main.python.sheng_wen.workbench.bilibili.runtime.transcribe",
                    return_value=SimpleNamespace(
                        segments=[{"start": 0, "end": 5, "text": "第二部分"}],
                        audio_duration=30,
                    ),
                ) as asr,
            ):
                process_bilibili(
                    worker,
                    {
                        "task_id": "sample",
                        "video_url": "https://www.bilibili.com/video/BV123",
                        "bilibili_parts": {"indices": [0, 1]},
                    },
                )
            asr.assert_called_once()
            worker.summary_worker.add_task.assert_awaited_once()
            self.assertTrue(store._store.meta("sample")["complete"])
            segments = store._store.segments("sample")
            self.assertEqual(segments[-1]["part"], 2)
            self.assertEqual(segments[-1]["start"], 30)
            self.assertEqual(segments[-1]["source_start"], 0)
        finally:
            os.chdir(old)

    def test_summary_checkpoint_skips_finished_chunks(self):
        from src.main.python.sheng_wen.summarization.chunked_summarizer import (
            ChunkedSummarizer,
        )
        from src.main.python.sheng_wen.summarization.chunker import TranscriptChunk

        fake = MagicMock()
        fake.config = "test-model"

        def instance():
            return ChunkedSummarizer(
                fake,
                "prompt",
                60,
                30,
                90,
                10,
                2,
                30,
                1,
                500,
                checkpoint_path=str(self.root / "checkpoint.json"),
            )

        chunks = [
            TranscriptChunk(0, "000000first", 0, 30, 1),
            TranscriptChunk(1, "000030second", 30, 60, 1),
        ]
        first = instance()
        first._call_llm_with_retry = AsyncMock(
            side_effect=["第一块内容", RuntimeError("interrupted")]
        )
        with patch(
            "src.main.python.sheng_wen.summarization.chunked_summarizer.split_transcript_into_chunks",
            return_value=chunks,
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(first.summarize("input"))
            second = instance()
            second._call_llm_with_retry = AsyncMock(return_value="第二块内容")
            result = asyncio.run(second.summarize("input"))
        second._call_llm_with_retry.assert_awaited_once()
        self.assertEqual(result.chunk_done, 2)

    def test_retry_uses_existing_transcript_instead_of_asr(self):
        fake = MagicMock()
        fake.add_task = AsyncMock()
        self.db.update_task("sample", {"status": "FAILED"})
        old = os.getcwd()
        try:
            os.chdir(self.root)
            with (
                patch.object(
                    api, "get_llm_worker", new_callable=AsyncMock, return_value=fake
                ),
                patch.object(
                    api, "get_transcriber_worker", new_callable=AsyncMock
                ) as asr,
            ):
                r = self.client.post("/workbench/tasks/sample/retry")
            self.assertEqual(r.status_code, 200, r.text)
            asr.assert_not_called()
            fake.add_task.assert_awaited_once()
        finally:
            os.chdir(old)

    def test_import_generate_pipeline_preserves_template_and_versions(self):
        from src.main.python.sheng_wen.llm.llm_worker import LLMWorker
        from src.main.python.sheng_wen.llm.llm import LLMConfig

        seen = []

        class FakeLLM:
            config = LLMConfig(
                base_url="https://example.invalid", api_key="fake", model_id="fake"
            )

            async def response(self, messages, resp_callback, stream=True, timeout=60):
                seen.extend(messages)
                resp_callback("标题：验收笔记\n\n缓存减少重复请求。(见 00:00:01)")

        old = os.getcwd()
        try:
            os.chdir(self.root)
            result = self.client.post(
                "/workbench/tasks/import-subtitles", json={"text": SRT, "format": "srt"}
            )
            self.assertEqual(result.status_code, 200, result.text)
            tid = result.json()["id"]

            async def run():
                worker = LLMWorker("LLMWorker", FakeLLM())
                worker.system_prompt = "请忠实总结"
                with patch.object(
                    api, "get_llm_worker", new_callable=AsyncMock, return_value=worker
                ):
                    worker.start()
                    await api.re_summarize_task(
                        tid,
                        api.ReSummarizeRequest(
                            summary_mode="standard", template="meeting"
                        ),
                    )
                    for _ in range(200):
                        if self.db.get_task(tid)["status"] == "COMPLETED":
                            break
                        await asyncio.sleep(0.02)
                    await worker.stop()
                return self.db.get_task(tid)

            task = asyncio.run(run())
            self.assertEqual(task["status"], "COMPLETED")
            self.assertIn("负责人", seen[0].content)
            self.assertTrue(store._store.versions(tid))
            self.assertTrue(store._store.segments(tid))
        finally:
            os.chdir(old)

    def test_duplicate_preview_uses_video_identity_without_bodies(self):
        r = self.client.get(
            "/workbench/source-preview",
            params={"url": "https://www.bilibili.com/video/BV123/?tracking=test"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["duplicates"][0]["id"], "sample")
        self.assertNotIn("summary", r.json()["duplicates"][0])

    def test_download_uses_only_the_active_connection(self):
        from src.main.python.sheng_wen.workbench.connections import attach_cookies

        connections._connection.import_value(
            "SESSDATA=FAKE_TEST_ONLY; bili_jct=FAKE_CSRF"
        )
        ydl = MagicMock()
        attach_cookies(ydl)
        values = {
            call.args[0].name: call.args[0].value
            for call in ydl.cookiejar.set_cookie.call_args_list
        }
        self.assertEqual(values["SESSDATA"], "FAKE_TEST_ONLY")
        connections._connection.disconnect()
        ydl.cookiejar.set_cookie.reset_mock()
        attach_cookies(ydl)
        ydl.cookiejar.set_cookie.assert_not_called()

    def test_storage_stats_include_per_task_subdirectories(self):
        old = os.getcwd()
        try:
            os.chdir(self.root)
            directory = Path("temp/task-123")
            directory.mkdir(parents=True)
            (directory / "audio.mp3").write_bytes(b"1234")
            result = self.client.get("/temp/stats")
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["total_files"], 1)
        finally:
            os.chdir(old)

    def test_audio_uses_playback_stream_without_forwarding_login_cookie(self):
        from src.main.python.sheng_wen.workbench.bilibili import download_audio

        video = MagicMock()
        video.get_download_url = AsyncMock(
            return_value={
                "dash": {
                    "audio": [
                        {
                            "baseUrl": "https://cdn.example/audio.m4a",
                            "bandwidth": 192000,
                            "codecs": "mp4a",
                        }
                    ]
                }
            }
        )
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.headers = {"Content-Length": "4"}
        response.iter_content.return_value = [b"data"]
        with patch("requests.get", return_value=response) as request:
            result = download_audio(video, 0, self.root, lambda: False)
        self.assertEqual(Path(result).read_bytes(), b"data")
        self.assertNotIn("Cookie", request.call_args.kwargs["headers"])

    def test_author_uses_authenticated_video_metadata_first(self):
        from src.main.python.sheng_wen.downloader.bilibili_author_resolver import (
            resolve_bilibili_author,
        )

        video = MagicMock()
        video.get_info = AsyncMock(
            return_value={"owner": {"name": "Test Author", "mid": 123}}
        )
        with (
            patch("bilibili_api.video.Video", return_value=video),
            patch(
                "src.main.python.sheng_wen.downloader.bilibili_author_resolver._extract_bilibili_author"
            ) as legacy,
        ):
            result = asyncio.run(
                resolve_bilibili_author("https://www.bilibili.com/video/BV123")
            )
        self.assertEqual(result["author_name"], "Test Author")
        legacy.assert_not_called()


if __name__ == "__main__":
    unittest.main()

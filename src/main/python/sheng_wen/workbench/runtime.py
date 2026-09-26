from __future__ import annotations
import gc, threading


class LazyTranscriber:
    """The worker thread loads ASR only when speech recognition is needed."""

    def __init__(self):
        self.lock = threading.RLock()
        self.instance = None
        self.key = None
        self.state = {"phase": "idle", "model": None, "error": None}

    def transcribe(self, *args, **kwargs):
        from .. import api
        from ..transcriber.transcriber import get_transcriber

        with self.lock:
            config = api.transcription_settings_manager.build_transcriber_kwargs()
            if "model_size" in config:
                from .models import ROOT

                name = (
                    "large-v3"
                    if config["model_size"] == "large"
                    else config["model_size"]
                )
                path = ROOT / f"faster-whisper-{name}"
                if not (path / "model.bin").is_file():
                    from ..transcriber.transcriber import ModelLoadError

                    raise ModelLoadError(
                        "模型尚未下载，请在设置 → 语音识别与模型中下载或选择本地模型"
                    )
                config = dict(config, model_size_or_path=str(path))
                config.pop("model_size")
            key = tuple(sorted(config.items()))
            if key != self.key or self.instance is None:
                self.instance = None
                gc.collect()
                self.key = None
                self.state = {
                    "phase": "loading",
                    "model": config.get("model_size_or_path", config.get("model_size")),
                    "error": None,
                }
                try:
                    self.instance = get_transcriber("fast_whisper", **config)
                    self.key = key
                except Exception as exc:
                    self.state["phase"] = "failed"
                    self.state["error"] = str(exc)
                    raise
            self.state["phase"] = "transcribing"
            try:
                return self.instance.transcribe(*args, **kwargs)
            finally:
                self.state["phase"] = "ready"

    def release(self):
        if not self.lock.acquire(blocking=False):
            raise ValueError("模型正在使用，请等待当前转录完成")
        try:
            self.instance = None
            self.key = None
            gc.collect()
            self.state = {"phase": "idle", "model": None, "error": None}
        finally:
            self.lock.release()


runtime = LazyTranscriber()

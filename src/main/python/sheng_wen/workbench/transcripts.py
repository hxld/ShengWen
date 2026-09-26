from __future__ import annotations
import re, math
from collections import Counter

TEMPLATES = {
    "course": {
        "label": "课程笔记",
        "prompt": "形成可复习的课程笔记，保留概念、论证、示例和时间引用。不要编造原文未提供的事实。",
    },
    "meeting": {
        "label": "会议纪要",
        "prompt": "整理议题、已作决定、未决问题和行动项。负责人、截止时间只有原文明确提到才填写，否则写未明确。保留时间引用。",
    },
    "tutorial": {
        "label": "操作教程",
        "prompt": "整理前提条件、操作步骤、原理、注意事项和故障排查。不要补造命令或步骤，保留时间引用。",
    },
    "review": {
        "label": "复习卡片",
        "prompt": "生成问题—答案形式的复习卡片，每张卡只问一个知识点，附原文时间引用；无来源依据的内容不要补充。",
    },
}


def seconds(value):
    h, m, s = value.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_transcript(text):
    result = []
    for line in text.splitlines():
        m = re.match(r"^(\d{2})(\d{2})(\d{2})(.*)$", line)
        if m:
            result.append(
                {
                    "start": int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]),
                    "text": m[4].strip(),
                    "timing_inferred": True,
                }
            )
        elif line.strip():
            if result:
                result[-1]["text"] += " " + line.strip()
            else:
                result.append(
                    {"start": 0, "text": line.strip(), "timing_inferred": True}
                )
    for i, s in enumerate(result):
        s["end"] = max(
            s["start"] + 0.1,
            result[i + 1]["start"] if i + 1 < len(result) else s["start"] + 5,
        )
    return result


def parse_subtitles(text):
    text = text.replace("\r\n", "\n").lstrip("\ufeff")
    result = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.splitlines()
        for i, line in enumerate(lines):
            m = re.match(
                r"((?:\d+:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+((?:\d+:)?\d{2}:\d{2}[.,]\d{3})",
                line,
            )
            if not m:
                continue
            a, b = m.groups()
            a = a if a.count(":") == 2 else "00:" + a
            b = b if b.count(":") == 2 else "00:" + b
            start, end = seconds(a), seconds(b)
            if end <= start:
                raise ValueError("字幕结束时间必须晚于开始时间")
            content = re.sub("<[^>]+>", "", "\n".join(lines[i + 1 :])).strip()
            if content:
                result.append(
                    {
                        "start": start,
                        "end": end,
                        "text": content,
                        "timing_inferred": False,
                    }
                )
            break
    if not result:
        raise ValueError("没有找到有效 SRT/VTT 字幕段")
    return sorted(result, key=lambda s: s["start"])


def plain(segments):
    return "".join(
        f"{int(s['start']) // 3600:02d}{int(s['start']) // 60 % 60:02d}{int(s['start']) % 60:02d}{s['text'].replace(chr(10), ' ')}\n"
        for s in segments
    )


def stamp(t, sep=","):
    ms = round(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}{sep}{ms % 1000:03d}"


def export_subtitles(segments, fmt="srt"):
    if fmt not in ("srt", "vtt"):
        raise ValueError("仅支持 srt/vtt")
    return (
        ("WEBVTT\n\n" if fmt == "vtt" else "")
        + "\n\n".join(
            f"{i + 1}\n{stamp(s['start'], '.' if fmt == 'vtt' else ',')} --> {stamp(s['end'], '.' if fmt == 'vtt' else ',')}\n{s['text']}"
            for i, s in enumerate(segments)
        )
        + "\n"
    )


def terms(text):
    words = re.findall(r"[a-z0-9_]+", text.lower())
    chars = re.findall(r"[\u4e00-\u9fff]", text)
    return Counter(words + [chars[i] + chars[i + 1] for i in range(len(chars) - 1)])


def retrieve(segments, question, limit=8):
    query = terms(question)
    ranked = []
    for i, s in enumerate(segments):
        document = terms(s["text"])
        score = sum(min(n, document[t]) for t, n in query.items()) / math.sqrt(
            max(1, sum(document.values()))
        )
        if score > 0:
            ranked.append((score, i, s))
    return [
        dict(s, id=i + 1)
        for _, i, s in sorted(ranked, key=lambda x: (-x[0], x[1]))[:limit]
    ]

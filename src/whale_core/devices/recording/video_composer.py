"""FFmpeg 合成：原始录像 + 轨迹 → 最终 MP4（规划 §9 两个导出模板）。

- ``phone_only``：原始画面 + 底部步骤字幕条（当前步骤 label + 状态色块），
  片尾 1.5s Whale 字样；
- ``demo``：右侧 320px 步骤面板（当前步骤高亮 + 进度 + 上一步），
  开场 2s 标题卡片（叠加实现，不改变时间轴）；
- 输出统一 H.264（libx264）/ AAC（静音轨）MP4；
- 中文字体可配（``WHALE_FONT`` 环境变量或参数）；找不到 CJK 字体时
  降级为英文/ASCII label，绝不输出豆腐块。

scrcpy 源的 video.h264 是 raw Annex-B 流（无容器时间戳），输入侧用
``-fflags +genpts -r <实测 fps>`` 重建时间轴，时长误差在一帧以内。

为可测试性，命令拼装是纯函数 ``build_ffmpeg_command``，执行在 ``compose``。
"""

from __future__ import annotations

import asyncio
import json
import os
import re

from pathlib import Path

from .trace_align import AlignedEvent

PANEL_WIDTH = 320                     # demo 模板右侧步骤面板宽度
TITLE_CARD_S = 2.0                    # demo 模板标题卡片时长
END_CARD_S = 1.5                      # phone_only 片尾 Whale 字样时长

# 状态色块（与事件 result/verification 对应）
COLOR_OK = "0x3CB371"                 # 成功：绿
COLOR_FAIL = "0xE6473D"               # 失败：红
COLOR_NEUTRAL = "0x8A8F98"            # 其他：灰
COLOR_HIGHLIGHT = "0x2E6BE6"          # demo 当前步骤高亮：蓝

# 字体候选（按优先级）：文泉驿正黑 / Noto CJK / Unifont / DejaVu
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/unifont/unifont.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_CJK_FONT_HINTS = ("wqy", "noto", "cjk", "unifont", "zenhei", "uming", "ukai")


class ComposeError(Exception):
    """ffmpeg/ffprobe 执行失败。"""


# ---------------------------------------------------------------------------
# 字体与文本
# ---------------------------------------------------------------------------

def find_font(font_path: str | None = None) -> str | None:
    """按优先级找可用字体文件；找不到返回 None（drawtext 走 fontconfig 默认）。"""
    candidates = []
    if font_path:
        candidates.append(font_path)
    env_font = os.environ.get("WHALE_FONT")
    if env_font:
        candidates.append(env_font)
    candidates.extend(_FONT_CANDIDATES)
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def font_supports_cjk(font_path: str | None) -> bool:
    """按文件名启发式判断字体是否含 CJK 字形（不解析字体文件）。"""
    if not font_path:
        return False
    name = os.path.basename(font_path).lower()
    return any(hint in name for hint in _CJK_FONT_HINTS)


def to_display_text(text: str, *, cjk_ok: bool) -> str:
    """CJK 字体缺失时把非 ASCII 内容降级为英文占位，避免豆腐块。"""
    if cjk_ok or all(ord(c) < 128 for c in text):
        return text
    # 保留 ASCII 片段；纯中文时给英文占位
    ascii_part = re.sub(r"[^\x00-\x7f]+", " ", text).strip()
    return ascii_part if ascii_part else "(Chinese text)"


def escape_drawtext(text: str) -> str:
    """转义 drawtext text 值中的特殊字符（filtergraph + drawtext 两层）。

    反斜杠必须先转义；随后是 filtergraph 分隔符 ``':,;[]`` 与 drawtext
    的 ``%``（宏展开前缀）。换行替换为空格。
    """
    text = text.replace("\n", " ").replace("\r", " ")
    text = text.replace("\\", "\\\\")
    for ch in ("'", ":", "%", ",", ";", "[", "]"):
        text = text.replace(ch, "\\" + ch)
    return text


def event_label(event: dict, *, cjk_ok: bool, max_chars: int = 28) -> str:
    """从 RunTraceEvent 提取展示 label：优先 action.label，否则 task_summary/kind。"""
    action = event.get("action") or {}
    label = action.get("label") or event.get("task_summary") or event.get("kind") or ""
    label = to_display_text(str(label), cjk_ok=cjk_ok).strip()
    if len(label) > max_chars:
        label = label[: max_chars - 1] + "…" if cjk_ok else label[: max_chars - 3] + "..."
    return label


def event_color(event: dict) -> str:
    """状态色块：result/verification 含失败词 → 红；成功 → 绿；否则灰。"""
    result = str(event.get("result") or "").lower()
    verification = event.get("verification") or {}
    vstatus = str(verification.get("status") or "").lower()
    kind = str(event.get("kind") or "")
    if "fail" in result or vstatus == "failed" or kind in ("action_failed", "task_failed"):
        return COLOR_FAIL
    if result.startswith("succeed") or vstatus == "passed" or kind == "task_completed":
        return COLOR_OK
    return COLOR_NEUTRAL


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------

async def probe_video(
    video_path: Path | str, *, ffprobe_path: str = "ffprobe"
) -> dict:
    """读取视频 duration_s / width / height / fps（raw H.264 无 duration）。"""
    proc = await asyncio.create_subprocess_exec(
        ffprobe_path, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate:format=duration",
        "-of", "json", str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    if proc.returncode != 0:
        raise ComposeError(
            f"ffprobe 失败: {stderr.decode(errors='replace')[-300:]}"
        )
    data = json.loads(stdout.decode())
    stream = (data.get("streams") or [{}])[0]
    fps = 0.0
    rate = stream.get("r_frame_rate", "0/1")
    try:
        num, den = rate.split("/")
        fps = int(num) / max(1, int(den))
    except (ValueError, ZeroDivisionError):
        pass
    duration_s = 0.0
    try:
        duration_s = float(data.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        pass
    return {
        "duration_s": duration_s,
        "width": int(stream.get("width", 0)),
        "height": int(stream.get("height", 0)),
        "fps": fps,
    }


# ---------------------------------------------------------------------------
# filtergraph 拼装（纯函数）
# ---------------------------------------------------------------------------

def _event_windows(
    events: list[AlignedEvent], duration_s: float
) -> list[tuple[AlignedEvent, float, float]]:
    """每个事件的展示窗口：[offset, 下一事件 offset)，最后一个到片尾。"""
    windows = []
    for i, ev in enumerate(events):
        start = ev.video_offset_ms / 1000.0
        end = (
            events[i + 1].video_offset_ms / 1000.0
            if i + 1 < len(events) else duration_s
        )
        windows.append((ev, start, max(end, start)))
    return windows


def build_filter_phone_only(
    events: list[AlignedEvent],
    *,
    width: int,
    height: int,
    duration_s: float,
    font: str | None,
    cjk_ok: bool,
) -> str:
    """phone_only：底部字幕条 + 状态色块 + 片尾 Whale 字样。"""
    font_opt = f"fontfile='{escape_drawtext(font)}':" if font else ""
    filters = ["[0:v]format=yuv420p[base]"]
    last = "base"
    n = 0

    def chain(expr: str) -> None:
        nonlocal last, n
        filters.append(f"[{last}]{expr}[f{n}]")
        last = f"f{n}"
        n += 1

    # 底部字幕条背景（全程）
    chain("drawbox=x=0:y=ih-110:w=iw:h=110:color=black@0.55:t=fill")
    for ev, start, end in _event_windows(events, duration_s):
        label = escape_drawtext(event_label(ev.event, cjk_ok=cjk_ok))
        enable = f"enable='between(t,{start:.3f},{end:.3f})'"
        chain(
            f"drawbox=x=14:y=h-88:w=24:h=24:color={event_color(ev.event)}@0.95:t=fill:{enable}"
        )
        chain(
            f"drawtext={font_opt}text='{label}':x=52:y=h-92:fontsize=34:"
            f"fontcolor=white:{enable}"
        )
    # 片尾 Whale 字样
    tail_start = max(0.0, duration_s - END_CARD_S)
    chain(
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.85:t=fill:"
        f"enable='between(t,{tail_start:.3f},{duration_s:.3f})'"
    )
    chain(
        f"drawtext={font_opt}text='Whale':x=(w-text_w)/2:y=(h-text_h)/2:"
        f"fontsize=72:fontcolor=white:"
        f"enable='between(t,{tail_start:.3f},{duration_s:.3f})'"
    )
    filters.append(f"[{last}]copy[vout]")  # 收尾统一出口标签
    return ";".join(filters)


def build_filter_demo(
    events: list[AlignedEvent],
    *,
    width: int,
    height: int,
    duration_s: float,
    font: str | None,
    cjk_ok: bool,
    title: str,
) -> str:
    """demo：右侧 320px 步骤面板（当前步骤高亮 + 进度）+ 开场 2s 标题卡片。"""
    font_opt = f"fontfile='{escape_drawtext(font)}':" if font else ""
    total = len(events)
    panel_x = width
    filters = [
        f"[0:v]pad=w={width + PANEL_WIDTH}:h={height}:x=0:y=0:color=0x101418,"
        "format=yuv420p[base]"
    ]
    last = "base"
    n = 0

    def chain(expr: str) -> None:
        nonlocal last, n
        filters.append(f"[{last}]{expr}[f{n}]")
        last = f"f{n}"
        n += 1

    # 面板标题（全程）
    chain(
        f"drawtext={font_opt}text='Whale Agent':x={panel_x + 24}:y=36:"
        "fontsize=30:fontcolor=white"
    )
    for i, (ev, start, end) in enumerate(_event_windows(events, duration_s)):
        enable = f"enable='between(t,{start:.3f},{end:.3f})'"
        label = escape_drawtext(event_label(ev.event, cjk_ok=cjk_ok, max_chars=16))
        # 当前步骤高亮底 + label
        chain(
            f"drawbox=x={panel_x + 14}:y=130:w={PANEL_WIDTH - 28}:h=64:"
            f"color={COLOR_HIGHLIGHT}@0.9:t=fill:{enable}"
        )
        chain(
            f"drawtext={font_opt}text='{label}':x={panel_x + 26}:y=148:"
            f"fontsize=26:fontcolor=white:{enable}"
        )
        # 进度 k/N
        progress = escape_drawtext(
            to_display_text(f"步骤 {i + 1}/{total}", cjk_ok=cjk_ok)
        )
        chain(
            f"drawtext={font_opt}text='{progress}':x={panel_x + 24}:y=86:"
            f"fontsize=24:fontcolor=0x9AA0A6:{enable}"
        )
        # 上一步（灰）
        if i > 0:
            prev = escape_drawtext(
                event_label(events[i - 1].event, cjk_ok=cjk_ok, max_chars=16)
            )
            chain(
                f"drawtext={font_opt}text='{prev}':x={panel_x + 26}:y=220:"
                f"fontsize=22:fontcolor=0x9AA0A6:{enable}"
            )
    # 开场标题卡片（叠加，不改变时间轴）
    card_end = min(TITLE_CARD_S, duration_s)
    chain(
        f"drawbox=x=0:y=0:w=iw:h=ih:color=0x101418@1.0:t=fill:"
        f"enable='lt(t,{card_end:.3f})'"
    )
    safe_title = escape_drawtext(to_display_text(title, cjk_ok=cjk_ok))
    chain(
        f"drawtext={font_opt}text='{safe_title}':x=(w-text_w)/2:y=(h-text_h)/2:"
        f"fontsize=48:fontcolor=white:enable='lt(t,{card_end:.3f})'"
    )
    filters.append(f"[{last}]copy[vout]")
    return ";".join(filters)


# ---------------------------------------------------------------------------
# 命令拼装与执行
# ---------------------------------------------------------------------------

def build_ffmpeg_command(
    *,
    video_path: Path | str,
    out_path: Path | str,
    filtergraph: str,
    input_fps: float,
    ffmpeg_path: str = "ffmpeg",
) -> list[str]:
    """拼 ffmpeg argv（纯函数，便于测试）。raw .h264 用 genpts+fps 重建时间轴。"""
    input_opts: list[str] = []
    if str(video_path).endswith(".h264"):
        # raw H.264 无容器时间戳：按实测 fps 重建（保留小数，四舍五入到 int
        # 会把变帧率设备的导出时长拉偏）
        fps = input_fps if input_fps > 0 else 30.0
        input_opts = ["-fflags", "+genpts", "-r", f"{fps:.3f}"]
    return [
        ffmpeg_path, "-y",
        *input_opts, "-i", str(video_path),
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-filter_complex", filtergraph,
        "-map", "[vout]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]


async def compose(
    *,
    video_path: Path | str,
    events: list[AlignedEvent],
    profile: str,
    out_path: Path | str,
    title: str,
    measured_fps: float = 0.0,
    duration_ms: int = 0,
    width: int = 0,
    height: int = 0,
    font_path: str | None = None,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    timeout_s: float = 600.0,
) -> Path:
    """执行合成，返回导出路径；失败抛 ComposeError。"""
    if profile not in ("phone_only", "demo"):
        raise ComposeError(f"未知导出模板: {profile!r}")
    probe = await probe_video(video_path, ffprobe_path=ffprobe_path)
    width = width or probe["width"]
    height = height or probe["height"]
    fps = measured_fps or probe["fps"] or 30.0
    duration_s = duration_ms / 1000.0 if duration_ms else probe["duration_s"]
    if not width or not height or duration_s <= 0:
        raise ComposeError(
            f"无法确定视频参数: {width}x{height}, {duration_s:.3f}s"
        )
    font = find_font(font_path)
    cjk_ok = font_supports_cjk(font)
    if profile == "phone_only":
        filtergraph = build_filter_phone_only(
            events, width=width, height=height, duration_s=duration_s,
            font=font, cjk_ok=cjk_ok,
        )
    else:
        filtergraph = build_filter_demo(
            events, width=width, height=height, duration_s=duration_s,
            font=font, cjk_ok=cjk_ok, title=title,
        )
    argv = build_ffmpeg_command(
        video_path=video_path, out_path=out_path,
        filtergraph=filtergraph, input_fps=fps, ffmpeg_path=ffmpeg_path,
    )
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    if proc.returncode != 0:
        raise ComposeError(
            "ffmpeg 合成失败: " + stderr.decode(errors="replace")[-800:]
        )
    return Path(out_path)

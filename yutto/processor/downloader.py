from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import aiohttp

from yutto._typing import AudioUrlMeta, DownloaderOptions, EpisodeData, VideoUrlMeta
from yutto.bilibili_typing.quality import audio_quality_map, video_quality_map
from yutto.processor.progressbar import show_progress
from yutto.processor.selector import select_audio, select_video
from yutto.utils.console.colorful import colored_string
from yutto.utils.console.logger import Badge, Logger
from yutto.utils.danmaku import write_danmaku
from yutto.utils.fetcher import Fetcher
from yutto.utils.ffmpeg import FFmpeg
from yutto.utils.file_buffer import AsyncFileBuffer
from yutto.utils.funcutils import filter_none_value, xmerge
from yutto.utils.metadata import write_metadata
from yutto.utils.subtitle import write_subtitle


def slice_blocks(start: int, total_size: int | None, block_size: int | None = None) -> list[tuple[int, int | None]]:
    """生成分块后的 (start, size) 序列

    ### Args

    - start (int): 总起始位置
    - total_size (Optional[int]): 需要分块的总大小
    - block_size (Optional[int], optional): 每块的大小. Defaults to None.

    ### Returns

    - list[tuple[int, Optional[int]]]: 分块大小序列，使用元组组织，格式为 (start, size)
    """
    if total_size is None:
        return [(0, None)]
    if block_size is None:
        return [(0, total_size - 1)]
    assert start <= total_size, f"起始地址（{start}）大于总地址（{total_size}）"
    offset_list: list[tuple[int, int | None]] = [(i, block_size) for i in range(start, total_size, block_size)]
    if (total_size - start) % block_size != 0:
        offset_list[-1] = (
            start + (total_size - start) // block_size * block_size,
            total_size - start - (total_size - start) // block_size * block_size,
        )
    return offset_list


def show_videos_info(videos: list[VideoUrlMeta], selected: int):
    """显示视频详细信息"""
    if not videos:
        Logger.info("不包含任何视频流")
        return
    Logger.info(f"共包含以下 {len(videos)} 个视频流：")
    for i, video in enumerate(videos):
        log = "{}{:2} [{:^4}] [{:>4}x{:<4}] <{:^8}> #{}".format(
            "*" if i == selected else " ",
            i,
            video["codec"].upper(),
            video["width"],
            video["height"],
            video_quality_map[video["quality"]]["description"],
            len(video["mirrors"]) + 1,
        )
        if i == selected:
            log = colored_string(log, fore="blue")
        Logger.info(log)


def show_audios_info(audios: list[AudioUrlMeta], selected: int):
    """显示音频详细信息"""
    if not audios:
        Logger.info("不包含任何音频流")
        return
    Logger.info(f"共包含以下 {len(audios)} 个音频流：")
    for i, audio in enumerate(audios):
        log = "{}{:2} [{:^4}] <{:^8}>".format(
            "*" if i == selected else " ", i, audio["codec"].upper(), audio_quality_map[audio["quality"]]["description"]
        )
        if i == selected:
            log = colored_string(log, fore="magenta")
        Logger.info(log)


async def download_video_and_audio(
    session: aiohttp.ClientSession,
    video: VideoUrlMeta | None,
    video_path: Path,
    audio: AudioUrlMeta | None,
    audio_path: Path,
    options: DownloaderOptions,
):
    """下载音视频"""

    buffers: list[AsyncFileBuffer | None] = [None, None]
    sizes: list[int | None] = [None, None]
    coroutines_list: list[list[Coroutine[Any, Any, None]]] = []
    Fetcher.set_semaphore(options["num_workers"])
    if video is not None:
        vbuf = await AsyncFileBuffer(video_path, overwrite=options["overwrite"])
        vsize = await Fetcher.get_size(session, video["url"])
        video_coroutines = [
            Fetcher.download_file_with_offset(session, video["url"], video["mirrors"], vbuf, offset, block_size)
            for offset, block_size in slice_blocks(vbuf.written_size, vsize, options["block_size"])
        ]
        coroutines_list.append(video_coroutines)
        buffers[0], sizes[0] = vbuf, vsize

    if audio is not None:
        abuf = await AsyncFileBuffer(audio_path, overwrite=options["overwrite"])
        asize = await Fetcher.get_size(session, audio["url"])
        audio_coroutines = [
            Fetcher.download_file_with_offset(session, audio["url"], audio["mirrors"], abuf, offset, block_size)
            for offset, block_size in slice_blocks(abuf.written_size, asize, options["block_size"])
        ]
        coroutines_list.append(audio_coroutines)
        buffers[1], sizes[1] = abuf, asize

    # 为保证音频流和视频流尽可能并行，因此将两者混合一下～
    coroutines = list(xmerge(*coroutines_list))
    coroutines.insert(0, show_progress(list(filter_none_value(buffers)), sum(filter_none_value(sizes))))
    Logger.info("开始下载……")
    await asyncio.gather(*coroutines)
    Logger.info("下载完成！")

    for buffer in buffers:
        if buffer is not None:
            await buffer.close()


def merge_video_and_audio(
    video: VideoUrlMeta | None,
    video_path: Path,
    audio: AudioUrlMeta | None,
    audio_path: Path,
    output_path: Path,
    options: DownloaderOptions,
):
    """合并音视频"""

    ffmpeg = FFmpeg()
    Logger.info("开始合并……")

    # Using FFmpeg to Create HEVC Videos That Work on Apple Devices：
    # https://aaron.cc/ffmpeg-hevc-apple-devices/
    # see also: https://github.com/yutto-dev/yutto/issues/85
    vtag: str | None = None
    if options["video_save_codec"] == "hevc" or (
        options["video_save_codec"] == "copy" and video is not None and video["codec"] == "hevc"
    ):
        vtag = "hvc1"

    if video is not None and video["codec"] == options["video_save_codec"]:
        options["video_save_codec"] = "copy"
    if audio is not None and audio["codec"] == options["audio_save_codec"]:
        options["audio_save_codec"] = "copy"

    args_list: list[list[str]] = [
        ["-i", str(video_path)] if video is not None else [],
        ["-i", str(audio_path)] if audio is not None else [],
        ["-vcodec", options["video_save_codec"]] if video is not None else [],
        ["-acodec", options["audio_save_codec"]] if audio is not None else [],
        # see also: https://www.reddit.com/r/ffmpeg/comments/qe7oq1/comment/hi0bmic/?utm_source=share&utm_medium=web2x&context=3
        ["-strict", "unofficial"],
        ["-tag:v", vtag] if vtag is not None else [],
        ["-threads", str(os.cpu_count())],
        ["-y", str(output_path)],
    ]

    ffmpeg.exec(functools.reduce(lambda prev, cur: prev + cur, args_list))
    Logger.info("合并完成！")

    if video is not None:
        video_path.unlink()
    if audio is not None:
        audio_path.unlink()


async def start_downloader(
    session: aiohttp.ClientSession,
    episode_data: EpisodeData,
    options: DownloaderOptions,
):
    """处理单个视频下载任务，包含弹幕、字幕的存储"""

    videos = episode_data["videos"]
    audios = episode_data["audios"]
    subtitles = episode_data["subtitles"]
    danmaku = episode_data["danmaku"]
    metadata = episode_data["metadata"]
    output_dir = Path(episode_data["output_dir"])
    tmp_dir = Path(episode_data["tmp_dir"])
    filename = episode_data["filename"]
    require_video = options["require_video"]
    require_audio = options["require_audio"]

    Logger.info(f"开始处理视频 {filename}")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    video_path = tmp_dir.joinpath(filename + "_video.m4s")
    audio_path = tmp_dir.joinpath(filename + "_audio.m4s")

    video = select_video(videos, options["video_quality"], options["video_download_codec"])
    audio = select_audio(audios, options["audio_quality"], options["audio_download_codec"])
    will_download_video = video is not None and require_video
    will_download_audio = audio is not None and require_audio

    # 显示音视频详细信息
    show_videos_info(
        videos, videos.index(video) if will_download_video else -1  # pyright: ignore [reportGeneralTypeIssues]
    )
    show_audios_info(
        audios, audios.index(audio) if will_download_audio else -1  # pyright: ignore [reportGeneralTypeIssues]
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    output_format = ".mp4"
    if not will_download_video:
        if options["output_format_audio_only"] != "infer":
            output_format = "." + options["output_format_audio_only"]
        elif will_download_audio and audio["codec"] == "fLaC":  # pyright: ignore [reportOptionalSubscript]
            output_format = ".flac"
        else:
            output_format = ".aac"
    else:
        if options["output_format"] != "infer":
            output_format = "." + options["output_format"]
        elif will_download_audio and audio["codec"] == "fLaC":  # pyright: ignore [reportOptionalSubscript]
            output_format = ".mkv"  # MP4 does not support FLAC audio

    output_path = output_dir.joinpath(filename + output_format)

    # 保存字幕
    if subtitles:
        for subtitle in subtitles:
            write_subtitle(subtitle["lines"], output_path, subtitle["lang"])
        Logger.custom(
            "{} 字幕已全部生成".format(", ".join([subtitle["lang"] for subtitle in subtitles])),
            badge=Badge("字幕", fore="black", back="cyan"),
        )

    # 保存弹幕
    if danmaku["data"]:
        write_danmaku(
            danmaku,
            str(output_path),
            video["height"] if video is not None else 1080,  # 未下载视频时自动按照 1920x1080 处理
            video["width"] if video is not None else 1920,
        )
        Logger.custom("{} 弹幕已生成".format(danmaku["save_type"]).upper(), badge=Badge("弹幕", fore="black", back="cyan"))

    # 保存媒体描述文件
    if metadata is not None:
        write_metadata(metadata, output_path)
        Logger.custom("NFO 媒体描述文件已生成", badge=Badge("描述文件", fore="black", back="cyan"))

    if output_path.exists():
        if not options["overwrite"]:
            Logger.info(f"文件 {filename} 已存在")
            return
        else:
            Logger.info("文件已存在，因启用 overwrite 选项强制删除……")
            output_path.unlink()

    if not (will_download_audio or will_download_video):
        Logger.warning("没有音视频需要下载")
        return

    video = video if will_download_video else None
    audio = audio if will_download_audio else None

    # 下载视频 / 音频
    await download_video_and_audio(session, video, video_path, audio, audio_path, options)

    # 合并视频 / 音频
    merge_video_and_audio(video, video_path, audio, audio_path, output_path, options)

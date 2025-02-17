from __future__ import annotations

import argparse
import re
from collections.abc import Coroutine
from typing import Any

import aiohttp

from yutto._typing import EpisodeData, MId
from yutto.api.space import get_user_name, get_user_space_all_videos_avids
from yutto.api.ugc_video import UgcVideoListItem, get_ugc_video_list
from yutto.exceptions import NotFoundError
from yutto.extractor._abc import BatchExtractor
from yutto.extractor.common import extract_ugc_video_data
from yutto.utils.console.logger import Badge, Logger
from yutto.utils.fetcher import Fetcher


class UserAllUgcVideosExtractor(BatchExtractor):
    """UP 主个人空间全部投稿视频"""

    REGEX_SPACE = re.compile(r"https?://space\.bilibili\.com/(?P<mid>\d+)(/video)?")

    mid: MId

    def match(self, url: str) -> bool:
        if match_obj := self.REGEX_SPACE.match(url):
            self.mid = MId(match_obj.group("mid"))
            return True
        else:
            return False

    async def extract(
        self, session: aiohttp.ClientSession, args: argparse.Namespace
    ) -> list[Coroutine[Any, Any, EpisodeData | None] | None]:
        username = await get_user_name(session, self.mid)
        Logger.custom(username, Badge("UP 主投稿视频", fore="black", back="cyan"))

        ugc_video_info_list: list[tuple[UgcVideoListItem, str, str]] = []
        for avid in await get_user_space_all_videos_avids(session, self.mid):
            try:
                ugc_video_list = await get_ugc_video_list(session, avid)
                await Fetcher.touch_url(session, avid.to_url())
                for ugc_video_item in ugc_video_list["pages"]:
                    ugc_video_info_list.append(
                        (
                            ugc_video_item,
                            ugc_video_list["title"],
                            ugc_video_list["pubdate"],
                        )
                    )
            except NotFoundError as e:
                Logger.error(e.message)
                continue

        return [
            extract_ugc_video_data(
                session,
                ugc_video_item["avid"],
                ugc_video_item,
                args,
                {
                    "title": title,
                    "username": username,
                    "pubdate": pubdate,
                },
                "{username}的全部投稿视频/{title}/{name}",
            )
            for ugc_video_item, title, pubdate in ugc_video_info_list
        ]

"""B 站视频 / 番剧 / 直播 / 专栏 / 动态解析核心。

移植自 mengshouer/nonebot_plugin_analysis_bilibili（MIT），
将 NoneBot 的配置读取与消息发送替换为可注入的配置快照和 ``aiohttp`` 会话。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from time import localtime, strftime
from typing import Any

from aiohttp import ClientSession, ClientTimeout
from yarl import URL

from .cache import ExpiringCache
from .sign import get_query, get_ticket

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0"
}

B23_REGEX = re.compile(r"b23\.tv/(\w+)|(bili(22|23|33|2233)\.cn)/(\w+)", re.I)
PAGE_REGEX = re.compile(r"([?&]|&amp;)p=\d+")
TIME_REGEX = re.compile(r"([?&]|&amp;)t=\d+")
AID_REGEX = re.compile(r"(?<![a-zA-Z0-9])av\d+(?![a-zA-Z0-9])")
BVID_REGEX = re.compile(r"(?<![a-zA-Z0-9])BV[A-Za-z0-9]{10}(?![A-Za-z0-9])")
EPID_REGEX = re.compile(r"(?<![a-zA-Z0-9])ep\d+(?![a-zA-Z0-9])")
SSID_REGEX = re.compile(r"(?<![a-zA-Z0-9])ss\d+(?![a-zA-Z0-9])")
MDID_REGEX = re.compile(r"(?<![a-zA-Z0-9])md\d+(?![a-zA-Z0-9])")
ROOM_ID_REGEX = re.compile(r"live\.bilibili\.com/(blanc/|h5/)?(\d+)")
CVID_REGEX = re.compile(
    r"(/read/(cv|mobile|native)(/|\?id=)?|(?<![a-zA-Z0-9])cv)(\d+)(?![a-zA-Z0-9])"
)
DYNAMIC_TYPE2_REGEX = re.compile(r"(t|m)\.bilibili\.com/(\d+)\?(.*?)(&|&amp;)type=2")
DYNAMIC_ID_REGEX = re.compile(r"(t|m)\.bilibili\.com/(opus/)?(\d+)")
OPUS_ID_REGEX = re.compile(r"bilibili\.com/opus/(\d+)")
DYNAMIC_MOBILE_REGEX = re.compile(r"m\.bilibili\.com/dynamic/(\d+)")

RISK_CODES = {-352, -412, -509, -799}

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".jfif", ".webp")


def is_image_url(text: str) -> bool:
    """判断一个字符串是否是可以直接发送的网络图片。"""
    if not isinstance(text, str):
        return False
    if not text.startswith(("http://", "https://")):
        return False
    path = text.split("?", 1)[0].split("#", 1)[0]
    return path.lower().endswith(IMAGE_EXTENSIONS)


def parse_cookie(cookie: str) -> dict[str, str]:
    """把 ``a=1; b=2`` 形式的 cookie 字符串解析为字典。"""
    result: dict[str, str] = {}
    for item in cookie.split(";"):
        if "=" not in item:
            continue
        key, value = item.strip().split("=", 1)
        if key:
            result[key] = value
    return result


# 处理超过一万的数字
def handle_num(num: int) -> int | str:
    if num >= 10000:
        num = f"{num / 10000:.2f}万"
    return num


@dataclass
class BiliSettings:
    """一次解析所使用的配置快照。"""

    display_image: bool = True
    display_image_list: tuple[str, ...] = ()
    images_size: str = ""
    """图片大小，如 ``100h`` / ``100w`` / ``100h_100w``。"""
    cover_images_size: str = ""
    """封面图大小，留空则回退到 ``images_size``。"""
    reanalysis_time: int = 0
    """同一链接在多少秒内不重复解析，0 表示只记录最近一个。"""
    cookie: str = ""
    trust_env: bool = False
    timeout: float = 15.0

    def want_image(self, kind: str) -> bool:
        """判断某类内容是否需要附带封面/图片。"""
        if self.display_image:
            return True
        return kind in self.display_image_list


class BiliClient:
    """封装 B 站接口请求与解析逻辑。"""

    def __init__(
        self,
        settings_provider: Callable[[], BiliSettings],
        logger: Any,
    ) -> None:
        self._get_settings = settings_provider
        self._logger = logger
        self._session: ClientSession | None = None
        self._cached_spi_cookies: dict[str, str] = {}
        # analysis_stat: {session_key: ExpiringCache}
        self._stat: dict[str, ExpiringCache] = {}

    # ------------------------------------------------------------------ #
    # 会话与 cookies
    # ------------------------------------------------------------------ #
    async def _ensure_session(self) -> ClientSession:
        settings = self._get_settings()
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                trust_env=settings.trust_env,
                headers=HEADERS,
                timeout=ClientTimeout(total=settings.timeout),
            )
        return self._session

    async def close(self) -> None:
        """关闭底层会话。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _init_cookie(
        self, session: ClientSession, use_config: bool = True
    ) -> None:
        """初始化 cookies。失败时只记录日志，不影响后续请求。"""
        try:
            await self._init_cookie_inner(session, use_config)
        except Exception as e:
            self._logger.warning(f"analysis_bilibili: 初始化 cookie 失败 {e!r}")

    async def _init_cookie_inner(
        self, session: ClientSession, use_config: bool
    ) -> None:
        settings = self._get_settings()
        config_cookies = parse_cookie(settings.cookie) if use_config else {}
        current_cookies = session.cookie_jar.filter_cookies(
            URL("https://api.bilibili.com/")
        )

        cookies: dict[str, str] = {}
        if not current_cookies.get("buvid3") and "buvid3" not in config_cookies:
            if self._cached_spi_cookies.get("buvid3"):
                cookies = dict(self._cached_spi_cookies)
            else:
                try:
                    async with session.get(
                        "https://api.bilibili.com/x/frontend/finger/spi"
                    ) as resp:
                        spi = (await resp.json(content_type=None)).get("data") or {}
                    cookies = {
                        "buvid3": spi.get("b_3", ""),
                        "buvid4": spi.get("b_4", ""),
                    }
                    cookies = {k: v for k, v in cookies.items() if v}
                    if cookies.get("buvid3"):
                        self._cached_spi_cookies.update(cookies)
                except Exception as e:
                    self._logger.warning(f"analysis_bilibili: 获取 buvid 失败 {e!r}")

        if config_cookies:
            cookies.update(config_cookies)

        if cookies:
            if any(
                k not in current_cookies or current_cookies[k].value != v
                for k, v in cookies.items()
            ):
                if current_cookies:
                    for k, v in current_cookies.items():
                        if k not in cookies:
                            cookies[k] = v.value
                    session.cookie_jar.clear()
                session.cookie_jar.update_cookies(cookies)

    async def _fetch_api(
        self,
        session: ClientSession,
        url: str,
        retry: int = 2,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        for index in range(retry):
            # 首次尝试允许使用配置的 cookie；重试时只用匿名指纹，避免沿用已过期的配置
            await self._init_cookie(session, use_config=index == 0)
            try:
                async with session.get(url, headers=headers) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = None
                    if isinstance(data, dict):
                        inner = data.get("data")
                        has_voucher = isinstance(inner, dict) and "v_voucher" in inner
                        if data.get("code") not in RISK_CODES and not has_voucher:
                            return data
                        code = "v_voucher" if has_voucher else data.get("code")
                    else:
                        code = "非 JSON"
                    self._logger.warning(
                        f"analysis_bilibili: 接口风控 (HTTP {resp.status} / code {code}) {url}"
                    )
            except Exception as e:
                self._logger.warning(f"analysis_bilibili: 请求失败 {e!r} {url}")
            if index < retry - 1:
                # 清空 cookie 后重新获取，避免沿用已被风控的指纹
                self._cached_spi_cookies.clear()
                try:
                    ticket = session.cookie_jar.filter_cookies(
                        URL("https://api.bilibili.com/")
                    ).get("bili_ticket")
                    session.cookie_jar.clear()
                    if ticket and ticket.value:
                        session.cookie_jar.update_cookies({"bili_ticket": ticket.value})
                except Exception as e:
                    self._logger.warning(f"analysis_bilibili: 重置 cookie 失败 {e!r}")
                await asyncio.sleep(1)
        return None

    def _resize_image(self, src: str, is_cover: bool = False) -> str:
        """按配置给图片 URL 追加 B 站 CDN 的缩放参数。"""
        settings = self._get_settings()
        if not src:
            return ""
        img_type = src.rsplit(".", 1)[-1] if "." in src else src[-3:]
        if settings.cover_images_size and is_cover:
            return f"{src}@{settings.cover_images_size}.{img_type}"
        if settings.images_size:
            return f"{src}@{settings.images_size}.{img_type}"
        return src

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #
    async def parse(
        self,
        text: str,
        *,
        search: bool = False,
        session_key: str | None = None,
        title_hint: str | None = None,
    ) -> list | str | bool | None:
        """解析一段文本中的 B 站链接。

        Args:
            text: 待解析文本（可以直接包含分享文案）。
            search: 为 ``True`` 时把 ``text`` 当作标题进行搜索。
            session_key: 去重用的会话标识，通常是群号或 unified_msg_origin。
            title_hint: 无链接时的搜索标题提示（如 QQ 小程序卡片标题）。
        """
        if not text:
            return None

        session = await self._ensure_session()
        if search:
            found = await self.search_bili_by_title(text, session=session)
            if not found:
                return None
            text = found
            title_hint = None
        elif re.search(r"(b23\.tv)|(bili(22|23|33|2233)\.cn)", text, re.I):
            # 提前处理短链接，避免解析到其他的
            text = await self.b23_extract(text, session=session)

        return await self.bili_keyword(
            session_key, text, session=session, title_hint=title_hint
        )

    async def bili_keyword(
        self,
        session_key: str | None,
        text: str,
        session: ClientSession,
        title_hint: str | None = None,
    ) -> list | str | bool | None:
        settings = self._get_settings()
        try:
            if not text:
                return None
            # 提取 url
            url, page, time_location = extract(text)
            # 如果是小程序就去搜索标题
            if not url and title_hint:
                vurl = await self.search_bili_by_title(title_hint, session=session)
                if vurl:
                    url, page, time_location = extract(vurl)

            # 获取视频详细信息
            msg: Any = ""
            vurl = ""
            if "view?" in url:
                msg, vurl = await self.video_detail(
                    url, session, page=page, time_location=time_location
                )
            elif "pgc" in url:
                msg, vurl = await self.bangumi_detail(url, time_location, session)
            elif "xlive" in url:
                msg, vurl = await self.live_detail(url, session)
            elif "article" in url:
                msg, vurl = await self.article_detail(url, page, session)
            elif "dynamic" in url:
                # 新版动态的正文只存在于 opus 数据里，优先用 opus 接口解析
                if dynamic_match := re.search(r"detail\?id=(\d+)", url):
                    msg, vurl = await self.opus_detail(
                        "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail"
                        f"?id={dynamic_match[1]}",
                        session,
                    )
                if not msg:
                    detail_url = url.replace("/opus/detail", "/detail")
                    msg, vurl = await self.dynamic_detail(detail_url, session)

            # 避免多个机器人解析重复推送
            if session_key and vurl:
                cache = self._stat.setdefault(
                    session_key, ExpiringCache(expire_seconds=settings.reanalysis_time)
                )
                if cache.get(vurl):
                    return False
                cache.set(vurl)
        except Exception as e:
            msg = f"bili_keyword Error: {type(e)}"
        return msg

    async def b23_extract(self, text: str, session: ClientSession) -> str:
        """把 b23.tv 等短链还原为真实链接。"""
        b23 = B23_REGEX.search(text.replace("\\", ""))
        if not b23:
            return text
        url = f"https://{b23[0]}"
        try:
            async with session.get(url) as resp:
                return str(resp.url)
        except Exception as e:
            self._logger.warning(f"analysis_bilibili: 短链解析失败 {e!r} {url}")
            return text

    async def search_bili_by_title(
        self, title: str, session: ClientSession
    ) -> str | None:
        try:
            # 先请求主站以获取配套的 buvid3 与 b_nut，
            # 避免先调 _init_cookie 导致主站不下发 b_nut 触发搜索 v_voucher 验证
            async with session.get("https://www.bilibili.com") as resp:
                assert resp.status == 200

            query = await get_query({"keyword": title}, session)
            search_url = (
                f"https://api.bilibili.com/x/web-interface/wbi/search/all/v2?{query}"
            )

            bili_ticket = await get_ticket(session)
            session.cookie_jar.update_cookies({"bili_ticket": bili_ticket})

            result = await self._fetch_api(
                session,
                search_url,
                headers={"Referer": "https://search.bilibili.com/"},
            )
            if not result:
                return None

            for item in (result.get("data") or {}).get("result", []):
                if item.get("result_type") != "video":
                    continue
                videos = item.get("data") or []
                if videos:
                    # 只返回第一个结果
                    return videos[0].get("arcurl")
        except Exception as e:
            self._logger.warning(f"analysis_bilibili: 搜索视频失败 {e!r} {title}")
        return None

    # ------------------------------------------------------------------ #
    # 各类型解析
    # ------------------------------------------------------------------ #
    async def video_detail(
        self,
        url: str,
        session: ClientSession,
        page: Any = None,
        time_location: Any = None,
    ) -> tuple[list | None, str | None]:
        settings = self._get_settings()
        try:
            data = await self._fetch_api(session, url)
            if data is None:
                return None, None
            res = data.get("data")
            if not res:
                return "解析到视频被删了/稿件不可见或审核中/权限不足", url
            vurl = f"https://www.bilibili.com/video/av{res['aid']}"
            title = f"\n标题：{res['title']}\n"

            has_image = settings.want_image("video")

            cover = self._resize_image(res["pic"]) if has_image else ""
            vurl = "\n" + vurl if cover else vurl
            if page:
                page = page[0].replace("&amp;", "&")
                p = int(page[3:])
                if p <= len(res["pages"]):
                    vurl += f"?p={p}"
                    part = res["pages"][p - 1]["part"]
                    if part != res["title"]:
                        title += f"小标题：{part}\n"
            if time_location:
                time_location = time_location[0].replace("&amp;", "&")[3:]
                if page:
                    vurl += f"&t={time_location}"
                else:
                    vurl += f"?t={time_location}"
            pubdate = strftime("%Y-%m-%d %H:%M:%S", localtime(res["pubdate"]))
            tname = (
                f"类型：{res['tname']} | UP：{res['owner']['name']} | 日期：{pubdate}\n"
            )
            stat = (
                f"播放：{handle_num(res['stat']['view'])} | "
                f"弹幕：{handle_num(res['stat']['danmaku'])} | "
                f"收藏：{handle_num(res['stat']['favorite'])}\n"
            )
            stat += (
                f"点赞：{handle_num(res['stat']['like'])} | "
                f"硬币：{handle_num(res['stat']['coin'])} | "
                f"评论：{handle_num(res['stat']['reply'])}\n"
            )
            desc = f"简介：{res['desc']}"
            desc_list = desc.split("\n")
            desc = "".join(i + "\n" for i in desc_list if i)
            desc_list = desc.split("\n")
            if len(desc_list) > 4:
                desc = desc_list[0] + "\n" + desc_list[1] + "\n" + desc_list[2] + "……"
            msg = [cover, vurl, title, tname, stat, desc]
            return msg, vurl
        except Exception as e:
            self._logger.warning(f"analysis_bilibili: 视频解析失败 {e!r} {url}")
            return None, None

    async def bangumi_detail(
        self,
        url: str,
        time_location: Any,
        session: ClientSession,
    ) -> tuple[list | None, str | None]:
        settings = self._get_settings()
        try:
            is_media = False
            if "media_id" in url:
                is_media = True
                data = await self._fetch_api(session, url)
                ssid = ((data or {}).get("result") or {}).get("media") or {}
                ssid = ssid.get("season_id")
                if not ssid:
                    return None, None
                url = f"https://api.bilibili.com/pgc/view/web/season?season_id={ssid}"

            data = await self._fetch_api(session, url)
            if data is None:
                return None, None
            res = data.get("result")
            if not res:
                return None, None

            has_image = settings.want_image("bangumi")

            cover = self._resize_image(res["cover"], is_cover=True) if has_image else ""
            title = f"番剧：{res['title']}\n"
            desc = f"{res['new_ep']['desc']}\n"
            long_title = ""
            styles = "".join(f"{i}," for i in res["styles"])
            styles = f"类型：{styles[:-1]}\n"
            evaluate = f"简介：{res['evaluate']}\n"
            if is_media:
                vurl = f"https://www.bilibili.com/bangumi/media/md{res['media_id']}"
            elif "season_id" in url:
                vurl = f"https://www.bilibili.com/bangumi/play/ss{res['season_id']}"
            else:
                epid = re.compile(r"ep_id=\d+").search(url)[0][len("ep_id=") :]
                for i in res["episodes"]:
                    if str(i["ep_id"]) == epid:
                        long_title = f"标题：{i['long_title']}\n"
                        break
                vurl = f"https://www.bilibili.com/bangumi/play/ep{epid}"
            if time_location:
                time_location = time_location[0].replace("&amp;", "&")[3:]
                vurl += f"?t={time_location}"
            vurl = "\n" + vurl if cover else vurl
            msg = [cover, f"{vurl}\n", title, long_title, desc, styles, evaluate]
            return msg, vurl
        except Exception as e:
            self._logger.warning(f"analysis_bilibili: 番剧解析失败 {e!r} {url}")
            return None, None

    async def live_detail(
        self, url: str, session: ClientSession
    ) -> tuple[list | None, str | None]:
        settings = self._get_settings()
        try:
            data = await self._fetch_api(session, url)
            if not data or data.get("code") != 0:
                return None, None
            res = data["data"]
            uname = res["anchor_info"]["base_info"]["uname"]
            room_id = res["room_info"]["room_id"]
            title = res["room_info"]["title"]

            has_image = settings.want_image("live")

            cover = (
                self._resize_image(res["room_info"]["cover"], is_cover=True)
                if has_image
                else ""
            )
            live_status = res["room_info"]["live_status"]
            lock_status = res["room_info"]["lock_status"]
            parent_area_name = res["room_info"]["parent_area_name"]
            area_name = res["room_info"]["area_name"]
            online = res["room_info"]["online"]
            tags = res["room_info"]["tags"]
            watched_show = res["watched_show"]["text_large"]
            vurl = f"https://live.bilibili.com/{room_id}\n"
            if lock_status:
                lock_time = res["room_info"]["lock_time"]
                lock_time = strftime("%Y-%m-%d %H:%M:%S", localtime(lock_time))
                title = f"[已封禁]直播间封禁至：{lock_time}\n"
            elif live_status == 1:
                title = f"[直播中]标题：{title}\n"
            elif live_status == 2:
                title = f"[轮播中]标题：{title}\n"
            else:
                title = f"[未开播]标题：{title}\n"
            up = f"主播：{uname}  当前分区：{parent_area_name}-{area_name}\n"
            watch = f"观看：{watched_show}  直播时的人气上一次刷新值：{handle_num(online)}\n"
            if tags:
                tags = f"标签：{tags}\n"
            if live_status:
                player = (
                    "独立播放器：https://www.bilibili.com/blackboard/live/"
                    f"live-activity-player.html?enterTheRoom=0&cid={room_id}"
                )
            else:
                player = ""
            vurl = "\n" + vurl if cover else vurl
            msg = [cover, vurl, title, up, watch, tags, player]
            return msg, vurl
        except Exception as e:
            self._logger.warning(f"analysis_bilibili: 直播间解析失败 {e!r} {url}")
            return None, None

    async def article_detail(
        self, url: str, cvid: Any, session: ClientSession
    ) -> tuple[list | None, str | None]:
        settings = self._get_settings()
        try:
            data = await self._fetch_api(session, url)
            if not data:
                return None, None
            res = data.get("data")
            if not res:
                return None, None

            has_image = settings.want_image("article")

            images = (
                [self._resize_image(i) for i in res["origin_image_urls"]]
                if has_image
                else []
            )
            vurl = f"https://www.bilibili.com/read/cv{cvid}"
            title = f"标题：{res['title']}\n"
            up = f"作者：{res['author_name']} (https://space.bilibili.com/{res['mid']})\n"
            view = f"阅读数：{handle_num(res['stats']['view'])} "
            favorite = f"收藏数：{handle_num(res['stats']['favorite'])} "
            coin = f"硬币数：{handle_num(res['stats']['coin'])}"
            share = f"分享数：{handle_num(res['stats']['share'])} "
            like = f"点赞数：{handle_num(res['stats']['like'])} "
            dislike = f"不喜欢数：{handle_num(res['stats']['dislike'])}"
            desc = view + favorite + coin + "\n" + share + like + dislike + "\n"
            msg = [images, title, up, desc, vurl]
            return msg, vurl
        except Exception as e:
            self._logger.warning(f"analysis_bilibili: 专栏解析失败 {e!r} {url}")
            return None, None

    async def dynamic_detail(
        self, url: str, session: ClientSession
    ) -> tuple[list | None, str | None]:
        settings = self._get_settings()
        try:
            has_desktop_fallback = "/web-dynamic/v1/detail" in url
            data = await self._fetch_api(
                session, url, retry=1 if has_desktop_fallback else 2
            )
            if data is None and has_desktop_fallback:
                desktop_url = url.replace(
                    "/web-dynamic/v1/detail", "/web-dynamic/desktop/v1/detail"
                )
                data = await self._fetch_api(session, desktop_url)
            if not data or data.get("code") != 0:
                return None, None
            res = (data.get("data") or {}).get("item")
            if not res:
                return None, None
            res = _normalize_desktop_dynamic(res)
            dynamic_id = res["id_str"]
            if "rid=" in url and dynamic_id:
                opus_msg, opus_vurl = await self.opus_detail(
                    "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail"
                    f"?id={dynamic_id}",
                    session,
                )
                if opus_msg:
                    return opus_msg, opus_vurl
            vurl = f"https://t.bilibili.com/{dynamic_id}\n"

            # 动态内容
            module_dynamic = res["modules"]["module_dynamic"]
            module_type = res["type"]

            # 文字信息
            desc = module_dynamic["desc"] if module_dynamic["desc"] else {"text": ""}
            content = (desc.get("text") or "").replace("\r", "\n").replace("\n\n", "\n")

            # 额外信息(会员购)
            additional_msg = []
            additional = module_dynamic.get("additional")
            if isinstance(additional, dict):
                additional_type = additional.get("type")
                if additional_type == "ADDITIONAL_TYPE_GOODS":
                    items = (additional.get("goods") or {}).get("items", [])
                    for item in items:
                        additional_msg.append(
                            f"{item.get('name')}（{item.get('price')}）\n"
                        )

            # DRAW图片/ARCHIVE转发视频/null纯文字
            draws: list[str] = []
            archive_cover = ""
            archive_msg = ""
            major = module_dynamic["major"]
            if isinstance(major, dict):
                if module_type == "DYNAMIC_TYPE_DRAW":
                    draw_items = (major.get("draw") or {}).get("items") or []
                    if settings.want_image("dynamic"):
                        draws = [self._resize_image(i.get("src")) for i in draw_items]
                    else:
                        content += f"\nPS：动态中包含{len(draw_items)}张图片"

                elif module_type == "DYNAMIC_TYPE_AV":
                    archive = major.get("archive") or {}
                    jump_url = archive.get("jump_url")
                    archive_cover = (
                        self._resize_image(archive.get("cover") or "")
                        if settings.want_image("dynamic")
                        else ""
                    )
                    archive_msg += f"转发视频：https:{jump_url}\n"
                    archive_msg += f"简介：{archive.get('desc')}"

            elif module_type == "DYNAMIC_TYPE_FORWARD":
                desc = module_dynamic["desc"]
                orig_id = (res.get("orig") or {}).get("id_str")
                if orig_id:
                    archive_msg += f"转发动态：https://t.bilibili.com/{orig_id}\n"

            if not (content or draws or archive_msg or additional_msg):
                # 动态不存在或数据被风控时，避免发送空内容
                return None, None

            split = (
                "\n----------------------------------------\n"
                if (content or draws) and (archive_msg or additional_msg)
                else ""
            )

            msg = [
                content,
                draws,
                split,
                archive_cover,
                archive_msg,
                additional_msg,
                f"\n动态链接：{vurl}",
            ]
            return msg, vurl
        except Exception as e:
            self._logger.warning(f"analysis_bilibili: 动态解析失败 {e!r} {url}")
            return None, None

    async def opus_detail(
        self, url: str, session: ClientSession
    ) -> tuple[list | None, str | None]:
        settings = self._get_settings()
        try:
            data = await self._fetch_api(session, url)
            if not data or data.get("code") != 0:
                return None, None
            inner_data = data.get("data") or {}
            item = inner_data.get("item")
            if not item:
                fallback = inner_data.get("fallback") or {}
                fallback_id = fallback.get("id")
                if fallback.get("type") == 2 and fallback_id:
                    return await self.article_detail(
                        "https://api.bilibili.com/x/article/viewinfo"
                        f"?id={fallback_id}&mobi_app=pc&from=web",
                        str(fallback_id),
                        session,
                    )
                return None, None

            dynamic_id = item.get("id_str") or ""
            vurl = f"https://www.bilibili.com/opus/{dynamic_id}\n"

            title = ""
            author = ""
            pub_time = ""
            content = ""
            images: list[str] = []
            raw_pics: list[str] = []
            for module in item.get("modules") or []:
                module_type = module.get("module_type")
                if module_type == "MODULE_TYPE_TITLE":
                    title = (
                        (module.get("module_title") or {}).get("text") or ""
                    ).strip()
                elif module_type == "MODULE_TYPE_AUTHOR":
                    author_info = module.get("module_author") or {}
                    author = (author_info.get("name") or "").strip()
                    pub_time = (author_info.get("pub_time") or "").strip()
                elif module_type == "MODULE_TYPE_TOP":
                    top_pics = (
                        ((module.get("module_top") or {}).get("display") or {}).get(
                            "album"
                        )
                        or {}
                    ).get("pics") or []
                    raw_pics.extend(pic["url"] for pic in top_pics if pic.get("url"))
                elif module_type == "MODULE_TYPE_CONTENT":
                    module_content = module.get("module_content") or {}
                    content = opus_text(module_content)
                    for para in module_content.get("paragraphs") or []:
                        pics = (para.get("pic") or {}).get("pics") or []
                        raw_pics.extend(pic["url"] for pic in pics if pic.get("url"))

            if settings.want_image("dynamic"):
                images = [self._resize_image(url) for url in raw_pics[:9]]
            elif raw_pics:
                prefix = "\n" if content else ""
                content += f"{prefix}PS：动态中包含{len(raw_pics)}张图片"

            if not (content or title or images):
                return None, None

            vurl = "\n" + vurl if images else vurl
            msg = [
                images,
                vurl,
                f"标题：{title}\n" if title else "",
                (f"UP：{author}" + (f" | 日期：{pub_time}" if pub_time else "") + "\n")
                if author
                else "",
                content,
            ]
            return msg, vurl
        except Exception as e:
            self._logger.warning(f"analysis_bilibili: 图文动态解析失败 {e!r} {url}")
            return None, None


def _normalize_desktop_dynamic(item: dict) -> dict:
    modules_list = item.get("modules")
    if not isinstance(modules_list, list):
        return item

    desc = None
    major = None
    orig = None
    for mod in modules_list:
        mtype = mod.get("module_type")
        if mtype == "MODULE_TYPE_DESC":
            desc = mod.get("module_desc")
        elif mtype == "MODULE_TYPE_DYNAMIC":
            mdyn = mod.get("module_dynamic") or {}
            if mdyn.get("dyn_archive"):
                arch = mdyn["dyn_archive"]
                bvid = arch.get("bvid") or ""
                major = {
                    "archive": {
                        "jump_url": f"//www.bilibili.com/video/{bvid}" if bvid else "",
                        "cover": arch.get("cover") or "",
                        "desc": arch.get("desc") or "",
                    }
                }
            elif mdyn.get("dyn_draw"):
                major = {"draw": mdyn["dyn_draw"]}
            elif mdyn.get("dyn_forward"):
                orig = (mdyn["dyn_forward"] or {}).get("item")

    return {
        "id_str": item.get("id_str", ""),
        "type": item.get("type", ""),
        "orig": orig,
        "modules": {
            "module_dynamic": {
                "desc": desc,
                "additional": None,
                "major": major,
            }
        },
    }


def _extract_nodes_text(nodes: Iterable[dict]) -> str:
    parts: list[str] = []
    for node in nodes:
        words = (node.get("word") or {}).get("words")
        if words:
            parts.append(words)
        rich = (node.get("rich") or {}).get("text")
        if rich:
            parts.append(rich)
    return "".join(parts)


def opus_text(content: dict) -> str:
    lines: list[str] = []
    for para in content.get("paragraphs") or []:
        text = para.get("text") or {}
        line = _extract_nodes_text(text.get("nodes") or []).strip()
        if line:
            for sub in line.splitlines():
                if sub.strip():
                    lines.append(sub.strip())
        for item in (para.get("list") or {}).get("items") or []:
            item_line = _extract_nodes_text(item.get("nodes") or []).strip()
            if item_line:
                lines.append(item_line)
    if len(lines) > 6:
        result = "\n".join(lines[:6]) + "……"
    else:
        result = "\n".join(lines)
    if len(result) > 300:
        result = result[:300].rstrip() + "……"
    return result


def extract(text: str) -> tuple[str, str | None, str | None]:
    """从任意文本中提取 B 站 API 地址、分 P 和播放定位时间。"""
    try:
        url = ""
        # 视频分p
        page = PAGE_REGEX.search(text)
        # 视频播放定位时间
        time_match = TIME_REGEX.search(text)
        # 主站视频 av 号
        aid = AID_REGEX.search(text)
        # 主站视频 bv 号
        bvid = BVID_REGEX.search(text)
        # 番剧视频页
        epid = EPID_REGEX.search(text)
        # 番剧剧集ssid(season_id)
        ssid = SSID_REGEX.search(text)
        # 番剧详细页
        mdid = MDID_REGEX.search(text)
        # 直播间
        room_id = ROOM_ID_REGEX.search(text)
        # 文章
        cvid = CVID_REGEX.search(text)
        # 动态
        dynamic_id_type2 = DYNAMIC_TYPE2_REGEX.search(text)
        # 动态
        dynamic_id = DYNAMIC_ID_REGEX.search(text)
        # 动态/图文（新版 opus 链接）
        opus_id = OPUS_ID_REGEX.search(text)
        # 动态（移动端分享链接）
        dynamic_id_mobile = DYNAMIC_MOBILE_REGEX.search(text)
        if bvid:
            url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid[0]}"
        elif aid:
            url = f"https://api.bilibili.com/x/web-interface/view?aid={aid[0][2:]}"
        elif epid:
            url = f"https://api.bilibili.com/pgc/view/web/season?ep_id={epid[0][2:]}"
        elif ssid:
            url = (
                f"https://api.bilibili.com/pgc/view/web/season?season_id={ssid[0][2:]}"
            )
        elif mdid:
            url = f"https://api.bilibili.com/pgc/review/user?media_id={mdid[0][2:]}"
        elif room_id:
            url = (
                "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom"
                f"?room_id={room_id[2]}"
            )
        elif cvid:
            page = cvid[4]
            url = (
                f"https://api.bilibili.com/x/article/viewinfo?id={page}"
                "&mobi_app=pc&from=web"
            )
        elif opus_id:
            url = (
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail"
                f"?id={opus_id[1]}"
            )
        elif dynamic_id_mobile:
            url = (
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"
                f"?id={dynamic_id_mobile[1]}"
            )
        elif dynamic_id_type2:
            url = (
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"
                f"?rid={dynamic_id_type2[2]}&type=2"
            )
        elif dynamic_id:
            url = (
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"
                f"?id={dynamic_id[3]}"
            )
        return url, page, time_match
    except Exception:
        return "", None, None

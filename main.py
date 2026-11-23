"""AstrBot 版 B 站链接解析插件。

移植自 mengshouer/nonebot_plugin_analysis_bilibili（MIT License）：
https://github.com/mengshouer/nonebot_plugin_analysis_bilibili
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .bilibili import BiliClient, BiliSettings, is_image_url

__all__ = ["AnalysisBilibiliPlugin"]

# 触发的链接特征：av/BV 号、b23.tv 等短链、bilibili.com 域名
LINK_PATTERN = re.compile(
    r"(?:av|cv)\d+"
    r"|BV[A-Za-z0-9]{10}"
    r"|b23\.tv"
    r"|bili(?:22|23|33|2233)\.cn"
    r"|bilibili\.com",
    re.I,
)

# 从文本/卡片里提取一个 B 站链接
BILI_URL_PATTERN = re.compile(
    r"(?:https?://)?"
    r"(?:b23\.tv/[\w]+"
    r"|bili(?:22|23|33|2233)\.cn/[\w]+"
    r"|(?:[\w-]+\.)?bilibili\.com/[\w./?=&%#@:~+-]*)",
    re.I,
)

SEARCH_PATTERN = re.compile(r"^[/／]?\s*搜视频")


def _to_str_list(value: Any) -> list[str]:
    """把配置项统一转换为字符串列表，兼容 ``"1,2"`` 这样的手写值。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in re.split(r"[,，\s]+", value) if part]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 15.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iter_strings(obj: Any) -> Iterator[str]:
    """递归遍历一个 JSON 结构中的所有字符串。"""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_strings(value)
    elif isinstance(obj, (list, tuple, set)):
        for value in obj:
            yield from _iter_strings(value)


def _raw_message_segments(event: AstrMessageEvent) -> Iterable[dict]:
    """取出平台原始消息中的消息段（仅 OneBot v11 风格的 ``message`` 列表可用）。"""
    raw = getattr(event.message_obj, "raw_message", None)
    if raw is None:
        return []
    if isinstance(raw, dict):
        segments = raw.get("message")
    else:
        segments = getattr(raw, "message", None)
    if not isinstance(segments, (list, tuple)):
        return []
    return [segment for segment in segments if isinstance(segment, dict)]


def card_text(event: AstrMessageEvent) -> str:
    """拼接消息中 JSON/XML 卡片（QQ 小程序、结构化分享）里的所有字符串。

    AstrBot 的 ``message_str`` 只包含纯文本，B 站小程序卡片需要从原始消息中补充。
    """
    texts: list[str] = []
    for segment in _raw_message_segments(event):
        if segment.get("type") not in ("json", "xml", "markdown", "light_app"):
            continue
        texts.extend(_iter_strings(segment.get("data")))
    return "\n".join(texts)


def _find_bili_title(payload: Any) -> str:
    """从一个分享卡片 JSON 中推测 B 站视频标题。"""
    try:
        blob = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
    if "哔哩哔哩" not in blob and "bilibili" not in blob.lower():
        return ""
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if isinstance(meta, dict):
        for value in meta.values():
            if not isinstance(value, dict):
                continue
            for key in ("title", "desc"):
                title = value.get(key)
                if not isinstance(title, str):
                    continue
                title = title.strip()
                if title and title.lower() not in ("哔哩哔哩", "bilibili"):
                    return title
    return ""


def parse_card(event: AstrMessageEvent) -> tuple[str, str]:
    """从卡片消息中取出 ``(B 站链接, 标题)``，取不到时返回空字符串。"""
    blob = card_text(event)
    if not blob:
        return "", ""
    # 部分分享卡片会用 \/ 转义斜杠，或用 &amp; 转义 &
    normalized = blob.replace("\\/", "/").replace("&amp;", "&")
    match = BILI_URL_PATTERN.search(normalized)
    if match:
        return match.group(0), ""
    # 没有链接时尝试用卡片标题去搜索
    for text in blob.splitlines():
        if not text.strip():
            continue
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        title = _find_bili_title(payload)
        if title:
            return "", title
    return "", ""


def message_text(event: AstrMessageEvent, extra_url: str = "") -> str:
    """消息纯文本 + 卡片中解析出的链接。"""
    text = event.message_str or ""
    if extra_url and extra_url not in text:
        text = f"{text} {extra_url}"
    return text.strip()


class BiliLinkFilter(filter.CustomFilter):
    """匹配纯文本或 QQ 小程序卡片中的 B 站链接。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        if LINK_PATTERN.search(event.message_str or ""):
            return True
        blob = card_text(event)
        if not blob:
            return False
        return bool(
            LINK_PATTERN.search(blob)
            or "bilibili" in blob.lower()
            or "哔哩哔哩" in blob
        )


def build_message_chain(
    msg_list: Iterable[Any],
) -> list[Comp.BaseMessageComponent]:
    """把解析结果转换为 AstrBot 消息链。

    解析结果是一个可以任意嵌套的列表：字符串为文本，图片 URL 会被识别为图片。
    相邻文本会被合并，减少消息段数量。
    """
    components: list[Comp.BaseMessageComponent] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        text = "".join(buffer)
        buffer.clear()
        if text.strip():
            components.append(Comp.Plain(text))

    def walk(items: Iterable[Any]) -> None:
        for item in items:
            if not item:
                continue
            if isinstance(item, (list, tuple, set)):
                walk(item)
                continue
            text = str(item)
            if is_image_url(text):
                flush()
                try:
                    components.append(Comp.Image.fromURL(text))
                except Exception:
                    buffer.append(text)
            else:
                buffer.append(text)

    walk(msg_list)
    flush()
    return components


class AnalysisBilibiliPlugin(Star):
    """自动解析 B 站链接（视频 / 番剧 / 直播 / 专栏 / 动态）。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: dict = config if config is not None else {}
        self._client = BiliClient(self._build_settings, logger)

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    def _build_settings(self) -> BiliSettings:
        cfg = self.config
        return BiliSettings(
            display_image=bool(cfg.get("display_image", True)),
            display_image_list=tuple(_to_str_list(cfg.get("display_image_list", []))),
            images_size=str(cfg.get("images_size", "") or ""),
            cover_images_size=str(cfg.get("cover_images_size", "") or ""),
            reanalysis_time=_to_int(cfg.get("reanalysis_time", 0)),
            cookie=str(cfg.get("bilibili_cookie", "") or ""),
            trust_env=bool(cfg.get("trust_env", False)),
            timeout=_to_float(cfg.get("timeout", 15)),
        )

    def _ids(self, key: str) -> list[str]:
        return _to_str_list(self.config.get(key, []))

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        """白名单 / 黑名单判断，逻辑与 NoneBot 原插件一致。"""
        user_id = str(event.get_sender_id() or "")
        group_id = str(event.get_group_id() or "")
        whitelist = self._ids("user_whitelist")
        group_whitelist = self._ids("group_whitelist")

        if user_id in whitelist or (group_id and group_id in group_whitelist):
            return True
        if whitelist or group_whitelist:
            return False

        blacklist = self._ids("user_blacklist")
        group_blacklist = self._ids("group_blacklist")
        if user_id in blacklist or (group_id and group_id in group_blacklist):
            return False
        return True

    def _stop(self, event: AstrMessageEvent) -> None:
        if self.config.get("stop_event", True):
            event.stop_event()

    def _session_key(self, event: AstrMessageEvent) -> str:
        return str(event.get_group_id() or event.unified_msg_origin)

    def _apply_desc_blacklist(self, event: AstrMessageEvent, msg: list) -> None:
        group_id = str(event.get_group_id() or "")
        if not group_id or group_id not in self._ids("desc_blacklist"):
            return
        if msg and isinstance(msg[-1], str) and msg[-1].startswith("简介"):
            msg[-1] = ""

    # ------------------------------------------------------------------ #
    # 事件处理
    # ------------------------------------------------------------------ #
    @filter.custom_filter(BiliLinkFilter)
    async def parse_bilibili(self, event: AstrMessageEvent):
        """检测到 B 站链接时自动解析，支持视频、番剧、直播、专栏与动态。"""
        if not self._is_allowed(event):
            self._stop(event)
            return

        extra_url, title_hint = parse_card(event)
        text = message_text(event, extra_url)
        if not text and not title_hint:
            return

        try:
            result = await self._client.parse(
                text,
                session_key=self._session_key(event),
                title_hint=title_hint or None,
            )
        except Exception as e:
            logger.error(f"analysis_bilibili: 解析失败 {e!r}")
            self._stop(event)
            return

        if not result:
            # False 表示该链接在本次会话中已解析过，其余情况表示没有解析到内容
            self._stop(event)
            return

        if isinstance(result, str):
            # 接口返回的错误提示
            yield event.plain_result(result)
            self._stop(event)
            return

        self._apply_desc_blacklist(event, result)
        chain = build_message_chain(result)
        if not chain:
            self._stop(event)
            return

        yield event.chain_result(chain)
        self._stop(event)

    @filter.regex(SEARCH_PATTERN.pattern)
    async def search_video(self, event: AstrMessageEvent):
        """搜索 B 站视频，用法：搜视频 <关键词>"""
        if not self.config.get("enable_search", False):
            return
        if not self._is_allowed(event):
            self._stop(event)
            return

        keyword = SEARCH_PATTERN.sub("", event.message_str or "").strip()
        if not keyword:
            yield event.plain_result("请输入要搜索的视频标题，例如：搜视频 原神")
            self._stop(event)
            return

        try:
            result = await self._client.parse(
                keyword,
                search=True,
                session_key=self._session_key(event),
            )
        except Exception as e:
            logger.error(f"analysis_bilibili: 搜索失败 {e!r}")
            yield event.plain_result("搜索出错了，请稍后再试")
            self._stop(event)
            return

        if result is False:
            yield event.plain_result(f"「{keyword}」的视频刚刚已经解析过了")
            self._stop(event)
            return
        if not result:
            yield event.plain_result(f"没有找到「{keyword}」相关的视频")
            self._stop(event)
            return
        if isinstance(result, str):
            yield event.plain_result(result)
            self._stop(event)
            return

        self._apply_desc_blacklist(event, result)
        chain = build_message_chain(result)
        if not chain:
            yield event.plain_result(f"没有找到「{keyword}」相关的视频")
            self._stop(event)
            return

        yield event.chain_result(chain)
        self._stop(event)

    async def terminate(self) -> None:
        """插件被停用/重载时释放 HTTP 会话。"""
        await self._client.close()

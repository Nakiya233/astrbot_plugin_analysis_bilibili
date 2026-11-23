"""B 站 WBI 签名与 bili_ticket 获取。

移植自 nonebot_plugin_analysis_bilibili 的 ``sign.py``，并做了以下调整：

- 支持复用调用方传入的 ``aiohttp.ClientSession``，从而继承插件的代理/超时配置；
- 请求失败时抛出异常，由上层统一记录日志。
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from functools import reduce
from hashlib import md5

from aiohttp import ClientSession, ClientTimeout

# doc: https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/misc/sign/wbi.md

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
}
REQUEST_TIMEOUT = ClientTimeout(total=15)
CACHE_TTL = 3600

_wbi_cache: tuple[str, str, float] | None = None
_ticket_cache: tuple[str, float] | None = None

# fmt: off
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52
]
# fmt: on


def get_mixin_key(orig: str) -> str:
    """对 imgKey 和 subKey 进行字符顺序打乱编码。"""
    return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]


def enc_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    """为请求参数进行 wbi 签名。"""
    mixin_key = get_mixin_key(img_key + sub_key)
    params = dict(params)
    params["wts"] = round(time.time())  # 添加 wts 字段
    params = dict(sorted(params.items()))  # 按照 key 重排参数
    # 过滤 value 中的 "!'()*" 字符
    params = {
        k: "".join(filter(lambda char: char not in "!'()*", str(v)))
        for k, v in params.items()
    }
    query = urllib.parse.urlencode(params)  # 序列化参数
    wbi_sign = md5((query + mixin_key).encode()).hexdigest()  # 计算 w_rid
    params["w_rid"] = wbi_sign
    return params


async def _fetch_wbi_keys(session: ClientSession, now: float) -> tuple[str, str]:
    global _wbi_cache

    async with session.get("https://api.bilibili.com/x/web-interface/nav") as resp:
        json_content = await resp.json(content_type=None)
    wbi_img = (json_content.get("data") or {}).get("wbi_img") or {}
    img_url: str = wbi_img["img_url"]
    sub_url: str = wbi_img["sub_url"]
    img_key = img_url.rsplit("/", 1)[1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[1].split(".")[0]
    _wbi_cache = (img_key, sub_key, now + CACHE_TTL)
    return img_key, sub_key


async def get_wbi_keys(session: ClientSession | None = None) -> tuple[str, str]:
    """获取最新的 img_key 和 sub_key。"""
    global _wbi_cache

    now = time.monotonic()
    if _wbi_cache and now < _wbi_cache[2]:
        return _wbi_cache[0], _wbi_cache[1]
    if session is not None:
        return await _fetch_wbi_keys(session, now)
    async with ClientSession(headers=HEADERS, timeout=REQUEST_TIMEOUT) as new_session:
        return await _fetch_wbi_keys(new_session, now)


async def get_query(params: dict, session: ClientSession | None = None) -> str:
    """获取签名后的查询参数。"""
    img_key, sub_key = await get_wbi_keys(session)
    signed_params = enc_wbi(params=params, img_key=img_key, sub_key=sub_key)
    return urllib.parse.urlencode(signed_params)


def hmac_sha256(key: str, message: str) -> str:
    """使用 HMAC-SHA256 算法对给定的消息进行加密。"""
    hmac_obj = hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    return hmac_obj.digest().hex()


async def _fetch_ticket(session: ClientSession, now: float) -> str:
    global _ticket_cache

    ts = int(time.time())
    o = hmac_sha256("XgwSnGZ1p", f"ts{ts}")
    url = "https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket"
    params = {
        "key_id": "ec02",
        "hexsign": o,
        "context[ts]": f"{ts}",
        "csrf": "",
    }
    async with session.post(url, params=params) as resp:
        json_content = await resp.json(content_type=None)
    ticket = (json_content.get("data") or {})["ticket"]
    _ticket_cache = (ticket, now + CACHE_TTL)
    return ticket


async def get_ticket(session: ClientSession | None = None) -> str:
    """获取 bili_ticket。"""
    global _ticket_cache

    now = time.monotonic()
    if _ticket_cache and now < _ticket_cache[1]:
        return _ticket_cache[0]
    if session is not None:
        return await _fetch_ticket(session, now)
    async with ClientSession(headers=HEADERS, timeout=REQUEST_TIMEOUT) as new_session:
        return await _fetch_ticket(new_session, now)


# 兼容旧命名（原插件使用驼峰式）
getWbiKeys = get_wbi_keys
encWbi = enc_wbi

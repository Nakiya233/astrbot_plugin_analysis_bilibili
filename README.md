# astrbot_plugin_analysis_bilibili

_✨ AstrBot 的 B 站链接解析插件 ✨_

移植自 [nonebot_plugin_analysis_bilibili](https://github.com/mengshouer/nonebot_plugin_analysis_bilibili)，在 AstrBot 上实现同样的「发链接自动出卡片」体验。

## 功能

- 支持 **视频**（av / BV 号、b23.tv 短链）、**番剧**（ep / ss / md）、**直播**、**专栏**（cv）、**动态 / 图文**（t.bilibili.com、opus）
- 支持 QQ 小程序 / 结构化分享卡片：AstrBot 的纯文本里拿不到卡片内容，插件会从原始消息段中提取链接；只有标题时会自动搜索第一个相关视频
- 自动附带封面与配图（可按内容类型分别开关）、播放 / 弹幕 / 点赞等数据、UP 主与简介
- 命中同一链接时在会话内去重，避免多个机器人重复推送
- 按 AstrBot 的 **UID / UMO** 精确控制白名单、黑名单与「不显示简介」的会话
- 支持图片尺寸缩放、代理、自定义 Cookie
- 可选的「搜视频」功能

## 安装

推荐在 AstrBot WebUI 的插件市场安装。

手动安装：

```bash
cd AstrBot/data/plugins
git clone https://github.com/Nakiya233/astrbot_plugin_analysis_bilibili.git
```

然后在 WebUI 的「插件」页面重载插件。插件仅依赖 AstrBot 自带的 `aiohttp`，无需额外安装依赖。

## 使用

私聊或群聊直接发送 B 站链接 / 小程序卡片即可，例如：

```text
【标题】 https://b23.tv/xxxxxx
```

开启「搜视频」后还可以发送：

```text
搜视频 关键词
```

## 配置

配置文件位于 `data/config/astrbot_plugin_analysis_bilibili_config.json`，也可以直接在 WebUI 的插件配置面板中修改。

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `display_image` | bool | `true` | 是否显示封面 / 图片；开启时所有类型都显示 |
| `display_image_list` | list | `[]` | 关闭总开关后，仍需要显示封面的类型（`video` / `bangumi` / `live` / `article` / `dynamic`） |
| `images_size` | string | `""` | 图片尺寸，如 `100w` / `100h` / `100h_100w`，留空为原图 |
| `cover_images_size` | string | `""` | 封面图尺寸，留空跟随 `images_size` |
| `reanalysis_time` | int | `0` | 同一链接重新解析间隔（秒），`0` 表示不重复解析 |
| `enable_search` | bool | `false` | 是否开启「搜视频」 |
| `stop_event` | bool | `true` | 解析后是否阻止事件继续传播（避免 LLM 或其他插件重复回复） |
| `timeout` | int | `15` | 请求超时时间（秒） |
| `trust_env` | bool | `false` | 是否使用系统代理 |
| `bilibili_cookie` | string | `""` | 可选，整段 Cookie；留空自动获取匿名指纹 |
| `uid_whitelist` | list | `[]` | 用户 UID 白名单（AstrBot 的 sender ID） |
| `umo_whitelist` | list | `[]` | 会话 UMO 白名单 |
| `uid_blacklist` | list | `[]` | 用户 UID 黑名单 |
| `umo_blacklist` | list | `[]` | 会话 UMO 黑名单 |
| `umo_desc_blacklist` | list | `[]` | 不显示简介的会话 UMO |

> 填写任意白名单后黑名单失效，与上游插件行为一致。

### 关于 UID 与 UMO

插件按 AstrBot 的会话模型做黑白名单，不再使用裸 QQ 群号：

- **UID**：`event.get_sender_id()`，即发送者的平台用户 ID。
- **UMO**：`event.unified_msg_origin`，格式为 `平台:消息类型:会话ID`，例如
  `aiocqhttp:GroupMessage:123456789`、`aiocqhttp:FriendMessage:10001`。

在 WebUI 中可以直接使用「用户选择器」和「会话选择器」添加，也可以手动填写。
在机器人所在会话发送 `/sid` 指令即可查看当前会话的 UMO 与 UID。

> 为兼容旧配置，会话列表中填写纯群号（如 `123456789`）仍然有效；
> 但推荐使用完整的 UMO，这样私聊和多平台会话也能精确命中。

## 与 NoneBot 版本的差异

| NoneBot 配置 | AstrBot 配置 | 说明 |
| --- | --- | --- |
| `analysis_images_size` | `images_size` | 同名逻辑 |
| `analysis_cover_images_size` | `cover_images_size` | 同名逻辑 |
| `analysis_display_image` | `display_image` | 同上 |
| `analysis_display_image_list` | `display_image_list` | 同上 |
| `analysis_reanalysis_time` | `reanalysis_time` | 同上 |
| `analysis_enable_search` | `enable_search` | 同上 |
| `analysis_trust_env` | `trust_env` | 同上 |
| `analysis_bilibili_cookie` | `bilibili_cookie` | 同上 |
| `analysis_whitelist` / `analysis_blacklist` | `uid_whitelist` / `uid_blacklist` | 改为按 AstrBot UID 匹配 |
| `analysis_group_whitelist` / `analysis_group_blacklist` | `umo_whitelist` / `umo_blacklist` | 改为按 AstrBot UMO 匹配 |
| `analysis_desc_blacklist` | `umo_desc_blacklist` | 改为按 AstrBot UMO 匹配 |
| `analysis_use_on_message` | —— | AstrBot 的正则 / 自定义监听器本身就会处理所有消息，且会从原始消息段读取卡片，因此不再需要 |

另外：

- 发送方式由 OneBot 消息段改为 AstrBot `MessageChain`，`stop_event` 用于控制解析后是否拦截后续处理；
- 请求会复用同一个 `aiohttp.ClientSession`，插件停用时自动关闭。

## 致谢与许可

- 原项目：[mengshouer/nonebot_plugin_analysis_bilibili](https://github.com/mengshouer/nonebot_plugin_analysis_bilibili)（MIT License）
- WBI 签名实现参考 [bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect)

本项目同样以 MIT License 发布，详见 [LICENSE](LICENSE)。

# GPT Image 生图改图

[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-2783DE)](https://github.com/AstrBotDevs/AstrBot)
[![Version](https://img.shields.io/badge/version-v1.6.0-46A171)](https://github.com/serenite/astrbot_plugin_gpt_image)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](./LICENSE)

面向 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的 GPT Image 生图与改图插件。通过 [leik1000/adobe2api](https://github.com/leik1000/adobe2api) 调用 `firefly-gpt-image-*` 模型，支持参考图编辑、自动画幅、输入审核、权限控制、每日额度与并发队列。

> 当前版本：**v1.6.0**
> Python 依赖：`aiohttp>=3.9.0,<4.0.0`

---

## 功能

- **文生图**：根据自然语言描述生成图片
- **改图 / 图生图**：读取当前消息或引用消息中的参考图，最多 8 张
- **原始提示词直传**：仅剥离控制参数，不翻译、不润色、不扩写
- **自动模型参数**：固定或自动选择分辨率，自动选择画幅
- **保留参考图画幅**：未手动指定比例时，根据第一张参考图选择最接近的支持画幅
- **输入审核**：关键词预检（独立于 LLM 审核开关）、LLM 审核、审核故障降级策略
- **权限与额度**：管理员模式、用户及群组黑白名单、每日使用次数
- **并发保护**：全局、单用户、单群并发限制与等待队列
- **安全防护**：SSRF 防护、重定向逐跳验证、本地路径白名单、图片体积与像素限制、动画图片检测、日志脱敏
- **LLM Tool**：提供 `gpt_image_generate` 和 `gpt_image_edit` 给 AstrBot Agent 调用

## 工作流程

```text
用户描述 + 可选参考图
          |
          +-- 权限 / 配额 / 队列检查
          +-- 参考图安全读取
          +-- 关键词与 LLM 审核
          +-- 选择分辨率和画幅
                    |
                    v
     firefly-gpt-image-{resolution}-{ratio}
                    |
                    v
                adobe2api
          +---------+---------+
          |                   |
       文生图                改图
/v1/images/generations  /v1/chat/completions
```

LLM 只负责审核和参数选择。实际提交给生图模型的 `prompt` 始终是剥离控制参数后的用户原文。

---

## 快速开始

### 1. 准备 adobe2api

先部署并确认 [adobe2api](https://github.com/leik1000/adobe2api) 可正常访问，记录：

- 服务地址，例如 `http://127.0.0.1:6001`
- API Key（如果服务端启用了鉴权）

### 2. 安装插件

将插件目录放入 AstrBot 的插件目录：

```text
AstrBot/
└── data/
    └── plugins/
        └── astrbot_plugin_gpt_image/
            ├── main.py
            ├── client.py
            ├── security.py
            ├── metadata.yaml
            └── requirements.txt
```

重启 AstrBot，或在 WebUI 中重载插件。AstrBot 通常会自动安装 `requirements.txt` 中的依赖；也可以手动安装：

```bash
pip install "aiohttp>=3.9.0,<4.0.0"
```

### 3. 最小配置

| 配置项 | 建议值 | 说明 |
|---|---|---|
| `base_url` | `http://127.0.0.1:6001` | adobe2api 地址，不要带末尾 `/` |
| `api_key` | 按服务端配置填写 | 同时通过 Bearer Token 和 `X-API-Key` 发送 |
| `daily_limit` | `5` | 普通用户每天成功生成图片的次数 |
| `resolution_mode` | `fixed` | 固定分辨率更稳定 |
| `default_resolution` | `2k` | 默认或固定分辨率 |

> 非回环地址默认必须使用 HTTPS。只有在可信内网确有需要时，才开启 `allow_insecure_api_http`。

### 4. 测试

```text
/gpt图 一只在樱花树下睡觉的橘猫，柔和晨光，摄影风格
```

---

## 使用方法

### 指令

| 指令 | 说明 |
|---|---|
| `/gpt图 <描述>` | 文生图；消息带图时自动进入改图模式 |
| `/gptimage <描述>` | `/gpt图` 的英文别名 |
| `/gimg <描述>` | `/gpt图` 的简写 |
| `/gpt改图 <说明>` | 强制改图，必须附图或引用带图消息 |
| `/gpt编辑 <说明>` | `/gpt改图` 的别名 |
| `/gedit <说明>` | `/gpt改图` 的英文别名 |
| `/改图 <说明>` | `/gpt改图` 的中文别名 |
| `/gpt图次数` | 查看当天额度 |
| `/gpt图帮助` | 查看内置帮助 |

中文指令后可以不加空格：

```text
/gpt图给她换上球衣
```

### 控制参数

| 参数 | 说明 |
|---|---|
| `--ratio 16:9` | 指定画幅，覆盖自动选择结果 |
| `--aspect 16:9` | `--ratio` 的等价写法 |
| `比例:16:9` | 中文比例写法 |
| `--no-auto` | 禁用 LLM 自动选画幅，使用配置默认值 |
| `--no-audit` | 跳过 LLM 审核，仅管理员生效 |

用户不能通过指令覆盖分辨率。分辨率统一由 `resolution_mode` 和 `default_resolution` 控制。

### 文生图示例

```text
/gpt图 一只在樱花树下睡觉的橘猫，柔和晨光，摄影风格
/gpt图 --ratio 9:16 竖版赛博朋克城市夜景
/gpt图 比例:21:9 电影感沙漠全景
```

### 改图示例

同一条消息附上图片：

```text
[图片] /gpt改图 把背景换成海边，保留人物姿势
```

或者回复一条带图消息：

```text
/gpt编辑 改成水彩插画风格
```

`/gpt图` 检测到参考图时也会自动进入改图模式。未手动指定画幅时，插件会优先保持第一张参考图的画幅方向。

---

## 支持的模型

模型 ID 格式：

```text
firefly-gpt-image-{resolution}-{ratio}
```

### 分辨率

- `1k`
- `2k`
- `4k`

### 画幅

| 横向 | 竖向 | 方形 / 传统照片 |
|---|---|---|
| `21:9`、`16:9`、`3:2`、`4:3` | `9:16`、`2:3`、`3:4`、`4:5` | `1:1`、`5:4` |

模型 ID 中的比例使用 `x`：

```text
firefly-gpt-image-2k-16x9
firefly-gpt-image-4k-1x1
```

最终可用模型、画质和生成稳定性取决于 adobe2api 及其上游配置。

---

## 配置

完整字段及 WebUI 提示位于 `_conf_schema.json`。以下是常用配置。

### 模型与输出

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `resolution_mode` | `fixed` | `fixed` 固定分辨率；`llm` 由 LLM 选择 |
| `default_resolution` | `2k` | 固定值或 LLM 失败时的回退值 |
| `default_aspect_ratio` | `1:1` | 默认画幅 |
| `auto_select_aspect_ratio` | `true` | 是否让 LLM 自动选择画幅 |
| `max_ref_images` | `3` | 参考图上限，范围 `1~8` |
| `show_meta` | `true` | 在结果中显示分辨率、画幅和模式 |

`auto_select_size` 是旧版兼容字段。新配置请使用 `auto_select_aspect_ratio`。

### 审核

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `enable_audit` | `true` | 启用 LLM 输入审核 |
| `enable_keyword_filter` | `true` | 启用关键词预检 |
| `audit_provider_id` | 空 | 单独指定审核模型 Provider ID |
| `audit_prompt` | 内置提示词 | 审核与参数选择系统提示词，留空用内置默认 |
| `audit_strict` | `false` | 对政治擦边内容从严 |
| `audit_reference_images` | `false` | 将参考图同时交给视觉审核模型 |
| `audit_failure_policy` | `block` | 审核服务异常时的策略 |
| `llm_timeout` | `45` | LLM 分析超时，单位为秒 |

`audit_failure_policy`：

- `block`：审核服务异常时拒绝请求，适合公开部署
- `keyword_only`：仅执行关键词预检，通过后继续
- `allow`：审核异常时直接放行，不建议公开部署

> 关键词预检独立于 `enable_audit`，只要 `enable_keyword_filter` 为 `true` 就会执行，覆盖东突、台独等政治敏感词。
>
> 内置审核规则主要面向中国大陆政治安全（尤其台湾问题从严），文娱、体育、二次元等内容从松。不等同于完整的未成年人、色情、暴力、隐私或换脸审核。公开部署时，应结合上游模型策略和平台规则补充审核。

### 权限与额度

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `permission_mode` | `all` | `all` / `admin` / `whitelist` |
| `allowed_users` | 空 | 用户白名单 |
| `denied_users` | 空 | 用户黑名单，优先级最高（管理员除外） |
| `allowed_groups` | 空 | 群白名单；留空表示不限制群 |
| `denied_groups` | 空 | 群黑名单 |
| `allow_private_chat` | `true` | 是否允许私聊使用 |
| `daily_limit` | `5` | 普通用户每日额度；`0` 表示禁止普通用户使用 |
| `command_alias` | 空 | 额外指令别名，逗号分隔 |

额度按东八区自然日重置。管理员不限额。请求在执行前原子预留一次额度；审核拒绝、参数错误、参考图无效、API 失败、发送失败或任务取消时会退还。

额度记录保存在：

```text
data/plugin_data/astrbot_plugin_gpt_image/daily_quota.json
```

### 并发与队列

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `max_concurrent_global` | `2` | 全局同时生图任务数 |
| `max_concurrent_per_user` | `1` | 单用户同时生图任务数 |
| `max_concurrent_per_group` | `1` | 单群同时生图任务数 |
| `max_queue_length` | `10` | 等待任务上限；`0` 表示不接受排队 |

超出队列容量的新请求会被立即拒绝，并退还预留额度。

### 请求与重试

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `request_timeout` | `300` | 单次生图请求超时，单位为秒 |
| `max_retries` | `1` | 可重试错误的额外重试次数 |
| `retry_backoff` | `2` | 线性重试间隔基数，单位为秒 |
| `max_output_bytes` | `31457280` | 输出图片上限，默认 30 MiB |

超时、限流、服务繁忙和部分 `5xx` 错误会自动重试。

### 参考图与网络安全

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `napcat_hosts` | `127.0.0.1 localhost ::1` | NapCat 本机回调地址白名单 |
| `image_host_suffixes` | `qpic.cn qq.com myqcloud.com gtimg.cn` | 用户图片 URL 域名白名单 |
| `allow_public_http` | `false` | 是否允许 HTTP 公网图片 |
| `allow_insecure_api_http` | `false` | 是否允许 HTTP 连接非回环 adobe2api |
| `allowed_media_dirs` | 空 | 额外允许读取的本地媒体目录 |
| `max_single_image_bytes` | `15728640` | 单张参考图上限，默认 15 MiB |
| `reject_animated_images` | `true` | 拒绝 GIF、APNG 和动画 WebP |

安全建议：

1. 为 `napcat_hosts` 明确填写端口，例如：

   ```text
   127.0.0.1:3000 localhost:3000 [::1]:3000
   ```

   不写端口会放行该主机的任意端口。

2. 不要把 `/`、用户主目录、磁盘根目录或整个 AstrBot 数据目录加入 `allowed_media_dirs`。
3. 公网 API 使用 HTTPS，不要在不可信网络中开启 `allow_insecure_api_http`。
4. 用户消息中的图片 URL 使用独立严格策略，不能通过 `napcat_hosts` 访问回环或内网地址。

---

## LLM Tool

插件注册两个工具：

- `gpt_image_generate(prompt, aspect_ratio="")`
- `gpt_image_edit(prompt, aspect_ratio="")`

工具同样经过权限、额度、审核、图片安全和并发检查。生成任务提交后会在当前会话中直接发送结果。

---

## 故障排查

### 指令被主 Agent 接走

- 优先使用带 `/` 的完整指令
- NapCat 消息格式建议设为 `array`
- 检查插件日志中是否出现 `[gpt_image] entry matched`
- 改图时把图片和指令放在同一条消息，或回复带图消息

### 提示未配置 adobe2api

检查 `base_url`：

- 地址可从 AstrBot 所在环境访问
- 协议为 `http` 或 `https`
- 不包含用户名、密码、查询参数或片段
- 末尾没有多余 `/`

### 返回 401 或 403

确认 `api_key` 与 adobe2api 服务端一致。插件会同时发送：

```text
Authorization: Bearer <api_key>
X-API-Key: <api_key>
```

### 生图超时或服务繁忙

- 将 `request_timeout` 调整到 `180~600`
- 检查 adobe2api 日志和上游服务状态
- 按需调整 `max_retries` 和 `retry_backoff`
- 为 `audit_provider_id` 配置响应更快的模型

### 参考图读取失败

- 重新发送原图，避免只发送表情包缩略图
- 检查图片是否超过 `max_single_image_bytes`
- 检查图床域名、NapCat 地址或本地目录是否在对应白名单中
- 公网 HTTP 图片默认被拒绝，优先使用 HTTPS
- 动画图片默认被拒绝

### 一直显示"生成中"但没有结果

- 查看日志中是否存在后台任务异常或消息发送失败
- 检查机器人是否仍有当前群或私聊的发送权限
- 检查 adobe2api 返回的是有效图片 URL 或 Base64

---

## 数据与隐私

- 用户提示词会发送给审核模型和 adobe2api
- 开启 `audit_reference_images` 后，参考图也会发送给审核模型
- 改图参考图会发送给 adobe2api
- 日志默认仅记录提示词、URL 和路径的长度或摘要，不记录完整内容
- 生成结果暂存于插件 `output` 目录，旧文件会定期清理

部署者应根据所在地区、平台规则和隐私政策向用户说明数据流向。

---

## 版本记录

### v1.6.0

**安全加固**

- URL 策略拆分为四套：NapCat 回调、用户消息图片（严格禁环回/私网/仅白名单域名）、API 请求（同源）、输出下载
- 用户消息 Image 组件 URL 预检查，阻止框架转换方法绕过 SSRF 策略
- `base_url` 拒绝含 userinfo、query、fragment 的地址
- 日志 `redact_url` 剥离 userinfo，`base_url` 日志统一脱敏
- 上游错误日志不再回显响应正文，仅记录状态码、长度和哈希
- 输出图片增加动画检测，拒绝 GIF/APNG/animated WebP
- `PathPolicy` 默认白名单缩小到 `plugin_data`/`temp`/`cache`，不再包含整个 AstrBot data 根目录
- Windows 盘符根目录判断修复（`Path` vs `str` 比较错误）
- 异步 DNS 解析（`asyncio.to_thread` + 缓存 + 超时），不再阻塞事件循环

**审核策略**

- 关键词预检脱钩 `enable_audit`，审核关闭时政治硬拦截仍然生效
- `audit_failure_policy` 默认改为 `block`
- LLM 审核输出严格布尔（只接受 `true`/`false`，含糊值视为拒绝）
- 审核提示词调整为中国政治（尤其台湾问题）从严，文娱/体育/二次元从松
- 台湾相关关键词扩充（台独/青天白日/蔡英文/赖清德等）
- `audit_strict` 默认关闭（删除了依赖未输出 `risk` 字段的死代码）
- LLM 审核输出日志改用 hash+length 脱敏

**并发与配额**

- 额度预留和准入检查前移到图片下载之前，无额度用户不再触发远程下载
- 全局/单用户/单群并发 Semaphore（user -> group -> global 获取顺序）
- Admission 计数器封顶总任务数（`max_concurrent_global + max_queue_length`）
- `max_queue_length=0` 修复（配置解析 `0 or default` bug + 语义改为仅等待数）
- 任务取消时配额与准入槽兜底退款（`_on_bg_task_done` + `_gpt_cleaned` 标记防双重清理）
- 信号量字典惰性清理（超 200 条时清理 idle 条目）
- 不再依赖 asyncio 私有属性（`_value`/`_waiters`），改用自维护计数器

**权限**

- 新增 `denied_users` 个人黑名单
- 新增 `allowed_groups`/`denied_groups` 群白/黑名单
- 新增 `allow_private_chat` 私聊开关

**其他**

- `gpt_edit` 别名补齐（正则/入口/剥离三处统一）
- `cmd_alias_entry` 未配置别名时直接跳过
- `__init__` 不再重复建 client，`initialize` 加 `old is None` 保护
- LLM Tool 启动后台任务后仅在成功时 yield 确认消息
- `parse_user_overrides` 不再全局压缩空白，保留换行和连续空格
- `probe_image_size` 解码头从 8KB 增到 1MB，修复大 EXIF JPEG 尺寸解析失败
- 下载图片 Content-Type 不再信任远端声明，仅使用签名嗅探结果
- `client.configured()` 拒绝带 userinfo/query/fragment 的 base_url
- 输出 URL 下载改用 `validate_async`，不阻塞事件循环
- 添加 MIT License

### v1.5.5

- 增加 SSRF、DNS rebinding 与重定向防护
- 增加本地路径白名单、图片字节及像素限制
- 增加额度原子预留和失败退款
- 增加全局、用户、群组并发与队列控制
- 增加 LLM 超时和审核失败策略
- 加强日志脱敏

### v1.5.0

- 加强 QQ 图床与 Data URL 参考图处理，完善图生图支持

### v1.4.x

- 增加改图指令并调整审核策略

### v1.3.0

- 用户原文直接作为 prompt，LLM 仅负责审核与模型参数选择

### v1.0.0

- 首次发布

---

## 相关项目

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [leik1000/adobe2api](https://github.com/leik1000/adobe2api)

## License

[MIT](./LICENSE)

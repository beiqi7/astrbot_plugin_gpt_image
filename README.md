# GPT Image 生图改图

面向 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的图片生成插件，通过 [leik1000/adobe2api](https://github.com/leik1000/adobe2api) 调用 `firefly-gpt-image-*` 模型，支持文生图、参考图编辑、模型参数自动选择、输入审核与每日额度管理。

> 当前版本：`v1.5.5` · Python 依赖：`aiohttp>=3.9.0,<4.0.0`

## 功能亮点

- **文生图**：根据自然语言描述生成图片
- **图生图 / 改图**：支持当前消息附图或引用消息中的图片，最多可读取 8 张参考图
- **保留原始提示词**：用户描述会原样提交，LLM 不负责改写、翻译或扩写
- **智能选择参数**：可由 LLM 自动选择分辨率与画幅，也可固定分辨率或手动指定画幅
- **输入审核**：支持关键词预检、LLM 审核与审核服务异常时的降级策略
- **额度与权限**：支持每日次数限制、管理员不限额、管理员模式与用户白名单
- **自动重试**：上游超时、繁忙、限流或部分 `5xx` 错误可自动重试
- **安全防护**：包含 SSRF 防护、本地路径白名单、图片体积限制和日志脱敏
- **LLM Tool**：提供 `gpt_image_generate` 与 `gpt_image_edit`，可供 AstrBot Agent 调用

## 工作流程

```text
用户描述 ───────────────────────────────┐
  │                                     │
  ├─ 可选：当前消息 / 引用消息中的图片    │
  │                                     ▼
  └─ LLM 或规则：审核 + 选择模型参数 ──► 构建模型 ID
                                        │
                    firefly-gpt-image-{resolution}-{ratio}
                                        │
                                        ▼
                                   adobe2api
                         ┌───────────────┴───────────────┐
                         │                               │
                  无参考图：文生图                有参考图：改图
             /v1/images/generations       /v1/chat/completions
```

LLM 只参与审核和参数选择；实际生图 `prompt` 始终使用剥离控制参数后的用户原文。

## 快速开始

### 1. 准备 adobe2api

先部署并确认 [adobe2api](https://github.com/leik1000/adobe2api) 可正常访问，记录：

- 服务地址，例如 `http://127.0.0.1:6001`
- API Key（如果服务端已启用鉴权）

### 2. 安装插件

将插件目录放入 AstrBot 的插件目录：

```text
AstrBot/
└── data/
    └── plugins/
        └── astrbot_plugin_gpt_image/
            ├── main.py
            ├── metadata.yaml
            ├── requirements.txt
            └── ...
```

重启 AstrBot 或在 WebUI 中重载插件。AstrBot 应根据 `requirements.txt` 安装依赖；如需手动安装：

```bash
pip install "aiohttp>=3.9.0,<4.0.0"
```

### 3. 完成基础配置

在 AstrBot WebUI 的插件配置中至少确认以下项目：

| 配置项 | 建议值 | 说明 |
|---|---|---|
| `base_url` | `http://127.0.0.1:6001` | adobe2api 服务地址，不要带末尾 `/` |
| `api_key` | 按服务端配置填写 | 以 Bearer Token / `X-API-Key` 发送 |
| `daily_limit` | `5` | 普通用户每天成功生成图片的次数 |
| `default_resolution` | `2k` | 默认或固定分辨率 |
| `resolution_mode` | `fixed` | 固定分辨率更稳定；也可设为 `llm` |

### 4. 测试

```text
/gpt图 一只在樱花树下睡觉的橘猫
```

## 使用方法

### 指令

| 指令 | 说明 |
|---|---|
| `/gpt图 <描述>` | 文生图；消息中带图时自动切换为改图 |
| `/gptimage <描述>` | `/gpt图` 的英文别名 |
| `/gimg <描述>` | `/gpt图` 的简写别名 |
| `/gpt改图 <说明>` | 强制改图，必须附图或引用带图消息 |
| `/gpt编辑 <说明>` | `/gpt改图` 的别名 |
| `/gedit <说明>` | `/gpt改图` 的英文别名 |
| `/改图 <说明>` | `/gpt改图` 的中文别名 |
| `/gpt图次数` | 查看当天剩余次数 |
| `/gpt图帮助` | 查看插件内置帮助 |

中文指令与描述之间可以不加空格，例如：

```text
/gpt图给她换上球衣
```

### 可选参数

| 参数 | 说明 |
|---|---|
| `--ratio 16:9` | 指定画幅，覆盖自动选择结果 |
| `--aspect 16:9` | `--ratio` 的等价写法 |
| `-r 16:9` | `--ratio` 的简写 |
| `比例:16:9` | 中文画幅写法 |
| `--no-auto` | 禁用 LLM 自动选画幅，使用配置默认值 |
| `--no-audit` | 跳过 LLM 审核，仅管理员生效 |

用户不能通过指令覆盖分辨率；分辨率统一由 `resolution_mode` 与 `default_resolution` 控制。

### 示例

**文生图**

```text
/gpt图 一只在樱花树下睡觉的橘猫，柔和晨光，摄影风格
/gpt图 --ratio 9:16 竖版赛博朋克城市夜景
/gpt图 比例:21:9 电影感沙漠全景
```

**改图**

```text
# 同一条消息附上图片
/gpt改图 把背景换成海边，保留人物姿势

# 回复一条带图消息
/gpt编辑 改成水彩插画风格
```

`/gpt图` 检测到参考图时也会自动进入改图流程。未手动指定画幅时，插件会优先根据第一张参考图的宽高比选择最接近的支持画幅，减少图片变形。

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

- `1:1`
- `5:4`
- `9:16`
- `21:9`
- `16:9`
- `4:3`
- `3:2`
- `4:5`
- `3:4`
- `2:3`

模型 ID 中的画幅使用 `x`，例如：

```text
firefly-gpt-image-2k-16x9
firefly-gpt-image-4k-1x1
```

> 最终画质、可用模型与生成稳定性取决于 adobe2api 及其上游配置。

## 配置说明

### 基础与模型参数

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `base_url` | `http://127.0.0.1:6001` | adobe2api 服务地址 |
| `api_key` | 空 | adobe2api API Key |
| `daily_limit` | `5` | 普通用户每日成功生成次数；`0` 表示禁止普通用户使用 |
| `max_ref_images` | `3` | 最大参考图数量，实际范围 `1～8` |
| `resolution_mode` | `fixed` | `fixed`：固定分辨率；`llm`：由 LLM 选择 |
| `default_resolution` | `2k` | 固定分辨率或 LLM 失败时的回退值 |
| `default_aspect_ratio` | `1:1` | 默认画幅或 LLM 失败时的回退值 |
| `auto_select_aspect_ratio` | `true` | 是否使用 LLM 自动选择画幅 |
| `show_meta` | `true` | 结果中显示分辨率、画幅等信息 |

`auto_select_size` 是旧版配置名，仅为向后兼容保留；新配置请使用 `auto_select_aspect_ratio`。

### 审核

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `enable_audit` | `true` | 启用输入审核 |
| `enable_keyword_filter` | `true` | 启用关键词预检 |
| `audit_provider_id` | 空 | 审核和参数选择使用的独立 LLM Provider ID |
| `audit_prompt` | 内置提示词 | 审核与参数选择系统提示词 |
| `audit_strict` | `false` | 对政治擦边内容更严（可能误杀娱乐内容），默认关闭 |
| `audit_failure_policy` | `block` | LLM 审核失败时的处理策略 |
| `llm_timeout` | `45` | LLM 分析超时，单位：秒 |

`audit_failure_policy` 可选值：

- `block`：审核服务异常时拒绝请求，默认值，适合公开部署
- `keyword_only`：仅执行关键词预检，通过后继续
- `allow`：审核服务异常时直接放行，不建议公开部署使用

#### 使用独立审核模型（推荐）

默认使用当前会话模型完成审核和参数选择。如果主模型较慢，可在 AstrBot WebUI 的服务提供商页面新增一个响应较快的小模型，并将其 `provider_id` 填入 `audit_provider_id`。

这样可以保持主对话模型不变，同时缩短生图前的等待时间。指定 Provider 不存在时，插件会自动回退到当前会话模型。

### 请求与重试

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `request_timeout` | `300` | 单次生图请求超时，单位：秒 |
| `max_retries` | `1` | 可重试错误的额外重试次数，默认最多请求 2 次 |
| `retry_backoff` | `2` | 线性重试间隔基数，单位：秒 |
| `max_output_bytes` | `31457280` | 生成结果最大体积，默认 30 MiB |

### 权限与额度

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `permission_mode` | `all` | `all` / `admin` / `whitelist` |
| `allowed_users` | 空 | 白名单用户 ID，使用逗号或换行分隔 |
| `denied_users` | 空 | 个人黑名单用户 ID，优先级最高（管理员除外），使用逗号或换行分隔 |
| `command_alias` | 空 | 额外指令别名，使用逗号分隔 |

额度规则：

1. 普通用户每天最多成功生成 `daily_limit` 次，按东八区自然日重置。
2. `event.is_admin()` 返回真的 AstrBot 全局管理员不受次数限制。
3. 审核拒绝、参数错误、参考图无效或 API 失败不会扣除次数。
4. 生成成功后扣除 1 次，并在结果中显示剩余额度。

额度记录保存在：

```text
data/plugin_data/astrbot_plugin_gpt_image/daily_quota.json
```

### 参考图与安全

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `napcat_hosts` | `127.0.0.1 localhost ::1` | 允许访问的 NapCat 本机 `host[:port]` 白名单，**强烈建议写明端口**（如 `127.0.0.1:3000 localhost:3000`） |
| `image_host_suffixes` | `qpic.cn qq.com myqcloud.com gtimg.cn` | 允许的图床域名后缀 |
| `allow_public_http` | `false` | 是否允许通过 HTTP 下载公网图片，不建议开启 |
| `allowed_media_dirs` | 空 | 允许读取的额外本地媒体目录 |
| `max_single_image_bytes` | `15728640` | 单张参考图最大体积，默认 15 MiB |

默认已允许插件自身 `plugin_data` 目录、AstrBot 的 `temp`、`cache` 子目录及 NapCat 系统缓存目录；不再默认包含整个 AstrBot data 根目录，避免跨插件图片泄露。请仅添加可信子目录，不要将 `/`、用户家目录或整个磁盘加入白名单（会被自动忽略）。

## 常见问题

### 指令被主 Agent 接走，插件日志中没有 `[gpt_image]`

- 建议使用带 `/` 的完整指令，例如 `/gpt图 ...`
- NapCat 消息格式建议使用 `array`
- 改图时将图片和指令放在同一条消息中，或回复一条带图消息
- 中文指令支持无空格写法，例如 `/gpt图给她换装`

### 提示“未配置 adobe2api base_url”

检查插件配置中的 `base_url`，确认地址可从 AstrBot 所在环境访问，并且末尾没有多余的 `/`。

### 返回 401 或 403

确认 `api_key` 与 adobe2api 服务端配置一致。插件会同时发送：

```text
Authorization: Bearer <api_key>
X-API-Key: <api_key>
```

### 生图超时或服务繁忙

- 适当提高 `request_timeout`，建议范围 `180～600`
- 检查 adobe2api 日志和上游服务状态
- 按需调整 `max_retries` 与 `retry_backoff`
- 若审核阶段较慢，为 `audit_provider_id` 配置一个更快的模型

### 改图时提示参考图无效

- 确认消息中确实附带图片，或引用的消息包含图片
- 检查图片是否超过 `max_single_image_bytes`
- 检查图片域名、本机地址或本地目录是否在对应白名单中
- 公网 HTTP 图片默认被拒绝，优先使用 HTTPS

## 版本记录

- **v1.5.5**：增加 SSRF 防护、本地路径白名单、图片体积限制、额度原子预留、LLM 超时控制、审核失败策略与日志脱敏
- **v1.5.0**：加强 QQ 图床与 Data URL 参考图处理，完善 adobe2api 图生图支持
- **v1.4.x**：增加改图指令并调整审核策略
- **v1.3.0**：用户原文直接作为 prompt，LLM 仅负责审核与模型参数选择
- **v1.0.0**：首次发布

## 相关项目

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [leik1000/adobe2api](https://github.com/leik1000/adobe2api)

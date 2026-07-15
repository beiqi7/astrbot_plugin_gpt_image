# astrbot_plugin_gpt_image

AstrBot 生图插件：专门对接 [leik1000/adobe2api](https://github.com/leik1000/adobe2api) 的 **Firefly GPT Image** 模型族（`firefly-gpt-image-{resolution}-{ratio}`）。

## 核心逻辑

```text
用户原文  ──►  生图/改图 prompt（原语言原样，LLM 不改写）
     │
     ├──►  （可选）消息/引用中的图片 → 参考图
     │
     └──►  LLM 仅做：审核 + 选择 resolution / aspect_ratio
              └──►  model = firefly-gpt-image-{res}-{ratio}
                        └──►  adobe2api
                              无图: /v1/images/generations
                              有图: /v1/chat/completions + image_url（图生图/改图）
```

## 功能

- **文生图**：`/gpt图 <描述>`
- **改图 / 图生图**：
  - `/gpt改图 <修改说明>` + 附图，或回复一张图
  - `/gpt图` 消息里带图时也会自动走图生图
- **LLM 只负责审核 + 选模型参数**，不改写用户描述
- **输入审核**、**每日次数限制**、LLM Tool（`gpt_image_generate` / `gpt_image_edit`）

> 画质由 adobe2api / 反代底层配置。

## 支持的模型 ID

命名：`firefly-gpt-image-{resolution}-{ratio}`

| 分辨率 | 画幅后缀 |
|--------|----------|
| `1k` / `2k` / `4k` | `1x1` `5x4` `9x16` `21x9` `16x9` `4x3` `3x2` `4x5` `3x4` `2x3` |

示例：`firefly-gpt-image-2k-16x9`、`firefly-gpt-image-4k-1x1`

## 安装

1. 将本目录放到 AstrBot 的 `data/plugins/` 下
2. 依赖：`aiohttp`、`aiofiles`
3. 确保已部署 adobe2api
4. WebUI 填写 `base_url`、`api_key`，按需改 `daily_limit`

## 配置项

| 配置 | 说明 | 默认 |
|------|------|------|
| `base_url` | adobe2api 地址 | `http://127.0.0.1:6001` |
| `api_key` | API Key | 空 |
| `daily_limit` | 普通用户每日成功生图次数 | `5` |
| `default_resolution` | LLM 失败回退分辨率 | `2k` |
| `default_aspect_ratio` | LLM 失败回退比例 | `1:1` |
| `auto_select_size` | LLM 自动选尺寸 | `true` |
| `enable_audit` | 输入审核 | `true` |
| `audit_provider_id` | 审核/选尺寸使用的独立 LLM ID | 空（用当前会话模型） |
| `request_timeout` | 生图超时（秒） | `300` |
| `permission_mode` | `all` / `admin` / `whitelist` | `all` |

额度文件位置：`data/plugin_data/astrbot_plugin_gpt_image/daily_quota.json`

### 审核用独立 LLM（推荐）

默认情况下，插件用**当前会话正在使用的对话模型**做审核 + 选分辨率。如果这个模型比较重（比如 GPT-4/Claude），会给每次生图前多加 3～8 秒等待。

推荐在 WebUI → 服务提供商 里额外配置一个响应快的小模型（例如 Qwen 4B/8B、GLM-4-Flash、通义千问 turbo 等），然后把它的 `provider_id` 填到 `audit_provider_id`。这样：

- 主对话仍用原来的模型
- 生图审核走这个专用小模型 → 显著减少 `/gpt图` 到实际请求 adobe2api 之间的空档

留空则回退到当前会话模型。指定的 provider 若查找失败也会自动回退。

## 指令

| 指令 | 说明 |
|------|------|
| `/gpt图 <描述>` | 文生图；带图时自动改图 |
| `/gpt改图 <说明>` | 强制改图（必须附图或回复图） |
| `/gpt编辑` `/gedit` `/改图` | 改图别名 |
| `/gpt图次数` | 今日剩余次数 |
| `/gpt图帮助` | 帮助 |

### 可选参数

```text
--ratio 16:9
--res 4k
--no-auto
--no-audit          # 仅管理员
--model firefly-gpt-image-2k-16x9
```

### 示例

```text
/gpt图 一只在樱花树下睡觉的橘猫
/gpt图 --ratio 9:16 --res 4k 竖版赛博朋克夜景
/gpt图次数
```

## 次数规则

1. 普通用户：每天最多 `daily_limit` 次成功生图
2. `event.is_admin()` 为真的全局管理员：不限次
3. 审核拒绝、参数错误、API 失败：**不扣次**
4. 生成成功后扣 1 次，并在结果里显示剩余

## 版本

- `v1.5.0` 图生图取图加固（QQ 图床/data URL）；实测 adobe2api i2i 通过
- `v1.4.x` 改图指令、政治审核收紧、娱乐放宽
- `v1.3.0` 用户原文=prompt；LLM 仅审核+选模型
- `v1.0.0` 首版

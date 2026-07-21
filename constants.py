"""常量与 GPT Image 模型目录。"""

from __future__ import annotations

# adobe2api firefly-gpt-image 支持的画幅（见 core/models/catalog.py）
GPT_IMAGE_RATIOS: dict[str, str] = {
    "1:1": "1x1",
    "5:4": "5x4",
    "9:16": "9x16",
    "21:9": "21x9",
    "16:9": "16x9",
    "3:2": "3x2",
    "4:3": "4x3",
    "4:5": "4x5",
    "3:4": "3x4",
    "2:3": "2x3",
}

RESOLUTIONS = ("1k", "2k", "4k")

MODEL_PREFIX = "firefly-gpt-image"


def build_model_id(resolution: str, aspect_ratio: str) -> str:
    res = (resolution or "2k").strip().lower()
    if res not in RESOLUTIONS:
        res = "2k"
    ratio = (aspect_ratio or "1:1").strip()
    suffix = GPT_IMAGE_RATIOS.get(ratio)
    if not suffix:
        # 兼容 16x9 / 16-9
        normalized = ratio.lower().replace("x", ":").replace("-", ":")
        suffix = GPT_IMAGE_RATIOS.get(normalized)
        if suffix:
            ratio = normalized
        else:
            ratio = "1:1"
            suffix = "1x1"
    return f"{MODEL_PREFIX}-{res}-{suffix}"


def parse_ratio_token(token: str) -> str | None:
    raw = (token or "").strip().lower().replace("x", ":").replace("-", ":")
    if raw in GPT_IMAGE_RATIOS:
        return raw
    return None


def nearest_ratio(width: int, height: int) -> str:
    """
    根据宽高像素值，从 GPT_IMAGE_RATIOS 里挑最接近的比例。
    比较 log 空间下的差距，避免"横竖颠倒时误挑到反向比例"。
    """
    import math

    try:
        w = float(width)
        h = float(height)
    except Exception:
        return "1:1"
    if w <= 0 or h <= 0:
        return "1:1"

    target = math.log(w / h)
    best = "1:1"
    best_diff = float("inf")
    for ratio in GPT_IMAGE_RATIOS.keys():
        try:
            a, b = ratio.split(":")
            rv = float(a) / float(b)
        except Exception:
            continue
        diff = abs(math.log(rv) - target)
        if diff < best_diff:
            best_diff = diff
            best = ratio
    return best


def parse_resolution_token(token: str) -> str | None:
    raw = (token or "").strip().lower().replace(" ", "")
    mapping = {
        "1k": "1k",
        "1": "1k",
        "hd": "1k",
        "2k": "2k",
        "2": "2k",
        "fhd": "2k",
        "4k": "4k",
        "4": "4k",
        "uhd": "4k",
        "ultra": "4k",
    }
    return mapping.get(raw)


# 关键词预检：仅针对政治 / 分裂 / 暴恐 / 邪教（不含娱乐明星）
# 匹配前会做规范化（去空白/符号/零宽字符），降低「东 突」「东*突」等绕过。
SENSITIVE_KEYWORDS: tuple[str, ...] = (
    # —— 现任/历任领导人及近亲属（政治）——
    "习近平",
    "xijinping",
    "xi jinping",
    "xjp",
    "习仲勋",
    "彭丽媛",
    "毛泽东",
    "毛澤東",
    "邓小平",
    "鄧小平",
    "江泽民",
    "江澤民",
    "胡锦涛",
    "胡錦濤",
    "李克强",
    "周恩来",
    "周恩來",
    "薄熙来",
    "王岐山",
    "栗战书",
    "汪洋",
    "赵乐际",
    "韩正",
    "蔡奇",
    "丁薛祥",
    "李强总理",
    # —— 政治历史事件 ——
    "六四",
    "64事件",
    "六四事件",
    "八九学运",
    "天安门事件",
    "天安门大屠杀",
    "天安门母亲",
    "tiananmen",
    "tankman",
    "tank man",
    "坦克人",
    "文化大革命",
    "文革屠杀",
    "大跃进饥荒",
    # —— 分裂 / 疆独相关（重点：东突及变体）——
    "东突",
    "東突",
    "东突厥",
    "東突厥",
    "东突厥斯坦",
    "東突厥斯坦",
    "东土耳其斯坦",
    "東土耳其斯坦",
    "eastturkestan",
    "eastturkistan",
    "east turkestan",
    "east turkistan",
    "etim",
    "tip组织",
    "世界维吾尔代表大会",
    "世维会",
    "worlduyghur",
    "热比娅",
    "熱比婭",
    "台独",
    "臺獨",
    "台湾独立",
    "台灣獨立",
    "台湾建国",
    "台灣建國",
    "两个中国",
    "兩個中國",
    "一中一台",
    "青天白日",
    "中华民国",
    "中華民國",
    "蔡英文",
    "赖清德",
    "賴清德",
    "陈水扁",
    "陳水扁",
    "李登辉",
    "李登輝",
    "台独旗帜",
    "台獨旗幟",
    "藏独",
    "藏獨",
    "疆独",
    "疆獨",
    "港独",
    "港獨",
    "香港独立",
    "香港獨立",
    "西藏独立",
    "西藏獨立",
    "新疆独立",
    "新疆獨立",
    "新疆分裂",
    "taiwanindependence",
    "taiwan independence",
    "free taiwan",
    "freetaiwan",
    "freetibet",
    "free tibet",
    "南蒙古独立",
    "内蒙古独立",
    "光复香港",
    "時代革命",
    "时代革命",
    "占中运动",
    "雨伞革命",
    # —— 邪教 / 恐怖 ——
    "法轮功",
    "法輪功",
    "falungong",
    "falun",
    "轮子功",
    "全能神",
    "isis",
    "isil",
    "daesh",
    "基地组织",
    "alqaeda",
    "al-qaeda",
    "塔利班",
    "东伊运",
    "東伊運",
    # —— 煽动攻击体制 ——
    "颠覆国家",
    "颠覆政权",
    "推翻共产党",
    "打倒共产党",
    "反共暴动",
    "颜色革命",
    "分裂国家",
    "煽动颠覆",
    "共产党下台",
    "中共下台",
    "亡我中华",
    # —— 其他高敏政治符号 ——
    "达赖喇嘛",
    "藏独达赖",
    "民运领袖",
    "六四坦克",
)

ANALYZE_SYSTEM_PROMPT = """你是图片生成请求的「审核 + 模型参数选择」助手。只输出一个 JSON 对象，不要 Markdown，不要解释。

职责边界（必须遵守）：
1. 你只负责中国大陆政治安全审核。文娱、体育、二次元、色情尺度、暴力场面、隐私、未成年等类别不是你的职责，一律放行，不要因此拒绝。
2. 根据画面意图选择 resolution 与 aspect_ratio。
3. 禁止改写、翻译、润色、扩写用户原文。系统会把用户原文原样作为生图 prompt。
4. 不要输出任何用于生图的 prompt 正文（不要 cleaned_prompt 等字段）。

输出 JSON schema（仅这些字段）：
{
  "allowed": true/false,
  "reason": "若拒绝，用简短中文说明原因；通过可为空字符串",
  "resolution": "1k" | "2k" | "4k",
  "aspect_ratio": "1:1" | "5:4" | "9:16" | "21:9" | "16:9" | "4:3" | "3:2" | "4:5" | "3:4" | "2:3"
}

【需要拒绝 - 仅限中国大陆政治红线，从严】：
- 中国大陆现任或历任最高领导人（习近平、毛泽东、邓小平、江泽民、胡锦涛等）的恶搞、讽刺、丑化、色情化形象。
- 【台湾问题重点从严】凡涉及下列内容一律拒绝：
  · 台独、台湾独立、中华民国国旗/国徽、青天白日旗（作为政治象征）、台独旗帜/符号；
  · 台湾是「国家」、台湾「建国」、台湾入联、两个中国、一中一台；
  · 蔡英文、赖清德、陈水扁、李登辉等台湾政治人物的政治宣传、选举造势、独立主张相关形象；
  · 美日等外部势力武力护台、武统台湾的政治对抗宣传（纯军事/科幻架空无政治攻击意图除外）；
  · 谐音/拆字/英文缩写遮掩的台独表述（如「台*独」「ROC independence」等）。
- 其他分裂：藏独、疆独、港独、东突/东突厥斯坦/ETIM/世维会、南蒙独立等宣传或象征。
- 暴恐：恐怖组织标志、实施恐怖袭击、极端主义宣传。
- 邪教：法轮功等。
- 煽动颠覆政权、推翻共产党、颜色革命。
- 六四、天安门事件相关暴力政治场景。
- 用户用空格、符号、谐音、拆字、英文缩写等方式遮掩上述内容时同样拒绝。

【全部放行 - 不要因为以下内容拒绝】：
- 文娱：娱乐明星、演员、歌手、偶像、主播的写真、舞台、二创、卡通化、换装、壁纸、CP 向、同人，无论多有名气。
- 体育：运动员、赛事、球星、奥运、电竞相关任何创作。
- 二次元：动漫角色、原神/崩铁/鸣潮等游戏角色、Vtuber、同人、美少女、机甲、日常插画。
- 普通恋爱、时尚、美食、风景、科幻、产品图、抽象艺术。
- 色情、性感、暴露、内衣、泳装等成人向内容（不归你管）。
- 暴力、打斗、武器、血腥等（不归你管）。
- 真人照片、真人改图、AI 换脸（不归你管）。
- 外国政治人物（特朗普、拜登、普京等）的任何创作。
- 台湾风景、美食、城市夜景、普通人物写真等纯生活/旅游内容，且无独立/国家象征 → 放行。
- 模棱两可、看不清意图的内容 → 放行（政治红线除外）。

判定原则：
- 中国政治（尤其台湾问题）从严；文娱/体育/二次元从松。
- 默认放行，只在明确命中上述政治红线时才拒绝。
- 娱乐圈/体育明星/二次元角色不是政治人物，放行。
- 外国政治不归你管，放行。
- 台湾相关：有政治/独立/国家象征 → 拒；纯旅游风景/美食/日常 → 放。

分辨率选择：
- 1k：简单图标、emoji、小头像、草稿
- 2k：绝大多数场景（默认）
- 4k：海报、印刷、大场景、用户明确要求高清/4K

画幅：1:1 头像；9:16/2:3/3:4/4:5 竖图；16:9/21:9/3:2/4:3 横图；5:4 传统照片。
用户已明确比例或分辨率时优先尊重。
"""

HELP_TEXT = """🎨 GPT Image 生图 / 改图

文生图：
  /gpt图 <描述>
  /gptimage · /gimg

改图（需附图或回复一张图）：
  /gpt改图 <修改说明>
  /gpt编辑 · /gedit · /改图

其它：
  /gpt图次数
  /gpt图帮助

可选参数：
  --ratio 16:9     指定画幅（覆盖自动选择）
  --no-auto        禁用 LLM 自动选画幅，用配置默认值

分辨率策略由管理员配置：可固定为 1K/2K/4K，或由 LLM 自动选择；
用户不能手动覆盖分辨率。

示例：
  /gpt图 一只在樱花树下睡觉的橘猫
  同一条消息：图片 + /gpt改图 改成水彩风格
  回复带图消息：/gpt改图 把背景换成海边

NapCat + AstrBot 说明：
  · 同一条消息：【图片】+ /gpt图给她换上球衣（中文后可以没有空格）
  · 或回复带图消息再发 /gpt改图 ...
  · 若只看到机器人文字回复、插件日志无 [gpt_image]，说明指令没被插件接到
  · 消息格式用 array；用户原文直接作为 prompt
"""




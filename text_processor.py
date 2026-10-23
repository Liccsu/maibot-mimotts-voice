"""MiMo TTS 文本预处理。

只做纯文本处理，不依赖 maibot_sdk / aiohttp，方便单独测试。

处理内容包括：
1. 唱歌意图识别，并把 ``(唱歌)`` 标签归位到文本最开头（MiMo 只有在开头才生效）。
2. 清洗 AI 回复里不适合朗读的内容：Markdown 标记、代码块、URL、emoji、颜文字。
3. 括号内容分类：短括号（``（叹气）``）保留为音频标签；长括号（舞台说明）剔除。
4. 标点规范化与自动断句，避免长句一口气读完导致语速失真。
5. 按句子切分成适合单次合成的片段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "PreparedSpeech",
    "STYLE_WORDS",
    "contains_sing_intent",
    "prepare_speech",
    "clean_text",
    "split_chunks",
]


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

# MiMo 文档中列出的风格标签词表。命中的标签会被统一提到文本最开头。
STYLE_WORDS: frozenset[str] = frozenset(
    {
        # 基础情绪
        "开心", "悲伤", "愤怒", "恐惧", "惊讶", "兴奋", "委屈", "平静", "冷漠",
        # 复合情绪
        "怅然", "欣慰", "无奈", "愧疚", "释然", "嫉妒", "厌倦", "忐忑", "动情",
        # 整体语调
        "温柔", "高冷", "活泼", "严肃", "慵懒", "俏皮", "深沉", "干练", "凌厉",
        # 音色定位
        "磁性", "醇厚", "清亮", "空灵", "稚嫩", "苍老", "甜美", "沙哑", "醇雅",
        # 人设腔调
        "夹子音", "御姐音", "正太音", "大叔音", "台湾腔",
        # 方言
        "东北话", "四川话", "河南话", "粤语",
        # 唱歌
        "唱歌", "sing", "singing",
    }
)

# 唱歌意图关键词（用于自然语言风格指令里的兜底判断）
_SING_INTENT_RE = re.compile(
    r"(唱歌|唱一首|唱首|唱个|演唱|歌唱|哼唱|sing\s*a\s*song|\bsing\b|\bsinging\b)",
    re.IGNORECASE,
)

# 任意位置出现的唱歌标签
_SING_TAG_RE = re.compile(
    r"[（(\[【]\s*(?:唱歌|sing|singing|唱首歌|歌唱)\s*[）)\]】]",
    re.IGNORECASE,
)

_BRACKET_PAIRS = {"（": "）", "(": ")", "[": "]", "【": "】"}

_OPEN_BRACKETS = "（([【"
_CLOSE_BRACKETS = "）)]】"
_PUNCT_CHARS = "，。！？；：、…,.!?;:～~—-"

_KAOMOJI_CHARS = set("^_▽ω・´`∀≧≦╯╰ノ°Дﾟд⊙≖⌒╥〃′‵＊*")

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U0001f000-\U0001f2ff"
    "\U00002600-\U000027bf"
    "\U00002b00-\U00002bff"
    "\U0000fe0f\U0000200d\U000020e3"
    "\U00002190-\U000021ff"
    "\U000024c2"
    "\U0000203c\U00002049"
    "]+",
    flags=re.UNICODE,
)

_URL_RE = re.compile(r"(?:https?|ftp)://[^\s，。！？；、）)\]】]+", re.IGNORECASE)
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<[^>]{1,40}>")
_EMPHASIS_RE = re.compile(r"(\*\*|__|~~|\*|_)(?=\S)(.+?)(?<=\S)\1")
_LINE_PREFIX_RE = re.compile(r"^[ \t]*(?:#{1,6}\s*|>\s*|[-*+]\s+|\d{1,2}[.)]\s+|[-=*_]{3,}\s*$)", re.MULTILINE)

_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_MULTI_SPACE_RE = re.compile(r"[ \t\u3000]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{2,}")

_SENT_END = "。！？!?；;…\n"

# 语气词：优先在这里断句，停顿更自然
_FINAL_PARTICLES = "吧了呢啊吗呀嘛哦啦呗"

# 唱歌时常见的多余前缀，如「唱：」「歌词：」「好的，我来唱：」
_SING_PREFIX_RE = re.compile(
    r"^(?:好的[，,、]?\s*)?"
    r"(?:歌\s*词\s*[：:]|唱\s*歌\s*[：:]|唱\s*[：:]|我?来?唱(?:一?首)?\s*[：:,，、])"
    r"\s*"
)

# 开头的歌名元信息，如「《逆战》」「《逆战》- 张杰」。只认书名号且锚定开头，
# 不做「歌手名」的猜测——猜错会吃掉真正的歌词首句，宁可多留两个字。
_SING_META_RE = re.compile(
    r"^[《【][^》】]{1,30}[》】](?:\s*[-—–]\s*[^，,。\n\s]{1,10})?[\s，,。:：、]*"
)


@dataclass
class PreparedSpeech:
    """预处理结果。"""

    text: str
    kind: str = "speak"  # "speak" | "sing"
    style_tags: list[str] = field(default_factory=list)
    chunks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # 只剩括号标签（如「（笑）」）视为无效
        return _has_spoken_content(self.text)


def _has_spoken_content(text: str) -> bool:
    """去掉所有括号段后，是否还存在真正要念出来的文字。"""
    if not text:
        return False
    rest: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        close = _BRACKET_PAIRS.get(ch)
        if close is not None:
            end = text.find(close, i + 1)
            if end != -1:
                i = end + 1
                continue
        rest.append(ch)
        i += 1
    body = "".join(rest)
    return bool(_CJK_RE.search(body) or re.search(r"[A-Za-z0-9]", body))


# --------------------------------------------------------------------------
# 括注分类
# --------------------------------------------------------------------------


def _looks_like_kaomoji(inner: str) -> bool:
    return any(ch in _KAOMOJI_CHARS for ch in inner)


def _looks_like_tag(inner: str) -> bool:
    """短、纯文字、无颜文字特征的括号内容视为音频标签。"""
    core = inner.strip()
    if not core:
        return False
    if _looks_like_kaomoji(core):
        return False
    if len(core) > 12:
        return False
    # 只允许中文、字母、数字、空白和少量分隔符
    return bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9\s,，、·/\-]{1,12}", core))


def _rewrite_brackets(text: str, keep_all: bool = False) -> tuple[str, list[str]]:
    """保留短标签，剔除长旁白。返回 (新文本, 被剔除的旁白列表)。

    Args:
        keep_all: 为 True 时不做剔除，原样保留所有括号内容。
    """
    out: list[str] = []
    dropped: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        close = _BRACKET_PAIRS.get(ch)
        if close is None:
            # 落单的右括号直接丢掉，避免残留噪声
            if ch not in _CLOSE_BRACKETS:
                out.append(ch)
            i += 1
            continue
        end = text.find(close, i + 1)
        if end == -1:
            # 未闭合的开括号：丢掉括号本身，保留后面的内容
            i += 1
            continue
        inner = text[i + 1 : end]
        if keep_all or _looks_like_tag(inner):
            out.append(ch + inner + close)
        else:
            # 长旁白 / 颜文字：整段丢掉，避免被念出来
            if inner.strip():
                dropped.append(inner.strip())
            elif _looks_like_kaomoji(inner):
                dropped.append(inner)
        i = end + 1
    return "".join(out), dropped


# --------------------------------------------------------------------------
# 清洗
# --------------------------------------------------------------------------


def clean_text(text: str, *, strip_narration: bool = True) -> tuple[str, list[str]]:
    """清洗文本，返回 (清洗结果, 处理说明)。"""
    notes: list[str] = []
    if not text:
        return "", notes

    s = text.replace("\r\n", "\n").replace("\r", "\n")
    s = _ZERO_WIDTH_RE.sub("", s)

    s = _FENCED_CODE_RE.sub("", s)
    s = _URL_RE.sub("", s)
    s = _MD_IMAGE_RE.sub("", s)
    s = _MD_LINK_RE.sub(r"\1", s)
    s = _HTML_TAG_RE.sub("", s)
    s = _INLINE_CODE_RE.sub(r"\1", s)
    s = _EMPHASIS_RE.sub(r"\2", s)
    s = _LINE_PREFIX_RE.sub("", s)

    cleaned_emoji = _EMOJI_RE.sub("", s)
    if cleaned_emoji != s:
        notes.append("已移除 emoji")
        s = cleaned_emoji

    if strip_narration:
        s, dropped = _rewrite_brackets(s)
        if dropped:
            notes.append(f"已移除 {len(dropped)} 处旁白/颜文字")
    else:
        s, _ = _rewrite_brackets(s, keep_all=True)

    # 标点规范化
    s = s.replace("。。。", "……").replace("...", "……").replace("．．．", "……")
    s = re.sub(r"…{2,}", "……", s)
    s = re.sub(r"[~～]{2,}", "～", s)
    s = re.sub(r"-{2,}", "——", s)
    s = re.sub(r"[，,]{2,}", "，", s)
    s = re.sub(r"[。]{2,}", "。", s)
    s = re.sub(r"[！!]{2,}", "！", s)
    s = re.sub(r"[？?]{2,}", "？", s)

    # 去掉中文之间多余的空格
    s = re.sub(r"(?<=[\u4e00-\u9fff])[ \t\u3000]+(?=[\u4e00-\u9fff])", "", s)
    s = _MULTI_SPACE_RE.sub(" ", s)
    s = _MULTI_NEWLINE_RE.sub("\n", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    s = s.strip()
    s = s.strip("，,、；;")

    return s, notes


def _soft_break(text: str, limit: int = 30) -> str:
    """在超长无标点片段中补逗号，给 TTS 换气点。

    优先断在「吧 / 了 / 呢」这类语气词之后，实在没有就在满额处硬断。
    """
    if limit <= 0 or not text:
        return text

    out: list[str] = []
    run = 0
    run_at_particle = 0
    depth = 0
    particle_idx = -1

    for ch in text:
        if ch in _OPEN_BRACKETS:
            depth += 1
            out.append(ch)
            continue
        if ch in _CLOSE_BRACKETS:
            depth = max(0, depth - 1)
            out.append(ch)
            continue

        out.append(ch)
        if depth > 0:
            continue
        if ch in _PUNCT_CHARS or ch == "\n":
            run = 0
            particle_idx = -1
            continue
        if not _CJK_RE.match(ch):
            continue

        run += 1
        if ch in _FINAL_PARTICLES:
            # 语气词是天然换气点：读够一定长度就顺势断开
            if run >= max(8, limit * 3 // 5):
                out.append("，")
                run = 0
                particle_idx = -1
                continue
            particle_idx = len(out)
            run_at_particle = run

        if run >= limit:
            if particle_idx != -1 and run - run_at_particle <= max(6, limit // 3):
                out.insert(particle_idx, "，")
                run = run - run_at_particle
            else:
                out.append("，")
                run = 0
            particle_idx = -1

    s = "".join(out)
    # 硬断可能刚好落在语气词前面，读起来会断字，往后挪一位
    s = re.sub(rf"，([{_FINAL_PARTICLES}])(?=[^，。！？…\s])", r"\1，", s)
    s = re.sub(rf"，([{_FINAL_PARTICLES}])(?=[，。！？…])", r"\1", s)
    return s


def _ensure_terminal(text: str) -> str:
    if not text:
        return text
    # 末尾可能跟着括号标签，例如「你走吧。（叹气）」，需要剥掉标签再看
    core = text
    while True:
        match = re.search(r"[（(\[【][^（()）\[\]【】]{1,12}[）)\]】]\s*$", core)
        if not match:
            break
        core = core[: match.start()]
    if core and core[-1] in "。！？!?…～~.；;":
        return text
    return text + "。"


# --------------------------------------------------------------------------
# 风格标签归位
# --------------------------------------------------------------------------


def _collect_style_tags(text: str) -> tuple[list[str], str]:
    """把可识别的风格标签从文中抽出（保持顺序、去重），返回 (标签, 剩余文本)。"""
    tags: list[str] = []
    rest: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        close = _BRACKET_PAIRS.get(ch)
        if close is not None:
            end = text.find(close, i + 1)
            if end != -1:
                inner = text[i + 1 : end].strip()
                parts = [p for p in re.split(r"[\s,，、/]+", inner) if p]
                if parts and all(p.lower() in STYLE_WORDS for p in parts):
                    for p in parts:
                        if p not in tags:
                            tags.append(p)
                    i = end + 1
                    continue
        rest.append(ch)
        i += 1
    return tags, "".join(rest)


# --------------------------------------------------------------------------
# 分段
# --------------------------------------------------------------------------


def split_chunks(text: str, max_chars: int = 180) -> list[str]:
    """按句切分并合并成不超过 max_chars 的片段，不会切断括号内部。"""
    if not text:
        return []
    max_chars = max(20, int(max_chars))

    sentences: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        buf.append(ch)
        if ch in _OPEN_BRACKETS:
            depth += 1
        elif ch in _CLOSE_BRACKETS:
            depth = max(0, depth - 1)
        if ch in _SENT_END and depth == 0:
            sentences.append("".join(buf))
            buf = []
    if buf:
        sentences.append("".join(buf))

    chunks: list[str] = []
    current = ""
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) > max_chars:
            # 单句过长，按逗号二次切分
            for piece in re.split(r"(?<=[，,、])", sent):
                piece = piece.strip()
                if not piece:
                    continue
                if len(current) + len(piece) > max_chars and current:
                    chunks.append(current)
                    current = piece
                else:
                    current += piece
                while len(current) > max_chars:
                    chunks.append(current[:max_chars])
                    current = current[max_chars:]
            continue
        if len(current) + len(sent) > max_chars and current:
            chunks.append(current)
            current = sent
        else:
            current += sent
    if current.strip():
        chunks.append(current.strip())

    return [c for c in (c.strip() for c in chunks) if c and (_CJK_RE.search(c) or re.search(r"[A-Za-z0-9]", c))]


# --------------------------------------------------------------------------
# 对外主函数
# --------------------------------------------------------------------------


def contains_sing_intent(text: str = "", style_instruction: str = "") -> bool:
    """判断是否要走唱歌模式。"""
    if text and _SING_TAG_RE.search(text):
        return True
    if style_instruction and _SING_INTENT_RE.search(style_instruction):
        return True
    return False


def prepare_speech(
    text: str,
    *,
    style_instruction: str = "",
    force_sing: bool = False,
    max_chars: int = 180,
    sing_max_chars: int = 400,
    clean: bool = True,
    strip_narration: bool = True,
    auto_punctuate: bool = True,
    punctuation_limit: int = 30,
) -> PreparedSpeech:
    """把 LLM 原始回复整理成可直接送 TTS 的文本。

    Args:
        text: 原始回复文本。
        style_instruction: 风格指令，仅用于判断唱歌意图。
        force_sing: 工具显式指定了唱歌模式。
        max_chars: 普通朗读单段上限。
        sing_max_chars: 演唱单段上限（过长会破坏旋律，所以给得更宽）。
    """
    notes: list[str] = []
    raw = text or ""

    sing = force_sing or contains_sing_intent(raw, style_instruction)
    if sing and not force_sing:
        notes.append("检测到唱歌意图")

    # 先摘出风格标签，避免清洗过程中被当作旁白删掉
    style_tags, body = _collect_style_tags(raw)

    # 移除残留的唱歌标签（稍后统一放到最前面）
    body = _SING_TAG_RE.sub("", body)

    if sing:
        # 歌名元信息趁空格还在时剥掉（清洗会压掉空格，之后歌手名会和歌词粘连）
        stripped = _SING_META_RE.sub("", body)
        if stripped != body:
            notes.append("已移除歌名/歌手元信息")
        body = stripped

    if clean:
        body, clean_notes = clean_text(body, strip_narration=strip_narration)
        notes.extend(clean_notes)
    else:
        body = body.strip()

    if sing:
        # 歌词按行连成一句，保持旋律连贯
        body = re.sub(r"\n+", "，", body)
        body = re.sub(r"[，,]{2,}", "，", body)
        body = _SING_PREFIX_RE.sub("", body)
        body = body.strip("，,、；; ")
        if auto_punctuate:
            body = _soft_break(body, punctuation_limit)
        body = _ensure_terminal(body)
        if body:
            lead = ["唱歌"] + [t for t in style_tags if t.lower() not in {"唱歌", "sing", "singing"}]
            body = "(" + " ".join(lead) + ")" + body
        chunk_limit = sing_max_chars
    else:
        if auto_punctuate:
            body = _soft_break(body, punctuation_limit)
            body = _ensure_terminal(body)
        chunk_limit = max_chars

        if style_tags:
            body = "(" + " ".join(style_tags) + ")" + body

    chunks = split_chunks(body, chunk_limit)
    return PreparedSpeech(
        text=body,
        kind="sing" if sing else "speak",
        style_tags=style_tags,
        chunks=chunks,
        notes=notes,
    )

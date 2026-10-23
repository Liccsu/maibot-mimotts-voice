"""AI Voice Service Plugin for MaiBot.

基于 MiMo-V2.5-TTS 的语音回复插件。

修复/优化要点见 README「更新日志」，核心是：
* 唱歌改用 ``mimo-v2.5-tts`` + ``(唱歌)`` 前置标签（clone 模型根本不支持唱歌）；
* 去掉空 user 消息，风格指令改为结构化拼接；
* 合成前清洗 Markdown / emoji / 旁白，自动补换气停顿，避免"语速奇怪的朗读"。
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from maibot_sdk import API, CONFIG_RELOAD_SCOPE_SELF, MaiBotPlugin, Tool, Field, PluginConfigBase
from maibot_sdk.types import ActivationType, ToolParameterInfo, ToolParamType

try:
    from .text_processor import PreparedSpeech, prepare_speech
    from .tts_service import MiMoTTSService
except ImportError:  # 以单文件方式加载时的兜底
    from text_processor import PreparedSpeech, prepare_speech
    from tts_service import MiMoTTSService


# --------------------------------------------------------------------------
# 情绪 / 语速 → 自然语言指令
# 用文档推荐的说法，比让模型自由发挥稳定得多
# --------------------------------------------------------------------------

EMOTION_PHRASES: dict[str, str] = {
    "开心": "语气开心愉悦，声音明亮上扬，句尾带着笑意",
    "兴奋": "语气兴奋激动，音量和音高都有起伏，藏不住的雀跃",
    "温柔": "语气温柔，声音柔和松弛，气息平稳，像在低声关心对方",
    "安慰": "语气温柔而笃定，节奏放慢，带着安抚人心的力量",
    "悲伤": "语气悲伤低落，气息下沉、略有断续，声音闷在胸腔里",
    "委屈": "语气委屈，鼻音略重，尾音发软发颤，像在忍着哭",
    "愤怒": "语气愤怒，声音压紧有力度，字字咬重，气息短促",
    "冷淡": "语气冷淡疏离，声音平直，起伏很小，不带情绪",
    "严肃": "语气严肃，声音沉稳有力，节奏稳，不容置疑",
    "俏皮": "语气俏皮活泼，语调轻快跳脱，带点小得意和小坏",
    "撒娇": "语气撒娇，声音软糯黏腻，尾音上扬拖长",
    "慵懒": "语气慵懒，声音松垮带点鼻音，语速慢，像刚睡醒",
    "惊讶": "语气惊讶，起音突然拔高，带出不敢相信的感觉",
    "无奈": "语气无奈，带着轻叹，尾音下坠，有点自嘲",
    "害羞": "语气害羞，声音放轻发抖，句尾越说越小声",
    "深情": "语气深情，声音低柔绵长，气息贴近，像在耳边说话",
}

SPEED_PHRASES: dict[str, str] = {
    "slow": "语速偏慢，句间留出自然的停顿，不要赶",
    "normal": "",
    "fast": "语速偏快，像急着把话说完，但咬字依然清晰",
}

SING_STYLE_PREFIX = "用自然流畅的歌声演唱这段歌词，旋律连贯、节奏稳定、咬字清晰，不要像朗读。"

# 唱歌专用：情绪落在「演唱表现」上，不能沿用说话那套「语气XX」
# 且只在调用方显式指定情绪时才使用，不继承 style.default_emotion
SING_EMOTION_PHRASES: dict[str, str] = {
    "开心": "唱得轻快明亮，句尾带笑意",
    "兴奋": "唱得热烈有推进感，但气息和音准要稳住",
    "温柔": "唱得柔和松弛，气息连绵不断",
    "安慰": "唱得轻缓温和，像在耳边低声安抚",
    "悲伤": "唱得低沉克制，尾音带一点叹息",
    "委屈": "唱得轻而发颤，情绪收着不外放",
    "愤怒": "唱得有力，咬字扎实，情绪层层递进",
    "冷淡": "唱得平直克制，几乎不带起伏",
    "严肃": "唱得沉稳端正，节奏规整",
    "俏皮": "唱得轻巧跳脱，节奏带点弹性",
    "撒娇": "唱得软糯一点，尾音轻轻上扬",
    "慵懒": "唱得松垮放缓，气息松散",
    "惊讶": "唱出突然的起伏，但不要破音",
    "无奈": "唱得带点叹息，尾音缓缓下坠",
    "害羞": "唱得轻而含蓄，音量收着",
    "深情": "唱得低柔绵长，气息贴近",
}

# 唱歌的「快慢」是节奏，不是语速，措辞要换
SING_SPEED_PHRASES: dict[str, str] = {
    "slow": "节奏放缓，气息拉长，不要赶拍",
    "normal": "",
    "fast": "节奏明快，但每个字都要唱清楚，不要含混",
}

# 唱歌未指定情绪时的默认基调：把控制权交回歌词
SING_NEUTRAL_HINT = "情绪随歌词本身自然表达，不要刻意夸张或用力过猛"

SEND_METHODS = ("auto", "record", "voice", "hybrid")
VOICE_MODES = ("preset", "clone", "design")


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    enabled: bool = Field(default=True, description="是否启用")
    config_version: str = Field(default="2.0.0", description="配置版本")


class VoiceSectionConfig(PluginConfigBase):
    __ui_label__ = "音色"
    mimo_api_key: str = Field(default="", description="MiMo API Key")
    api_base_url: str = Field(
        default="https://token-plan-cn.xiaomimimo.com/v1",
        description="MiMo API 地址",
    )
    voice_mode: str = Field(
        default="clone",
        description="语音模式：preset(预置音色) / clone(音色复刻) / design(文本设计音色)",
    )
    preset_voice: str = Field(default="mimo_default", description="预置音色 ID（preset 模式）")
    singing_voice: str = Field(
        default="mimo_default",
        description="唱歌使用的预置音色 ID。唱歌只能用预置音色，clone/design 模型不支持唱歌",
    )
    voice_design_prompt: str = Field(default="", description="文本设计音色的描述（design 模式）")
    voices_dir: str = Field(default="voices", description="音色样本目录（相对于插件目录）")
    default_voice: str = Field(default="", description="默认音色名称（clone 模式下为音频文件名）")
    clone_voice: str = Field(default="", description="优先使用的复刻音色文件名")
    fallback_to_preset: bool = Field(
        default=True, description="复刻音色不可用时是否回退到预置音色（关闭则直接报错）"
    )
    audio_format: str = Field(
        default="wav",
        description="返回音频格式。QQ 适配器对 wav 兼容最好，改成 mp3 可减小体积",
    )


class StyleSectionConfig(PluginConfigBase):
    __ui_label__ = "风格"
    default_emotion: str = Field(
        default="",
        description="默认情绪（如 温柔/俏皮），留空则不加情绪描述。仅作用于「说话」，唱歌不继承",
    )
    default_speed: str = Field(
        default="normal",
        description="默认语速：slow / normal / fast。仅作用于「说话」，唱歌时忽略",
    )
    extra_instruction: str = Field(
        default="",
        description="额外风格后缀，会拼到每次语音指令末尾，例如「像真人聊天，不要播音腔」",
    )


class SpeechSectionConfig(PluginConfigBase):
    __ui_label__ = "合成与发送"
    max_chars: int = Field(default=180, description="单段朗读字数上限，超出会分段发送")
    sing_max_chars: int = Field(default=400, description="单段演唱字数上限（过长会破坏旋律）")
    clean_text: bool = Field(default=True, description="合成前清洗 Markdown/emoji/URL 等噪声")
    strip_narration: bool = Field(default=True, description="移除长括号旁白（舞台说明），避免被念出来")
    auto_punctuate: bool = Field(default=True, description="自动补标点与换气停顿，长句不再一口气读完")
    punctuation_limit: int = Field(default=30, description="连续多少字没有标点时自动插入逗号")
    timeout: int = Field(default=120, description="单次请求超时（秒），唱歌建议 120 以上")
    max_retries: int = Field(default=2, description="失败重试次数（429/5xx/超时）")
    inter_message_delay: float = Field(default=0.6, description="分段发送之间的间隔（秒）")
    send_method: str = Field(
        default="auto", description="语音发送方式：auto / record / voice / hybrid"
    )


class VoicePluginConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    voice: VoiceSectionConfig = Field(default_factory=VoiceSectionConfig)
    style: StyleSectionConfig = Field(default_factory=StyleSectionConfig)
    speech: SpeechSectionConfig = Field(default_factory=SpeechSectionConfig)


# --------------------------------------------------------------------------
# 内部数据结构
# --------------------------------------------------------------------------


@dataclass
class VoicePlan:
    mode: str  # preset / clone / design
    voice_id: str = ""
    reference: str = ""
    reference_name: str = ""
    design_prompt: str = ""
    label: str = ""
    note: str = ""


# --------------------------------------------------------------------------
# 插件
# --------------------------------------------------------------------------


class AIVoicePlugin(MaiBotPlugin):
    config_model = VoicePluginConfig

    def __init__(self) -> None:
        super().__init__()
        self.tts_service: Optional[MiMoTTSService] = None
        self.voices: dict[str, str] = {}
        self.default_voice: str = ""
        self._tasks: set[asyncio.Task] = set()

    # -- 生命周期 ---------------------------------------------------------

    async def on_load(self) -> None:
        self._ensure_config_exists()
        self.ctx.logger.info("AI Voice Plugin loading...")
        api_key = self.config.voice.mimo_api_key
        if not api_key:
            self.ctx.logger.warning("MiMo API Key 未配置，语音功能不可用")
        self._rebuild_service()
        self.default_voice = self.config.voice.default_voice or self.config.voice.clone_voice
        await self._load_voices()
        self.ctx.logger.info(
            "AI Voice Plugin loaded: mode=%s default_voice=%s voices=%s singing_voice=%s",
            self.config.voice.voice_mode,
            self.default_voice,
            list(self.voices.keys()),
            self.config.voice.singing_voice,
        )

    async def on_unload(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if self.tts_service:
            await self.tts_service.close()
            self.tts_service = None
        self.voices.clear()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        self.ctx.logger.info("插件配置已更新: version=%s", version)
        self._rebuild_service()
        self.default_voice = self.config.voice.default_voice or self.config.voice.clone_voice
        await self._load_voices()
        self.ctx.logger.info(
            "配置重载完成: mode=%s default_voice=%s", self.config.voice.voice_mode, self.default_voice
        )

    def _rebuild_service(self) -> None:
        speech = self.config.speech
        if self.tts_service is None:
            self.tts_service = MiMoTTSService(
                api_key=self.config.voice.mimo_api_key,
                api_base_url=self.config.voice.api_base_url,
                logger=self.ctx.logger,
                timeout=speech.timeout,
                max_retries=speech.max_retries,
            )
        else:
            self.tts_service.update_api_key(self.config.voice.mimo_api_key)
            self.tts_service.update_api_base_url(self.config.voice.api_base_url)
            self.tts_service.update_timeout(speech.timeout, speech.max_retries)

    def _ensure_config_exists(self) -> None:
        """首次启动时从 config.example.toml 生成 config.toml，不覆盖已有配置。"""
        import shutil

        plugin_dir = Path(__file__).parent
        config_path = plugin_dir / "config.toml"
        example_path = plugin_dir / "config.example.toml"
        if config_path.exists():
            return
        if example_path.exists():
            shutil.copy2(example_path, config_path)
            self.ctx.logger.info("已由 config.example.toml 生成 config.toml")
        else:
            self.ctx.logger.warning("未找到 config.toml 与 config.example.toml")

    # -- 音色加载 ---------------------------------------------------------

    def _voices_path(self) -> Path:
        raw = Path(self.config.voice.voices_dir or "voices")
        return raw if raw.is_absolute() else Path(__file__).parent / raw

    async def _load_voices(self) -> None:
        voices_dir = self._voices_path()
        if not voices_dir.exists():
            voices_dir.mkdir(parents=True, exist_ok=True)
            self.ctx.logger.info("已创建音色目录: %s", voices_dir)
            return

        audio_files = sorted([p for p in voices_dir.iterdir() if p.suffix.lower() in (".wav", ".mp3")])
        if not audio_files:
            self.ctx.logger.warning("音色目录内没有 .wav/.mp3 文件: %s", voices_dir)
            return

        self.voices.clear()
        for path in audio_files:
            mime = "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg"
            try:
                raw = path.read_bytes()
            except OSError as exc:
                self.ctx.logger.error("读取音色文件失败 %s: %s", path.name, exc)
                continue
            b64 = base64.b64encode(raw).decode("ascii")
            if len(b64) > MiMoTTSService.MAX_REF_B64_BYTES:
                self.ctx.logger.warning(
                    "音色 %s 的 Base64 超过 10MB（%dKB），MiMo 会拒绝，请压缩或截短",
                    path.stem,
                    len(b64) // 1024,
                )
            self.voices[path.stem] = f"data:{mime};base64,{b64}"
            self.ctx.logger.info("已加载音色: %s (%.1fKB)", path.stem, len(raw) / 1024)

        if not self.default_voice and self.voices:
            self.default_voice = next(iter(self.voices))
        self.ctx.logger.info("音色加载完成，共 %d 个: %s", len(self.voices), list(self.voices))

    def _pick_voice(self, voice_name: str = "") -> Optional[str]:
        """把用户/模型给的名字宽松匹配到已加载音色（忽略大小写、扩展名）。"""
        if not voice_name:
            return None
        key = voice_name.strip()
        if key.lower().endswith((".wav", ".mp3")):
            key = key.rsplit(".", 1)[0]
        if key in self.voices:
            return key
        lowered = key.lower()
        for name in self.voices:
            if name.lower() == lowered:
                return name
        return None

    # -- 音色规划 ---------------------------------------------------------

    def _resolve_voice(self, voice_name: str, kind: str) -> VoicePlan:
        cfg = self.config.voice
        want = (voice_name or "").strip()

        # 唱歌：clone / design 模型都不支持，只能用预置音色
        if kind == "sing":
            preset = cfg.singing_voice or cfg.preset_voice or "mimo_default"
            note = ""
            if cfg.voice_mode == "clone" and want:
                note = "唱歌模式只支持预置音色，已忽略复刻音色请求"
                self.ctx.logger.warning(note)
            return VoicePlan(mode="preset", voice_id=preset, label=preset, note=note)

        mode = (cfg.voice_mode or "preset").lower()
        if mode not in VOICE_MODES:
            self.ctx.logger.warning("未知 voice_mode=%s，按 preset 处理", mode)
            mode = "preset"

        if mode == "design":
            if cfg.voice_design_prompt.strip():
                return VoicePlan(
                    mode="design", design_prompt=cfg.voice_design_prompt.strip(), label="自定义设计音色"
                )
            self.ctx.logger.warning("design 模式缺少 voice_design_prompt，回退到预置音色")
            mode = "preset"

        if mode == "clone":
            name = self._pick_voice(want) or self._pick_voice(self.default_voice) or self._pick_voice(cfg.clone_voice)
            if name:
                return VoicePlan(
                    mode="clone",
                    reference=self.voices[name],
                    reference_name=f"{name}.wav",
                    label=f"复刻音色 {name}",
                )
            if not cfg.fallback_to_preset:
                return VoicePlan(mode="", label="", note="clone 模式下没有可用音色样本")
            self.ctx.logger.error(
                "clone 模式没有可用音色（voices=%s），回退到预置音色 %s",
                list(self.voices),
                cfg.preset_voice,
            )
            return VoicePlan(
                mode="preset",
                voice_id=cfg.preset_voice or "mimo_default",
                label=cfg.preset_voice or "mimo_default",
                note="未找到复刻音色，已回退到预置音色",
            )

        return VoicePlan(
            mode="preset", voice_id=cfg.preset_voice or "mimo_default", label=cfg.preset_voice or "mimo_default"
        )

    # -- 风格指令 ---------------------------------------------------------

    def _compose_style(self, base: str, emotion: str, speed: str, kind: str) -> str:
        cfg = self.config.style
        singing = kind == "sing"
        parts: list[str] = []

        # 文档里 user 消息是可选参数，"可传入指令来调整语气与风格"，
        # 唱歌靠 (唱歌) 标签生效，同时用一条 user 指令约束演唱方式是正当用法。
        # 但*不要*把说话那套「语气XX」搬到演唱上——那是要朗读化。
        if singing:
            parts.append(SING_STYLE_PREFIX)

        if base.strip():
            parts.append(base.strip())

        # 唱歌不继承全局默认情绪/语速。歌唱的情绪应由歌词决定，
        # 只有调用方显式指定时才叠加，否则唱什么都一个味儿。
        emo_key = (emotion or "").strip()
        if emo_key.lower() in ("", "auto", "默认"):
            emo_key = "" if singing else cfg.default_emotion.strip()

        if emo_key:
            if singing:
                parts.append(
                    SING_EMOTION_PHRASES.get(emo_key, f"演唱情绪偏{emo_key}，但以歌词表达为主")
                )
            else:
                parts.append(EMOTION_PHRASES.get(emo_key, f"语气{emo_key}"))
        elif singing:
            parts.append(SING_NEUTRAL_HINT)

        speed_key = (speed or "").strip().lower()
        if speed_key in ("", "auto", "默认"):
            speed_key = "" if singing else cfg.default_speed.strip().lower()
        phrase = (SING_SPEED_PHRASES if singing else SPEED_PHRASES).get(speed_key, "")
        if phrase:
            parts.append(phrase)

        if cfg.extra_instruction.strip():
            parts.append(cfg.extra_instruction.strip())

        # 清理每段结尾标点，统一用「。」连接
        cleaned = [p.strip().rstrip("。.；;，, ") for p in parts if p and p.strip()]
        return "。".join(cleaned) + "。" if cleaned else ""

    # -- 流查找 -----------------------------------------------------------

    async def _find_stream_id(self, kwargs: dict) -> str:
        sid = kwargs.get("stream_id") or ""
        if sid:
            return str(sid)

        msg = kwargs.get("message")
        if isinstance(msg, dict):
            sid = msg.get("stream_id") or msg.get("chat_id") or ""
            if sid:
                return str(sid)

        msg_id = kwargs.get("msg_id") or ""
        message_proxy = getattr(self.ctx, "message", None)
        getter = getattr(message_proxy, "get_by_id", None) if message_proxy is not None else None
        if msg_id and callable(getter):
            try:
                record = await getter(msg_id)
                if isinstance(record, dict):
                    sid = record.get("stream_id") or ""
                    if sid:
                        return str(sid)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.debug("message.get_by_id 失败: %s", exc)

        # 不再回退到「最近活跃的群聊」：多群场景下会把语音发进错误的群。
        # 定位不到就返回空，由调用方提示 AI 改用文字回复。
        self.ctx.logger.warning(
            "未能定位消息所属聊天流（msg_id=%s），将改用文字回复", msg_id or "(none)"
        )
        return ""

    # -- 发送 -------------------------------------------------------------

    async def _call_custom(self, custom_type: str, data: Any, stream_id: str) -> Any:
        try:
            return await self.ctx.send.custom(custom_type=custom_type, data=data, stream_id=stream_id)
        except TypeError:
            return await self.ctx.send.custom(custom_type, data, stream_id)

    async def _send_voice(self, audio_b64: str, stream_id: str) -> bool:
        b64 = (audio_b64 or "").strip()
        if b64.startswith("data:"):
            b64 = b64.split(",", 1)[1].strip()
        if not b64:
            self.ctx.logger.error("音频数据为空")
            return False

        url = f"base64://{b64}"
        method = (self.config.speech.send_method or "auto").lower()

        attempts: list[tuple[str, Any]] = []
        if method in ("auto", "record"):
            attempts.append(("record", {"file": url}))
        if method in ("auto", "voice"):
            attempts.append(("voice", {"file": url}))
            attempts.append(("voice", b64))
        if method in ("auto", "hybrid"):
            attempts.append(("__hybrid__", None))

        for ctype, data in attempts:
            try:
                if ctype == "__hybrid__":
                    ok = await self.ctx.send.hybrid(
                        segments=[{"type": "voice", "data": b64}], stream_id=stream_id
                    )
                else:
                    ok = await self._call_custom(ctype, data, stream_id)
                if ok is not False:
                    self.ctx.logger.debug("语音发送成功: %s", ctype)
                    return True
                self.ctx.logger.warning("语音发送返回 False: %s", ctype)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("语音发送失败 (%s): %s", ctype, exc)

        self.ctx.logger.error("所有语音发送方式均失败，b64_len=%d", len(b64))
        return False

    # -- 后台任务 ---------------------------------------------------------

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _done(finished: asyncio.Task) -> None:
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                self.ctx.logger.error("语音任务异常: %s", exc)

        task.add_done_callback(_done)

    async def _deliver(
        self, prepared: PreparedSpeech, plan: VoicePlan, style: str, stream_id: str
    ) -> None:
        if self.tts_service is None:
            self.ctx.logger.error("TTS 服务已释放，放弃发送")
            return
        chunks = prepared.chunks or [prepared.text]
        total = len(chunks)
        fmt = (self.config.voice.audio_format or "wav").lower()
        delay = max(0.0, float(self.config.speech.inter_message_delay or 0))

        # 唱歌时把歌词全文打出来：词错能立刻分辨是 AI 写错了还是模型唱错了
        if prepared.kind == "sing":
            shown = prepared.text if len(prepared.text) <= 200 else prepared.text[:200] + "…"
        else:
            shown = prepared.text[:40] + ("…" if len(prepared.text) > 40 else "")

        self.ctx.logger.info(
            "开始合成: kind=%s voice=%s chunks=%d total_chars=%d text=%r style=%r",
            prepared.kind,
            plan.label,
            total,
            len(prepared.text),
            shown,
            style,
        )

        for idx, chunk in enumerate(chunks, 1):
            text = chunk
            if prepared.kind == "sing" and idx > 1 and not text.startswith("("):
                text = "(唱歌)" + text

            result = await self.tts_service.synthesize(
                text=text,
                mode=plan.mode,
                voice_id=plan.voice_id,
                reference_audio=plan.reference,
                reference_name=plan.reference_name,
                design_prompt=plan.design_prompt,
                style_instruction=style,
                audio_format=fmt,
            )

            if not result.get("success"):
                self.ctx.logger.error("第 %d/%d 段合成失败: %s", idx, total, result.get("error"))
                continue

            audio_b64 = result.get("audio_base64", "")
            ok = await self._send_voice(audio_b64, stream_id)
            audio_b64 = ""
            self.ctx.logger.info("第 %d/%d 段%s", idx, total, "已发送" if ok else "发送失败")

            if idx < total and delay:
                await asyncio.sleep(delay)

    # -- 核心合成流程 -----------------------------------------------------

    async def _synthesize_and_send(
        self,
        reply_text: str,
        stream_id: str,
        *,
        style_instruction: str = "",
        emotion: str = "",
        speed: str = "",
        mode: str = "auto",
        voice_name: str = "",
    ) -> dict[str, Any]:
        if not self.tts_service:
            return {"success": False, "error": "TTS 服务未初始化"}
        if not (reply_text or "").strip():
            return {"success": False, "error": "文本为空"}

        mode = (mode or "auto").lower()
        prepared = prepare_speech(
            reply_text,
            style_instruction=style_instruction,
            force_sing=(mode == "sing"),
            max_chars=self.config.speech.max_chars,
            sing_max_chars=self.config.speech.sing_max_chars,
            clean=self.config.speech.clean_text,
            strip_narration=self.config.speech.strip_narration,
            auto_punctuate=self.config.speech.auto_punctuate,
            punctuation_limit=self.config.speech.punctuation_limit,
        )

        if not prepared:
            return {
                "success": False,
                "error": "清洗后没有可合成的文本",
                "content": "清洗后没有可朗读的内容（可能只有表情或旁白），请直接用文字回复。",
            }

        style = self._compose_style(style_instruction, emotion, speed, prepared.kind)
        plan = self._resolve_voice(voice_name, prepared.kind)
        if not plan.mode:
            return {"success": False, "error": plan.note or "没有可用音色"}

        self._spawn(self._deliver(prepared, plan, style, stream_id))

        kind_label = "演唱" if prepared.kind == "sing" else "朗读"
        detail = f"语音已提交（{kind_label} · {plan.label} · {len(prepared.chunks)} 段）"
        if plan.note:
            detail += f"。注意：{plan.note}"
        if prepared.notes:
            detail += f"。文本处理：{'、'.join(prepared.notes)}"

        return {
            "success": True,
            "content": detail,
            "kind": prepared.kind,
            "voice": plan.label,
            "chunks": len(prepared.chunks),
        }

    # -- 对外 API（供其他插件调用） ---------------------------------------

    @API("voice_clone_tts", description="使用指定音色合成并发送语音", version="2", public=True)
    async def voice_clone_tts(
        self,
        text: str,
        style_instruction: str = "",
        stream_id: str = "",
        voice_name: str = "",
        emotion: str = "",
        speed: str = "",
        mode: str = "auto",
    ) -> dict[str, Any]:
        if not stream_id:
            return {"success": False, "error": "缺少 stream_id"}
        return await self._synthesize_and_send(
            text,
            stream_id,
            style_instruction=style_instruction,
            emotion=emotion,
            speed=speed,
            mode=mode,
            voice_name=voice_name,
        )

    # -- LLM 工具 ---------------------------------------------------------

    @Tool(
        "send_voice_reply",
        brief_description="用语音回复用户，可控制情绪、语速，也可以唱歌",
        detailed_description=(
            "把一段回复合成语音并发到当前聊天。用户要求语音、发来语音消息、或情境适合语音时使用。\n"
            "**写完这段语音后不要再发送同样的文字内容**，避免重复。\n\n"
            "【mode 怎么选】\n"
            "- `speak`：普通说话（默认，不确定就选它）\n"
            "- `sing`：唱歌。用户让你唱歌、哼歌、来一段时必须选它。"
            "此时 reply_text 里**只放歌词本身**，不要写「唱：」「好的我来唱」这类前缀，"
            "也不要写歌名说明；插在括号里的动作标签也一并去掉，只要干净的歌词。"
            "歌词用中文效果最好（官方明确建议），英文歌词的旋律稳定性会明显下降。"
            "注意：唱歌只能用预置音色，复刻音色无法唱歌。\n"
            "【歌词必须准确】\n"
            "- 用户消息里**粘贴了歌词**时，reply_text 必须**逐字照抄**那段歌词："
            "不许改字、不许换词、不许减句、不许为了押韵加词。少一个词用户都能听出来。\n"
            "- 用户**只给了歌名**时，用你确实知道的歌词；记不准就**搜索确认**再写进 reply_text。"
            "**绝不允许**顺着韵脚自己编词填空——编出来的词一听就是假的。"
            "实在想不起的句子宁可跳过，也不要拿自创的词顶上。\n"
            "- 用户没规定长度时，唱 2~4 句就够，别一口气塞整首。\n"
            "- 歌名、歌手名、专辑名、「以下是歌词」这类说明文字**不要**写进 reply_text，"
            "它们会被原样唱出来。\n"
            "- 唱的时候开头只要有（唱歌）就够了，不要叠（兴奋）（深情）等情绪标签，"
            "情绪交给歌词本身，叠了只会越唱越用力。\n"
            "- `auto`：由插件根据文本里的（唱歌）标签自动判断。\n\n"
            "【emotion 情绪】从这些里选一个最贴合的：开心、兴奋、温柔、安慰、悲伤、委屈、愤怒、"
            "冷淡、严肃、俏皮、撒娇、慵懒、惊讶、无奈、害羞、深情。不确定就留空。\n\n"
            "【speed 语速】slow / normal / fast。紧张、撒娇、兴奋可以 fast；"
            "深情、悲伤、严肃、安慰建议 slow；一般用 normal。\n\n"
            "【reply_text 里可以用的括号标签】\n"
            "- 整段风格：写在开头，如（温柔）（东北话）（粤语）（磁性）（唱歌）\n"
            "- 句内细节：插在任意位置，如（叹气）（轻笑）（哽咽）（深呼吸）（语速加快，碎碎念）（小声）\n"
            "- 不要写 Markdown、emoji、颜文字、网址、以及大段括号动作描写，插件会清洗掉，"
            "但一开始就别写效果更好。\n\n"
            "【style_instruction 自由描述】需要更细腻的演绎时再填，像给演员说戏，"
            "例如「声音低沉，语速极慢，每个字都像在舌尖滚过，尾音带一丝疲惫的气声」。"
            "通常配合 emotion/speed 已足够，不必写长句。"
        ),
        activation_type=ActivationType.ALWAYS,
        core_tool=True,
        parameters=[
            ToolParameterInfo(
                name="reply_text",
                param_type=ToolParamType.STRING,
                description="要合成语音的文本。可在开头写整段风格标签（温柔），句中插语气标签（叹气）。唱歌时只放歌词。",
                required=True,
            ),
            ToolParameterInfo(
                name="msg_id",
                param_type=ToolParamType.STRING,
                description="当前消息 ID（可留空，插件会从上下文定位聊天流）",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="mode",
                param_type=ToolParamType.STRING,
                description="auto（自动判断）/ speak（说话）/ sing（唱歌）。用户要求唱歌时务必用 sing",
                required=False,
                default="auto",
                enum_values=["auto", "speak", "sing"],
            ),
            ToolParameterInfo(
                name="emotion",
                param_type=ToolParamType.STRING,
                description="情绪：开心/兴奋/温柔/安慰/悲伤/委屈/愤怒/冷淡/严肃/俏皮/撒娇/慵懒/惊讶/无奈/害羞/深情，可留空",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="speed",
                param_type=ToolParamType.STRING,
                description="语速：auto（跟随插件配置）/ slow / normal / fast",
                required=False,
                default="auto",
                enum_values=["auto", "slow", "normal", "fast"],
            ),
            ToolParameterInfo(
                name="style_instruction",
                param_type=ToolParamType.STRING,
                description="（可选）自由风格描述，像给演员说戏。emotion/speed 不够用时再填",
                required=False,
                default="",
            ),
        ],
    )
    async def send_voice_reply(
        self,
        reply_text: str,
        msg_id: str = "",
        mode: str = "auto",
        emotion: str = "",
        speed: str = "auto",
        style_instruction: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        stream_id = await self._find_stream_id({**kwargs, "msg_id": msg_id})
        if not stream_id:
            self.ctx.logger.error("无法定位聊天流，msg_id=%s，kwargs=%s", msg_id, list(kwargs))
            return {
                "success": False,
                "error": "无法定位聊天流",
                "content": "语音发送失败：没能定位到当前聊天流，请改用文字回复。",
            }

        try:
            return await self._synthesize_and_send(
                reply_text,
                stream_id,
                style_instruction=style_instruction,
                emotion=emotion,
                speed=speed,
                mode=mode,
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.exception("语音回复失败")
            return {
                "success": False,
                "error": str(exc),
                "content": f"语音合成出错：{exc}。请改用文字回复。",
            }


def create_plugin() -> AIVoicePlugin:
    return AIVoicePlugin()

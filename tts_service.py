"""MiMo TTS 服务封装。

对应官方文档：语音合成（MiMo-V2.5-TTS 系列）
https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/speech-synthesis-v2.5

三种模型的分工（很容易踩坑，务必注意）：

===============  ==========================  ========  ==========
模型              用途                         唱歌      音色来源
===============  ==========================  ========  ==========
mimo-v2.5-tts     预置音色                     支持      预置音色 ID
...-voiceclone    音频样本复刻                  不支持    参考音频 base64
...-voicedesign   文本描述设计音色              不支持    文本描述
===============  ==========================  ========  ==========

也就是说：**唱歌只能用 ``mimo-v2.5-tts`` + 预置音色**。这是原插件唱歌失败的根因——
它默认走 ``voiceclone``，再在 user 消息里描述"唱歌"，模型既不支持唱歌、标签位置也不对。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiohttp

__all__ = ["MiMoTTSService", "MODEL_PRESET", "MODEL_CLONE", "MODEL_DESIGN"]

MODEL_PRESET = "mimo-v2.5-tts"
MODEL_CLONE = "mimo-v2.5-tts-voiceclone"
MODEL_DESIGN = "mimo-v2.5-tts-voicedesign"

# 官方支持的音色参考音频 MIME
_REF_MIME = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
}

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class MiMoTTSService:
    """MiMo 语音合成客户端（异步、复用连接、带重试）。"""

    DEFAULT_API_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
    MAX_REF_B64_BYTES = 10 * 1024 * 1024  # 单条参考音频 base64 上限 10MB

    def __init__(
        self,
        api_key: str,
        api_base_url: str = "",
        logger: Optional[logging.Logger] = None,
        *,
        timeout: float = 120.0,
        max_retries: int = 2,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.api_base_url = self.normalize_base_url(api_base_url)
        self.logger = logger or logging.getLogger(__name__)
        self.timeout = float(timeout) if timeout else 120.0
        self.max_retries = max(0, int(max_retries))
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    @classmethod
    def normalize_base_url(cls, url: str) -> str:
        """把各种写法收敛成 ``https://host/v1``。"""
        base = (url or "").strip() or cls.DEFAULT_API_BASE_URL
        base = base.rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")].rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = "https://" + base
        return base

    @property
    def chat_url(self) -> str:
        return f"{self.api_base_url}/chat/completions"

    def update_api_key(self, api_key: str) -> None:
        self.api_key = (api_key or "").strip()

    def update_api_base_url(self, api_base_url: str) -> None:
        self.api_base_url = self.normalize_base_url(api_base_url)

    def update_timeout(self, timeout: float, max_retries: Optional[int] = None) -> None:
        self.timeout = float(timeout) if timeout else 120.0
        if max_retries is not None:
            self.max_retries = max(0, int(max_retries))

    # ------------------------------------------------------------------
    # 请求体构造（纯函数，便于测试）
    # ------------------------------------------------------------------

    def build_payload(
        self,
        *,
        model: str,
        text: str,
        style_instruction: str = "",
        voice: Optional[str] = None,
        audio_format: str = "wav",
        optimize_text_preview: Optional[bool] = None,
    ) -> dict[str, Any]:
        """按官方文档构造请求体。

        关键点：
        * 待合成文本必须放在 ``role="assistant"``。
        * 风格指令放在 ``role="user"``；**没有指令时不要塞空 user 消息**，
          空 content 会干扰模型，导致语气平淡、节奏发虚。
        * 音频格式与音色放在 ``audio`` 下。
        """
        messages: list[dict[str, str]] = []
        style = (style_instruction or "").strip()
        if style:
            messages.append({"role": "user", "content": style})
        messages.append({"role": "assistant", "content": text})

        audio: dict[str, Any] = {"format": (audio_format or "wav").lower()}
        if voice:
            audio["voice"] = voice
        if optimize_text_preview is not None:
            audio["optimize_text_preview"] = bool(optimize_text_preview)

        return {
            "model": model,
            "modalities": ["text", "audio"],
            "messages": messages,
            "audio": audio,
            "stream": False,
        }

    @classmethod
    def reference_audio_uri(cls, reference: str, filename: str = "") -> str:
        """把参考音频整理成 ``data:<mime>;base64,<...>``。"""
        data = (reference or "").strip()
        if data.startswith("data:"):
            return data
        mime = "audio/wav"
        if filename:
            suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
            mime = _REF_MIME.get(suffix, "audio/wav")
        return f"data:{mime};base64,{data}"

    # ------------------------------------------------------------------
    # 高层接口
    # ------------------------------------------------------------------

    async def synthesize(
        self,
        *,
        text: str,
        mode: str = "preset",
        voice_id: str = "",
        reference_audio: str = "",
        reference_name: str = "",
        design_prompt: str = "",
        style_instruction: str = "",
        audio_format: str = "wav",
        optimize_text_preview: Optional[bool] = None,
    ) -> dict[str, Any]:
        """合成语音。

        Args:
            mode: ``preset`` / ``clone`` / ``design``。
            voice_id: preset 模式的音色 ID（如 ``冰糖``）。
            reference_audio: clone 模式参考音频（纯 base64 或 data URI）。
            design_prompt: design 模式音色描述，会作为 user 消息内容。
            style_instruction: 自然语言风格指令（user 消息）。
        """
        if not self.api_key:
            return {"success": False, "error": "API Key 未配置（config.toml -> [voice].mimo_api_key）"}
        if not text or not text.strip():
            return {"success": False, "error": "合成文本为空"}

        mode = (mode or "preset").lower()

        if mode == "clone":
            if not reference_audio:
                return {"success": False, "error": "clone 模式缺少参考音频"}
            voice = self.reference_audio_uri(reference_audio, reference_name)
            if len(voice) > self.MAX_REF_B64_BYTES * 1.4:
                return {"success": False, "error": "参考音频过大（Base64 超过 10MB），请压缩或缩短样本"}
            model = MODEL_CLONE
        elif mode == "design":
            if not design_prompt.strip():
                return {"success": False, "error": "design 模式缺少音色描述 voice_design_prompt"}
            model = MODEL_DESIGN
            voice = None
            style_instruction = design_prompt.strip()
        else:
            model = MODEL_PRESET
            voice = (voice_id or "mimo_default").strip()

        payload = self.build_payload(
            model=model,
            text=text.strip(),
            style_instruction=style_instruction,
            voice=voice,
            audio_format=audio_format,
            optimize_text_preview=optimize_text_preview,
        )

        user_part = payload["messages"][0]["content"] if len(payload["messages"]) > 1 else "(无)"
        self.logger.debug(
            "TTS request: model=%s voice=%s fmt=%s text_len=%d style=%r text=%r",
            model,
            (voice or "")[:24] if voice else "-",
            audio_format,
            len(text),
            user_part[:80],
            text[:80],
        )

        return await self._post(payload)

    async def synthesize_with_preset(
        self, text: str, voice_id: str = "mimo_default", style_instruction: str = "", audio_format: str = "wav"
    ) -> dict[str, Any]:
        """兼容旧接口。"""
        return await self.synthesize(
            text=text, mode="preset", voice_id=voice_id, style_instruction=style_instruction, audio_format=audio_format
        )

    async def synthesize_with_voice_clone(
        self,
        text: str,
        reference_audio_base64: str,
        style_instruction: str = "",
        audio_format: str = "wav",
        reference_name: str = "",
    ) -> dict[str, Any]:
        """兼容旧接口。"""
        return await self.synthesize(
            text=text,
            mode="clone",
            reference_audio=reference_audio_base64,
            reference_name=reference_name,
            style_instruction=style_instruction,
            audio_format=audio_format,
        )

    # ------------------------------------------------------------------
    # 传输层
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout, sock_connect=20)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "api-key": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error = "unknown"
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                session = await self._get_session()
                async with session.post(self.chat_url, json=payload, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        return self._parse(body, payload)
                    last_error = f"HTTP {resp.status}: {body[:400]}"
                    if resp.status in _RETRYABLE_STATUS and attempt + 1 < attempts:
                        delay = min(2 ** attempt, 8)
                        self.logger.warning(
                            "TTS 请求失败（%s），%.1fs 后重试 %d/%d", resp.status, delay, attempt + 1, attempts - 1
                        )
                        await asyncio.sleep(delay)
                        continue
                    self.logger.error("TTS API 错误: status=%s body=%s", resp.status, body[:400])
                    return {"success": False, "error": last_error}
            except asyncio.TimeoutError:
                last_error = f"请求超时（{self.timeout:.0f}s）"
                self.logger.error(last_error)
            except aiohttp.ClientError as exc:
                last_error = f"网络错误: {exc}"
                self.logger.error(last_error)
            except Exception as exc:  # noqa: BLE001
                last_error = f"未预期错误: {exc}"
                self.logger.exception("TTS 请求异常")
                return {"success": False, "error": last_error}

            if attempt + 1 < attempts:
                await asyncio.sleep(min(2 ** attempt, 8))

        return {"success": False, "error": last_error}

    def _parse(self, body: str, payload: dict[str, Any]) -> dict[str, Any]:
        import json

        try:
            data = json.loads(body)
        except (TypeError, ValueError):
            return {"success": False, "error": f"响应不是合法 JSON: {body[:200]}"}

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return {"success": False, "error": f"API 返回错误: {msg}"}

        choices = data.get("choices") or []
        if not choices:
            return {"success": False, "error": f"响应中没有 choices: {body[:200]}"}

        choice = choices[0] or {}
        message = choice.get("message") or {}
        audio = message.get("audio") or choice.get("audio") or {}
        if not isinstance(audio, dict):
            return {"success": False, "error": "响应 audio 字段格式异常"}

        audio_b64 = audio.get("data") or audio.get("audio") or ""
        if not audio_b64:
            # 有些返回把文本放在 content，提示模型可能拒绝了文本
            hint = message.get("content") or choice.get("finish_reason") or ""
            return {"success": False, "error": f"响应中没有音频数据（{str(hint)[:120]}）"}

        return {
            "success": True,
            "audio_base64": audio_b64,
            "text": payload["messages"][-1]["content"],
        }

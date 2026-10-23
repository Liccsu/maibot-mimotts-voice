# MiMo TTS Voice Plugin for MaiBot

基于小米 **MiMo-V2.5-TTS** 的语音回复插件，支持**预置音色**、**音色复刻**、**文本设计音色**，并且支持**唱歌**。

> v2.0.0 重点修复了原插件的两个硬伤：**唱歌变成奇怪朗读**、**语气/语速/情感控制不灵**。根因分析和修复方式见下文「为什么之前不行」。

---

## 为什么之前不行

对照 [官方文档](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/speech-synthesis-v2.5)，MiMo 的三个模型能力边界是这样的：

| 模型 | 用途 | 支持唱歌 | 音色来源 |
|------|------|:---:|------|
| `mimo-v2.5-tts` | 预置音色 | ✅ | 预置音色 ID |
| `mimo-v2.5-tts-voiceclone` | 音频样本复刻 | ❌ | 参考音频 |
| `mimo-v2.5-tts-voicedesign` | 文本描述设计音色 | ❌ | 文本描述 |

### 唱歌为什么只会"朗读"

三重错误叠加：

1. **模型选错了**。插件默认 `voice_mode = "clone"`，走的是 `voiceclone` 模型——这个模型**根本不支持唱歌**，怎么调都是朗读。
2. **标签位置错了**。官方要求唱歌必须在待合成文本**最开头**写 `(唱歌)歌词`，且这段文本要放在 `role="assistant"` 里。原插件把"唱歌"写在 `role="user"` 的风格指令里，模型只当普通描述。
3. **没人告诉模型"要唱"**。没有 `(唱歌)` 标签，模型就按正常语调念，于是出现"语速奇怪的朗读"。

**现在的做法**：检测到唱歌意图后，强制切换到 `mimo-v2.5-tts` 模型、把 `(唱歌)` 顶到文本最开头、并把歌词清洗干净（去掉"唱："前缀、动作标签、Markdown）。

### 语气 / 语速 / 情感为什么控不住

1. **空的 user 消息**。原插件在没有风格指令时也会塞一条 `{"role":"user","content":""}`。文档里 `user` 是可选参数，空内容会干扰模型，导致语气平淡、节奏发虚。现在空指令直接不发送该消息。
2. **文本噪声被念出来**。AI 回复里常有 `**加粗**`、`~`、`😂`、`(*^▽^*)`、长括号旁白。这些会被逐字读出来，听起来就是"语速奇怪"。现在合成前统一清洗。
3. **长句一口气读完**。没有标点的长句会让模型加速赶完。现在会自动补换气停顿，并优先断在"吧 / 了 / 呢"这类语气词后面。
4. **风格只能靠模型自由发挥**。现在提供结构化的 `emotion` / `speed` 参数，插件翻译成文档推荐的自然语言指令，稳定得多。

---

## 安装

```bash
cd /path/to/MaiBot/plugins/
git clone https://github.com/Liccsu/maibot-mimotts-voice.git
pip install aiohttp
```

首次启动会自动从 `config.example.toml` 生成 `config.toml`（不会覆盖已有配置）。

> 本仓库是 [Emilia-awa/maibot-mimotts-voice](https://github.com/Emilia-awa/maibot-mimotts-voice) 的 fork 维护版，
> `_manifest.json` 的 `id` 与 `urls.repository` 均已指向本仓库，插件市场不会再把更新指向上游旧版本。

### 从上游旧版迁移

插件 `id` 从 `Emilia-awa.maibot-mimotts-voice` 改成了 `liccsu.maibot-mimotts-voice`，旧安装实例需要做一次迁移：

```bash
cd /path/to/MaiBot/plugins/
# 1. 备份旧配置与音色样本
cp Emilia-awa.maibot-mimotts-voice/config.toml /tmp/config.toml.bak
cp -r Emilia-awa.maibot-mimotts-voice/voices /tmp/voices.bak

# 2. 换成 fork 版本
git clone https://github.com/Liccsu/maibot-mimotts-voice.git
cp /tmp/config.toml.bak liccsu.maibot-mimotts-voice/config.toml
cp -r /tmp/voices.bak liccsu.maibot-mimotts-voice/voices

# 3. 确认插件能正常加载后，再归档旧目录（先改名，别直接删）
mv Emilia-awa.maibot-mimotts-voice Emilia-awa.maibot-mimotts-voice.bak
```

如果旧目录本身就是 git 仓库、想保留提交历史，也可以直接改 remote：

```bash
cd Emilia-awa.maibot-mimotts-voice
# 有本地改动先存起来，避免被覆盖
git stash
git remote set-url origin https://github.com/Liccsu/maibot-mimotts-voice.git
git fetch origin
git checkout main
git merge --ff-only origin/main    # 若提示无法快进，说明历史已分叉，改用上面的克隆方案
```

这样目录名不必跟着 `id` 改，`config.toml` 与 `voices/` 原地保留，不需要搬文件。

## 配置

```toml
[plugin]
enabled = true
config_version = "2.0.0"

[voice]
mimo_api_key = "your_api_key_here"
api_base_url = "https://token-plan-cn.xiaomimimo.com/v1"
voice_mode = "clone"            # preset / clone / design
preset_voice = "mimo_default"
singing_voice = "mimo_default"  # 唱歌专用预置音色
voice_design_prompt = ""
voices_dir = "voices"
default_voice = ""
clone_voice = ""
fallback_to_preset = true
audio_format = "wav"

[style]
default_emotion = ""
default_speed = "normal"
extra_instruction = ""

[speech]
max_chars = 180
sing_max_chars = 400
clean_text = true
strip_narration = true
auto_punctuate = true
punctuation_limit = 30
timeout = 120
max_retries = 2
inter_message_delay = 0.6
send_method = "auto"
```

### 配置说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `mimo_api_key` | MiMo API Key（必填） | - |
| `api_base_url` | API 地址，可省略 `/v1` 或误贴 `/chat/completions`，会自动纠正 | `https://token-plan-cn.xiaomimimo.com/v1` |
| `voice_mode` | `preset` 预置音色 / `clone` 音色复刻 / `design` 文本设计音色 | `clone` |
| `preset_voice` | 预置音色 ID | `mimo_default` |
| `singing_voice` | 唱歌用的预置音色（唱歌不走 clone） | `mimo_default` |
| `voice_design_prompt` | design 模式的音色描述 | - |
| `voices_dir` | 音色样本目录，支持相对/绝对路径 | `voices` |
| `fallback_to_preset` | 复刻音色不可用时是否回退到预置音色 | `true` |
| `audio_format` | 返回音频格式，`wav` 兼容性最好，`mp3` 更小 | `wav` |
| `default_emotion` | 默认情绪，留空不注入。**仅作用于说话，唱歌不继承** | - |
| `default_speed` | 默认语速 `slow` / `normal` / `fast`。**仅作用于说话，唱歌时忽略** | `normal` |
| `extra_instruction` | 追加到每条语音指令末尾的补充要求（说话与唱歌都会加） | - |
| `max_chars` | 单段朗读字数上限，超出分段发送 | `180` |
| `sing_max_chars` | 单段演唱字数上限（过长破坏旋律） | `400` |
| `clean_text` | 合成前清洗 Markdown/emoji/URL | `true` |
| `strip_narration` | 移除长括号旁白 | `true` |
| `auto_punctuate` | 自动补标点与换气停顿 | `true` |
| `punctuation_limit` | 连续多少字没标点就补逗号 | `30` |
| `timeout` | 单次请求超时（秒），唱歌建议 ≥120 | `120` |
| `max_retries` | 429/5xx/超时重试次数 | `2` |
| `send_method` | 语音发送方式 `auto`/`record`/`voice`/`hybrid` | `auto` |

### API 地址

| 用户类型 | 地址 |
|---------|------|
| Plan 用户 | `https://token-plan-cn.xiaomimimo.com/v1` |
| 按量计费 | `https://api.xiaomimimo.com/v1` |

---

## 音色

### 预置音色

| 音色名 | 语言 | 性别 |
|--------|------|------|
| `mimo_default` | 跟随集群（中国集群为冰糖） | 女性 |
| `冰糖` / `茉莉` | 中文 | 女性 |
| `苏打` / `白桦` | 中文 | 男性 |
| `Mia` / `Chloe` | 英文 | 女性 |
| `Milo` / `Dean` | 英文 | 男性 |

### 音色复刻

```toml
voice_mode = "clone"
```

把 30 秒以上、无背景噪音的 `.wav` 或 `.mp3` 放进 `voices/` 目录，文件名即音色名。Base64 后不能超过 10MB。

### 文本设计音色

```toml
voice_mode = "design"
voice_design_prompt = "清冷慵懒的年轻女声，语速偏慢，气息松弛，像深夜电台"
```

无需样本，模型按描述生成音色。**不支持唱歌**。

---

## 唱歌

有两条路，都行：

**路线 A：让 AI 自己判断（推荐）**

用户说"唱首歌"，AI 会调用工具时带 `mode="sing"`，插件自动切模型、加标签。

**路线 B：手动在文本里写标签**

在语音文本最开头写 `（唱歌）`，插件会自动识别并归位：

```
（唱歌）长亭外，古道边，芳草碧连天。
```

想要更好的效果，歌词**用中文**（官方建议），且不要夹杂 `（叹气）` 这类动作标签——它们会打断旋律（插件在唱歌模式下会自动剔除）。

> ⚠️ `clone` 模式唱歌时会自动切换到 `singing_voice` 指定的预置音色，因为复刻模型没有唱歌能力。这是模型限制，不是插件问题。

---

## 工具说明

`send_voice_reply` 已设为 `core_tool`，AI 每轮都能看到，不需要先搜工具。

| 参数 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `reply_text` | string | ✅ | 要合成的文本，可含括号标签 |
| `msg_id` | string | ❌ | 消息 ID，用于定位聊天流 |
| `mode` | string | ❌ | `auto` / `speak` / `sing` |
| `emotion` | string | ❌ | 开心/兴奋/温柔/安慰/悲伤/委屈/愤怒/冷淡/严肃/俏皮/撒娇/慵懒/惊讶/无奈/害羞/深情 |
| `speed` | string | ❌ | `auto` / `slow` / `normal` / `fast` |
| `style_instruction` | string | ❌ | 自由风格描述，emotion/speed 不够用时再填 |

### 文本标签

- **整段风格**写在开头：`（温柔）（东北话）（粤语）（磁性）`
- **句内细节**插在任意位置：`（叹气）（轻笑）（哽咽）（深呼吸）（语速加快，碎碎念）（小声）`

常用风格：开心/悲伤/愤怒/温柔/慵懒/俏皮/磁性/沙哑/甜美/冷漠/严肃/活泼/深沉/东北话/粤语

常用动作：叹气/轻笑/哽咽/深呼吸/咳嗽/打哈欠/低语/提高音量

---

## 排错

| 现象 | 原因 / 处理 |
|------|------------|
| 唱歌还是朗读 | 确认 `mode="sing"` 或文本以 `（唱歌）` 开头；看日志里 `model=` 是否为 `mimo-v2.5-tts` |
| 语气没变化 | 把 `clean_text`/`auto_punctuate` 保持开启；试着加 `emotion` 或 `extra_instruction` |
| 语速忽快忽慢 | 调小 `punctuation_limit`（如 20），让停顿更密 |
| 语音发不出去 | 改 `send_method` 为 `record` / `voice` / `hybrid` 逐个试 |
| 401 / 403 | 检查 `mimo_api_key` 与 `api_base_url` 是否匹配用户类型（Plan vs 按量） |
| 只有括号标签时没声 | 正常行为，会回退让 AI 用文字回复 |

日志里每次合成都会打印 `model / voice / text_len / style`，便于定位。

---

## 目录结构

```
maibot-mimotts-voice/
├── plugin.py            # 插件主入口：配置、音色路由、工具、发送
├── tts_service.py       # MiMo API 封装：模型路由、请求体构造、重试
├── text_processor.py    # 文本清洗、唱歌标签归位、断句分段
├── config.example.toml
├── _manifest.json
├── requirements.txt
└── voices/              # 音色样本（gitignored）
```

## 更新日志

### v2.0.3

官方插件中心评审后的修正：

- 🔐 **能力声明与实现对齐**：删掉不存在的 `send.record` 和从未使用的 `send.text`，`chat.get_all_streams` 一并移除。现在只声明实际调用的三项——`send.custom`、`send.hybrid`、`message.get_by_id`。此前声明与调用不符，Host 会按名逐项授权，一旦走到兜底路径会被拒，表现为语音发不出去
- 🛑 **移除「最近活跃群聊」兜底**：定位不到 `stream_id` 时不再猜一个群发出去，改为记 warning 并让 AI 改用文字回复。多群部署下这条兜底会把语音发进错误的群
- 🏷️ 显式声明 `plugin_type = "provider"`
- 📅 `host_application` 上界改为 `1.99.99`、`sdk` 上界改为 `2.99.99`（此前写 `999.999.999` 属占位值，无实际验证依据）

### v2.0.2

- 🎵 **修复唱歌被全局默认情绪污染**：此前 `kind=sing` 也会套用 `style.default_emotion`，导致配置了「兴奋」后唱什么歌都变成「语气兴奋激动、音量和音高都有起伏」。现在唱歌**不再继承** `default_emotion` / `default_speed`，情绪交回歌词本身（默认注入「情绪随歌词自然表达，不要夸张」）
- 🎤 **唱歌情绪改用演唱措辞**：显式传 `emotion` 时使用独立的 `SING_EMOTION_PHRASES`（如「唱得柔和松弛，气息连绵不断」），不再用「语气XX」这种说话的说法
- 🥁 **唱歌速度改称节奏**：`speed=slow/fast` 在唱歌时翻译为「节奏放缓，气息拉长」「节奏明快，但每个字都要唱清楚」，避免被理解成加速朗读
- 🔧 风格拼接顺序固定为：唱歌基调 → 自由风格 → 情绪 → 速度 → 附加后缀

### v2.0.1

- 🏷️ **插件元数据改指 fork 仓库**：`urls.repository` / `homepage` / `issues` 全部指向 `Liccsu/maibot-mimotts-voice`，避免在线更新时被拉回上游旧版本
- 🔑 **修正插件 `id`**：`Emilia-awa.maibot-mimotts-voice` → `liccsu.maibot-mimotts-voice`。旧 id 含大写字母，不符合新版 Host 的 `^[a-z0-9]+(?:[.-][a-z0-9]+)+$` 规范，严格校验下会被拒绝加载；同时彻底断开与上游市场的 id 绑定
- 📦 版本号 2.0.0 → 2.0.1（仅元数据变更，配置结构 `config_version` 仍为 2.0.0，旧 `config.toml` 可直接沿用）
- 📝 补充「从上游旧版迁移」步骤

### v2.0.0

- 🎤 **修复唱歌**：唱歌强制使用 `mimo-v2.5-tts` + 前置 `(唱歌)` 标签；新增 `mode="sing"` 与 `singing_voice`
- 🗣️ **修复语气/语速**：不再发送空 user 消息；新增 `emotion` / `speed` 结构化参数并翻译成官方推荐的自然语言指令
- 🧹 **新增文本清洗**：移除 Markdown、emoji、颜文字、URL、长旁白；自动补换气停顿，优先断在语气词后
- ✂️ **长文本分段**：超长内容按句切分后依次发送，唱歌单独放宽上限
- 🎨 **新增文本设计音色**（`voice_mode = "design"`）
- 🛡️ **稳定性**：请求重试（429/5xx/超时）、超时与重试可配、发送方式多级回退（record → voice → hybrid）、后台任务强引用防 GC、URL 自动纠正
- 🔍 **可观测性**：日志打印实际请求模型、音色与风格指令

### v1.6.0

- 工具链暴露、AI 智能风格控制、配置自动初始化

### v1.0.0

- 初始版本：预置音色、音色复刻

## 注意事项

1. **API Key** 在 [小米 MiMo 平台](https://platform.xiaomimimo.com) 申请。
2. **音色复刻**建议 30 秒以上、无噪音，Base64 不超过 10MB。
3. **费用**：MiMo-V2.5-TTS 系列目前限时免费。
4. `core_tool` 需要 SDK 2.x；更老的 SDK 会忽略该字段，工具仍可用但可能需要先被检索到。

## 许可证

MIT License

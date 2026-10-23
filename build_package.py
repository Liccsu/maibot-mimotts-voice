"""构建 MaiBot 插件安装包（ZIP）。

打包内容由白名单决定，``config.toml``（含 API Key）、``voices/``、``.git``、
``__pycache__`` 一律不会被打进去。

用法::

    python build_package.py

产物::

    dist/<plugin-id>-v<version>.zip

ZIP 内结构为唯一一层插件目录::

    maibot-mimotts-voice/
    ├── _manifest.json
    ├── plugin.py
    └── ...

可直接在 MaiBot WebUI「插件扩展 → 更多操作 → 从 ZIP 安装插件」上传。
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"

# 打包白名单：只打这些文件，新增模块记得加进来
INCLUDE = [
    "_manifest.json",
    "plugin.py",
    "text_processor.py",
    "tts_service.py",
    "__init__.py",
    "config.example.toml",
    "requirements.txt",
    "README.md",
    "LICENSE",
]

# 兜底排除：即使出现在 INCLUDE 里也不会入包
EXCLUDE_NAMES = {"config.toml", ".gitignore"}
EXCLUDE_PARTS = {".git", ".workbuddy", "__pycache__", "voices", "dist", "data", "config_back"}

# MaiBot 官方限制
MAX_ZIP_BYTES = 100 * 1024 * 1024
MAX_UNPACKED_BYTES = 300 * 1024 * 1024
MAX_ENTRIES = 10_000

ID_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def load_manifest() -> dict:
    path = ROOT / "_manifest.json"
    if not path.is_file():
        raise SystemExit("找不到 _manifest.json")
    manifest = json.loads(path.read_text(encoding="utf-8"))

    plugin_id = manifest.get("id", "")
    if not ID_RE.fullmatch(plugin_id):
        raise SystemExit(f"_manifest.json 的 id 不合规: {plugin_id!r}（需匹配 {ID_RE.pattern}）")
    version = manifest.get("version", "")
    if not SEMVER_RE.fullmatch(version):
        raise SystemExit(f"版本号必须为严格三段式 X.Y.Z: {version!r}")
    if manifest.get("manifest_version") != 2:
        raise SystemExit("manifest_version 必须为 2")
    return manifest


def collect_files() -> list[Path]:
    missing = [name for name in INCLUDE if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit("缺少必需文件: " + ", ".join(missing))
    return [ROOT / name for name in INCLUDE]


def build(manifest: dict, files: list[Path]) -> Path:
    DIST.mkdir(exist_ok=True)
    out = DIST / f"{manifest['id']}-v{manifest['version']}.zip"
    if out.exists():
        out.unlink()

    folder = ROOT.name  # ZIP 内唯一一层插件目录
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for src in files:
            rel = src.relative_to(ROOT)
            if rel.name in EXCLUDE_NAMES or EXCLUDE_PARTS & set(rel.parts):
                continue
            zf.write(src, f"{folder}/{rel.as_posix()}")
    return out


def verify(out: Path, manifest: dict) -> dict:
    folder = ROOT.name
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        infos = zf.infolist()
        unpacked = sum(i.file_size for i in infos)

        bad = [n for n in names if n.startswith("/") or ".." in Path(n).parts or "\\" in n]
        if bad:
            raise SystemExit("包内存在非法路径: " + ", ".join(bad))

        for required in ("_manifest.json", "plugin.py"):
            if f"{folder}/{required}" not in names:
                raise SystemExit(f"{required} 不在插件目录根，MaiBot 会拒绝安装")

        leaked = [n for n in names if Path(n).name == "config.toml" or ".git" in Path(n).parts]
        if leaked:
            raise SystemExit("包内混入了不该发布的文件: " + ", ".join(leaked))

        # 包内 manifest 必须与源文件一致
        inner = json.loads(zf.read(f"{folder}/_manifest.json").decode("utf-8"))
        if inner.get("id") != manifest["id"] or inner.get("version") != manifest["version"]:
            raise SystemExit("包内 _manifest.json 与源文件不一致")

        if out.stat().st_size > MAX_ZIP_BYTES:
            raise SystemExit("压缩包超过 100 MB")
        if unpacked > MAX_UNPACKED_BYTES:
            raise SystemExit("解压后超过 300 MB")
        if len(names) > MAX_ENTRIES:
            raise SystemExit("文件数超过 10000")

    return {"entries": len(names), "unpacked": unpacked}


def main() -> int:
    manifest = load_manifest()
    files = collect_files()
    out = build(manifest, files)
    stat = verify(out, manifest)

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    size_kb = out.stat().st_size / 1024

    print(f"插件      : {manifest['name']}  ({manifest['id']})")
    print(f"版本      : {manifest['version']}")
    print(f"产物      : {out.relative_to(ROOT).as_posix()}")
    print(f"体积      : {size_kb:.1f} KB（解压 {stat['unpacked'] / 1024:.1f} KB，{stat['entries']} 个条目）")
    print(f"SHA-256   : {digest}")
    print()

    with zipfile.ZipFile(out) as zf:
        for info in zf.infolist():
            print(f"  {info.file_size:>8,}  {info.filename}")

    print("\n校验通过：结构合规 / 无 config.toml 与 .git / 体积在限内")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

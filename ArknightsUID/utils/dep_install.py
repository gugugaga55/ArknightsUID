"""可选依赖聊天内安装（ark安装依赖）：白名单解析 + core 同款 pip 安装。

依赖安装的主路径是 core 的 AutoInstallDep（默认开）：启动加载插件时读
pyproject 主 dependencies，缺什么自动 pip 安装（阿里→字节→清华→官方
多镜像回退）。本模块兜底自动安装失败的场景（镜像不可达、core 配置关
了自动安装、插件目录拷贝部署等），让管理员在聊天里一条命令补装。

白名单直接解析本插件 pyproject.toml——依赖声明的单一事实源（core 读的
也是它）：命令只接受依赖名 / extras 组名 / 少量别名（如 ocr），不接受
任意包名。目标包在插件内全部运行时惰性导入（rapidocr / pytakumi），装
完无需重启或热重载即可生效。
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any, NamedTuple

from packaging.requirements import Requirement

from gsuid_core.logger import logger

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

# 友好别名 → 依赖名（pyproject 里没有但用户顺口会打的词）
_ALIAS = {"ocr": "rapidocr", "截图识别": "rapidocr"}

_install_lock = asyncio.Lock()

# pyproject 解析失败时的静态兜底（正常不会走到；防止 pyproject 被挪走
# 后命令直接不可用）
_FALLBACK_SPECS: list[tuple[str, str]] = [
    ("rapidocr", "rapidocr[onnxruntime]>=3.0.0"),
    ("ocr", "rapidocr[onnxruntime]>=3.0.0"),
    ("takumi", "pytakumi>=0.1.4"),
    ("pytakumi", "pytakumi>=0.1.4"),
]


class DepEntry(NamedTuple):
    keys: list[str]  # 可用匹配名（含别名/组名，小写）
    spec: str  # pip 安装规格
    module: str  # 导入验证用的模块名
    installed: bool


def _declared_specs() -> list[tuple[str, str]]:
    """(匹配名, pip 规格) 列表：主依赖 + 可选组（extras 组名也算匹配名）。"""
    try:
        import tomllib

        with open(_PYPROJECT, "rb") as f:
            data: dict[str, Any] = tomllib.load(f)
        project = data.get("project", {})
        pairs: list[tuple[str, str]] = []
        for spec in project.get("dependencies", []):
            pairs.append((Requirement(spec).name.lower(), str(spec)))
        for group, group_specs in (project.get("optional-dependencies") or {}).items():
            for spec in group_specs:
                pairs.append((str(group).lower(), str(spec)))
                pairs.append((Requirement(spec).name.lower(), str(spec)))
        if pairs:
            return pairs
    except Exception as e:  # noqa: BLE001 - 解析失败走静态兜底
        logger.warning(f"[ArknightsUID·依赖] pyproject 解析失败，用静态兜底: {e}")
    return _FALLBACK_SPECS


def _module_name(spec: str) -> str:
    return Requirement(spec).name.lower().replace("-", "_")


def is_installed(module: str) -> bool:
    """模块当前是否可导入（依赖懒装后即时可见，调用前需 invalidate_caches）。"""
    return importlib.util.find_spec(module) is not None


def resolve_dependency(name: str) -> tuple[str, str] | None:
    """用户输入 → (pip 规格, 模块名)。不在白名单返回 None。"""
    key = name.strip().lower()
    key = _ALIAS.get(key, key)
    if not key:
        return None
    for match_key, spec in _declared_specs():
        if match_key == key:
            return spec, _module_name(spec)
    return None


def dependency_status() -> list[DepEntry]:
    """全部可装依赖的安装状态（无参数用法时的列表输出）。"""
    by_spec: dict[str, DepEntry] = {}
    for key, spec in _declared_specs():
        module = _module_name(spec)
        entry = by_spec.get(spec)
        if entry is None:
            entry = DepEntry(
                keys=[],
                spec=spec,
                module=module,
                installed=importlib.util.find_spec(module) is not None,
            )
            by_spec[spec] = entry
        if key not in entry.keys:
            entry.keys.append(key)
    # 友好别名并入对应条目（resolve 认的别名在列表里也要能看见）
    for alias, dep in _ALIAS.items():
        for entry in by_spec.values():
            if Requirement(entry.spec).name.lower() == dep:
                if alias not in entry.keys:
                    entry.keys.append(alias)
    return list(by_spec.values())


def _pip_install_and_verify(spec: str, module: str) -> bool:
    """同步执行：core 同款多镜像 pip 安装 + 导入验证（放线程里跑）。"""
    try:
        from gsuid_core.server import install_packages
    except ImportError as exc:
        # gsuid_core 内部安装器路径变动时的可读降级（不静默假成功）
        logger.warning(
            f"[ArknightsUID·依赖] core 安装器不可用: {exc.__class__.__name__}"
        )
        return False
    install_packages([spec])
    importlib.invalidate_caches()
    return importlib.util.find_spec(module) is not None


async def install_dependency(name: str) -> tuple[bool, str]:
    """装一个白名单依赖。返回 (成功?, 面向用户的消息)。"""
    resolved = resolve_dependency(name)
    if resolved is None:
        known = "、".join("/".join(entry.keys) for entry in dependency_status())
        return False, f"未知依赖名「{name.strip()}」。可用：{known}"
    spec, module = resolved
    if importlib.util.find_spec(module) is not None:
        return True, f"{module} 已安装，无需重复安装。"
    if _install_lock.locked():
        return False, "已有安装任务进行中，请等它结束再试。"
    async with _install_lock:
        ok = await asyncio.to_thread(_pip_install_and_verify, spec, module)
    if ok:
        return True, f"✅ {spec} 安装成功，相关功能即时可用（无需重启）。"
    return False, (
        f"❌ {spec} 安装失败（所有镜像源均不可用？）。"
        f'可稍后重试，或手动执行：pip install "{spec}"'
    )

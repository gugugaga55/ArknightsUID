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
import re
from pathlib import Path
from typing import Any, NamedTuple

from gsuid_core.logger import logger

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _req_name(spec: str) -> str:
    """解析 PEP 508 依赖名（惰性用 packaging，缺省走正则兜底）。"""
    try:
        from packaging.requirements import Requirement

        return Requirement(spec).name.lower()
    except Exception:
        return re.split(r"[<>=!~;\[\s]", spec.strip(), maxsplit=1)[0].strip().lower()

# 友好别名 → 依赖组名/依赖名（pyproject 里没有但用户顺口会打的词，以及把单组件收敛到整组）
_ALIAS = {"截图识别": "ocr", "rapidocr": "ocr", "onnxruntime": "ocr"}

_install_lock = asyncio.Lock()

# pyproject 解析失败时的静态兜底（正常不会走到；防止 pyproject 被挪走
# 后命令直接不可用）
_FALLBACK_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("ocr", ("rapidocr>=3.0.0", "onnxruntime>=1.16.0")),
    ("takumi", ("pytakumi>=0.1.4",)),
    ("pytakumi", ("pytakumi>=0.1.4",)),
]


class DepEntry(NamedTuple):
    keys: list[str]  # 可用匹配名（含别名/组名，小写）
    specs: tuple[str, ...]  # pip 安装规格组
    modules: tuple[str, ...]  # 导入验证用的模块名组
    installed: bool

    @property
    def spec(self) -> str:
        return " ".join(self.specs)


def _declared_groups() -> list[tuple[str, tuple[str, ...]]]:
    """(匹配名, pip 规格元组) 列表：主依赖 + 可选组（组名与组内包名统一映射到整组）。"""
    try:
        import tomllib

        with open(_PYPROJECT, "rb") as f:
            data: dict[str, Any] = tomllib.load(f)
        project = data.get("project", {})
        pairs: list[tuple[str, tuple[str, ...]]] = []
        for spec in project.get("dependencies", []):
            pairs.append((_req_name(spec), (str(spec),)))
        for group, group_specs in (project.get("optional-dependencies") or {}).items():
            specs_tuple = tuple(str(s) for s in group_specs)
            if not specs_tuple:
                continue
            pairs.append((str(group).lower(), specs_tuple))
            for spec in specs_tuple:
                pairs.append((_req_name(spec), specs_tuple))
        if pairs:
            return pairs
    except Exception as e:  # noqa: BLE001 - 解析失败走静态兜底
        logger.warning(f"[ArknightsUID·依赖] pyproject 解析失败，用静态兜底: {e}")
    return _FALLBACK_GROUPS


def _module_name(spec: str) -> str:
    return _req_name(spec).replace("-", "_")


def is_installed(modules: str | tuple[str, ...]) -> bool:
    """模块（组）当前是否全部可导入（依赖懒装后即时可见，调用前需 invalidate_caches）。"""
    if isinstance(modules, str):
        modules = (modules,)
    return bool(modules) and all(
        importlib.util.find_spec(m) is not None for m in modules
    )


def resolve_dependency(name: str) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """用户输入 → (pip 规格元组, 模块名元组)。不在白名单返回 None。"""
    key = name.strip().lower()
    key = _ALIAS.get(key, key)
    if not key:
        return None
    for match_key, specs in _declared_groups():
        if match_key == key:
            return specs, tuple(_module_name(s) for s in specs)
    return None


def dependency_status() -> list[DepEntry]:
    """全部可装依赖的安装状态（无参数用法时的列表输出）。"""
    by_specs: dict[tuple[str, ...], DepEntry] = {}
    for key, specs in _declared_groups():
        modules = tuple(_module_name(s) for s in specs)
        entry = by_specs.get(specs)
        if entry is None:
            entry = DepEntry(
                keys=[],
                specs=specs,
                modules=modules,
                installed=is_installed(modules),
            )
            by_specs[specs] = entry
        if key not in entry.keys:
            entry.keys.append(key)
    # 友好别名并入对应条目（resolve 认的别名在列表里也要能看见）
    for alias, target_key in _ALIAS.items():
        for entry in by_specs.values():
            if target_key in entry.keys and alias not in entry.keys:
                entry.keys.append(alias)
    return list(by_specs.values())


def _pip_install_and_verify(
    specs: tuple[str, ...], modules: tuple[str, ...]
) -> bool:
    """同步执行：core 同款多镜像 pip 安装 + 全模块导入验证（放线程里跑）。"""
    try:
        from gsuid_core.server import install_packages
    except ImportError as exc:
        # gsuid_core 内部安装器路径变动时的可读降级（不静默假成功）
        logger.warning(
            f"[ArknightsUID·依赖] core 安装器不可用: {exc.__class__.__name__}"
        )
        return False
    install_packages(list(specs))
    importlib.invalidate_caches()
    return all(importlib.util.find_spec(m) is not None for m in modules)


async def install_dependency(name: str) -> tuple[bool, str]:
    """装一个白名单依赖（组）。返回 (成功?, 面向用户的消息)。"""
    resolved = resolve_dependency(name)
    if resolved is None:
        known = "、".join("/".join(entry.keys) for entry in dependency_status())
        return False, f"未知依赖名「{name.strip()}」。可用：{known}"
    specs, modules = resolved
    if is_installed(modules):
        return True, f"{'/'.join(modules)} 已安装，无需重复安装。"
    if _install_lock.locked():
        return False, "已有安装任务进行中，请等它结束再试。"
    async with _install_lock:
        ok = await asyncio.to_thread(_pip_install_and_verify, specs, modules)
    joined = " + ".join(specs)
    if ok:
        return True, f"✅ {joined} 安装成功，相关功能即时可用（无需重启）。"
    pip_args = " ".join(f'"{s}"' for s in specs)
    return False, (
        f"❌ {joined} 安装失败（所有镜像源均不可用？）。"
        f"可稍后重试，或手动执行：pip install {pip_args}"
    )

"""ArknightsUID 渲染层。

仅提供渲染引擎（Playwright 池 + 远程渲染 + 字体挂载），
**不包含任何 HTML 模板**——模板由各业务模块在 `templates/` 目录自研。

所有视觉产物由开发者自行设计，不使用任何第三方项目模板。
"""
from __future__ import annotations

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..path import TEMPLATES_PATH

# 共享 jinja2 Environment，所有业务模块用同一个实例
# （避免每个模块各自初始化浪费内存）
# 注意：保持同步渲染（不开 enable_async），与 EndUID 行为一致——
# render_utils.render_html 内部用 template.render() 同步调用，
# 开 enable_async 会把 render 变成协程导致 "never awaited" 警告。
ark_templates = Environment(
    loader=FileSystemLoader(str(TEMPLATES_PATH)),
    autoescape=select_autoescape(["html", "xml"]),
)

from .render_utils import render_html, image_to_base64, get_image_b64_with_cache  # noqa: E402,F401

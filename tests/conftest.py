"""测试环境修正：解除外层仓库目录对内层 ArknightsUID 包的遮蔽。

仓库根目录自带一个 __init__.py（gsuid_core 插件加载标记），而 pytest 在
rootdir=gsuid_core 下收集时会把 plugins/ 目录顶到 sys.path 前端，导致
`import ArknightsUID` 命中外层仓库目录（其下没有 utils/ 等子包）。

这里在收集测试模块之前就以正确路径把内层包导入并缓存，
后续所有 `import ArknightsUID.xxx` 直接命中缓存，不受路径顺序影响。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules.pop("ArknightsUID", None)

import ArknightsUID  # noqa: F401,E402 —— 立即以仓库根路径缓存内层包

assert ArknightsUID.__file__ is not None and str(ROOT) in ArknightsUID.__file__, (
    f"ArknightsUID 解析到了错误的位置: {ArknightsUID.__file__}"
)

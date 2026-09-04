"""init"""
import os

# 双加载模式兼容垫片（消除全插件的 try/except 双导入回退）：
# - 生产（nest 模式）：loader 把 plugins/ 追加进 sys.path，`import ArknightsUID`
#   命中本外层包；把内层同名包目录并入 __path__ 后，短拼写
#   `ArknightsUID.utils.x` / `ArknightsUID.arknightsuid_x` 可直接解析，
#   与测试模式（仓库根在 sys.path，ArknightsUID 即内层包）解析到同一组
#   模块对象——处理器因此可以用单一短绝对导入，不再需要回退分支。
__path__.append(os.path.join(os.path.dirname(__file__), "ArknightsUID"))

# ArknightsUID

GsCore 明日方舟插件，优先复用 EndUID 的森空岛登录凭据。

## 功能

### EndUID / 森空岛

- `ark账号` / `ark我的方舟`: 查看当前可复用的明日方舟账号。
- `ark绑定列表`: 查看 EndUID 中记录的明日方舟 UID 与凭据状态。
- `ark理智`: 查询默认账号理智和基础状态。
- `ark每日` / `ark日常`: 查询默认账号森空岛日常状态。
- `ark理智 2` / `ark理智 123456789`: 按列表序号或 UID 查询指定账号。
- `ark签到` / `ark签到 2` / `ark签到 123456789`: 对默认或指定账号发起森空岛签到。

### PRTS / Wiki

- `arkwiki 词条`: 查询 PRTS 词条摘要。
- `ark干员 干员名`: 查询干员资料。
- `ark材料 材料名`: 查询材料资料。
- `ark敌人 敌人名`: 查询敌人资料。
- `ark关卡 1-7`: 查询关卡信息、掉落、敌人和 PRTS.Map 链接。

### 帮助

- `ark帮助`: 查看插件帮助。

## 说明

本插件不重复实现森空岛登录。请先安装并登录 EndUID；EndUID 在登录时写入的 `EndBind.ark_uid` 与 `EndUser(game_id=1)` 会被本插件自动读取。

凭据复用范围：

- `EndUser.cookie` 作为森空岛 `cred` 使用。
- `EndUser.token` 作为森空岛 API token 缓存使用；失效时会用 `cred` 刷新。
- `EndUser.hg_token` 仅作为可复用登录态字段保留，不会在命令输出中展示。

安全约束：命令回复、帮助和 README 都不会输出 `cookie` / `cred` / `token` / `hg_token` 明文。

# 森空岛 2.0.0 → 2.2.0 接口差异复核

复核日期：2026-10-10（Asia/Singapore）。对象为 [原始对比笔记](SKLAND-API-DIFF-2.0.0-VS-2.2.0-2026-10-10.md)。本次只读检查 APK，并做匿名 GET 探测，没有使用私人凭据，没有执行设置、编辑、发送、解绑等写请求。本文记录发现与证据；原笔记的纠正已另行处理。

## 结论

原笔记的 RN bundle 相同、七个 native 新路径及七个 native 不再引用的路径，均得到原始字节支持。但以下结论需要收窄或纠正：

1. `user/pendant/list`、`user/pendant/configure`、`user/edit` 在 **2.0.0 的 RN bundle 中已存在**；2.2.0 是新增 native 引用，不能称为本次新增服务端接口。
2. native 不再引用旧路径，只能说明这两个 APK 中的调用面变化，不能证明服务端下线；旧版已经包含明日方舟名片装饰页和好友页，不能据此推断本次才新增页面或整体替换装饰体系。
3. 相同的内置 RN bundle 不代表线上 Web/H5 或服务端无变化。动态加载的网页和资源可以独立发布。
4. 静态明文路径、类名、方法名同存，不能直接还原 Retrofit 注解、主机选择、签名拦截器、参数或响应结构。新路径可在 `zonai.skland.com` 接受匿名 GET 校验，是另有当前在线证据支持的事实。
5. `chart.skland.com/arknights/game-card` 是明日方舟名片 H5 页面。仅凭 URL 不能称其为“战绩卡图片生成接口”；页面可包含既有肉鸽等战绩摘要，但没有证据说明它是新肉鸽或 auto-chess 的独立战绩接口。
6. 当前 `conf/resource` 公开返回的是 **签到动画资源配置**，不能直接当作头像、挂件、状态卡背景目录。

## 原始样本及哈希

通过 `aapt dump badging` 独立核对包名、版本号和 versionCode；两者包名均为 `com.hypergryph.skland`。原文所列构建日期不作为本次已经验证的事实。

| 样本 | versionName / versionCode | 原始位置 | APK SHA256 |
|---|---|---|---|
| 旧 | 2.0.0 / 200000200 | `d1adcd442f3e2cafa0ad706e9d5600c9.apk` | `3444fb0f865293e7eaf8576eb016990adf188e131241719daf6a75b8d2264a21` |
| 新 | 2.2.0 / 200200057 | `23bd2915c3aafefb2ac2221e99a0f8d0.apk` | `dfb763a21a25b1850a0db6ab7fa732d0e83bb1956df83af3fdb874875a57e432` |

直接从 ZIP 读取 `assets/rn/main.jsbundle` 和 **全部根目录 `classes*.dex`**，未依赖既有 `strings` 文件。两个 APK 都只有一个 `classes.dex`，没有遗漏 classes2.dex 等文件。

| ZIP 内文件 | 旧版字节数 | 新版字节数 | SHA256 |
|---|---:|---:|---|
| `assets/rn/main.jsbundle` | 3539736 | 3539736 | 两者均为 `22171b12e7f180fcb913b8b155b64d0b9286ce6f3fcaa345031c61ba6c9dbf64` |
| 旧 `classes.dex` | 42963996 | — | `8a374f016dbbd391398ac0fb04683dd3f5247e70044506345a55a2fb19d74f7c` |
| 新 `classes.dex` | — | 43885580 | `dc22e0df5b81ccd38cc5c5da00da606298d39df42ed55ed9dff2a8540ef74246` |

两份 RN 文件还直接比较了文件内容，完全相同；头部 magic 为 `c61fbc03c103191f`。两份 DEX 的 magic 均为 `dex\n037\0`，头部 `class_defs_size=5`，`string_ids_size=271`，标准头部声明的数据段约 13 KB，远小于整个文件的约 43 MB。业务路径处在较后位置，例如新版 `game/friend/list` 字节起点为 35397575。这支持“有壳及额外业务 payload”的观察，但本次没有恢复受保护 DEX，也不把“内嵌 DEX 头部被抹掉”作为已确定的加固实现细节。

## 路径原始字节命中

下表统计完整表列 ASCII 字节串出现次数。RN 中的三个旧接口直接在相同 bundle 中命中；不需要从 native 调用推断。

| 路径 / URL | 旧 RN | 新 RN | 旧全部 DEX | 新全部 DEX | 能支持的结论 |
|---|---:|---:|---:|---:|---|
| `/api/v1/game/friend/list` | 0 | 0 | 0 | 1 | 新增 native 路径引用 |
| `/api/v1/game/player/default-binding` | 0 | 0 | 0 | 1 | 新增 native 路径引用 |
| `/api/v1/user/avatar/category/list` | 0 | 0 | 0 | 1 | 新增 native 路径引用 |
| `/api/v1/user/pendant/list` | 1 | 1 | 0 | 1 | RN 早已存在，新增 native 引用 |
| `/api/v1/user/pendant/configure` | 1 | 1 | 0 | 1 | RN 早已存在，新增 native 引用 |
| `/api/v1/user/edit` | 1 | 1 | 0 | 1 | RN 早已存在，新增 native 引用 |
| `/api/v1/conf/resource` | 0 | 0 | 0 | 1 | 新增 native 路径引用 |
| `/api/v1/game/cards` | 0 | 0 | 2* | 0 | 旧 native 引用在新版不再出现 |
| `/api/v1/game/cards/sort` | 0 | 0 | 1 | 0 | 同上 |
| `/api/v1/gameplat/list` | 0 | 0 | 1 | 0 | 同上 |
| `/api/v1/gameplat/account/unbind` | 0 | 0 | 1 | 0 | 同上 |
| `/api/v1/user/game/card/decoration/background/list` | 0 | 0 | 1 | 0 | 同上 |
| `/api/v1/user/game/card/decoration/character/search` | 0 | 0 | 1 | 0 | 同上 |
| `/api/v1/user/game/card/decoration/configure` | 0 | 0 | 1 | 0 | 同上 |
| `https://chart.skland.com/arknights/game-card` | 0 | 0 | 1 | 1 | 两版均有 native 完整 URL |
| `https://zonai.skland.com` | 0 | 0 | 1 | 1 | 两版均有 native 主机常量 |
| `https://assets.skland.com/skland-game/json/arknights/game-data.json` | 1 | 1 | 0 | 0 | 两版均有 RN 资源 URL |

\* `game/cards` 的 2 次是字节子串计数：包含 `game/cards/sort` 那一次。计数 `b'/api/v1/game/cards\0'` 可得到独立终止的原始路径为 1 次，不能把子串数当作调用次数。

完整 chart URL 两版 RN 均不命中；RN 中确有 `chart.skland.com` 主机子串（各 1 次），但不含 `game-card`。Hermes 存储可能复用字符串片段，原始字节命中不等于完整语义分析；上述计数可证明完整 URL 在 native，但不能据此断言 RN 运行时绝无该 URL。原笔记把完整路由归为已核实 RN 接口，证据不足。

## 方法名、类和功能历史

这里的命中只是原始 payload 中的符号或文字证据，不是已解析的调用关系。

| 标记 | 旧 DEX | 新 DEX | 解释 |
|---|---:|---:|---|
| `getGameFriendList` | 0 | 1 | GET 的合理线索，不足以独立证明参数和响应 |
| `Lcom/hypergryph/skland/messagepage/friend/api/FriendService;` | 0 | 1 | 新服务类型符号 |
| `Lcom/hypergryph/skland/gameinfo/GameInfoService;` | 0 | 1 | 新服务类型符号 |
| `Lcom/hypergryph/resourcemanager/resource/impl/ResourceApiService;` | 0 | 1 | 新资源服务类型符号 |
| `getPendantList` | 0 | 1 | 新 native 方法名；路径在旧 RN 已存在 |
| `getAvatarCategory` | 0 | 1 | GET 的合理线索 |
| `postEditAvatarPendant` | 0 | 1 | POST 的合理线索；本次未请求 |
| `postEditUserInfo` | 0 | 1 | POST 的合理线索；本次未请求 |
| `PostEditUserAvatarPendantBody` | 4 | 5 | body 类型文字旧版已经出现 |
| `ArknightsFriendList.kt` | 5 | 6 | 好友页源文件线索旧版已有 |
| `GameCardArknightsDecorationPage` | 6 | 7 | 方舟名片装饰页旧版已有 |
| `ArknightsDecorationBottomInfo` | 5 | 7 | 同上 |
| `UserEditBackgroundFragment` | 12 | 14 | 背景编辑页旧版已有 |
| `HomeGameConfigRepo` | 0 | 2 | 新主页游戏配置符号 |
| `SteamGameCardSection` | 0 | 2 | 新 Steam 游戏卡片符号 |

`getDefaultBinding` 两版均未命中，未解析出 `default-binding` 对应的 Retrofit 注解。不要把“默认绑定查询”与“切换默认号”的写操作混为一谈，也不要直接采用原文的 GET/PUT 二选一作为完整协议。主机常量与相对路径在同一 APK 出现，也不能单独证明全部请求经过同一 base URL 或完全相同的签名链。

## 2026-10-10 当前在线只读证据

以下为匿名 GET 探测，空 cred、空 token、platform=1、vName=2.2.0；没有私人登录态。第一方来源均为 `https://zonai.skland.com`。原始结果保存在 `/tmp/arkuid-skland-readonly-probes-20261010.json`。这些在线结果发生在复核日，不是两个历史版本的服务端快照。

| 路径 | HTTP | JSON code / message | 可支持的范围 |
|---|---:|---|---|
| `/api/v1/game/friend/list` | 400 | 10001 / 参数错误 | 当前 GET 进入参数校验，查询字段未知 |
| `/api/v1/game/player/default-binding` | 401 | 10002 / 用户未登录 | 当前 GET 进入鉴权，账号数据结构未知 |
| `/api/v1/user/avatar/category/list` | 401 | 10002 / 用户未登录 | 同上 |
| `/api/v1/user/pendant/list` | 400 | 10001 / 参数错误 | 当前 GET 进入参数校验，查询字段未知 |
| `/api/v1/conf/resource` | 200 | 0 / OK | 返回了当前公开资源配置 |
| `/api/v1/game/player/info` | 400 | 10001 / 参数错误 | 既有路径的对照 |
| `/api/v1/arkuid-audit-nonexistent-route` | 404 | 非 JSON，`404 page not found` | 虚构路径对照 |

这些结果不能证明“GET 是唯一允许的方法”、写方法、必需参数名、有效账号响应或所有请求都可复用插件签名。特别是 400/401 可能来自网关或统一校验层，不等于业务返回已经核验。

好友接口另做了不涉及真实账号的参数对照：仅 `gameId=1` 仍为 400 / 10001；仅 `uid=0`、`uid=0&gameId=1`、`uid=0&gameId=1&userId=0` 均变为 401 / 10002。原始结果为 `/tmp/arkuid-skland-friend-params-20261010.json`。这说明 `uid` 参与路由或参数校验，不能证明成功请求只需要 uid，也不能证明 gameId、userId 是否必需、分页字段和返回结构。

### conf/resource 的实际用途

第一方 [GET /api/v1/conf/resource](https://zonai.skland.com/api/v1/conf/resource) 用裸 GET、不带任何凭据或签名，也返回 code=0。当前 `data.list` 长度为 1，内容为 `type=checkin`、`version=2026090301`、`zipUrl=https://bbs.hycdn.cn/asset/skin/checkin_d9125b.zip`，`resources` 长度为 6：`gameId1`、`gameId3`、`gameId2`、`gameId4`、`gameId100`、`gameId101`，每项包含 `lottie` / `darkLottie` 文件名，例如 `arknights_checkin_lottie.json`。

原始 JSON：`/tmp/arkuid-skland-resource-20261010.json`。这次实际结果是签到动画配置；不能据此为状态卡增加头像、挂件或背景字段。本次未下载或解包资源 ZIP。

### game-data.json 的覆盖范围

第一方 [game-data.json](https://assets.skland.com/skland-game/json/arknights/game-data.json) 当前 GET 为 HTTP 200、5535 字节，顶层为 `activityMap`（29 项）、`rogueMap`（4 项）、`bossRushCodeBgMap`（5 项）。`rogueMap` 只有 `rogue_1` 至 `rogue_4`，截至萨卡兹主题，没有后续肉鸽主题。

原始 JSON：`/tmp/arkuid-skland-game-data-20261010.json`。因此不能把这个 CDN JSON 当作完整的当前肉鸽列表，也不能因其中缺主题就断言主题未被账号接口支持。

### chart/game-card 的含义

第一方 [明日方舟名片页面](https://chart.skland.com/arknights/game-card) 当前 GET 为 HTTP 200、`text/html`、2295 字节，HTML 加载官方 CDN 的 [main.1e0124.js](https://bbs.hycdn.cn/skland-fe-static/skland-chart/main.1e0124.js)。重新获取该脚本得到 HTTP 200，内容与本地 `/tmp/skland_apk/chart/main.1e0124.js` 逐字节相同（70278 字节，SHA256 `71ac33b33ec264654412d3a997b4ace6f10b55b26dbf4c66616a5e29090b851f`）。脚本包含 `/api/v1/game/item-card`、`/api/v1/game/item-card/send`、状态 `akGameCard`、`handleAddFriend`，另有 `rogueList` 和既有肉鸽摘要转换代码。`handleAddFriend` 的参数线索是 `gameId`、`uid`、`itemId`、`userId`。本次 HTML 保存在 `/tmp/arkuid-skland-chart-20261010.html`。

这是“名片展示及好友交互页面”的直接线索；没有执行 `send` 或加好友方法，没有把该 URL 作为图片 API 调用，也没有据此认定存在新的肉鸽独立接口。页面可以包含战绩摘要，与“URL 本身是战绩卡图片生成 API”是不同的命题。

## 对后续接入的建议

### 后续已确认：公开头像目录可通过 Web 接口读取

2026-10-10 继续读取第一方 [www.skland.com](https://www.skland.com/) 当前首屏脚本 [3158.cd61aa37.js](https://bbs.hycdn.cn/skland-fe-static/skland-bbs/3158.cd61aa37.js)，确认头像组件直接调用：

```javascript
() => Ae("/web/v1/user/avatar/category/list")
(e = 0) => Ae("/web/v1/user/avatars", {categoryId: e})
// 成功后取 t.data.list；列表项使用 e.id 与 e.url。
```

使用插件现有 `web_public_get` 的公共 token/设备签名链实网读取成功，无私人凭据：分类接口返回 5 项，其中 `{id: 2, title: "明日方舟"}`；头像接口带 `categoryId=2` 返回 422 项，每条含 `id`、`url`，包括路径日期为 2026-10-09 的新素材。本次未下载所有头像图片。原始匿名响应为 `/tmp/arkuid-public-avatar-contract-20261010.json` 与 `/tmp/arkuid-public-avatar-arknights-20261010.json`。

因此官方头像素材已有可靠的公开读取入口，可以复用现有公共 Web 客户端，不需要强行从 `conf/resource` 获取，也不需要先解决 native `api/v1/user/avatar/category/list` 的私人鉴权。上述 Web 目录与 native API 是不同路径，不把两者的参数或鉴权规则混用。

### 优先级

- **最高优先级：好友查询。** 继续确认 `friend/list` 的 query、响应和分页，但当前还不足以写成可靠好友查询功能；已有 UI 符号也不能证明它首次支持游戏好友。
- **次优先级：默认账号语义。** `default-binding` 先确认为读查询；插件已经通过 [本地账号选择](../ArknightsUID/utils/account_switch.py) 和 `ark切换` 支持默认查询账号，不需要因为官方出现同名概念就替换现有行为。森空岛默认角色与插件本地默认账号是否应同步是独立产品决定；切换官方账号仍需核对已知 `default-switch` 或对应官方调用，不能猜 PUT。
- 头像素材已有上述明确的公开 Web 契约；挂件仍需确认参数和成功响应。资料编辑不是读取素材所必需的接口。
- `conf/resource` 当前明确服务签到动画，对普通状态图卡的帮助有限。
- 本文没有新肉鸽、黑流树海或 auto-chess 独立接口的证据；这些需求需要另查官方页面调用，不能从本 diff 得出“已支持”或“未支持”。
- 既有路径仍在 APK、RN 相同或匿名错误码相同，都不足以证明真实账号字段和分页完全兼容；本次没有完成真实账号协议验证。

## 复查命令

以下脚本只读两个原始 APK，可复现哈希、全部 DEX 枚举、路径和符号计数。给 `needles` 增加上述表的完整字符串即可逐项复查；`bytes.count` 统计子串，不统计运行时调用。

```python
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile
import re

samples = {
    "old": "d1adcd442f3e2cafa0ad706e9d5600c9.apk",
    "new": "23bd2915c3aafefb2ac2221e99a0f8d0.apk",
}
needles = [
    "/api/v1/game/friend/list", "/api/v1/game/player/default-binding",
    "/api/v1/user/pendant/list", "/api/v1/user/pendant/configure",
    "/api/v1/user/edit", "/api/v1/conf/resource",
    "https://chart.skland.com/arknights/game-card",
    "ArknightsFriendList.kt", "GameCardArknightsDecorationPage",
]
bundles = []
for version, path in samples.items():
    print(version, "apk", sha256(Path(path).read_bytes()).hexdigest())
    with ZipFile(path) as archive:
        names = ["assets/rn/main.jsbundle"] + [
            n for n in archive.namelist()
            if re.fullmatch(r"classes(?:[0-9]+)?\.dex", n)
        ]
        for name in names:
            data = archive.read(name)
            print(name, len(data), sha256(data).hexdigest())
            print({n: data.count(n.encode()) for n in needles})
        bundles.append(archive.read("assets/rn/main.jsbundle"))
assert bundles[0] == bundles[1]
```

版本元数据：`aapt dump badging <APK路径>`。受保护 native payload 尚未还原；本次未声称已解析 HTTP 注解或请求 DTO，也未修改生产代码。

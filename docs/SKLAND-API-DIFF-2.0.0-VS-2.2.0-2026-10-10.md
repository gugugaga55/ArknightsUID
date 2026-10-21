# 森空岛 Skland API 对比：v2.0.0 vs v2.2.0

> 首版：2026-10-10 · 复核：2026-10-10（[复核记录](SKLAND-API-DIFF-REVIEW-2026-10-10.md)）· REA 再分析：2026-10-13
> 对比样本：
> - **旧版** `d1adcd442f3e2cafa0ad706e9d5600c9.apk` — Skland **2.0.0**（versionCode 200000200）
> - **新版** `23bd2915c3aafefb2ac2221e99a0f8d0.apk` — Skland **2.2.0**（versionCode 200200057）
>
> 注：中间还有一个 2.1 版本未取得样本，本对比的基准是 2.0.0。

## 分析方法与证据等级

- 两版 RN 侧 `assets/rn/main.jsbundle`（Hermes 字节码）**字节级完全相同**；这只能证明内嵌 RN 资源相同，不能证明远程 Web/H5 或服务端没有变化。
- 接口引用差异来自原生 Kotlin 层 `classes.dex`（43.0MB → 43.9MB）。
- **加固已识别**：壳类为 `com.secneo.apkwrapper.*`（SecNeo/思涅奥），标准 DEX 头部只声明 5 个类，其余 43MB 为运行时解密的业务 payload。本次尝试重建内嵌 DEX（map_list/基址扫描）未成功——SecNeo 对索引表做了变换，无法静态还原 Retrofit 注解；但 payload 字符串池为明文（ULEB 长度 + UTF-8 + `\0`，按内容排序）。
- **字符串池精确 diff**（REA 再分析）：从两版 payload 各提取约 **5.2 万条**池条目（含 1~7 字符短字符串），集合 diff 后复核首版结论。另做全 APK 文件级 SHA256 diff（约 3550 个文件）。
- **HTTP 动词为实测**（2026-10-13，无凭证）：该网关**先路由后鉴权**——错误动词返回 `404 "page not found"`（纯文本），正确动词返回 401 `10002 用户未登录` 或 400 `10001 参数错误`。据此可无登录分辨动词。
- 证据等级：动词/参数触发/旧接口存活 = **observed**；接口用途归类 = **inferred**（依据类名/方法名）。

## 复核与再分析后的最终结论

1. **新增原生路径引用 7 个**（首版 9 个中的 `/api/FriendService` 是类名碎片假阳性；`/api/remote_config/...` 归入内部节）。
2. 其中 `user/pendant/list`、`user/pendant/configure`、`user/edit` **在 2.0.0 的 RN bundle 中已存在**——2.2.0 是补 native 调用面，不是新服务端接口。真正全新上线路径的是：`game/friend/list`、`game/player/default-binding`、`user/avatar/category/list`、`conf/resource`。
3. **7 个旧路径仅是客户端移除引用，服务端实测仍在线**（2026-10-13 正确动词请求返回 401/400，说明路由仍存在并要求登录；错误动词 404 为对照）。老客户端和既有第三方调用不受影响。
4. `ArknightsFriendList`（旧 8 处→新 10）、`GameCardArknightsDecoration`（24→27）、`UserEditBackgroundFragment`（54→56）**旧版已存在**，2.2.0 只是引用增多；`FriendService`、`GameInfoService`、`SteamGameCardSection`、`HomeGameConfigRepo` 为**真正新增符号**。
5. HTTP 动词全部实测确认（见下表），不再停留在方法名推断。

## 新增/变化的接口（动词实测于 2026-10-13）

全部挂 `zonai.skland.com`；鉴权接口走森空岛 app 签名（与插件 `skland/client.py` `_generate_sign` 同源，未做登录态端到端验证）。

| 接口 | 动词(实测) | 无凭证实测 | 用途（inferred） | 2.0.0 是否已有 |
|---|---|---|---|---|
| `/api/v1/game/friend/list` | **GET** | 错动词 404；缺参 400；带 `uid` 后 401 | 明日方舟游戏内好友列表（`FriendService.getGameFriendList`）★ | 全新 |
| `/api/v1/game/player/default-binding` | **GET** | 401 | 默认绑定角色读取（`GameInfoService`）；是否支持写未验证 | 全新 |
| `/api/v1/user/avatar/category/list` | **GET** | 401 | 头像分类列表（`getAvatarCategory`/`getAvatarList`） | 全新（web 侧另有 `/web/v1/user/avatar/category/list`） |
| `/api/v1/user/pendant/list` | **GET** | 缺参 400（必填参数未定） | 头像挂件列表（`getPendantList`） | RN 已有 |
| `/api/v1/user/pendant/configure` | **POST** | 401 | 设置挂件（body 类型 `PostEditUserAvatarPendantBody`，旧版已出现该类型字样） | RN 已有 |
| `/api/v1/user/edit` | **POST** | 空 body 400 | 编辑昵称/头像（`postEditUserInfo`，body DTO `PostEditUserBody` 为新增字样） | RN 已有 `edit_button`，`edit` 本体需登录态确认 |
| `/api/v1/conf/resource` | **GET** | **200 无需鉴权** | 签到动画资源配置（新类 `ResourceApiService`） | 全新 |

### `/api/v1/conf/resource` 实测返回（公开，裸 GET 即可）

```json
{"code":0,"data":{"list":[{"type":"checkin","version":"2026090301",
  "zipUrl":"https://bbs.hycdn.cn/asset/skin/checkin_d9125b.zip",
  "resources":[
    {"id":"gameId1","lottie":"arknights_checkin_lottie.json","darkLottie":"arknights_checkin_dark_lottie.json"},
    {"id":"gameId3","lottie":"endfield_checkin_lottie.json", "...":"..."},
    {"id":"gameId2","lottie":"exastris_checkin_lottie.json","...":"..."},
    {"id":"gameId4","lottie":"popucom_checkin_lottie.json","...":"..."},
    {"id":"gameId100","lottie":"community_checkin_lottie.json"},
    {"id":"gameId101","lottie":"openUp_checkin_lottie.json"}]}]}}
```

**gameId 官方映射**（实测）：`1`=明日方舟、`2`=来自星尘（Ex Astris）、`3`=终末地（Endfield）、`4`=Popucom、`100`=社区、`101`=openUp。
注意：当前只返回签到动画配置，不能当作头像/挂件/背景素材目录（复核记录同此结论）。

### 客户端移除引用、服务端仍在线的路径（正确动词实测 401/400）

`/api/v1/game/cards`(GET)、`/api/v1/game/cards/sort`(POST)、`/api/v1/gameplat/list`(GET)、`/api/v1/gameplat/account/unbind`(POST)、`/api/v1/user/game/card/decoration/background/list`(GET)、`.../character/search`、`.../configure`(POST)。

### 内部/遥测（非业务）

- `https://game-config.hypergryph.com/api/remote_config/104/prod-download/default/Android/download-remote-config`
- `eventlog-service.hypergryph.com`、`event-log-api-ipv6.hypergryph.com`、`event-log-api-data-lake-prod-cn.hypergryph.com`（2.2.0 遥测迁入新 native 库 `libHGEventlog.so`，`assets/HGEventLogConfig.json` 相应移除）
- `https://user.hypergryph.com/protocol/plain/children_privacy`

## 其他实证变化（REA 全 APK diff，2026-10-13）

| 变化 | 证据 |
|---|---|
| `AndroidManifest.xml` 新增权限 `FOREGROUND_SERVICE`、`FOREGROUND_SERVICE_DATA_SYNC`、`com.google.android.gms.permission.AD_ID` | aapt xmltree diff；组件数不变（activity 53/53、service 21/21），即既有服务改为前台 data-sync 形态，配合新增的 `gamecenter/download/data/PackageObservation`、`InstallationTargetKey` 类，指向游戏中心下载管理增强 |
| `lib/arm64-v8a/libhpatchz.so` 尺寸 105728→122128 | 热更新（HPatchZ）库升级 |
| `lib/arm64-v8a/libHGEventlog.so` 新增、`assets/HGEventLogConfig.json` 移除 | 事件日志原生化 |
| 新增 JSON 字段 `gameCards`；新增 body DTO 字样 `PostEditUserBody` | 字符串池 diff |
| 其余约 90 个 .so 同尺寸不同哈希 | 重编译噪音（build-id 类变化），无实质差异信号 |
| `resources.arsc`、`assets/supplierconfig.json`（仅空白变化）、dexopt profile | 常规重打包 |

## RN 侧与外部资源（两版相同）

- `https://assets.skland.com/skland-game/json/arknights/game-data.json` — 实测 5535 字节：`activityMap`(29)、`rogueMap`(**仅 rogue_1..4，至萨卡兹**)、`bossRushCodeBgMap`(5)。不是完整肉鸽主题表。
- `https://chart.skland.com/arknights/game-card` — 明日方舟名片 H5 页（非图片 API）；其脚本调用 `/api/v1/game/item-card`、`/api/v1/game/item-card/send`，含 `handleAddFriend`（参数线索 `gameId`/`uid`/`itemId`/`userId`）。
- RN `/api/v1/*` 既有调用面：`game/attendance`、`game/player/info`、`game/player/bindingData`、`game/player/asset-show`、`game/player/show-configurable`、`game/player/default-switch`、`game/exastris/awardCount`、`user/avatar/list`、`user/info/*`、`creator/*`、`moderator/*`、`report/*`、`relation/*`、`score/*` 等。

## 对 ArknightsUID 的影响与机会

1. **兼容性**：插件现有接口（`game/player/info`、`game/attendance`、`game/player/binding`、`game/cultivate/*`、`game/arknights/rogue`、`auth/refresh`、`user`）两版引用未变，匿名对照探测行为正常（401/400 预期响应），无需改动；字符串未变不能保证登录态响应结构不变，暂无迁移证据。
2. **立即可用（公开，无鉴权）**：
   - `GET /api/v1/conf/resource` — 签到动画资源 + gameId 官方映射。
   - 官方头像目录（复核确认，走插件现有 `web_public_get` 公共 token 链）：`GET /web/v1/user/avatar/category/list`（5 个分类，`{id:2,title:"明日方舟"}`）→ `GET /web/v1/user/avatars?categoryId=2`（422 项，`{id,url}`，含 2026-10-09 新素材）。裸 UA 会被 `10001 设备信息无效` 拒绝，必须带设备签名链。
3. **新功能机会（需登录态）**：
   - `GET /api/v1/game/friend/list?uid=...`（`uid` 参与校验，`gameId`/分页字段待登录态确认）→ 明日方舟好友查询优先级最高。
   - `GET /api/v1/game/player/default-binding` — 先确认为纯读查询；插件已有本地 `ark切换`，不要把机器人默认账号与森空岛默认角色混为一项设置。
   - `GET /api/v1/user/pendant/list` — 必填参数未知，登录态下确认。
4. **注意事项**：`user/edit`/`pendant/configure` 为写操作，接入需真实账号验证 body 契约（候选字段 `nickname`/`avatarId`/`pendantId`，SecNeo 加固下无法静态解出）；不要为验证方法而盲发写请求。

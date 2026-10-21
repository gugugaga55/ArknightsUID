# 活动个人进度与玩法战绩接口调查（2026-10-06）

## 结论

**可以新增普通活动个人通关进度，并聚合部分玩法最高战绩，最小实现不需要新接口。** 当前森空岛第一方 H5 明确从既有 `GET /api/v1/game/player/info` 消费 `data.activity`、`data.tower`、`data.bossRush`、`data.sandbox` 等字段。普通活动卡显示各分区已通关关卡数之和 / 关卡总数之和；保全派驻显示最高进度，引航者试炼显示最高试炼档位和关卡。[第一方数据消费者 S2](https://bbs.hycdn.cn/skland-fe-static/skland-game/9560.b49ee94b.js) · [第一方展示组件 S3](https://bbs.hycdn.cn/skland-fe-static/skland-game/8624.b27ec983.js)

用户尚未给出具体活动名，因此本调查确认的是**通用能力及前端字段契约**，不声称某个当前活动必然已由服务端收录。没有使用真实账号、读取凭据或向鉴权接口发送请求；下文所有个人字段证据来自公开第一方 JavaScript，不是某位玩家的真实接口样本。

## 来源与可复查方式

- **S1，第一方入口**：[game.skland.com](https://game.skland.com/)。2026-10-06 实际获取 HTML，确认加载 S2、S4 等脚本。
- **S2，玩家信息请求、数据映射、玩法格式化**：[9560.b49ee94b.js](https://bbs.hycdn.cn/skland-fe-static/skland-game/9560.b49ee94b.js)。关键字符串：`/api/v1/game/player/info`、`getAkActInfo(e,t)`、`Qo={NORMAL:`。
- **S3，玩家战绩分类与展示**：[8624.b27ec983.js](https://bbs.hycdn.cn/skland-fe-static/skland-game/8624.b27ec983.js)。关键字符串：`case"activity":return`、`Ho=function`、`最高进度`、`最高剿灭数`。文件哈希由当前 S4 runtime 的 chunk 映射确认，并重新从官方 CDN 获取。
- **S4，服务接口及动态 chunk 映射**：[main-7bd12dde.57049ecf.js](https://bbs.hycdn.cn/skland-fe-static/skland-game/main-7bd12dde.57049ecf.js)。关键字符串：`/api/v1/game/arknights/auto-chess`、`/api/v1/game/arknights/rogue`、`getArknightsStrongholdInfo`。
- **S5，独立 auto-chess 展示页**：[4086.81bd7cd0.js](https://bbs.hycdn.cn/skland-fe-static/skland-game/4086.81bd7cd0.js)。由 S4 的 `./arknights/stronghold/index.tsx` → chunk 4086 映射定位，并重新获取。
- **S6，独立 auto-chess 页面模型**：[main-0a037d97.3091b5d8.js](https://bbs.hycdn.cn/skland-fe-static/skland-game/main-0a037d97.3091b5d8.js)。模块 `95932` 调用 `getArknightsStrongholdInfo(uid)` 并将响应 `data` 保存为 `state.info`。

下载的公开资源留在 `/tmp/activity-progress-research/`；关键未经改写的原文摘录位于该目录 `first-party-contract-excerpts.txt`。临时目录可随环境清理，持久证据以以上第一方 URL 和搜索字符串为准。

## player/info 的准确字段契约

**以下路径都直接位于响应信封 `data` 根级，不存在额外的 `data.gameData` 包装。** S2 成功分支直接读取 `t.data`，构造 `activityMap` 等索引并写回同一个 `t.data`；`getAkActInfo` 再从 `getGameData("ak")` 的 `.data` 消费。前端构造的 `activityMap`、`towerMap` 等是派生索引，后端原始输入应读取下表左列。[S2](https://bbs.hycdn.cn/skland-fe-static/skland-game/9560.b49ee94b.js)

| 能力 | 原始个人记录路径 | 元数据路径 | 第一方明确消费的内容 |
|---|---|---|---|
| 普通活动 | `data.activity[]`：`actId`、`zones[].clearedStage`、`zones[].totalStage` | `data.activityInfoMap[actId]`：`name`、`type`、`isReplicate`、`picUrl` | 汇总分区已通关数与总数，展示“进行中 / 已完成”和进度条 |
| 保全派驻 | `data.tower.records[]`：`towerId`、`best` | `data.towerInfoMap[towerId]`：`name`、`subName`、`picUrl` | “最高进度：best” |
| 引航者试炼 | `data.bossRush[]`：`id`、`record.stageId`、`record.difficulty`、`record.played`、`picUrl` | `data.stageInfoMap[stageId].code` | 标題常量“引航者试炼”；最高档位、关卡编号；未参与显示“暂无记录” |
| 生息演算 | `data.sandbox[]`：`id`、`maxDay`、`maxDayChallenge`、`mainQuest`、`subQuest`、`baseLv`、`unlockNode`、`enemyKill`、`createRift`、`fixRift` | 当前消费者不读取 `sandboxInfoMap`，不据此假设其存在 | 第一方通过 `getAkActInfo(...,"sandbox")` 传出除 `id` 外的记录字段；展示组件直接消费这些计数 |
| 肉鸽概要 | `data.rogue.records[]`：`rogueId`、`relicCnt`、`bank.current` | `data.rogueInfoMap[rogueId]`：`name`、`picUrl` | 投资系统计数、解锁道具数；点击跳转独立战绩页 |
| 剿灭概要 | `data.campaign.records[]`：`campaignId`、`maxKills` | `data.campaignInfoMap[campaignId]`：`name`、`campaignZoneId`、`picUrl`；`data.campaignZoneInfoMap[campaignZoneId].name` | 最高剿灭数，≥400 显示完成 |

表内路径来自 [S2](https://bbs.hycdn.cn/skland-fe-static/skland-game/9560.b49ee94b.js)，对应中文展示语义来自 [S3](https://bbs.hycdn.cn/skland-fe-static/skland-game/8624.b27ec983.js)。注意插件当前 `parse_player_info` 的剿灭解析将 `campaign.records` 当 dict；第一方当前消费者按数组遍历。此处只是调查记录，不在本轮修改旧解析。[本地解析](../ArknightsUID/utils/player/parse.py)

### 普通活动：能确认与不能确认的范围

S2 的 `activity` 分支明确采用以下规则：[S2](https://bbs.hycdn.cn/skland-fe-static/skland-game/9560.b49ee94b.js)

```javascript
const {activityMap:t, activityInfoMap:i} = n;
const {zones:a} = t[e];
const {name:o, type:r, isReplicate:s, picUrl:l} = i[e];
if (s) return null;
if (!["SIDESTORY","BRANCHLINE"].includes(r)) return null;
let c=0, _=0;
a.forEach(({clearedStage:e,totalStage:t}) => { c+=e; _+=t; });
return {id:e,title:o,value:c,max:_,bgUrl:l};
```

因此 `SIDESTORY`、`BRANCHLINE` 是**直接观察到的前端枚举**，不是按名字猜出来的类型。官方这张卡过滤复刻 `isReplicate=true`，且没有图片时不显示；文本入口无需依赖图片下载，但是否展示复刻应作为明确产品决定，不能偷偷继承或改变筛选。S3 用 `value === max` 显示“已完成”，否则显示“进行中：value/max”。这里的“完成”指前端展示的**关卡计数完成**，不代表商店搬空、任务奖励领取或蚀刻章集齐。[S2](https://bbs.hycdn.cn/skland-fe-static/skland-game/9560.b49ee94b.js) · [S3](https://bbs.hycdn.cn/skland-fe-static/skland-game/8624.b27ec983.js)

已检查的上述消费者未提供普通活动逐关星级、首通奖励领取、活动商店货币/购买状态、活动任务完成/领奖、活动蚀刻章套组明细的读取证据。S3 中 `medalCount` 是账号收藏概况“蚀刻章”总数，不能用它表示某个活动的蚀刻章完成率。本调查也没有证据证明这些字段或其他接口绝对不存在。[S3](https://bbs.hycdn.cn/skland-fe-static/skland-game/8624.b27ec983.js)

### 引航者试炼与生息演算的显示边界

引航者 `difficulty` 的第一方映射明确为 `NORMAL → 初始试炼`、`TEAM → 定向试炼`、`EX → 恢弘试炼`、`SP → 最终试炼`。`played=false` 显示“暂无记录”；未知枚举不应猜成已通关。[S2](https://bbs.hycdn.cn/skland-fe-static/skland-game/9560.b49ee94b.js)

生息演算 `mainQuest` 在 S3 中通过 `>0`、`>1`、`>2` 分别点亮三个主线篇章，说明它是该界面的篇章推进计数，不能直接当百分比；其余计数应保留“数据未提供”和真实 0 的区别，尚未拿真实响应核对所有玩法版本字段是否齐全。[S3](https://bbs.hycdn.cn/skland-fe-static/skland-game/8624.b27ec983.js)

## 与现有插件的衔接

- 插件已经接入 `player/info`，适配层返回完整响应；`fetch_cached_player_info` 缓存完整成功信封并按 UID 串行读取，不局限于目前渲染过的字段。[客户端](../ArknightsUID/utils/skland/client.py) · [适配层](../ArknightsUID/utils/enduid/adapter.py) · [缓存服务](../ArknightsUID/utils/player/service.py)
- `PlayerData` 当前没有 `activity`、`tower`、`bossRush`、`sandbox` 专用字段，但保留 `raw=data`，因此可以从一次既有请求读取这些数据；扩展应通过独立安全解析器建立字段契约，避免把前端的无守卫解构原样搬入机器人。[现有解析](../ArknightsUID/utils/player/parse.py)
- 现有 `ark肉鸽战绩` 使用独立 `/api/v1/game/arknights/rogue`，用于详细战绩。该路径也在当前第一方 S4 中出现，原先客户端“仅社区实测端点”的注释不再是唯一证据；鉴权和真实账号响应仍未在本调查联调。[S4](https://bbs.hycdn.cn/skland-fe-static/skland-game/main-7bd12dde.57049ecf.js) · [既有肉鸽入口](../ArknightsUID/arknightsuid_rogue/__init__.py)
- `ark日历` 当前走 PRTS 的公开活动查询，说明活动时间/资讯，不携带个人 UID 进度。它与新增个人活动进度应分别命名，不能把日历当个人战绩来源。[日历入口](../ArknightsUID/arknightsuid_wiki/__init__.py) · [公开日历查询](../ArknightsUID/utils/prts/client.py)

## 可做的最小功能与合成样例

建议最小范围为 `ark活动进度`（活动名 + 已通关数/总数）及 `ark活动战绩`（保全最高进度、引航者最高档位/关卡、生息已确认计数），复用当前默认账号的 `player/info` 缓存。没有相应列表/元数据时明确“森空岛未提供该类数据”，不要把缺字段格式化为 0% 或“全部完成”。详细肉鸽战绩继续复用既有入口，不复制一套查询。[字段依据 S2](https://bbs.hycdn.cn/skland-fe-static/skland-game/9560.b49ee94b.js) · [本地完整响应缓存](../ArknightsUID/utils/player/service.py)

以下为**人工合成、仅用于说明消费结构的样例**，不是实测个人响应；ID、名称、数值均为占位值。没有证据的奖励、星级、任务、蚀刻章字段故意不加入。

```json
{
  "code": 0,
  "data": {
    "activity": [{
      "actId": "example_activity",
      "zones": [{"clearedStage": 8, "totalStage": 10}]
    }],
    "activityInfoMap": {
      "example_activity": {
        "name": "示例活动", "type": "SIDESTORY",
        "isReplicate": false, "picUrl": ""
      }
    },
    "tower": {"records": [{"towerId": "example_tower", "best": 6}]},
    "towerInfoMap": {
      "example_tower": {"name": "示例保全", "subName": "示例区域", "picUrl": ""}
    },
    "bossRush": [{
      "id": "example_boss_rush", "picUrl": "",
      "record": {"stageId": "example_stage", "difficulty": "EX", "played": true}
    }],
    "stageInfoMap": {"example_stage": {"code": "EXAMPLE-1"}},
    "sandbox": [{
      "id": "example_sandbox", "maxDay": 100, "maxDayChallenge": 20,
      "mainQuest": 2, "subQuest": 1, "baseLv": 4, "unlockNode": 30,
      "enemyKill": 1000, "createRift": 2, "fixRift": 1
    }]
  }
}
```

## 独立 auto-chess 战绩不是普通活动关卡进度

当前 S4 的 `getArknightsStrongholdInfo` 使用 `GET /api/v1/game/arknights/auto-chess`，参数 `{uid}`。S6 将成功响应 `data` 存为 `info`；S5 使用 `info.userInfo` 的 `pass/shortestRound/upgrade/defeat/gold` 对象，每项消费 `count/isMost`，展示“总通关次数 / 最短回合数 / 总升金次数 / 歼灭敌人数 / 金币获得数”；另使用 `info.gameData[kind].teams[]` 中的 `id/name/passCount/shortestRound/isMostPass/isFastest`。[S4](https://bbs.hycdn.cn/skland-fe-static/skland-game/main-7bd12dde.57049ecf.js) · [S5](https://bbs.hycdn.cn/skland-fe-static/skland-game/4086.81bd7cd0.js) · [S6](https://bbs.hycdn.cn/skland-fe-static/skland-game/main-0a037d97.3091b5d8.js)

这些是独立玩法汇总与队伍统计，不能把它当任意新 SideStory 的活动商店/任务进度，也没有当前消费者证据将其解释成逐局完整历史或“残局”记录。官方页面模型使用 App 内游戏角色 SDK 获取 UID，当前调查没有验证机器人鉴权、返回字段可用性或玩法日期；最小 `player/info` 实现应暂不接入这个新端点。[S5](https://bbs.hycdn.cn/skland-fe-static/skland-game/4086.81bd7cd0.js) · [S6](https://bbs.hycdn.cn/skland-fe-static/skland-game/main-0a037d97.3091b5d8.js)

S4 同时含 `/api/v1/activity/.../brief/rank` 等森空岛站内小游戏活动端点；这些 URL 的出现不证明它们查询的是明日方舟游戏内当前活动进度，不应按 `activity` 字样混入上述个人通关功能。[S4](https://bbs.hycdn.cn/skland-fe-static/skland-game/main-7bd12dde.57049ecf.js)

## 验证限制

本调查证明的是第一方前端当下存在这些数据消费者与接口路径。未进行真实登录查询，因此未知：当前指定账号会收到哪些活动、列表是否只保留部分历史、数据刷新延迟、隐私展示配置影响、缺失与未参与的完整区别。不能宣称已验证当前活动关卡全覆盖、奖励进度可用或所有玩法详情齐备。

# wiki 卡片化 · 前端交接文档（给 kimi）

> 后端已就绪：5 个 wiki 命令全部接入「有模板出图、无模板/报错回退纯文本」的 `_render_or_text`。
> 前端任务：在 `ArknightsUID/templates/` 下编写下列 **4 个 HTML 模板**（视觉自研）。
> 模板就位后命令自动出图，无需后端改动。

## 通用约定（所有模板必须遵守）

1. **根元素**：`<div class="container">...</div>`——截图锚点固定为 `.container`（`render_utils.py` 的 Playwright `wait_for_selector(".container")`），缺失会导致渲染超时。
2. **公共头**：`<head>{% include "ark_head.html" %}</head>`。`ark_head.html` 已内联 `ark_base.css`（设计 token + 组件类，「罗德岛终端」浅色终端设计系统，v14 起；容器自带舰桥背景图衬底），不要再 `<link>` 或 `@import`。
3. **字体**：`{{ font_css_url }}` 由后端 `render_html` **自动注入**，模板里直接 `<link rel="stylesheet" href="{{ font_css_url }}">` 即可，后端 context 不含此 key。
4. **图片字段**：`map_image`/`avatar` 等二进制资源，后端已转成 `data:image/...;base64,...` 字符串，模板用 `<img src="{{ map_image }}">`。**字段可能为空字符串**（无图源时），模板必须优雅降级（如 `{% if map_image %}<img ...>{% endif %}`）。
5. **截图格式**：JPEG（无透明通道）。设计即「终端屏幕」——舰桥背景图 + 浅色画幅承担视觉骨架（纱罩/寻访斜纹/菱形水印/幽灵字由 .container 继承）。
6. **version 字段**：来自 `from ArknightsUID.version import ArknightsUID_version`（当前 `0.2.0`）。
   > ⚠️ 历史遗留：`tools/render_previews.py:23` 硬编码了 `"v0.3.0"`，与本字段不一致。以 `ArknightsUID_version` 为准，预览工具的硬编码待后续统一。
7. **模板文件顶部**写 HTML 注释列出 context 字段（照搬现有 `ark_status.html` 等的写法），作为 schema 的第二权威记录。

## 命令 → 模板映射

| 命令 | 模板文件名 | 后端组装函数 | 数据源 dataclass |
|---|---|---|---|
| `arkwiki` / `ark材料`（词条命中） | `ark_wiki_page.html` | `_build_page_context` | `WikiPageSummary` |
| `ark干员` | `ark_wiki_operator.html` | `_build_operator_context` | `WikiPageSummary`（干员字段） |
| `ark敌人` | `ark_wiki_enemy.html` | `_build_enemy_context` | `EnemyInfo` + `EnemyLevel` |
| `ark关卡` | `ark_wiki_stage.html` | `_build_stage_context` | `StageInfo` |

> **例外**：`arkwiki` 未命中词条时走搜索列表，保持纯文本，**无模板**（列表型结果不套卡片）。

---

## 1. `ark_wiki_page.html`（通用词条 / 材料）

### context schema

| key | 类型 | 来源 | 必填 |
|---|---|---|---|
| `title` | str | `page.title` | ✅ |
| `summary` | str | `page.summary`（已 clip） | 可空 |
| `fields` | list[{key,value}] | `page.fields` 去空 | 可空 |
| `source_url` | str | `page.source_url` | 可空 |
| `version` | str | `ArknightsUID_version` | ✅ |

### mock 样例（可直接渲染验证）

```python
{
    "title": "龙门币",
    "summary": "明日方舟中的通用货币，用于干员升级、技能升级等。",
    "fields": [
        {"key": "稀有度", "value": "材料"},
        {"key": "用途", "value": "干员升级"},
        {"key": "获取方式", "value": "关卡掉落"},
    ],
    "source_url": "https://prts.wiki/w/龙门币",
    "version": "0.2.0",
}
```

---

## 2. `ark_wiki_operator.html`（干员）

### context schema

| key | 类型 | 来源 | 必填 |
|---|---|---|---|
| `title` | str | `page.title` | ✅ |
| `name` | str | `fields.干员名` / `名称` / title | ✅ |
| `summary` | str | `page.summary`（已 clip） | 可空 |
| `rarity` | str | `fields.稀有度` | 可空 |
| `profession` | str | `fields.职业` | 可空 |
| `branch` | str | `fields.分支` | 可空 |
| `position` | str | `fields.位置` | 可空 |
| `faction` | str | `fields.所属势力` | 可空 |
| `tags` | str | `fields.标签` | 可空 |
| `obtain` | str | `fields.获得方式` | 可空 |
| `source_url` | str | `page.source_url` | 可空 |
| `version` | str | `ArknightsUID_version` | ✅ |

> 注：`rarity` 是文本（如 "★6"），非数字。PRTS 词条的稀有度字段是字符串。

### mock 样例

```python
{
    "title": "阿米娅",
    "name": "阿米娅",
    "summary": "罗德岛公开领导人。",
    "rarity": "★6",
    "profession": "术师",
    "branch": "中坚术师",
    "position": "近战位",
    "faction": "罗德岛",
    "tags": "输出 / 生存",
    "obtain": "主线赠送",
    "source_url": "https://prts.wiki/w/阿米娅",
    "version": "0.2.0",
}
```

---

## 3. `ark_wiki_enemy.html`（敌人）

### context schema

| key | 类型 | 来源 | 必填 |
|---|---|---|---|
| `title` | str | `enemy.title` | ✅ |
| `name` | str | `enemy.name` / title | ✅ |
| `index` | str | `enemy.index` | 可空 |
| `rank` | str | `enemy.rank`（级别） | 可空 |
| `description` | str | `enemy.description`（已 clip 240） | 可空 |
| `damage_type` | str | `enemy.damage_type` | 可空 |
| `attack_type` | str | `enemy.attack_type` | 可空 |
| `move_type` | str | `enemy.move_type` | 可空 |
| `kind` | str | `enemy.kind` | 可空 |
| `has_stats` | bool | 是否有数值等级 | ✅ |
| `max_hp` / `atk` / `defense` / `res` / `weight` | str | `levels[0]` 的属性 | `has_stats` 为真时填 |
| `abilities` | list[str] | `levels[0].abilities` | 可空 |
| `source_url` | str | `enemy.source_url` | 可空 |
| `version` | str | `ArknightsUID_version` | ✅ |

> 注：当前只取 `levels[0]`（第一个数值等级）。`has_stats=False` 时隐藏数值区。

### mock 样例

```python
{
    "title": "源石虫",
    "name": "源石虫",
    "index": "1",
    "rank": "普通",
    "description": "最常见的感染生物。",
    "damage_type": "物理",
    "attack_type": "近战",
    "move_type": "地面",
    "kind": "感染者",
    "has_stats": True,
    "max_hp": "1000", "atk": "100", "defense": "0", "res": "0", "weight": "1",
    "abilities": ["无特殊能力"],
    "source_url": "https://prts.wiki/w/源石虫",
    "version": "0.2.0",
}
```

---

## 4. `ark_wiki_stage.html`（关卡）

### context schema

| key | 类型 | 来源 | 必填 |
|---|---|---|---|
| `title` | str | `code` / title | ✅ |
| `name` | str | `stage.name` | 可空 |
| `stage_id` | str | `stage.stage_id` | 可空 |
| `stage_type` | str | `stage.stage_type` | 可空 |
| `difficulty` | str | `stage.difficulty` | 可空 |
| `recommended_level` | str | `stage.recommended_level` | 可空 |
| `ap_cost` | str | `stage.ap_cost`（理智） | 可空 |
| `enemy_count` | str | `stage.enemy_count` | 可空 |
| `description` | str | `stage.description`（已 clip 220） | 可空 |
| `drops` | list[str] | `stage.drops[:8]` | 可空 |
| `extra_drops` | list[str] | `stage.extra_drops[:8]` | 可空 |
| `enemies` | list[{name,count}] | `stage.enemies[:10]` | 可空 |
| `map_image` | str | `map_url` 烘焙的 base64 data URL | **可空**（无图时降级） |
| `map_url` | str | `stage.map_url`（外链） | 可空 |
| `source_url` | str | `stage.source_url` | 可空 |
| `version` | str | `ArknightsUID_version` | ✅ |

> ⚠️ `map_image` 是唯一有图源的字段（来自 `mapen.ark-nights.com`），但烘焙可能失败（网络/格式），**必须** `{% if map_image %}` 降级；降级时可展示 `map_url` 外链。

### mock 样例

```python
{
    "title": "1-7",
    "name": "暴君",
    "stage_id": "main_01-07",
    "stage_type": "主线",
    "difficulty": "普通",
    "recommended_level": "Lv.1",
    "ap_cost": "6",
    "enemy_count": "10",
    "description": "击败所有敌人。",
    "drops": ["固源岩", "代糖", "酯原料"],
    "extra_drops": ["基础作战记录"],
    "enemies": [{"name": "源石虫", "count": "5"}, {"name": "猎犬", "count": "3"}],
    "map_image": "",  # 实际运行时为 data:image/webp;base64,... 或空
    "map_url": "https://mapen.ark-nights.com/map/main_01-07",
    "source_url": "https://prts.wiki/w/1-7",
    "version": "0.2.0",
}
```

---

## 验证方式

模板写好后，用 `tools/render_previews.py` 的方式注入 mock dict 调 `render_html` 自检：

```python
from ArknightsUID.utils.render import ark_templates, render_html
img = await render_html(ark_templates, "ark_wiki_page.html", MOCK)
# img 为 JPEG bytes 即模板可用
```

后端单测见 `tests/test_wiki_render.py`（验证 context 组装与回退逻辑，不依赖模板存在）。

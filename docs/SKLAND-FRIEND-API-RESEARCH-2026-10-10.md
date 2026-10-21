# 森空岛好友接口原生契约研究

日期：2026-10-10（Asia/Singapore）。本次在 [接口差异复核](SKLAND-API-DIFF-REVIEW-2026-10-10.md) 之后，进一步解析 2.2.0 APK 的 native payload。只读分析原始 APK/DEX，没有使用私人凭据，没有执行 APK，没有发送写请求，没有修改生产代码。

## 当前结果

后续已完成好友及公开头像的客户端、命令、缓存、卡片与离线回归，状态及实号验证边界见 [实现记录](SOCIAL-FEATURES-2026-10-10.md)。这不改变下文对尚未恢复 REST 注解和成功响应包装的证据限制。

**好友 DTO 的 JSON 键和字段类型已从真实 DEX 索引、class_data 和 Moshi 适配器指令恢复，好友接口的完整 query、外层返回结构和分页仍未恢复。** 这比共享明文字符串推测前进了一步，但还不够宣称好友查询已具备完整可接入契约。

- `GET /api/v1/game/friend/list` 的匿名参数对照表明 `uid` 参与进入校验；无法证明 uid 是唯一必要参数。
- 好友模型 `GameUserInfo` 的六个 JSON 键确定为 `uid`、`name`、`level`、`avatar`、`lastOnlineTs`、`sklandUser`，且两个嵌套模型也已确定。
- 不能仅因模型在好友包里，就断言该接口的 `data` 是这个模型的列表。模型与 REST 方法的真实泛型签名/注解链仍缺失。
- `default-binding` 当前匿名 GET 返回未登录；对应模型 `GameDefaultPlayer` 的 JSON 键为 `gameBinding`、`link`。未证明这个模型直接对应该路径，也未证明接口只允许 GET。
- 公开头像目录已有独立的第一方 Web 调用与实际成功响应，可先实现；不必等待好友接口。详见 [前次复核中新增的公开头像证据](SKLAND-API-DIFF-REVIEW-2026-10-10.md)。

公开头像分类及明日方舟目录已纳入 `tools/check_public_sources.py`，支持用 `--source avatar_categories --source arknights_avatars` 单独复测。完整实网检查 **21 项全部成功**，分类为 5 项、明日方舟头像为 422 项；检查器相关测试 **16 passed**，Ruff 和 `git diff --check` 通过。已有 19 项检查内容沿用原入口，没有用公开目录成功替代私人好友接口成功响应。

本次还独立执行了末尾解析脚本，四个 Moshi 适配器的键顺序与表中结论一致。好友的真实调用仍需用户指定用于只读联调的已绑定账号，未从本机任意选择其他聊天用户的凭据。

## 样本与恢复方法

第一方原始样本：`23bd2915c3aafefb2ac2221e99a0f8d0.apk`，`com.hypergryph.skland` 2.2.0 / versionCode 200200057。直接 ZIP 中的 `classes.dex` 与 `/tmp/arkapk/new/classes.dex` 一致，文件 SHA256 为 `dc22e0df5b81ccd38cc5c5da00da606298d39df42ed55ed9dff2a8540ef74246`，字节数 43885580。

壳 DEX 头无法作为业务 DEX 头直接使用，但业务 string_data、若干索引表、class_data 和部分 code_item 为明文。通过 `getGameFriendList` 起点及连续六条字符串的偏移差，找到 string_ids 的对应指针串，确定业务 DEX 相对偏移基址：

```text
业务基址 = 24019984 = 0x16e8410
getGameFriendList string_data_item 原始字节起点 = 36404206
该 string_ids 项的物理位置 = 24184168
该项保存的相对位置 = 12384222
36404206 - 12384222 = 24019984
```

业务 string_data 连续段起点为 35030372；按 ULEB128 长度及 MUTF-8 的零终止分隔重建出 54582 项。字节段前两项为合法空串和 Kotlin Metadata 字符串，不把所有可打印串都当索引项。`getGameFriendList` 恢复为 string_idx=41018，`game/friend/list` 为 4283，`default-binding` 为 4285。

恢复出的 string_ids 索引 32740 至 54581（21842 项）与保留的原始指针 **逐项完全一致**，不是只验几个样本。其前半部没有采用随机高熵指针，而是用连续 string_data 重建。

| 表 | 原始物理起点 | 相对业务基址 | 恢复项数 |
|---|---:|---:|---:|
| string_ids | 24020096 | 112 | 54582；保留指针验证尾部 21842 项 |
| type_ids | 24238424 | 218440 | 17101 |
| proto_ids | 24306828 | 286844 | 16247 |
| field_ids | 24501792 | 481808 | 43915 |
| method_ids | 24853112 | 833128 | 64482 |
| class_defs 明文段起点 | 25368968 | 1348984 | 本次解析的有序前段 9061 项；未声称全部 class_defs 已恢复 |

type_ids、field_ids、method_ids 的排序和索引边界按 DEX 格式核验。通过 class_defs→class_data→method_idx→proto_ids/code_item 建立模型方法归属，随后解析 `const-string`、`filled-new-array`/`filled-new-array/range` 和 `invoke-static`，确认实际调用的是：

```text
Lcom/squareup/moshi/JsonReader$Options;.of(
    [Ljava/lang/String;
)Lcom/squareup/moshi/JsonReader$Options;
```

所以以下 JSON 键有具体适配器消费证据，不是根据方法旁边的字符串猜测。没有重建可执行 APK，没有把保护头、注解重定位和被擦除的代码假装恢复完整。

## 好友模型：已核实部分

### GameUserInfo

实际类型：`Lcom/hypergryph/skland/messagepage/friend/data/GameUserInfo;`。构造原型：

```text
(Ljava/lang/String;Ljava/lang/String;I
 Lcom/hypergryph/skland/messagepage/friend/data/Avatar;
 J
 Lcom/hypergryph/skland/messagepage/friend/data/SKLandUser;)V
```

| JSON 键 | 实际 DEX 字段类型 | 可用解释与限制 |
|---|---|---|
| `uid` | `Ljava/lang/String;` | 游戏角色 UID 字符串 |
| `name` | `Ljava/lang/String;` | 名称；真实格式尚无账号响应验证 |
| `level` | `I` | 32 位整型等级 |
| `avatar` | 好友包内的 `Avatar` | 嵌套头像模型 |
| `lastOnlineTs` | `J` | 64 位整型；后续 UI 指令已确认按秒转换为毫秒；0 为模型默认值，见下文追加研究 |
| `sklandUser` | 好友包内的 `SKLandUser` | 森空岛用户信息；可缺失/为空与否尚未核验 |

物理来源：`GameUserInfoJsonAdapter.<init>`，method_idx=38269，class_data_item=26198832，code_item=29896880。关键原始指令区在 code_item 头后：

```text
1a 05 ac ae   const-string v5, string@0xaeac  # lastOnlineTs
1a 06 ca c8   const-string v6, string@0xc8ca  # sklandUser
1a 01 44 cd   const-string v1, string@0xcd44  # uid
1a 02 84 b3   const-string v2, string@0xb384  # name
1a 03 ac af   const-string v3, string@0xafac  # level
1a 04 01 85   const-string v4, string@0x8501  # avatar
25 06 3c 42 01 00   filled-new-array/range {v1 .. v6}, [Ljava/lang/String;
0c 00               move-result-object v0
71 10 66 b2 00 00   invoke-static {v0}, method@0xb266  # Options.of
```

字段和 getter 由 field_ids/method_ids 与相同所属类核实；本次不猜测 assistChars、好友上限、分页计数等模型中没有证据的字段。仅模型类型不能证明时间戳单位；后续通过官方 UI 消费指令确认了秒单位，见下文追加研究。

### Avatar 与 SKLandUser

| 模型 | `Options.of` 顺序 | 实际字段类型 | adapter 来源 |
|---|---|---|---|
| 好友包 `Avatar` | `type`, `id`, `url` | 三者均 `Ljava/lang/String;` | method_idx=38243；class_data=26198593；code_item=29894948 |
| 好友包 `SKLandUser` | `userId`, `nickname` | 两者均 `Ljava/lang/String;` | method_idx=38287；class_data=26199015；code_item=29898616 |

数组顺序由 `filled-new-array` 的寄存器顺序恢复：Avatar 先给 v0=id、v1=url、v2=type，再传 `{v2,v0,v1}`；SKLandUser 先给 v0=nickname、v1=userId，再传 `{v1,v0}`。没有把字符串的出现顺序误当实际 options 顺序。

## REST 注解与分页：仍未核实

`FriendService` 原始描述符和 `getGameFriendList` 保存在恢复后的字符串表中，但不在对应 type_ids/method_ids 原名位置。这可能是 R8 混淆后的 Kotlin Metadata 遗留，不能由此断言类被删除、服务被迁移或接口不存在。

进一步对 REST 路径做 DEX `encoded_value` 的 STRING 引用扫描：

```text
friend/list:     string_idx=4283 (0x10bb)，两字节编码应为 37 bb 10
default-binding:string_idx=4285 (0x10bd)，两字节编码应为 37 bd 10
```

好友路径的最短编码在完整 DEX 只有一次字节匹配，物理位置 40531327，位于高熵段；邻近内容不能解析成有效 annotation_item，没有建立到 method_idx 的关系。默认绑定路径该编码零匹配。也检查了三/四字节 STRING 编码，未恢复有效注解关系。以上只表示 **本次未恢复明文 REST 注解**，不是方法或路径不存在的证明。

class_defs 中的注解指针也不能直接按标准 DEX 相对偏移使用，例如好友 Avatar 的 annotation_off=25182096，`业务基址 + annotation_off` 超出完整文件；把它当绝对偏移指向 method_ids，把高位掩掉则落到被擦零的指令段，都没有合法 annotation directory。未在没有格式证据时继续猜指针修复规则。

因此下面这些仍未知，禁止用全局共享 `uid`、`pageToken`、`hasMore` 等字符串补成“已确认契约”：

- query 的完整字段及类型、uid/gameId/userId 的必要性；
- 返回是 `data.list`、直接数组、其他包装还是包含两个好友来源；
- `GameUserInfo` 与该方法的直接泛型关联；
- 是否一次返回全部好友，是否分页，分页字段和终止条件；
- 非森空岛好友的 `sklandUser` 空值语义，头像缺省行为；
- 正常账号、无好友、官服/B服、多绑定角色场景的返回。

匿名 GET 对照引用此前保存的 `/tmp/arkuid-skland-friend-params-20261010.json`：无参或仅 gameId=1 为 400 / code10001；加入假 uid=0（分别仅 uid、uid+gameId、uid+gameId+userId）为 401 / code10002。只能证明 uid 参与进入参数/鉴权校验，不能证明有效账号完整成功条件。

## default-binding：可确认的模型信息

`GameDefaultPlayer` 实际字段为 `gameBinding:Ljava/util/Map;`、`link:Ljava/lang/String;`；Moshi 适配器 constructor 的 options 为 `gameBinding`、`link`。来源：method_idx=36806，class_data=26185496，code_item=29787460。

适配器还调用 `Types.newParameterizedType`，构造 `Map<String, Player>` 适配器，所以该模型的 gameBinding 类型有实际泛型构造证据。它不意味着 `default-binding` 已被证明确实返回此模型。GET 401 仅支持当前路由进入鉴权，没有请求 PUT/POST，也未证明接口排他地只读。

插件已有本地 `ark切换` 默认查询账号功能，当前无需据这个未核实官方接口替换；官方默认角色和插件本地选择的关系应另行明确。

## 可以继续落地的公开头像接口

父审从第一方 `www.skland.com` 的线上 `3158.cd61aa37.js` 核对并实网验证了：

- `GET https://zonai.skland.com/web/v1/user/avatar/category/list`；读取 `data.list`，本次有 5 项，id=2 标题为明日方舟。
- `GET https://zonai.skland.com/web/v1/user/avatars?categoryId=2`；读取 `data.list`，本次有 422 项，每项 `id`、`url`，包含路径日期为 2026-10-09 的素材。
- 使用公共 Web token 与签名，无私人账号。这里的 Web 契约与匿名 native avatar/category/list 的 401 是不同接口，不应混在一起。

证据：`/tmp/arkuid-friend-h5-20261010/3158.cd61aa37.js`、`/tmp/arkuid-public-avatar-contract-20261010.json`、`/tmp/arkuid-public-avatar-arknights-20261010.json`。具体接入和分页判断以 [前次复核](SKLAND-API-DIFF-REVIEW-2026-10-10.md) 的新增公开头像节及实际 JSON 为准；这里未将 hasMore=false 的当次结果泛化为永不分页。

## 可复跑只读恢复器

工作副本保存于 `/tmp/arkuid-recover-skland-native.py`。下方保留完整脚本，复制到任意临时目录执行 `python3 <脚本路径>` 即可复核该样本。它固定核对原始 SHA256，仅解析本样本的明文元数据与四个适配器 constructor，不构造或执行 APK，不做网络请求，不推测缺失 REST 注解。

```python
"""Read-only, sample-specific metadata recovery; no APK execution or network."""
from pathlib import Path
from hashlib import sha256
import struct,json
D=Path('/tmp/arkapk/new/classes.dex').read_bytes()
assert sha256(D).hexdigest()=='dc22e0df5b81ccd38cc5c5da00da606298d39df42ed55ed9dff2a8540ef74246'
B=24019984
u16=lambda o:struct.unpack_from('<H',D,o)[0]
u32=lambda o:struct.unpack_from('<I',D,o)[0]
def uleb(o):
    v=0;sh=0
    while True:
        x=D[o];o+=1;v|=(x&127)<<sh;sh+=7
        if x<128:return v,o
S=[];positions=[];p=35030372
for i in range(54582):
    positions.append(p);length,p=uleb(p);e=D.index(b'\0',p)
    S.append(D[p:e].decode('utf8',errors='replace'));p=e+1
assert S[41018]=='getGameFriendList'
assert positions[41018]==36404206
assert all(u32(B+112+4*i)==positions[i]-B for i in range(32740,54582))
T=[u32(24238424+4*i) for i in range(17101)]
TN=[S[i] for i in T]
PT=[struct.unpack_from('<III',D,24306828+12*i) for i in range(16247)]
FD=[struct.unpack_from('<HHI',D,24501792+8*i) for i in range(43915)]
MD=[struct.unpack_from('<HHI',D,24853112+8*i) for i in range(64482)]
assert all(a<b for a,b in zip(T,T[1:]))
assert all((a[0],a[2],a[1])<(b[0],b[2],b[1]) for a,b in zip(FD,FD[1:]))
assert all((a[0],a[2],a[1])<(b[0],b[2],b[1]) for a,b in zip(MD,MD[1:]))
def method(i):
    c,p,n=MD[i];sh,ret,ps=PT[p];args=[]
    if ps:args=[TN[u16(B+ps+4+2*j)] for j in range(u32(B+ps))]
    return TN[c]+'.'+S[n]+'('+''.join(args)+')'+TN[ret]
def methods(row):
    p=B+row[6];counts=[]
    for _ in range(4):v,p=uleb(p);counts.append(v)
    for group,cnt in enumerate(counts):
        idx=0
        for _ in range(cnt):
            delta,p=uleb(p);idx+=delta;acc,p=uleb(p)
            if group>=2:
                code,p=uleb(p);yield idx,code

def options(code):
    o=B+code+16;end=o+2*u32(B+code+12);regs={};array=None
    while o<end:
        op=D[o]
        if op==0x1a:regs[D[o+1]]=S[u16(o+2)]
        if op==0x24:
            count=D[o+1]>>4;rr=u16(o+4)
            ids=[rr&15,(rr>>4)&15,(rr>>8)&15,(rr>>12)&15,D[o+1]&15]
            array=[regs.get(i,'?') for i in ids[:count]]
        if op==0x25:array=[regs.get(i,'?') for i in range(u16(o+4),u16(o+4)+D[o+1])]
        if op==0x71 and method(u16(o+2)).startswith('Lcom/squareup/moshi/JsonReader$Options;.of('):return array
        if op in [0x24,0x25,0x6e,0x6f,0x70,0x71,0x72]:width=3
        elif op in [0x1a,0x1c,0x1f,0x22,0x23] or 0x44<=op<=0x6d:width=2
        elif op in [0,0x0c,0x0e,0x12]:width=1
        else:raise ValueError(('unexpected opcode',hex(op),o))
        o+=width*2
    raise ValueError('Options.of not found')

wanted=['Lcom/hypergryph/skland/messagepage/friend/data/AvatarJsonAdapter;',
        'Lcom/hypergryph/skland/messagepage/friend/data/GameUserInfoJsonAdapter;',
        'Lcom/hypergryph/skland/messagepage/friend/data/SKLandUserJsonAdapter;',
        'Lcom/hypergryph/skland/gameinfo/GameDefaultPlayerJsonAdapter;']
rows=[]
for i in range(9061):
    row=struct.unpack_from('<8I',D,25368968+32*i)
    if TN[row[0]] not in wanted:continue
    for idx,code in methods(row):
        if S[MD[idx][2]]=='<init>':rows.append({'class':TN[row[0]],'method_idx':idx,'class_data_physical':B+row[6],'code_item_physical':B+code,'json_options':options(code)})
print(json.dumps({'dex_base':B,'strings':len(S),'verified_string_ids':21842,'types':len(T),'protos':len(PT),'fields':len(FD),'methods':len(MD),'adapters':rows},ensure_ascii=False,indent=2))
for c,t,n in FD:
    if TN[c].startswith('Lcom/hypergryph/skland/messagepage/friend/data/') and not TN[c].endswith('JsonAdapter;'):
        print(TN[c],S[n],TN[t])
```

## 追加研究：实际 UI 调用端与显示语义

本节在用户要求实现好友查询后继续调查，仍未使用私人账号。增加了混淆类调用端扫描和独立 Dalvik 指令反汇编，没有把“类名缺失”作为分析停止条件。

允许 class_defs 中后半部接口/新增类的类索引重新起排后，扫描得到 11510 条结构成立且类型索引不重复的 class_defs 记录，建立 52391 个有效范围的 code_item 归属。此前报告的 9061 是单调递增前段，不是全部类数量。

临时工具：`/tmp/arkuid-find-friend-callers.py`、`/tmp/arkuid-friend-code-map.json`、`/tmp/arkuid-disassemble-friend-code.py`。Androguard 4.1.4 及其读取依赖仅安装到 `/tmp/arkuid-dex-tools`，用于单条原始指令解码，不运行 APK，也不依赖伪造的 DEX 头来判断接口。

### UI 和模型的直接关联已确认

| 混淆方法 | method_idx | 原始 code_item 物理位置 | 方法内保留的原始 UI trace |
|---|---:|---:|---|
| `LGj/d;.a(...GameUserInfo...;)V` | 4360 | 27485196 | `ArknightsFriendItem (ArknightsFriendList.kt:117)` |
| `LGj/d;.b(...GameUserInfo...;)V` | 4361 | 27486092 | `ArknightsFriendItemUserInfo (ArknightsFriendList.kt:143)` |
| `LGj/d;.c(...LHj/b;...;)V` | 4362 | 27487900 | `ArknightsFriendList (ArknightsFriendList.kt:56)` |

这证实 GameUserInfo 是官方方舟好友项 UI 的模型。它仍不能建立 `friend/list` REST 泛型和外层信封的直接关系。

`LHj/h` 继承 `androidx.lifecycle/h0`，其 constructor（method_idx=4869，物理 27550680）用 `LHj/b(状态, 空List, false)` 初始化列表状态流。`LHj/b` 的真实字段为 `a:LWo/h;`、`b:Ljava/util/List;`、`c:Z`；constructor method_idx=4849（物理 27549420）将三个形参直接赋给这三个字段。这是 **客户端 UI 状态**，不能把它映射成服务端 `data.list` 或 hasMore 协议。

### lastOnlineTs 确认按秒消费

`ArknightsFriendItemUserInfo` 中的 Dalvik 指令偏移以 16 位 code unit 为单位：

```text
code_item = 27486092
0x1af invoke-virtual/range v40, GameUserInfo.getLastOnlineTs()J
0x1b2 move-result-wide v2
0x1b3 const/16 v4, 1000
0x1b5 int-to-long v4, v4
0x1b6 mul-long/2addr v2, v4
```

之后与今日/昨日边界及 `Calendar.getTimeInMillis()` 比较，显示“今日”“昨天”“前天”，否则把毫秒值交给 `M月d日` 日期格式化。这是官方将秒时间戳乘 1000 后按毫秒处理的直接证据，插件无需按数量级猜测单位。

上述片段原始字节起点 27486970：`74 01 76 95 28 00 0b 02 13 04 e8 03 81 44 bd 42`，调用 method_idx=38262；其所属类和原型由真实 method_ids/proto_ids 解析确认。

合成默认 constructor method_idx=38251，物理 29895816，对 mask 位 16（lastOnlineTs）使用 `const-wide/16 0`。所以 0 是模型默认值，插件可以把未提供/0 显示为未知；不应让默认值生成 1970 年日期。负值或实际账号接口其他占位值未实号核验。

### 头像展示直接使用 url

`ArknightsFriendItem` code_item=27485196：

```text
0x13c invoke-virtual/range v23, GameUserInfo.getAvatar()Avatar
0x13f move-result-object v1
0x140 invoke-virtual v1, Avatar.getUrl()String
0x143 move-result-object v6
...
0x155 invoke-static/range v4 ... v13, Lpa/H4;.c(...Object...;)V
```

url 字符串作为图像 Composable 的 Object model 参数传入。原始起点 27485844：`74 01 75 95 17 00 0c 01 6e 10 60 95 01 00 0c 06`，调用 getAvatar method_idx=38261 和 getUrl method_idx=38240。该消费者没有用 avatar.id/type 推导 CDN 路径；类型值的枚举含义仍未知，插件应直接消费返回的 url，保留 id/type 而不猜映射。

### 森空岛空昵称及默认模型

`ArknightsFriendItemUserInfo` 偏移 0x256 调用 `getSklandUser().getNickname().length()`；为空时 0x276 的 const-string 为“未授权”，非空时显示昵称。原始起点 27487304：`74 01 79 95 28 00 0c 02 6e 10 8b 95 02 00 0c 02`。这说明官方有正常空昵称的展示路径，不能把所有空昵称作为拉取失败。

`GameUserInfo` 合成默认 constructor 的 mask 分支还确认：uid/name 默认空串，level 默认 0，avatar 默认 Avatar 对象，sklandUser 默认 SKLandUser 对象（其默认构造参数为两个空字符串）。这些是客户端模型默认值，不证明服务端对显式 null 的接受规则或所有账号都会返回嵌套对象。

### 请求与分页仍需真实只读响应补齐

追到列表 ViewModel `LHj/h` 和其协程 `LHj/c;.s`、`LHj/d;.s`、`LHj/e;.s` 后，原始 code_item 只保留保护骨架。独立指令解码确认：

| 方法 | method_idx | 物理 code_item | 原始 insns_size | 可见内容 |
|---|---:|---:|---:|---|
| `LHj/c;.s` | 4856 | 27549844 | 52 code units | nop、const 0、连续 nop、return-null |
| `LHj/d;.s` | 4860 | 27550080 | 40 | 同上 |
| `LHj/e;.s` | 4864 | 27550292 | 45 | 同上 |
| `LHh/g;.f`（DI provider） | 4771 | 27537892 | 728 | 同上 |

即便这些方法仍有完整 register/ins/outs 数量和 debug_info，原始网络调用指令已不在该明文方法体；参数名字和 local variable 信息也未从对应 debug_info 恢复。不能凭 outs_size 或 StateFlow 的 List 类型猜 uid 唯一必要、响应 data.list 或是否分页。本节没有把保护骨架里的 return-null 当成官方运行时真实行为。

**当前可用于实现的边界：** GET 路径和 uid 校验有匿名证据，好友项展示模型、秒时间戳、头像 URL、空森空岛昵称已有实际消费者证据；真实成功信封、分页与完整性仍需要账号只读响应或官方未保护的调用资料确认。实现和测试应明确区分样例支持、返回项展示及已验证的实际拉取完整性。

"""森空岛肉鸽战绩页官方图表资源（纯 URL 表 + 下载缓存助手）。

来源：森空岛「战绩查询」H5 页（game.skland.com）所用的官方页面
素材。每期一张键 → CDN URL 映射，与 App 内「历史战绩 / 个人生涯 /
全服数据」页渲染用的是同一批图（bbs.hycdn.cn 官方 CDN，无需鉴权）。

键语义（按 App 内用途）：
  historyCardTitleSuccess/Fail  战绩卡「通关/失败」标题横幅（1074×186）
  careerBg                      生涯/全服页背景（全幅竖图）
  profileBg / profileTitle      生涯页底图与标题字
  detailKVBgSuccess/Fail        单局详情 KV 背景条（通关/失败）
  detailInitialCharsTitle       详情「初始招募」区标题字
  detailReviewTitle             详情「结局回顾」区标题字
  detailRecruitTitle            详情「招募情况」区标题字
  detailFavourTitle             详情「收藏」区标题字
  detailStatisticsTitle         详情「作战统计」区标题字
  detailThoughtsTitle           详情「思绪」区标题字
  detailLegacyTitle             详情「得偿所愿」区标题字（仅树海期）
  detailScore/Favorite/TotemIcon 详情得分/收藏/图腾小图标
  kvImg / tabBg / etch / etchBg / topTopBg  页面装饰图

注意：URL 是页面构建产物里的内容寻址文件名，森空岛发版会整批替换。
若某图 404（get_image_b64_with_cache 返回空串），各消费点按无图降级，
不影响数据展示；需要追新版时按同口径更新 URL 表即可。
"""

from __future__ import annotations

from pathlib import Path

from gsuid_core.logger import logger

__all__ = [
    "SKLAND_TOPIC_ASSETS",
    "SKLAND_TOPIC_COLORS",
    "skland_asset_url",
    "skland_topic_accent",
    "skland_asset_b64",
]

# topicId → 官方主题色（H5 Qn.sw 配置表同源提取）。accent=生涯/全服页
# 数值强调色（profileContentColor），用于卡片深色带内数字/进度条，
# 逐期跟随官方视觉（傀影金 / 水月蓝 / 萨米冰蓝 / 萨卡兹肉红 /
# 界园绛红 / 树海青）。界园官方值 #A80036 过深，深底上按同色相提亮。
SKLAND_TOPIC_COLORS: dict[str, dict[str, str]] = {
    "rogue_1": {"accent": "#F9A82F"},
    "rogue_2": {"accent": "#418DFF"},
    "rogue_3": {"accent": "#9ED9FD"},
    "rogue_4": {"accent": "#E1A9AA"},
    "rogue_5": {"accent": "#D877A8"},
    "rogue_6": {"accent": "#80E3EE"},
}


def skland_topic_accent(topic_id: str) -> str:
    """期官方强调色；未知期回退信号黄。"""
    return (SKLAND_TOPIC_COLORS.get(topic_id) or {}).get("accent", "#ffc907")

# topicId（主题表键，与战绩端点同口径）→ 官方图 URL 表
SKLAND_TOPIC_ASSETS: dict[str, dict[str, str]] = {
    "rogue_1": {
        "careerBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/5ff0a9c07c9327886c5a683ee373e654.png",
        "detailFavoriteIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/307a46be80bcf2f3cbecc0e00e38d4a6.png",
        "detailFavourTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/87b2220db43a0ffe7afc041f3433fd35.png",
        "detailInitialCharsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/f7f9d057e4c1085e406c952252d8d3f7.png",
        "detailKVBgFail": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/30743789b5f3a6eceb93a144b0a41e25.png",
        "detailKVBgSuccess": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/324126416b64bc42e426b2ec7f0f3c3d.png",
        "detailRecruitTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/ecce9357441f1e7d5dac5e290e81e173.png",
        "detailReviewTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/5041aa49d14074ab62f1711c89b148ee.png",
        "detailScoreIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/97030f21f49e892774932594c9826eb1.png",
        "detailStatisticsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/537912bda33bdac22a4b1cffc6c32df5.png",
        "detailThoughtsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/4c98703829b96eea93ff11ef14849979.png",
        "detailTotemIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/07aa184237acdf12969b4998ce762015.png",
        "etch": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/91082958a042be8b141400acddffa0fc.png",
        "etchBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/3d2fe3be1ca84d898c9fc19e9e7541e5.png",
        "historyCardTitleFail": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/4ad5a78fc8a4b278921564129d9bf661.png",
        "historyCardTitleSuccess": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/3130e26ea1e36db8beb3f2f96330bc04.png",
        "kvImg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/dadca8fd24f79dafa3cd55da0b330d83.png",
        "profileBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/bea42257b97293d649480d9f5f5f9f1f.png",
        "profileTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/d393c5176fe1bae18a60b00e52a8a470.png",
        "shareProfileBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/6decd547021d789f34fb3875f1a02460.png",
        "tabBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/a028420e5f2022b7c966329e7d5b5880.png",
        "topTopBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/c68b4cab409ff649d8985a2cb8676157.png",
    },
    "rogue_2": {
        "careerBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/13d1d869cf35585d5f95feb1bb40f65c.png",
        "detailFavoriteIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/3c9f26959e129b597d1be94b6e9fc25f.png",
        "detailFavourTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/7a8af790937c3bcef9f69752b7c29028.png",
        "detailInitialCharsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/c20c546423ba48687d34d4f78bbbb41e.png",
        "detailKVBgFail": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/f84b6628624e925c72d8821e291fe0e0.png",
        "detailKVBgSuccess": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/306077cd29218ead66ac9d7b82b3df94.png",
        "detailRecruitTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/3458bebb75e458bbb5230be619e6bef5.png",
        "detailReviewTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/810ca1bef8c532017e5857b01b465646.png",
        "detailScoreIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/63d9de872aabcb802199d76d8fe8e44e.png",
        "detailStatisticsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/c45e0e18f58b83152e2c5decb4b2cc07.png",
        "detailThoughtsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/cd6b4d83a1e21896618f4adeb5ec30b7.png",
        "detailTotemIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/fba121acecbc5fe2fb8849a945aa0c22.png",
        "etch": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/f1f5f1702b48a797e0aa4cfd89d05d93.png",
        "etchBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/84c70b04b0fa73ba84de943e73fd6491.png",
        "historyCardTitleFail": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/cb2d01a3ab88e7199b4c78ea4c44d38e.png",
        "historyCardTitleSuccess": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/f02a4db848b937bbe0604c09f5f0b3f0.png",
        "kvImg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/3208d31dc3f61ded63a304443b7df4aa.png",
        "profileBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/fe19f23f8da82bd6c3d933500b2e34d5.png",
        "profileTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/d393c5176fe1bae18a60b00e52a8a470.png",
        "shareProfileBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/da47d4fd703075cbca99275e4c06d921.png",
        "tabBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/0b2843147d27c5d72c953f8e4259b1c2.png",
        "topTopBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/23cad0deb9acff381f88fc1bbecfb96b.png",
    },
    "rogue_3": {
        "careerBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/5ff0a9c07c9327886c5a683ee373e654.png",
        "detailFavoriteIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/d86ef5c07cf32ef82cc3654d785eef74.png",
        "detailFavourTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/d9c3b6a922edc0e51432603bc2c21457.png",
        "detailInitialCharsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/e93984db38aa28f62c8a90b8e40a65ce.png",
        "detailKVBgFail": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/3cabaad2751c3bd37e80a4ddc7c107d6.png",
        "detailKVBgSuccess": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/d0203d73d7fb298e00bfe8b754436aa2.png",
        "detailRecruitTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/1dc0577cd1b12a91fc17fbad3fdd1bfd.png",
        "detailReviewTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/226f4d63a49930762986bc0738ca7db0.png",
        "detailScoreIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/2207f66d299b4995aadd1673270c6332.png",
        "detailStatisticsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/9cdd1f7ff872418fef84732f42999c9e.png",
        "detailThoughtsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/69e48dc494ebfb02cfce8ac9a478897b.png",
        "detailTotemIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/8b663a179032c5322a79f6e993b87396.png",
        "etch": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/7cbbc1377a854699c699d457ef072c7b.png",
        "etchBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/be72bc5f814f7dc2eb73c04689de3393.png",
        "historyCardTitleFail": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/0bd78f6eb853550504866ad9da963f37.png",
        "historyCardTitleSuccess": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/c2fc3c46fd414368455cfe486251c6ca.png",
        "kvImg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/1ec495511f0a5fbc438662a2fde60ac8.png",
        "profileBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/ddffba527c2c6d77662a0783d8d0d789.png",
        "profileTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/d393c5176fe1bae18a60b00e52a8a470.png",
        "shareProfileBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/9e78252e88942681f9b4d9e4e0233d5a.png",
        "tabBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/20b57d5f038bd334ccd8ad427f5c4ccd.png",
        "topTopBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/9bdffe9c34c8ada56d007b40caf9fe2b.png",
    },
    "rogue_4": {
        "careerBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/b0e68dbd375eb1da58fe524e907c2407.png",
        "detailFavoriteIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/fa9a297bdf237b7cb711e0c9afdbc25f.png",
        "detailFavourTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/89abec47417c514cc93033e0b2ef0753.png",
        "detailInitialCharsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/eb2d26ca0fcc175c790e9ae49707ecdc.png",
        "detailKVBgFail": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/b88fd4abee776827b4b9b038d5c685db.png",
        "detailKVBgSuccess": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/5fb756e82f687ef025bac997e71295f9.png",
        "detailRecruitTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/c50bfdf8dff2dda3b32838db90274bcc.png",
        "detailReviewTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/7b810be6c225f715fb3c9065df7d037d.png",
        "detailScoreIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/ec4f1ecffff53c30cae691d8f725b334.png",
        "detailStatisticsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/73a254f093d9a183dd68204a1e47edab.png",
        "detailThoughtsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/3e84f62e5dee08cfe648be360ce6c570.png",
        "detailTotemIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/ed8bc6fa44ebabfc3566d9781ddbee74.png",
        "etch": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/78d2b0d593c3fa794de11693b2c5347f.png",
        "etchBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/aa4f978d2af7bb1fec9325bc667710a3.png",
        "historyCardTitleFail": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/9dec7df6d8a9cd93b98587ed8e01fb82.png",
        "historyCardTitleSuccess": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/5da25217d45de97435368bd9a3ccbeb6.png",
        "kvImg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/499a57e0303429758e8ff5b7ad2058c3.png",
        "profileBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/61769286dc060d42ef74e2a3093ff335.png",
        "profileTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/d393c5176fe1bae18a60b00e52a8a470.png",
        "shareProfileBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/b125df30024b8ffa1ddb61f3987927ad.png",
        "tabBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/f406d77ad3369002d269570c80a67b65.png",
        "topTopBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/15865e80c1411c9de5153b087f0d8497.png",
    },
    "rogue_5": {
        "careerBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/9efeae5bdadf1c6394c79d4d3f0bd087.png",
        "detailFavoriteIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/dab8af98c13ef345d84fce066d4c8929.png",
        "detailFavourTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/43b57276b3041120f4d32a14e1a4c8d2.png",
        "detailInitialCharsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/37c9b3d9235fefc02db02993ac308b18.png",
        "detailKVBgFail": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/4150322586de84c6111aaa7ee6aa37b4.png",
        "detailKVBgSuccess": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/c88caa51a4d4508ef30dc1d2e86cab78.png",
        "detailRecruitTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/341cf0cba236c25877e19fa9354de506.png",
        "detailReviewTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/f454c1c644022c9f7956301e95646c4a.png",
        "detailScoreIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/0586ac14d493873657abd925f9d52cbd.png",
        "detailStatisticsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/e5cd5217a8211edb53576cc069bb8a34.png",
        "detailThoughtsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/53961e9dc04002a4c8e6309d1277736d.png",
        "detailTotemIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/813c8b259108105f51cc970d125629f4.png",
        "etch": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/31e741a2669fdf5470ff54d4a4a4ecf0.png",
        "etchBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/d1b89f5bada57fa55dc57932e77d6faf.png",
        "historyCardTitleFail": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/5e73dea516f8d578327f852274a90384.png",
        "historyCardTitleSuccess": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/392ea183bb0037326d5b022df0b2bc7e.png",
        "kvImg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/9b426a77151b00f80777ddc255950e54.png",
        "profileBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/821a5ea0645f95ae744ddf3b6f7af413.png",
        "profileTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/d393c5176fe1bae18a60b00e52a8a470.png",
        "shareProfileBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/bfdd8e1bf0026cd627e79c4b7d29ae00.png",
        "shareProfileTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/6d28c202ceac3daf587360648e32fb23.png",
        "tabBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/0f3ca841bdc119e76799b1b209d19e65.png",
        "topTopBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/015e957c5b7b8920d0d09545e20cdb31.png",
    },
    "rogue_6": {
        "careerBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/d73ccb4a731fa7022d8751a4a80ae0d2.png",
        "detailFavoriteIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/becf32ce2a574c65f1f57c3192ba9dd1.png",
        "detailFavourTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/5a667ba383a3fc571fa64317747bb572.png",
        "detailInitialCharsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/4433ae2fa483b7d335a21b09ac7c25eb.png",
        "detailKVBgFail": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/729e48612f0c65e30cc34450785870dd.png",
        "detailKVBgSuccess": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/9d10c1f1ed8a701b3d0099b87338daab.png",
        "detailLegacyTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/2f1222863bd712266b92e86328999783.png",
        "detailRecruitTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/777973b3c397b6b34d0b9e8c2c10b08a.png",
        "detailReviewTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/cceb34f1ead13692373ed167a6988aa3.png",
        "detailScoreIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/9896bb453cf1c142372eaa0b802c49da.png",
        "detailStatisticsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/86e77e6d00345430146eb1fa8c64b1e3.png",
        "detailThoughtsTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/e9cf335eff53c7259eebe62a9b25faec.png",
        "detailTotemIcon": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/6a5fa0e36916544154efc94817a87a36.png",
        "etch": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/8f70d7ddaa92d11c0ea92d2a06a4ae4b.png",
        "etchBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/46e27409d990e73cfaf1d4296152e7a7.png",
        "historyCardTitleFail": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/d9419c1a3aff468c22839c6eda6eea6d.png",
        "historyCardTitleSuccess": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/8a1280fd36db97154d7f8270498a8f47.png",
        "kvImg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/36be7f9b5968c3e9402cfb41536bd7a5.png",
        "profileBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/d2b9746b6f346aaa9f48cf7aaf7f8758.png",
        "profileTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/1b2e621debf3ecb4d689d552109f3c5f.png",
        "shareProfileBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/5f4f35cdf477613af562288dc64ba7ee.png",
        "shareProfileTitle": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/1b2e621debf3ecb4d689d552109f3c5f.png",
        "tabBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/664410a9553adac854bd8a06598187bd.png",
        "topTopBg": "https://bbs.hycdn.cn/public/skland-game/image/arknights/dm01-rouge/e735e799ed7206034b1c56a40d383894.png",
    },
}


def skland_asset_url(topic_id: str, key: str) -> str:
    """期 topicId + 资源键 → 官方 CDN URL；未知期/键返回空串。"""
    return (SKLAND_TOPIC_ASSETS.get(topic_id) or {}).get(key, "")


async def skland_asset_b64(
    topic_id: str,
    key: str,
    *,
    quality: int | None = 82,
    cache_dir: Path | None = None,
) -> str:
    """官方图下载缓存 → data URI（b64 内嵌模板）；失败返回空串降级。

    缓存目录与肉鸽主题图共用 CACHE_PATH/skland_rogue（同源图不重复落盘）。
    """
    url = skland_asset_url(topic_id, key)
    if not url:
        return ""
    try:
        from ..render import get_image_b64_with_cache
        from ..resource.RESOURCE_PATH import CACHE_PATH

        return await get_image_b64_with_cache(
            url, cache_dir or (CACHE_PATH / "skland_rogue"), quality=quality
        )
    except Exception:
        # 官方图整批 404 属预期内（森空岛发版换内容寻址名），降级空串；
        # 留 debug 堆栈便于区分「下载失败」与「URL 表过期」
        logger.debug(f"[ArknightsUID·肉鸽] 官方图获取失败 {topic_id}.{key}", exc_info=True)
        return ""

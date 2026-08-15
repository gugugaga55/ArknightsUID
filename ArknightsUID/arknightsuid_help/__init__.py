from gsuid_core.bot import Bot
from gsuid_core.help.utils import register_help
from gsuid_core.models import Event
from gsuid_core.sv import SV

help_sv = SV("ArknightsUID帮助", priority=5)

register_help("ArknightsUID", "ark帮助")

HELP_TEXT = """「明日方舟」ArknightsUID

复用 EndUID 森空岛登录，不会回显登录凭据。

ark账号 / ark我的方舟 - 查看可复用账号
ark绑定列表 - 查看 EndUID 记录的明日方舟 UID
ark理智 - 查询理智和基础状态
ark每日 / ark日常 - 查询森空岛日常状态
ark理智 2 / ark理智 UID - 查询指定账号
ark签到 - 森空岛明日方舟签到
ark签到 2 / ark签到 UID - 指定账号签到

arkwiki 词条 - 查询 PRTS 词条
ark干员 干员名 - 查询干员资料
ark材料 材料名 - 查询材料资料
ark敌人 敌人名 - 查询敌人资料
ark关卡 1-7 - 查询关卡资料和地图链接

ark帮助 - 查看本帮助
"""


@help_sv.on_fullmatch(("帮助", "help", "方舟帮助"), block=True)
async def send_ark_help(bot: Bot, ev: Event) -> None:
    await bot.send((" " if ev.group_id else "") + HELP_TEXT, at_sender=bool(ev.group_id))

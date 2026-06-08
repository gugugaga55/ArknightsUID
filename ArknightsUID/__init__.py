"""ArknightsUID plugin entry."""

from gsuid_core.sv import Plugins

Plugins(
    name="ArknightsUID",
    force_prefix=["ark", "方舟", "明日方舟"],
    allow_empty_prefix=False,
    alias=["arknights", "arknightsuid"],
)

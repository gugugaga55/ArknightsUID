from pathlib import Path

from gsuid_core.data_store import get_res_path

MAIN_PATH = get_res_path("ArknightsUID")
CACHE_PATH = MAIN_PATH / "cache"
RESOURCE_PATH = MAIN_PATH / "resource"

for _path in (CACHE_PATH, RESOURCE_PATH):
    _path.mkdir(parents=True, exist_ok=True)

PLUGIN_ROOT = Path(__file__).resolve().parents[3]

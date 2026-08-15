from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = Path(__file__).resolve().parents[4]
REAL_DATA_ROOT = CORE_ROOT / "data"
TEST_DATA_ROOT_ENV = "GSUID_CORE_TEST_DATA_ROOT"

REQUIRED_MODULES = [
    "ArknightsUID",
    "ArknightsUID.utils.enduid.adapter",
    "ArknightsUID.arknightsuid_account",
    "ArknightsUID.arknightsuid_wiki",
    "ArknightsUID.arknightsuid_help",
    "ArknightsUID.utils.skland.client",
    "ArknightsUID.utils.prts.client",
]

IMPORT_SCRIPT = textwrap.dedent(
    r"""
    from __future__ import annotations

    import importlib
    import json
    import os
    import sys
    from pathlib import Path


    plugin_root = Path(os.environ["ARKNIGHTSUID_PLUGIN_ROOT"])
    core_root = Path(os.environ["GSUID_CORE_ROOT"])
    data_root = Path(os.environ["GSUID_CORE_TEST_DATA_ROOT"])
    real_data_root = Path(os.environ["GSUID_CORE_REAL_DATA_ROOT"])

    for import_root in (plugin_root, core_root):
        import_root_text = str(import_root)
        if import_root_text not in sys.path:
            sys.path.insert(0, import_root_text)

    import gsuid_core.data_store as data_store

    imported = []
    for module_name in os.environ["ARKNIGHTSUID_IMPORT_MODULES"].split(","):
        module = importlib.import_module(module_name)
        imported.append(module.__name__)

    import gsuid_core.config as gscore_config
    import gsuid_core.logger as gscore_logger
    from ArknightsUID.utils.resource import RESOURCE_PATH as resource_paths
    from ArknightsUID.utils.prts import client as prts_client

    eager_enduid_modules = [
        name
        for name in sys.modules
        if name == "EndUID" or name.startswith("EndUID.")
    ]
    if eager_enduid_modules:
        raise AssertionError(
            f"EndUID was imported eagerly: {eager_enduid_modules[:5]}"
        )

    print(
        json.dumps(
            {
                "data_root": str(data_root),
                "env_name": data_store.TEST_DATA_ROOT_ENV,
                "imported": imported,
                "real_data_root": str(real_data_root),
                "runtime_paths": {
                    "gscore_data_root": str(data_store.gs_data_path),
                    "gscore_res": str(data_store.RES),
                    "gscore_config": str(gscore_config.CONFIG_PATH),
                    "gscore_logs": str(gscore_logger.LOG_PATH),
                    "gscore_data_cache": str(data_store.data_cache_path),
                    "arknightsuid_main": str(resource_paths.MAIN_PATH),
                    "arknightsuid_cache": str(resource_paths.CACHE_PATH),
                    "arknightsuid_resource": str(resource_paths.RESOURCE_PATH),
                    "prts_cache": str(prts_client.PRTS_CACHE_PATH),
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    """
)


class RealRuntimeImportSmokeTest(unittest.TestCase):
    def assert_is_relative_to(
        self,
        path: Path,
        root: Path,
        *,
        msg: str,
    ) -> None:
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError:
            self.fail(msg)

    def assert_is_not_relative_to(
        self,
        path: Path,
        root: Path,
        *,
        msg: str,
    ) -> None:
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError:
            return
        self.fail(msg)

    def test_required_modules_import_with_real_gscore_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arknightsuid-gscore-") as tmp:
            data_root = Path(tmp) / "data"
            env = os.environ.copy()
            python_path = [str(PLUGIN_ROOT), str(CORE_ROOT)]
            if env.get("PYTHONPATH"):
                python_path.append(env["PYTHONPATH"])
            env["PYTHONPATH"] = os.pathsep.join(python_path)
            env["ARKNIGHTSUID_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
            env["GSUID_CORE_ROOT"] = str(CORE_ROOT)
            env[TEST_DATA_ROOT_ENV] = str(data_root)
            env["GSUID_CORE_REAL_DATA_ROOT"] = str(REAL_DATA_ROOT)
            env["ARKNIGHTSUID_IMPORT_MODULES"] = ",".join(REQUIRED_MODULES)

            proc = subprocess.run(
                [sys.executable, "-c", IMPORT_SCRIPT],
                cwd=PLUGIN_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(
                proc.returncode,
                0,
                msg=(
                    "real GsCore import smoke failed\n"
                    f"stdout:\n{proc.stdout}\n"
                    f"stderr:\n{proc.stderr}"
                ),
            )
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["env_name"], TEST_DATA_ROOT_ENV)
            self.assertEqual(payload["imported"], REQUIRED_MODULES)
            self.assertTrue((data_root / "config.json").exists())
            self.assertTrue((data_root / "logs").exists())
            runtime_paths = {
                name: Path(value)
                for name, value in payload["runtime_paths"].items()
            }
            for name, path in runtime_paths.items():
                self.assert_is_relative_to(
                    path,
                    data_root,
                    msg=f"{name} was not isolated under temp data root: {path}",
                )
                self.assert_is_not_relative_to(
                    path,
                    REAL_DATA_ROOT,
                    msg=f"{name} used the real GsCore data root: {path}",
                )

            for directory_name in (
                "gscore_logs",
                "gscore_data_cache",
                "arknightsuid_main",
                "arknightsuid_cache",
                "arknightsuid_resource",
                "prts_cache",
            ):
                self.assertTrue(
                    runtime_paths[directory_name].is_dir(),
                    msg=f"{directory_name} was not created as an isolated dir",
                )


if __name__ == "__main__":
    unittest.main()

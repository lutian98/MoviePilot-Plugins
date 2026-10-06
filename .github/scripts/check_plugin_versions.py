"""
校验插件市场索引与插件源码的版本、历史记录和元数据一致性。

对应官方《插件开发指南（V3）》发布前清单：``plugin_version``、索引 ``version``
与 ``history`` 顶部必须一致。作为 CI 门禁运行。

用法：``python .github/scripts/check_plugin_versions.py``
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INDEX_FILES = ("package.v3.json",)
REQUIRED_FIELDS = ("name", "description", "labels", "version", "icon", "author")


def check_index(index_path: Path) -> list[str]:
    """校验单个索引文件，返回问题列表。"""
    errors: list[str] = []
    index = json.loads(index_path.read_text(encoding="utf-8"))
    for plugin_id, meta in index.items():
        source = ROOT / "plugins.v3" / plugin_id.lower() / "__init__.py"
        if not source.exists():
            errors.append(f"{plugin_id}: 缺少插件源码 {source.relative_to(ROOT)}")
            continue

        for field in REQUIRED_FIELDS:
            if not meta.get(field):
                errors.append(f"{plugin_id}: 索引缺少字段 {field}")

        text = source.read_text(encoding="utf-8")
        match = re.search(r'plugin_version\s*=\s*"([^"]+)"', text)
        code_version = match.group(1) if match else None
        index_version = meta.get("version")
        if code_version != index_version:
            errors.append(
                f"{plugin_id}: 插件类 plugin_version={code_version} 与索引 version={index_version} 不一致"
            )

        history = meta.get("history") or {}
        if not history:
            errors.append(f"{plugin_id}: history 为空")
        elif next(iter(history)) != f"v{index_version}":
            errors.append(
                f"{plugin_id}: history 首条应为当前版本 v{index_version}，实际为 {next(iter(history))}"
            )
        if not source.with_name("__init__.py").exists():
            errors.append(f"{plugin_id}: 插件目录缺少 __init__.py")

        for meta_field, attr in (("name", "plugin_name"), ("version", "plugin_version")):
            if f'{attr} = "{meta.get(meta_field)}"' not in text and attr == "plugin_version":
                errors.append(f"{plugin_id}: 插件类缺少 {attr}")

    return errors


def main() -> int:
    """执行全部索引文件的校验。"""
    errors: list[str] = []
    for name in INDEX_FILES:
        path = ROOT / name
        if not path.exists():
            errors.append(f"缺少索引文件 {name}")
            continue
        errors.extend(check_index(path))

    if errors:
        print("插件索引校验失败：")
        for item in errors:
            print(f"  ✗ {item}")
        return 1
    print("✓ 插件索引与源码版本一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())

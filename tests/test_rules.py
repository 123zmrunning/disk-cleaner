import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import analyzer
from analyzer import (CLEANUP_RULES, FolderEntry, MB, apply_cleanup_rules,
                      build_system_items, FileEntry)


def folders(*paths, size=800 * MB):
    return [FolderEntry(path=p, size_bytes=size) for p in paths]


def by_path(items):
    return {item.path.rstrip("\\"): item for item in items}


class DangerousPathTest(unittest.TestCase):
    """这些路径绝对不能出现可执行（delete/backup）的清理项"""

    DANGEROUS = [
        r"C:\ProjectData\app_shell_cache",
        r"C:\ProjectData\JetBrains\MyApp\caches",
        r"C:\Users\Me\AppData\Roaming\Mozilla",
        r"C:\Users\Me\AppData\Roaming\Mozilla\Firefox",
        r"C:\Users\Me\AppData\Roaming\Mozilla\Firefox\Profiles\abc123.default-release",
        r"C:\Program Files\KOOK",
        r"C:\Program Files (x86)\KOOK",
        r"C:\Program Files\Razer",
        r"C:\Program Files\Razer\Synapse3",
        r"C:\Users\Me\AppData\Local\JianyingPro\User Data\Projects",
        r"C:\Users\Me\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft",
        r"C:\Users\Me\Documents\WeChat Files",
        r"C:\Windows\System32",
        r"C:\Windows\WinSxS",
        r"C:\Windows\Installer",
        r"C:\Program Files\WindowsApps",
        r"C:\ProgramData\Package Cache",
        r"C:\Users\Me\AppData\Local\Programs\Microsoft VS Code",
        r"C:\Users\Me\AppData\Local\Google\Chrome\User Data\Default\Preferences",
    ]

    def test_no_actionable_item_for_dangerous_paths(self):
        items = apply_cleanup_rules(folders(*["%s\\" % p for p in self.DANGEROUS]))
        actionable = [i for i in items if i.action != "guide"]
        for item in actionable:
            self.fail("危险路径被判定为可清理：%s -> %s" % (item.path, item.name))

    def test_system_components_are_guide_only(self):
        paths = [r"C:\Windows\WinSxS", r"C:\Windows\Installer",
                 r"C:\Program Files\WindowsApps", r"C:\ProgramData\Package Cache"]
        items = apply_cleanup_rules(folders(*["%s\\" % p for p in paths]))
        mapped = by_path(items)
        for path in paths:
            item = mapped.get(path)
            self.assertIsNotNone(item, "系统组件应作为只读建议出现：%s" % path)
            self.assertEqual(item.action, "guide")


class LegitCacheTest(unittest.TestCase):
    CASES = [
        (r"C:\Users\Me\AppData\Local\JetBrains\PyCharm2026.2\caches", "delete", 2),
        (r"C:\Users\Me\AppData\Local\NVIDIA\DXCache", "delete", 1),
        (r"C:\Users\Me\AppData\Local\AMD\GLCache", "delete", 1),
        (r"C:\Users\Me\AppData\Local\Temp", "delete", 1),
        (r"C:\Windows\Temp", "delete", 1),
        (r"C:\Windows\Prefetch", "delete", 2),
        (r"C:\Users\Me\AppData\Local\Google\Chrome\User Data\Default\Cache", "delete", 2),
        (r"C:\Users\Me\AppData\Local\Microsoft\Edge\User Data\Default\Code Cache", "delete", 2),
        (r"C:\Users\Me\AppData\Local\Mozilla\Firefox\Profiles\abc.default\cache2", "delete", 2),
        (r"C:\Users\Me\AppData\Local\KOOK\Cache", "delete", 2),
        (r"C:\Users\Me\AppData\Local\Microsoft\Windows\Explorer", "delete", 1),
        (r"C:\ProgramData\Microsoft\Windows\WER\ReportArchive", "delete", 1),
        (r"C:\Windows\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache",
         "delete", 2),
        (r"C:\Windows\Logs\CBS", "delete", 2),
        (r"C:\Program Files (x86)\Steam\htmlcache", "delete", 1),
        (r"C:\Steam\htmlcache", "delete", 1),
        (r"C:\Users\Me\AppData\Roaming\Adobe\Common\Media Cache Files", "delete", 2),
        (r"C:\Users\Me\AppData\Roaming\Microsoft\Teams\Cache", "delete", 2),
        (r"C:\Users\Me\AppData\Roaming\Lark\sdk_storage", "delete", 2),
        (r"C:\Users\Me\AppData\Roaming\Zoom\data", "backup", 4),
    ]

    def test_known_caches_match(self):
        items = apply_cleanup_rules(folders(*["%s\\" % p for p, _a, _p in self.CASES]))
        mapped = by_path(items)
        for path, action, priority in self.CASES:
            item = mapped.get(path)
            self.assertIsNotNone(item, "已知缓存没有被识别：%s" % path)
            self.assertEqual(item.action, action, path)
            self.assertEqual(item.priority, priority, path)

    def test_temp_deletes_contents_not_folder(self):
        items = apply_cleanup_rules(folders(r"C:\Users\Me\AppData\Local\Temp\\"))
        item = by_path(items)[r"C:\Users\Me\AppData\Local\Temp"]
        self.assertEqual(item.target, "contents")

    def test_prefetch_only_files(self):
        items = apply_cleanup_rules(folders(r"C:\Windows\Prefetch\\"))
        item = by_path(items)[r"C:\Windows\Prefetch"]
        self.assertEqual(item.target, "files")
        self.assertIn("*.pf", item.file_globs)
        self.assertFalse(item.recursive)

    def test_small_folders_ignored(self):
        small = [FolderEntry(path=r"C:\Users\Me\AppData\Local\NVIDIA\DXCache\\",
                             size_bytes=1 * MB)]
        self.assertEqual(apply_cleanup_rules(small), [])

    def test_parent_child_not_double_counted(self):
        paths = [r"C:\Users\Me\AppData\Local\Temp\\",
                 r"C:\Users\Me\AppData\Local\Temp\chrome_installer\\"]
        items = apply_cleanup_rules(folders(*paths))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].path, r"C:\Users\Me\AppData\Local\Temp")


class RuleSchemaTest(unittest.TestCase):
    REQUIRED = {"pattern", "hint", "priority", "name", "reason", "method",
                "risk", "action", "target", "globs", "recursive", "admin"}

    def test_rules_are_well_formed(self):
        self.assertGreater(len(CLEANUP_RULES), 40)
        for rule in CLEANUP_RULES:
            self.assertTrue(self.REQUIRED.issubset(rule), rule.get("name"))
            self.assertIn(rule["action"], ("delete", "backup", "guide"))
            self.assertIn(rule["target"], ("folder", "contents", "files"))
            self.assertIn(rule["priority"], (1, 2, 3, 4))
            self.assertTrue(rule["hint"], "缺少 hint 会导致扫描变慢：%s" % rule["name"])
            if rule["target"] == "files":
                self.assertTrue(rule["globs"], rule["name"])
            import re
            re.compile(rule["pattern"])

    def test_hints_are_lowercase(self):
        for rule in CLEANUP_RULES:
            hints = rule["hint"] if isinstance(rule["hint"], tuple) else (rule["hint"],)
            for hint in hints:
                self.assertEqual(hint, hint.lower(), rule["name"])


class SystemItemTest(unittest.TestCase):
    def test_hiberfil_and_pagefile(self):
        files = [FileEntry(r"C:\hiberfil.sys", 8 * 1024 ** 3),
                 FileEntry(r"C:\pagefile.sys", 16 * 1024 ** 3)]
        items = build_system_items(files)
        names = {item.name for item in items}
        self.assertTrue(any("hiberfil" in n for n in names))
        self.assertTrue(any("pagefile" in n for n in names))
        for item in items:
            self.assertEqual(item.action, "guide")


if __name__ == "__main__":
    unittest.main()

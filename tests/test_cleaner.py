import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cleaner
from cleaner import (ItemResult, execute_item, execute_items, path_is_safe,
                     prune_backups, write_backup_manifest)


class FakeItem(object):
    def __init__(self, path, action="delete", target="folder", globs=(), recursive=True):
        self.name = os.path.basename(path.rstrip("\\")) or path
        self.path = path
        self.action = action
        self.target = target
        self.file_globs = tuple(globs)
        self.recursive = recursive


class SafetyTest(unittest.TestCase):
    def test_steam_suffix_cannot_bypass_protected_locations(self):
        for base in (r"C:\Windows\System32", r"C:\Users\Me\Documents",
                     r"C:\Users\Me\Desktop", r"C:\ProgramData\Package Cache"):
            for suffix in (r"Steam\htmlcache", r"Steam\steamapps\downloading",
                           r"Steam\steamapps\workshop\downloads"):
                with self.subTest(base=base, suffix=suffix):
                    self.assertFalse(path_is_safe(os.path.join(base, suffix))[0])

    def test_only_standard_steam_install_has_program_files_exception(self):
        for base in (r"C:\Program Files\Steam", r"C:\Program Files (x86)\Steam"):
            for suffix in ("htmlcache", r"steamapps\downloading",
                           r"steamapps\workshop\downloads"):
                self.assertTrue(path_is_safe(os.path.join(base, suffix))[0])
        self.assertFalse(path_is_safe(r"C:\Program Files\Other\Steam\htmlcache")[0])
        self.assertFalse(path_is_safe(r"C:\Program Files\Steam\steamapps\common")[0])

    def test_rejects_drive_root_and_system_dirs(self):
        blocked = [
            "C:\\",
            "C:\\Windows",
            "C:\\Windows\\System32",
            "C:\\Windows\\System32\\drivers",
            "C:\\Windows\\WinSxS",
            "C:\\Windows\\Installer",
            "C:\\Program Files",
            "C:\\Program Files\\KOOK",
            "C:\\Program Files (x86)\\Steam\\steamapps",
            "C:\\Users",
            "C:\\Users\\Me",
            "C:\\Users\\Me\\Documents",
            "C:\\Users\\Me\\Desktop\\a",
            "C:\\Users\\Me\\AppData\\Local",
            "C:\\Users\\Me\\AppData\\Local\\Packages\\Foo",
            "C:\\Users\\Me\\AppData\\Roaming\\Mozilla\\Firefox\\Profiles\\x",
            "D:\\Temp",
        ]
        for path in blocked:
            safe, reason = path_is_safe(path)
            self.assertFalse(safe, "应该拒绝：%s" % path)
            self.assertTrue(reason)

    def test_allows_known_cache_locations(self):
        allowed = [
            "C:\\Users\\Me\\AppData\\Local\\Temp",
            "C:\\Users\\Me\\AppData\\Local\\NVIDIA\\DXCache",
            "C:\\Windows\\Temp",
            "C:\\Windows\\Prefetch",
            "C:\\ProgramData\\Microsoft\\Windows\\WER\\ReportArchive",
            "C:\\Program Files (x86)\\Steam\\htmlcache",
        ]
        for path in allowed:
            safe, reason = path_is_safe(path)
            self.assertTrue(safe, "应该允许：%s（%s）" % (path, reason))


class ExecuteTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="dc_test_")
        self.addCleanup(shutil.rmtree, self.base, True)

    def _make_tree(self, name="cache"):
        root = os.path.join(self.base, name)
        os.makedirs(os.path.join(root, "sub"))
        with open(os.path.join(root, "a.bin"), "wb") as f:
            f.write(b"x" * 1000)
        with open(os.path.join(root, "sub", "b.tmp"), "wb") as f:
            f.write(b"y" * 500)
        return root

    def test_delete_folder_counts_real_bytes(self):
        root = self._make_tree()
        result = execute_item(FakeItem(root))
        self.assertEqual(result.status, "deleted")
        self.assertEqual(result.bytes_freed, 1500)
        self.assertEqual(result.files, 2)
        self.assertFalse(os.path.exists(root))

    def test_contents_keeps_directory(self):
        root = self._make_tree()
        result = execute_item(FakeItem(root, target="contents"))
        self.assertEqual(result.status, "deleted")
        self.assertTrue(os.path.isdir(root))
        self.assertEqual(os.listdir(root), [])

    def test_locked_file_reports_partial_completion(self):
        root = self._make_tree()
        locked = os.path.join(root, "sub", "b.tmp")
        unlink = os.unlink

        def unlink_except_locked(path, *args, **kwargs):
            if os.path.normcase(path) == os.path.normcase(locked):
                raise PermissionError("文件被占用")
            return unlink(path, *args, **kwargs)

        with patch.object(cleaner.os, "unlink", side_effect=unlink_except_locked):
            result = execute_item(FakeItem(root))
        self.assertEqual(result.status, "partial")
        self.assertFalse(result.ok)
        self.assertEqual(result.status_label, "部分完成")
        self.assertEqual(result.bytes_freed, 1000)
        self.assertEqual(result.files, 1)
        self.assertGreaterEqual(result.failed_operations, 1)
        self.assertTrue(result.errors)
        self.assertTrue(os.path.isfile(locked))

    def test_zero_byte_file_deleted_before_failure_is_partial(self):
        root = os.path.join(self.base, "empty_files")
        os.makedirs(root)
        with open(os.path.join(root, "empty.txt"), "wb"):
            pass
        with patch.object(cleaner.os, "rmdir", side_effect=PermissionError("目录被占用")):
            result = execute_item(FakeItem(root))
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.files, 1)
        self.assertEqual(result.bytes_freed, 0)

    def test_total_failure_is_not_success(self):
        root = self._make_tree()
        with patch.object(cleaner.os, "unlink", side_effect=PermissionError("文件被占用")):
            result = execute_item(FakeItem(root))
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.ok)
        self.assertEqual(result.files, 0)
        self.assertGreater(result.failed_operations, 0)

    def test_files_target_only_matches_globs(self):
        root = os.path.join(self.base, "thumbs")
        os.makedirs(root)
        keep = os.path.join(root, "settings.ini")
        drop = os.path.join(root, "thumbcache_123.db")
        for path in (keep, drop):
            with open(path, "wb") as f:
                f.write(b"z" * 10)
        result = execute_item(FakeItem(root, target="files",
                                       globs=("thumbcache_*.db",), recursive=False))
        self.assertEqual(result.status, "deleted")
        self.assertTrue(os.path.exists(keep))
        self.assertFalse(os.path.exists(drop))
        self.assertEqual(result.bytes_freed, 10)

    def test_protected_path_is_denied(self):
        result = execute_item(FakeItem("C:\\Windows\\System32\\drivers"))
        self.assertEqual(result.status, "denied")
        self.assertTrue(result.errors)

    def test_readonly_file_is_removed(self):
        root = os.path.join(self.base, "ro")
        os.makedirs(root)
        path = os.path.join(root, "locked.bin")
        with open(path, "wb") as f:
            f.write(b"r" * 32)
        os.chmod(path, stat.S_IREAD)
        result = execute_item(FakeItem(root))
        self.assertEqual(result.status, "deleted")
        self.assertEqual(result.bytes_freed, 32)

    def test_backup_moves_and_writes_manifest(self):
        root = self._make_tree()
        backup_root = os.path.join(self.base, "backup")
        os.makedirs(backup_root)
        result = execute_items([FakeItem(root, action="backup")],
                               backup_root=backup_root)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].status, "moved")
        self.assertEqual(result[0].bytes_freed, 0)
        self.assertEqual(result[0].bytes_moved, 1500)
        self.assertFalse(os.path.exists(root))
        self.assertTrue(os.path.isdir(result[0].dest))
        runs = os.listdir(backup_root)
        self.assertEqual(len(runs), 1)
        manifest = os.path.join(backup_root, runs[0], "_manifest.json")
        self.assertTrue(os.path.isfile(manifest))

    def test_backup_prune_failure_preserves_completed_results(self):
        root = self._make_tree()
        backup_root = os.path.join(self.base, "backup")
        with patch.object(cleaner, "prune_backups", side_effect=OSError("旧备份被占用")):
            results = execute_items([FakeItem(root, action="backup")],
                                    backup_root=backup_root)
        self.assertEqual([r.status for r in results], ["moved", "failed"])
        self.assertTrue(os.path.isdir(results[0].dest))
        self.assertIn("旧备份被占用", results[1].errors[0])

    def test_prune_only_removes_expired_manifested_runs(self):
        backup_root = os.path.join(self.base, "backup")
        run_dir = os.path.join(backup_root, "20200101_000000")
        os.makedirs(run_dir)
        write_backup_manifest(run_dir, [ItemResult("x", "C:\\x", "backup", "moved",
                                                   bytes_moved=1)], 7)
        old = time.time() - 9 * 86400
        os.utime(os.path.join(run_dir, "_manifest.json"), (old, old))
        removed = prune_backups(backup_root, 7)
        self.assertEqual(len(removed), 1)
        self.assertFalse(os.path.exists(run_dir))

    def test_prune_keeps_fresh_run(self):
        backup_root = os.path.join(self.base, "backup")
        run_dir = os.path.join(backup_root, "20260101_000000")
        os.makedirs(run_dir)
        write_backup_manifest(run_dir, [ItemResult("x", "C:\\x", "backup", "moved",
                                                   bytes_moved=1)], 7)
        self.assertEqual(prune_backups(backup_root, 7), [])
        self.assertTrue(os.path.isdir(run_dir))

    def test_cancel_stops_after_current_item(self):
        items = [FakeItem(self._make_tree("c1")), FakeItem(self._make_tree("c2"))]

        class Cancel(object):
            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > 1

        results = execute_items(items, cancel=Cancel())
        self.assertEqual(len(results), 1)
        self.assertTrue(os.path.isdir(items[1].path))


class PruneSafetyTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="dc_prune_test_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.run_dir = os.path.join(self.base, "20200101_000000")
        os.makedirs(self.run_dir)
        self.payload = os.path.join(self.run_dir, "data.bin")
        with open(self.payload, "wb") as f:
            f.write(b"backup")
        write_backup_manifest(self.run_dir, [
            ItemResult("cache", r"C:\Cache", "backup", "moved", bytes_moved=6)], 7)
        self.manifest = os.path.join(self.run_dir, "_manifest.json")
        old = time.time() - 9 * 86400
        os.utime(self.manifest, (old, old))

    def test_delete_failure_is_not_reported_as_removed(self):
        with patch.object(cleaner.shutil, "rmtree", side_effect=PermissionError("被占用")):
            with self.assertRaisesRegex(OSError, "被占用"):
                prune_backups(self.base, 7)
        self.assertTrue(os.path.isfile(self.payload))

    def test_directory_must_actually_disappear(self):
        with patch.object(cleaner.shutil, "rmtree"):
            with self.assertRaisesRegex(OSError, "未完全删除"):
                prune_backups(self.base, 7)

    def test_protected_or_relative_root_never_reaches_delete(self):
        with patch.object(cleaner.shutil, "rmtree") as remove:
            for root in (r"C:\Windows\System32", r"C:\Users\Me\Documents", "relative"):
                with self.subTest(root=root), self.assertRaises(OSError):
                    prune_backups(root, 7)
            remove.assert_not_called()

    def test_linked_backup_root_is_refused(self):
        original = cleaner._is_reparse
        with patch.object(cleaner, "_is_reparse",
                          side_effect=lambda p: p == self.base or original(p)), \
                patch.object(cleaner.shutil, "rmtree") as remove:
            with self.assertRaisesRegex(OSError, "junction/symlink"):
                prune_backups(self.base, 7)
            remove.assert_not_called()

    def test_nested_reparse_point_is_refused(self):
        original = os.stat

        def reparse_stat(path, *args, **kwargs):
            st = original(path, *args, **kwargs)
            if path == self.payload:
                return SimpleNamespace(st_mode=st.st_mode, st_mtime=st.st_mtime,
                                       st_file_attributes=cleaner.FILE_ATTRIBUTE_REPARSE_POINT)
            return st

        with patch.object(cleaner.os, "stat", side_effect=reparse_stat), \
                patch.object(cleaner.shutil, "rmtree") as remove:
            with self.assertRaisesRegex(OSError, "junction/symlink"):
                prune_backups(self.base, 7)
            remove.assert_not_called()

    def test_linked_manifest_is_refused_before_reading(self):
        original = os.stat

        def reparse_stat(path, *args, **kwargs):
            st = original(path, *args, **kwargs)
            if path == self.manifest and kwargs.get("follow_symlinks") is False:
                return SimpleNamespace(st_mode=st.st_mode, st_mtime=st.st_mtime,
                                       st_file_attributes=cleaner.FILE_ATTRIBUTE_REPARSE_POINT)
            return st

        with patch.object(cleaner.os, "stat", side_effect=reparse_stat), \
                patch.object(cleaner.shutil, "rmtree") as remove:
            with self.assertRaisesRegex(OSError, "备份清单是"):
                prune_backups(self.base, 7)
            remove.assert_not_called()

    def test_unreadable_descendant_is_refused(self):
        original = os.stat

        def unreadable_stat(path, *args, **kwargs):
            if path == self.payload:
                raise PermissionError("无法检查属性")
            return original(path, *args, **kwargs)

        with patch.object(cleaner.os, "stat", side_effect=unreadable_stat), \
                patch.object(cleaner.shutil, "rmtree") as remove:
            with self.assertRaisesRegex(OSError, "无法检查属性"):
                prune_backups(self.base, 7)
            remove.assert_not_called()

    def test_non_batch_directory_is_preserved(self):
        os.rename(self.run_dir, os.path.join(self.base, "personal-data"))
        self.assertEqual(prune_backups(self.base, 7), [])
        self.assertTrue(os.path.isfile(os.path.join(self.base, "personal-data", "data.bin")))

    def test_unrecognized_manifest_is_preserved(self):
        with open(self.manifest, "w", encoding="utf-8") as f:
            f.write('[{"prunable": true}]')
        old = time.time() - 9 * 86400
        os.utime(self.manifest, (old, old))
        self.assertEqual(prune_backups(self.base, 7), [])
        self.assertTrue(os.path.isfile(self.payload))


if __name__ == "__main__":
    unittest.main()

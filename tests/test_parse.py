import io
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from analyzer import (BIG_FILE_MIN_BYTES, FileEntry, FolderEntry, parse_csv,
                      run_analysis)

HEADER = ("文件名称,大小,分配,修改时间,属性,文件,文件夹,"
          "DRIVECAPACITY,FREESPACE,USEDSPACE,RESERVEDSPACE")

ROWS = [
    '生成由 WizTree 4.25 2026/10/21 13:00:00',
    HEADER,
    '"C:\\",105392664576,105392664576,2026/10/21 13:00:00,0,900000,50000,'
    '126876643328,21483978752,105392664576,0',
    '"C:\\Windows\\",40000000000,40000000000,2026/10/21 12:00:00,0,300000,20000,0,0,0,0',
    '"C:\\Users\\Me\\AppData\\Local\\Temp\\",10485760,10485760,2026/10/21 12:00:00,0,100,5,0,0,0,0',
    '"C:\\Users\\Me\\AppData\\Local\\NVIDIA\\DXCache\\",52428800,52428800,2026/10/21 12:00:00,0,20,2,0,0,0,0',
    '"C:\\Users\\Me\\AppData\\Local\\NVIDIA\\",60000000,60000000,2026/10/21 12:00:00,0,30,3,0,0,0,0',
    '"C:\\pagefile.sys",8000000000,8000000000,2026/10/21 12:00:00,32,0,0,0,0,0,0',
    '"C:\\Users\\Me\\notes.txt",1024,1024,2026/10/21 12:00:00,32,0,0,0,0,0,0',
    '"C:\\Windows\\Installer\\",2000000000,2000000000,2026/10/21 12:00:00,0,500,10,0,0,0,0',
    '""',
]


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dc_csv_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "scan.csv")
        with io.open(self.path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("\n".join(ROWS))

    def test_disk_info_parsed(self):
        disk, folders, files = parse_csv(self.path)
        self.assertIsNotNone(disk)
        self.assertEqual(disk.total_bytes, 126876643328)
        self.assertEqual(disk.free_bytes, 21483978752)
        self.assertEqual(disk.used_bytes, 105392664576)
        self.assertEqual(disk.used_percent, 83.1)
        self.assertTrue(disk.scan_time.startswith("生成由 WizTree"))

    def test_folder_rows(self):
        _disk, folders, _files = parse_csv(self.path)
        self.assertEqual(len(folders), 5)
        flags = {f.path: f for f in folders}
        self.assertEqual(flags["C:\\Windows\\"].depth, 1)
        self.assertEqual(flags["C:\\Users\\Me\\AppData\\Local\\Temp\\"].file_count, 100)
        self.assertEqual(flags["C:\\Windows\\Installer\\"].depth, 2)

    def test_only_big_files_kept(self):
        _disk, _folders, files = parse_csv(self.path)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].path, "C:\\pagefile.sys")
        self.assertGreaterEqual(files[0].size_bytes, BIG_FILE_MIN_BYTES)
        self.assertTrue(files[0].name)

    def test_depth_and_name_helpers(self):
        entry = FolderEntry("C:\\Users\\Me\\AppData\\Local\\Temp\\", 1)
        self.assertEqual(entry.depth, 5)
        self.assertEqual(entry.name, "Temp")

    def test_gbk_file_is_readable(self):
        gbk = os.path.join(self.dir, "gbk.csv")
        text = "\n".join(ROWS)
        with io.open(gbk, "w", encoding="gbk", newline="") as f:
            f.write(text)
        disk, folders, _files = parse_csv(gbk)
        self.assertIsNotNone(disk)
        self.assertEqual(len(folders), 5)

    def test_run_analysis_shapes(self):
        result = run_analysis(self.path)
        self.assertIsNone(result["error"])
        self.assertIn("disk_info", result)
        self.assertTrue(result["cleanup_items"])
        # 每一条都必须是真实存在的目录行
        for item in result["cleanup_items"]:
            self.assertTrue(item.path.lower().startswith("c:\\"))
            self.assertNotEqual(item.action, "guide")
        # 系统组件应放进 guide
        self.assertTrue(any("Installer" in g.path for g in result["guide_items"]))
        # 大文件榜里有 pagefile
        self.assertEqual(result["big_files"][0].name, "pagefile.sys")

    def test_appdata_and_top_lists(self):
        result = run_analysis(self.path)
        self.assertTrue(result["top_level1"])
        self.assertEqual(result["top_level1"][0].path, "C:\\Windows\\")
        local_paths = [f.path for f in result["top_appdata_local"]]
        self.assertIn("C:\\Users\\Me\\AppData\\Local\\Temp\\", local_paths)
        self.assertIn("C:\\Users\\Me\\AppData\\Local\\NVIDIA\\", local_paths)

    def test_missing_file_returns_empty(self):
        disk, folders, files = parse_csv(os.path.join(self.dir, "nope.csv"))
        self.assertIsNone(disk)
        self.assertEqual(folders, [])
        self.assertEqual(files, [])


if __name__ == "__main__":
    unittest.main()

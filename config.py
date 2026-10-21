# ==============================================================
# config.py —— 配置读写与系统路径
# ==============================================================
# 【2026-10 改动】
#   1. 配置从"程序目录"迁到 %APPDATA%\DiskCleaner\ —— 程序放在
#      Program Files 时也能保存设置
#   2. 原子写入：先写临时文件再替换，断电不会写坏配置
#   3. 桌面路径改用 SHGetKnownFolderPath，兼容 OneDrive 重定向
# ==============================================================

import ctypes
import json
import os
import sys
import tempfile
from ctypes import wintypes


def get_app_dir() -> str:
    """应用程序运行目录（兼容 PyInstaller 打包环境）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_config_dir() -> str:
    """配置目录：%APPDATA%\\DiskCleaner"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "DiskCleaner")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return get_app_dir()
    return path


CONFIG_FILE = os.path.join(get_config_dir(), "config.json")
LEGACY_CONFIG_FILE = os.path.join(get_app_dir(), "config.json")
HISTORY_FILE = os.path.join(get_config_dir(), "cleanup_history.json")

DEFAULT_CONFIG = {
    "wiztree_path": "",
    "csv_dir": "",              # CSV 保存目录，空 = 桌面
    "report_dir": "",           # 报告保存目录，空 = 桌面
    "last_csv_file": "",
    "theme": "dark",
    "scan_timeout_sec": 900,    # 扫描超时（秒）
    "export_files": True,       # 是否导出文件（大文件榜需要）
    "admin_scan": "auto",       # auto = 已是管理员时才用 /admin=1
    "backup_dir": "",           # 备份目录，空 = 自动选择
    "backup_retention_days": 7, # 备份保留天数
}


def _migrate_legacy() -> dict:
    """把旧版本放在程序目录的 config.json 迁移过来"""
    if not os.path.isfile(LEGACY_CONFIG_FILE):
        return {}
    try:
        with open(LEGACY_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_config() -> dict:
    data = {}
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    else:
        data = _migrate_legacy()
        if data:
            save_config(data)

    # 兼容 v1.0 的 export_dir 字段
    if "export_dir" in data:
        if not data.get("csv_dir"):
            data["csv_dir"] = data["export_dir"]
        data.pop("export_dir", None)

    config = dict(DEFAULT_CONFIG)
    config.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})

    # 类型兜底，避免手改配置后程序崩在奇怪的地方
    for key in ("scan_timeout_sec", "backup_retention_days"):
        try:
            config[key] = max(1, int(config[key]))
        except (TypeError, ValueError):
            config[key] = DEFAULT_CONFIG[key]
    for key in ("export_files",):
        config[key] = bool(config[key])
    return config


def save_config(config: dict) -> bool:
    """原子写入配置；成功返回 True"""
    directory = os.path.dirname(CONFIG_FILE)
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="config_", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CONFIG_FILE)
        return True
    except OSError as e:
        print("[配置] 保存失败：%s" % e)
        try:
            os.remove(tmp_path)  # type: ignore[name-defined]
        except (OSError, UnboundLocalError):
            pass
        return False


# ── 已知文件夹（桌面）────────────────────────────────────────────

class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

# {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
_FOLDERID_DESKTOP = _GUID(
    0xB4BFCC3A, 0xDB2C, 0x424C,
    (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))


def _known_folder(guid) -> str:
    buf = ctypes.c_wchar_p()
    try:
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(buf))
        if hr != 0 or not buf.value:
            raise OSError(hr, "SHGetKnownFolderPath 失败")
        return buf.value
    finally:
        try:
            ctypes.windll.ole32.CoTaskMemFree(buf)
        except Exception:
            pass


def get_desktop_path() -> str:
    """桌面路径（兼容 OneDrive / 中文系统 / 组策略重定向）"""
    try:
        return _known_folder(_FOLDERID_DESKTOP)
    except Exception:
        return os.path.join(os.environ.get("USERPROFILE", "C:\\Users"), "Desktop")

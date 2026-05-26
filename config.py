import json
import os
import sys


def get_app_dir() -> str:
    """返回应用程序的运行目录（兼容PyInstaller打包环境）"""
    if getattr(sys, 'frozen', False):
        # PyInstaller打包后，sys.executable 指向 .exe 文件
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_FILE = os.path.join(get_app_dir(), "config.json")

DEFAULT_CONFIG = {
    "wiztree_path": "",
    "csv_dir": "",        # CSV扫描文件保存目录，空字符串=默认桌面
    "report_dir": "",     # 报告输出目录，空字符串=默认桌面
    "last_csv_file": "",
    "theme": "dark",
}


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            # 兼容 v1.0 的 export_dir 字段
            if "export_dir" in saved and not saved.get("csv_dir"):
                saved["csv_dir"] = saved.pop("export_dir")
            elif "export_dir" in saved:
                saved.pop("export_dir")
            return {**DEFAULT_CONFIG, **saved}
        except (json.JSONDecodeError, IOError):
            return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()


def save_config(config: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"[配置] 保存失败：{e}")


def get_desktop_path() -> str:
    return os.path.join(os.environ.get("USERPROFILE", "C:\\Users"), "Desktop")

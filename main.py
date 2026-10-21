# ==============================================================
# main.py —— 程序入口
# ==============================================================
# 只做三件事：检查依赖、设置工作目录、启动界面。
# 出错时把 traceback 写到 %APPDATA%\DiskCleaner\error.log，
# 方便用户把日志发回来排查（打包成 EXE 后没有控制台）。
# ==============================================================

import os
import sys
import time
import traceback

# ── 依赖检查 ──────────────────────────────────────────────────
try:
    import customtkinter  # noqa: F401
except ImportError:
    print("=" * 60)
    print("错误：未安装 customtkinter 库")
    print()
    print("请在命令提示符（cmd）中运行：")
    print()
    print("    pip install -r requirements.txt")
    print()
    print("如果 pip 不可用，说明 Python 未正确安装：")
    print("https://www.python.org/downloads/（安装时勾选 Add Python to PATH）")
    print("=" * 60)
    input("\n按回车键退出...")
    sys.exit(1)

# 确保相对路径以程序目录为基准（打包后不能用 __file__，它在临时解压目录里）
from config import get_app_dir

os.chdir(get_app_dir())

from app import DiskCleanerApp


def _write_crash_log(exc_text: str) -> str:
    """把崩溃信息写到配置目录，返回日志路径"""
    try:
        from config import get_config_dir
        log_path = os.path.join(get_config_dir(), "error.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n===== %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            f.write(exc_text)
        return log_path
    except Exception:
        return ""


def main():
    try:
        app = DiskCleanerApp()
        app.mainloop()
    except Exception:
        exc_text = traceback.format_exc()
        log_path = _write_crash_log(exc_text)
        sys.stderr.write(exc_text)
        try:
            from tkinter import messagebox
            messagebox.showerror(
                "程序异常退出",
                "发生了未处理的错误。\n\n"
                + (("日志已保存到：\n%s\n\n" % log_path) if log_path else "")
                + exc_text[-1200:])
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()

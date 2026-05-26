# ==============================================================
# main.py —— 程序入口
# ==============================================================
# 【作用】这是整个程序的启动入口。
#         双击运行这个文件（或通过启动.bat），软件就会打开。
#
# 【为什么需要单独的 main.py】
#   把启动逻辑和界面逻辑分开是好习惯。
#   main.py 只做一件事：检查依赖、然后启动应用。
#   这样出错时更容易定位问题。
# ==============================================================

import sys
import os

# ── 依赖检查 ──────────────────────────────────────────────────
# 在导入 customtkinter 前先检查它是否已安装
# 如果没安装，给出清晰的安装提示
try:
    import customtkinter
except ImportError:
    # 这段代码在 customtkinter 未安装时会执行
    print("=" * 60)
    print("错误：未安装 customtkinter 库")
    print()
    print("请在命令提示符（cmd）中运行以下命令安装：")
    print()
    print("    pip install customtkinter")
    print()
    print("如果 pip 命令不可用，说明 Python 未正确安装。")
    print("请到 https://www.python.org/downloads/ 下载安装 Python。")
    print("安装时勾选「Add Python to PATH」选项。")
    print("=" * 60)
    input("\n按回车键退出...")
    sys.exit(1)

# ── 启动应用 ──────────────────────────────────────────────────
# 确保工作目录是程序所在文件夹（避免相对路径问题）
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# 导入并启动主应用
from app import DiskCleanerApp

def main():
    """程序主函数"""
    app = DiskCleanerApp()
    # mainloop() 启动GUI事件循环，程序在这里"运行"
    # 直到用户关闭窗口，mainloop() 才会返回
    app.mainloop()


# Python的惯例：只有直接运行这个文件时才执行 main()
# 如果其他文件 import main，不会自动执行
if __name__ == "__main__":
    main()

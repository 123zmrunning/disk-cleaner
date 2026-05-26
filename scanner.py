# ==============================================================
# scanner.py —— WizTree扫描控制模块
# ==============================================================
# 【作用】负责启动WizTree程序，让它扫描C盘并把结果导出为CSV文件。
#
# 【WizTree命令行参数说明】
#   WizTree64.exe  C:              → 扫描C盘
#   /export="路径"                 → 把结果导出到指定CSV文件
#   /exportfolders                 → 只导出文件夹（速度快，文件小）
#
# 【为什么去掉 /admin=1】
#   /admin=1 会让WizTree通过UAC申请管理员权限，此时它会：
#   1. 启动一个新的「提升权限」进程
#   2. 原始进程立刻退出
#   我们的代码在等原始进程，原始进程一退出就认为「完成」了，
#   但实际上提升权限的那个进程还在扫描，CSV文件还没写出来。
#   去掉该参数后，WizTree用当前权限运行，等待才能真正有效。
# ==============================================================

import subprocess
import os
import time
from config import get_desktop_path


def build_export_path(export_dir: str = "") -> str:
    """
    生成CSV导出文件的完整路径。
    export_dir：导出目录，空字符串时使用桌面
    """
    if not export_dir:
        export_dir = get_desktop_path()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(export_dir, f"WizTree_scan_{timestamp}.csv")


def run_wiztree_scan(wiztree_path: str, export_path: str,
                     progress_callback=None):
    """
    启动WizTree扫描C盘并导出CSV，等待完成。
    返回值：(成功: bool, 文件路径或错误信息: str)
    """

    def notify(msg):
        if progress_callback:
            progress_callback(msg)
        print(f"[扫描] {msg}")

    # ── 1. 检查WizTree路径 ───────────────────────────────────────
    if not wiztree_path or not os.path.isfile(wiztree_path):
        return False, f"找不到WizTree程序：{wiztree_path}"

    # ── 2. 确保导出目录存在 ──────────────────────────────────────
    export_dir = os.path.dirname(export_path)
    if export_dir and not os.path.exists(export_dir):
        try:
            os.makedirs(export_dir)
        except OSError as e:
            return False, f"无法创建导出目录：{e}"

    # ── 3. 构建命令字符串 ────────────────────────────────────────
    # 整体用双引号包裹 export 路径，防止路径中的空格被当作分隔符
    # 例：D:\WizTree\WizTree64.exe C: /export="C:\Users\MINGRUI ZHU\Desktop\xxx.csv" /exportfolders
    cmd = f'"{wiztree_path}" C: /export="{export_path}" /exportfolders'

    notify("正在启动WizTree扫描，请稍候...")
    notify("扫描期间WizTree窗口会短暂弹出，属正常现象")

    try:
        # ── 4. 运行并等待 ────────────────────────────────────────
        # shell=True：让Windows命令解释器处理引号和路径
        # timeout=300：最多等5分钟
        subprocess.run(cmd, shell=True, timeout=300)

        # ── 5. 验证文件 ──────────────────────────────────────────
        if os.path.isfile(export_path):
            size_kb = os.path.getsize(export_path) // 1024
            notify(f"扫描完成！CSV已生成（{size_kb} KB）")
            return True, export_path
        else:
            return False, (
                "WizTree已退出，但未找到导出的CSV文件。\n\n"
                "可能原因：\n"
                "  1. WizTree版本过旧（需要4.0+），请更新WizTree\n"
                "  2. 桌面路径含特殊字符\n"
                "  3. 磁盘空间不足无法写文件\n\n"
                "备用方案：手动在WizTree里扫描后导出CSV，\n"
                "保存到桌面，再重新点击扫描按钮。"
            )

    except subprocess.TimeoutExpired:
        return False, "扫描超时（超过5分钟），请手动操作WizTree导出CSV后重试。"
    except Exception as e:
        return False, f"启动WizTree时出错：{e}"

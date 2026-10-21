# ==============================================================
# scanner.py —— WizTree 扫描控制
# ==============================================================
# 【2026-10 重写要点】
#   1. 用参数列表启动进程，不再拼 shell 字符串（避免路径含 & ^ 时空格/注入问题）
#   2. 显式写 /exportfolders=1 /exportfiles=1 —— 裸写 /exportfolders
#      会连文件一起导出，是上一版 CSV 体积暴涨 60 倍的原因
#   3. 用"CSV 大小连续 4 秒不变"判断扫描完成，而不是等进程退出：
#      /admin=1 时 WizTree 会启动提权子进程并立刻退出，等进程必然误判
#   4. 超时时间可配置，默认 15 分钟
# ==============================================================

import os
import subprocess
import time

from config import get_app_dir, get_desktop_path

# 判定扫描完成的静默时间（秒）
STABLE_SECONDS = 4.0
POLL_INTERVAL = 0.5
DEFAULT_TIMEOUT = 900


def build_export_path(export_dir: str = "") -> str:
    """生成 CSV 导出路径；export_dir 为空时使用桌面"""
    if not export_dir:
        export_dir = get_desktop_path()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(export_dir, "WizTree_scan_%s.csv" % stamp)


def _registry_install_dirs():
    """从卸载表里找 WizTree 的安装目录（键名不固定，需要遍历）"""
    locations = []
    try:
        import winreg
    except ImportError:
        return locations

    uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(root, uninstall, 0, winreg.KEY_READ | view) as base:
                    index = 0
                    while True:
                        try:
                            name = winreg.EnumKey(base, index)
                        except OSError:
                            break
                        index += 1
                        try:
                            with winreg.OpenKey(base, name) as sub:
                                try:
                                    display, _ = winreg.QueryValueEx(sub, "DisplayName")
                                except OSError:
                                    continue
                                if "wiztree" not in str(display).lower():
                                    continue
                                try:
                                    location, _ = winreg.QueryValueEx(sub, "InstallLocation")
                                except OSError:
                                    location = ""
                                if location:
                                    locations.append(location)
                        except OSError:
                            continue
            except OSError:
                continue
    return locations


def find_wiztree(configured_path: str = "") -> str:
    """
    查找 WizTree64.exe：先看用户配置，再看程序目录，最后查安装目录。
    返回空字符串表示没找到。
    """
    candidates = []
    if configured_path:
        candidates.append(configured_path)

    app_dir = get_app_dir()
    candidates.append(os.path.join(app_dir, "WizTree64.exe"))

    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            candidates.append(os.path.join(base, "WizTree", "WizTree64.exe"))

    for location in _registry_install_dirs():
        candidates.append(os.path.join(location, "WizTree64.exe"))

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return ""


def _csv_ready(path: str) -> bool:
    """文件已生成、非空、且至少包含表头 + 一行数据"""
    try:
        if os.path.getsize(path) < 64:
            return False
    except OSError:
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    text = head.decode("utf-8-sig", "replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return len(lines) >= 3


def run_wiztree_scan(wiztree_path: str, export_path: str,
                     progress_callback=None,
                     timeout: int = DEFAULT_TIMEOUT,
                     export_files: bool = True,
                     use_admin: bool = False,
                     target: str = "C:"):
    """
    启动 WizTree 扫描 C 盘并导出 CSV，等待文件写完。
    返回 (成功: bool, 文件路径或错误信息: str)
    """

    def notify(msg):
        if progress_callback:
            progress_callback(msg)
        print("[扫描] %s" % msg)

    wiztree_path = find_wiztree(wiztree_path)
    if not wiztree_path:
        return False, ("找不到 WizTree64.exe。\n\n"
                       "请安装 WizTree（免费），或在「设置」里手动指定路径。")

    export_dir = os.path.dirname(os.path.abspath(export_path))
    try:
        os.makedirs(export_dir, exist_ok=True)
    except OSError as e:
        return False, "无法创建导出目录：%s" % e

    # 先删掉可能存在的同名旧文件，避免把旧 CSV 误当成新结果
    try:
        if os.path.exists(export_path):
            os.remove(export_path)
    except OSError:
        pass

    args = [
        wiztree_path, target,
        "/export=%s" % export_path,
        "/exportfolders=1",
        "/exportfiles=%d" % (1 if export_files else 0),
        "/admin=%d" % (1 if use_admin else 0),
    ]

    notify("正在启动 WizTree 扫描（%s）..." % ("管理员模式" if use_admin else "普通权限"))
    if not use_admin:
        notify("提示：以管理员身份运行可启用 MFT 快速扫描，结果更完整")

    start = time.time()
    last_size = -1
    last_change = time.time()
    process = None

    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   creationflags=creationflags)
    except OSError as e:
        return False, "启动 WizTree 失败：%s" % e

    try:
        while True:
            elapsed = time.time() - start
            if elapsed > timeout:
                return False, ("扫描超时（超过 %d 分钟）。\n\n"
                               "可以在设置里调大超时时间，或手动用 WizTree 导出 CSV 后重试。"
                               % max(1, timeout // 60))

            try:
                size = os.path.getsize(export_path)
            except OSError:
                size = -1

            if size != last_size:
                last_size = size
                last_change = time.time()

            if _csv_ready(export_path) and (time.time() - last_change) >= STABLE_SECONDS:
                break

            if process.poll() is not None and size <= 0:
                # 进程退出且没有产物：再给 3 秒，仍没有就报错
                if time.time() - last_change > 3:
                    return False, (
                        "WizTree 已退出，但没有生成 CSV 文件。\n\n"
                        "常见原因：\n"
                        "  1. WizTree 版本过旧（需要 4.0+）\n"
                        "  2. 导出目录无写入权限\n"
                        "  3. 磁盘空间不足\n\n"
                        "备用方案：手动在 WizTree 里扫描 C 盘并导出 CSV，"
                        "放到桌面后重新点击扫描。")

            time.sleep(POLL_INTERVAL)
    finally:
        if process is not None and process.poll() is None:
            # 文件已经写完，但 WizTree 可能还停留在界面上，礼貌地结束它
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()

    size_kb = os.path.getsize(export_path) // 1024
    notify("扫描完成：CSV 已生成（%d KB）" % size_kb)
    return True, export_path

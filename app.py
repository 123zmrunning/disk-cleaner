import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import webbrowser
import threading
import queue
import subprocess
import os
import json
import time

from config import HISTORY_FILE, load_config, save_config, get_desktop_path
from scanner import run_wiztree_scan, build_export_path, find_wiztree
from analyzer import run_analysis, CleanupItem, KNOWLEDGE_DATE, KNOWLEDGE_SOURCES
from cleaner import (default_backup_root, execute_items, is_elevated,
                     prune_backups, restart_as_admin)
from reporter import generate_report, bytes_to_human

WIZTREE_DOWNLOAD_URL = "https://www.diskanalyzer.com/download"

COLORS = {
    "priority_1": "#e74c3c",
    "priority_2": "#e67e22",
    "priority_3": "#f39c12",
    "priority_4": "#27ae60",
    "success": "#2ecc71",
    "warning": "#e67e22",
    "danger": "#e74c3c",
    "info": "#3498db",
    "text_dim": "gray60",
}

PRIORITY_COLORS = {1: COLORS["priority_1"], 2: COLORS["priority_2"],
                   3: COLORS["priority_3"], 4: COLORS["priority_4"]}
PRIORITY_LABELS = {1: "🔴 立即清理", 2: "🟠 高收益缓存",
                   3: "🟡 游戏缓存", 4: "🟢 谨慎操作"}

# 磁盘空间缩略图的配色（与顶级目录一一对应）
TREEMAP_COLORS = [
    "#3498db", "#e74c3c", "#9b59b6", "#f39c12",
    "#1abc9c", "#e67e22", "#2980b9", "#c0392b",
    "#8e44ad", "#d35400", "#16a085", "#e91e63",
    "#00bcd4", "#ff5722", "#607d8b",
]


def _open_folder(path: str):
    """在 Windows 资源管理器中打开路径"""
    clean = path.strip('"').strip()
    if os.path.isdir(clean):
        subprocess.Popen(f'explorer "{clean}"')
    elif os.path.isfile(clean):
        subprocess.Popen(f'explorer /select,"{clean}"')
    else:
        # 路径不存在时，尝试打开其父目录
        parent = os.path.dirname(clean)
        if os.path.isdir(parent):
            subprocess.Popen(f'explorer "{parent}"')


def _big_file_hint(path: str) -> str:
    """给大文件榜加一句人话说明"""
    name = os.path.basename(path).lower()
    if name == "hiberfil.sys":
        return "休眠文件，可 powercfg /h off 关闭"
    if name == "pagefile.sys":
        return "虚拟内存，建议移到 D 盘"
    if name == "swapfile.sys":
        return "系统交换文件，由系统管理"
    if name.endswith((".vhdx", ".vhd")):
        return "虚拟磁盘（WSL/Docker/虚拟机）"
    if name.endswith(".iso"):
        return "系统镜像，可移到 D 盘"
    if name.endswith(".dmp"):
        return "内存转储，排障后可删"
    if name.endswith(".zip") or name.endswith(".7z"):
        return "压缩包，确认后可删或迁移"
    return ""


class DiskCleanerApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.session_csv_path = None  # 本次会话扫描生成的 CSV（用于关闭时询问）
        self.analysis_result = None
        self.guide_items = []
        self.big_files = []
        self._overview_table = None
        self._exec_cancel = threading.Event()
        self._exec_queue = queue.Queue()
        self._exec_running = False

        ctk.set_appearance_mode(self.config.get("theme", "dark"))
        ctk.set_default_color_theme("blue")

        self.title("磁盘清理助手 v3.1  |  C盘空间分析与安全清理")
        self.geometry("1100x760")
        self.minsize(880, 640)

        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
        if os.path.exists(icon_path):
            self.iconbitmap(icon_path)

        self._cleanup_checkboxes = {}
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if not find_wiztree(self.config.get("wiztree_path", "")):
            self.after(500, self._show_wiztree_dialog)
        self.after(2000, self._prune_old_backups)

    # ── 关闭处理 ────────────────────────────────────────────────────

    def _on_close(self):
        """关闭窗口时：先确认没有正在执行的清理，再询问是否保留 CSV"""
        if self._exec_running:
            if not messagebox.askyesno(
                    "清理正在进行",
                    "清理任务尚未结束，现在关闭会中断后续项目（已处理的文件不会回滚）。\n\n仍要关闭？"):
                return
            self._exec_cancel.set()

        csv = self.session_csv_path
        if csv and os.path.isfile(csv):
            size_kb = os.path.getsize(csv) // 1024
            keep = messagebox.askyesno(
                "关闭前确认",
                f"本次扫描生成了 CSV 文件（{size_kb} KB）：\n{csv}\n\n"
                "是否保留该文件？\n\n"
                "• 是 → 保留（下次可重新分析）\n"
                "• 否 → 立即删除（释放磁盘空间）",
            )
            if not keep:
                try:
                    os.remove(csv)
                    self.config["last_csv_file"] = ""
                    save_config(self.config)
                except OSError:
                    pass
        self.destroy()

    # ── 界面构建 ────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._build_toolbar()
        self._build_content_area()
        self._build_statusbar()

    def _build_toolbar(self):
        toolbar = ctk.CTkFrame(self, height=70, corner_radius=0)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(toolbar, text="🔍 磁盘清理助手 v3.0",
                     font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, padx=15, pady=15, sticky="w")

        path_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        path_frame.grid(row=0, column=1, padx=10, pady=10, sticky="ew")
        path_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(path_frame, text="WizTree:", width=65,
                     font=ctk.CTkFont(size=12)).grid(row=0, column=0, padx=5)
        self.wiztree_path_var = ctk.StringVar(
            value=(self.config.get("wiztree_path", "")
                   or find_wiztree("") or "（未设置）"))
        ctk.CTkEntry(path_frame, textvariable=self.wiztree_path_var,
                     state="readonly", font=ctk.CTkFont(size=11),
                     height=30).grid(row=0, column=1, padx=5, sticky="ew")
        ctk.CTkButton(path_frame, text="更改路径", width=80, height=30,
                      command=self._on_change_wiztree_path,
                      fg_color="gray40", hover_color="gray50").grid(row=0, column=2, padx=2)
        ctk.CTkButton(path_frame, text="下载WizTree", width=90, height=30,
                      command=lambda: webbrowser.open(WIZTREE_DOWNLOAD_URL),
                      fg_color=COLORS["info"], hover_color="#2980b9").grid(row=0, column=3, padx=(2, 5))

        btn_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        btn_frame.grid(row=0, column=2, padx=10, pady=10)

        self.theme_switch = ctk.CTkSwitch(btn_frame, text="深色",
                                          command=self._toggle_theme, width=60)
        if self.config.get("theme", "dark") == "dark":
            self.theme_switch.select()
        self.theme_switch.pack(side="left", padx=8)

        self.scan_btn = ctk.CTkButton(
            btn_frame, text="🚀  开始扫描 C盘", width=150, height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._on_scan_click,
            fg_color=COLORS["info"], hover_color="#2980b9")
        self.scan_btn.pack(side="left", padx=5)

    def _build_content_area(self):
        progress_frame = ctk.CTkFrame(self, height=35, corner_radius=0,
                                      fg_color=("gray90", "gray20"))
        progress_frame.grid(row=1, column=0, sticky="ew")
        progress_frame.grid_columnconfigure(1, weight=1)
        progress_frame.grid_propagate(False)

        self.progress_label = ctk.CTkLabel(
            progress_frame, text='就绪 — 点击「开始扫描」启动分析',
            font=ctk.CTkFont(size=12), text_color=COLORS["text_dim"])
        self.progress_label.grid(row=0, column=0, padx=15, sticky="w")

        self.progress_bar = ctk.CTkProgressBar(progress_frame, height=8,
                                               progress_color=COLORS["info"])
        self.progress_bar.grid(row=0, column=1, padx=10, sticky="ew")
        self.progress_bar.set(0)

        self.tabview = ctk.CTkTabview(self, anchor="nw")
        self.tabview.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 5))
        self.grid_rowconfigure(2, weight=1)

        self.tab_overview = self.tabview.add("📊 磁盘概况")
        self.tab_cleanup  = self.tabview.add("🧹 清理建议")
        self.tab_detail   = self.tabview.add("📁 目录详情")
        self.tab_bigfiles = self.tabview.add("📦 大文件")
        self.tab_report   = self.tabview.add("📄 报告操作")
        self.tab_history  = self.tabview.add("📋 历史记录")
        self.tab_settings = self.tabview.add("⚙️ 设置")

        self._build_tab_overview()
        self._build_tab_cleanup()
        self._build_tab_detail()
        self._build_tab_bigfiles()
        self._build_tab_report()
        self._build_tab_history()
        self._build_tab_settings()

    def _build_tab_overview(self):
        tab = self.tab_overview
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(3, weight=1)

        self.overview_cards = ctk.CTkFrame(tab)
        self.overview_cards.grid(row=0, column=0, sticky="ew", pady=5)

        self.overview_hint = ctk.CTkLabel(
            tab,
            text="扫描完成后，磁盘概况将显示在这里。\n\n"
                 "点击右上角「🚀 开始扫描 C盘」按钮开始分析。",
            font=ctk.CTkFont(size=14), text_color=COLORS["text_dim"])
        self.overview_hint.grid(row=1, column=0, pady=60)

        # 磁盘空间缩略图占位（扫描后填入）
        self.treemap_frame = ctk.CTkFrame(tab, fg_color="transparent")

    def _build_tab_cleanup(self):
        tab = self.tab_cleanup
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        self.cleanup_summary = ctk.CTkLabel(tab, text="扫描后将显示清理建议",
                                            font=ctk.CTkFont(size=13),
                                            text_color=COLORS["text_dim"])
        self.cleanup_summary.grid(row=0, column=0, pady=10, sticky="w", padx=10)

        self.cleanup_scroll = ctk.CTkScrollableFrame(tab, label_text="")
        self.cleanup_scroll.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        self.cleanup_scroll.grid_columnconfigure(0, weight=1)

        self.cleanup_hint = ctk.CTkLabel(self.cleanup_scroll,
                                         text="扫描完成后，清理建议将显示在这里。",
                                         font=ctk.CTkFont(size=13),
                                         text_color=COLORS["text_dim"])
        self.cleanup_hint.grid(row=0, column=0, pady=40)

    def _build_tab_detail(self):
        tab = self.tab_detail
        tab.grid_columnconfigure((0, 1), weight=1)
        tab.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(tab, text="AppData\\Local 占用排名",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, pady=5, sticky="w", padx=10)
        ctk.CTkLabel(tab, text="AppData\\Roaming 占用排名",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=1, pady=5, sticky="w", padx=10)

        self.local_scroll = ctk.CTkScrollableFrame(tab)
        self.local_scroll.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        self.local_scroll.grid_columnconfigure(0, weight=1)

        self.roaming_scroll = ctk.CTkScrollableFrame(tab)
        self.roaming_scroll.grid(row=1, column=1, sticky="nsew", padx=5, pady=5)
        self.roaming_scroll.grid_columnconfigure(0, weight=1)

        for scroll in [self.local_scroll, self.roaming_scroll]:
            ctk.CTkLabel(scroll, text="扫描后显示",
                         text_color=COLORS["text_dim"]).grid(row=0, column=0, pady=30)

    def _build_tab_bigfiles(self):
        tab = self.tab_bigfiles
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            tab,
            text="C 盘最大的文件（≥100MB）\n"
                 "pagefile.sys / hiberfil.sys / .vhdx / ISO 往往才是空间真正的大头，"
                 "先看这里再决定要不要清缓存。",
            font=ctk.CTkFont(size=12), justify="left",
            text_color=COLORS["text_dim"]).grid(row=0, column=0, sticky="w", padx=10, pady=8)

        self.bigfiles_scroll = ctk.CTkScrollableFrame(tab, label_text="")
        self.bigfiles_scroll.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        self.bigfiles_scroll.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.bigfiles_scroll, text="扫描完成后显示",
                     text_color=COLORS["text_dim"]).grid(row=0, column=0, pady=30)

    def _build_tab_report(self):
        tab = self.tab_report
        tab.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            tab,
            text=(
                "📄  报告操作说明\n\n"
                "• 「保存分析报告」把完整结果写成 Markdown，可用记事本 / Typora / VS Code 打开\n"
                "• 报告包含：磁盘概况、大文件榜、可执行清理项、只读建议、预期效果\n\n"
                "🗑️  清理说明（重要）\n\n"
                "• 优先级 1/2 的缓存会直接删除；需要备份的项会移动到备份目录，不会立刻消失\n"
                "• 执行前会列出真实路径、文件数、可回收量，确认后才会动手\n"
                "• 受系统保护的目录（WinSxS、Installer、Program Files、文档、桌面）一律拒绝删除\n"
                "• 失败、被占用、权限不足都会如实记录，不会再把失败报成成功\n\n"
                "💾  备份位置\n\n"
                "• 默认 C:\\DiskCleaner_Backup\\<时间戳>\\，保留天数可在「设置」里调整"
            ),
            font=ctk.CTkFont(size=13), justify="left").grid(
            row=0, column=0, padx=20, pady=20, sticky="nw")

        btn_frame = ctk.CTkFrame(tab, fg_color="transparent")
        btn_frame.grid(row=1, column=0, padx=20, pady=10, sticky="w")

        self.save_report_btn = ctk.CTkButton(
            btn_frame, text="💾  保存分析报告", width=170, height=40,
            font=ctk.CTkFont(size=13), command=self._on_save_report,
            state="disabled", fg_color=COLORS["info"])
        self.save_report_btn.pack(side="left", padx=5)

        self.exec_cleanup_btn = ctk.CTkButton(
            btn_frame, text="🗑️  清理优先级 1（预览）", width=210, height=40,
            font=ctk.CTkFont(size=13), command=self._on_exec_cleanup,
            state="disabled", fg_color=COLORS["danger"], hover_color="#c0392b")
        self.exec_cleanup_btn.pack(side="left", padx=5)

        self.exec_selected_btn = ctk.CTkButton(
            btn_frame, text="☑️  清理选中项（预览）", width=200, height=40,
            font=ctk.CTkFont(size=13), command=self._on_exec_selected,
            state="disabled", fg_color=COLORS["warning"], hover_color="#d35400")
        self.exec_selected_btn.pack(side="left", padx=5)

        self.cancel_exec_btn = ctk.CTkButton(
            btn_frame, text="⏹ 停止", width=90, height=40,
            font=ctk.CTkFont(size=13), command=self._on_cancel_exec,
            state="disabled", fg_color="gray40", hover_color="gray50")
        self.cancel_exec_btn.pack(side="left", padx=5)

        self.last_csv_label = ctk.CTkLabel(tab, text="",
                                           font=ctk.CTkFont(size=11),
                                           text_color=COLORS["text_dim"])
        self.last_csv_label.grid(row=2, column=0, padx=20, sticky="w")

    def _build_tab_settings(self):
        tab = self.tab_settings
        tab.grid_columnconfigure(1, weight=1)
        row = 0

        # ── 文件保存路径 ────────────────────────────────────────────
        ctk.CTkLabel(tab, text="📁  文件保存路径",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=row, column=0, columnspan=4, sticky="w", padx=15, pady=(18, 4))
        row += 1
        ctk.CTkFrame(tab, height=1, fg_color=("gray70", "gray35")).grid(
            row=row, column=0, columnspan=4, sticky="ew", padx=15, pady=(0, 10))
        row += 1

        csv_dir = self.config.get("csv_dir", "")
        self.csv_dir_var = ctk.StringVar(value=csv_dir if csv_dir else "（默认：桌面）")
        ctk.CTkLabel(tab, text="扫描CSV保存目录：", width=130, anchor="e").grid(
            row=row, column=0, padx=(15, 5), pady=8, sticky="e")
        ctk.CTkEntry(tab, textvariable=self.csv_dir_var, state="readonly",
                     height=32).grid(row=row, column=1, sticky="ew", padx=5, pady=8)
        ctk.CTkButton(tab, text="浏览", width=65, height=32,
                      command=self._on_browse_csv_dir).grid(row=row, column=2, padx=4)
        ctk.CTkButton(tab, text="恢复桌面", width=80, height=32,
                      fg_color="gray40", hover_color="gray50",
                      command=lambda: self._reset_dir("csv_dir", self.csv_dir_var)).grid(
            row=row, column=3, padx=(2, 15))
        row += 1

        report_dir = self.config.get("report_dir", "")
        self.report_dir_var = ctk.StringVar(value=report_dir if report_dir else "（默认：桌面）")
        ctk.CTkLabel(tab, text="报告输出目录：", width=130, anchor="e").grid(
            row=row, column=0, padx=(15, 5), pady=8, sticky="e")
        ctk.CTkEntry(tab, textvariable=self.report_dir_var, state="readonly",
                     height=32).grid(row=row, column=1, sticky="ew", padx=5, pady=8)
        ctk.CTkButton(tab, text="浏览", width=65, height=32,
                      command=self._on_browse_report_dir).grid(row=row, column=2, padx=4)
        ctk.CTkButton(tab, text="恢复桌面", width=80, height=32,
                      fg_color="gray40", hover_color="gray50",
                      command=lambda: self._reset_dir("report_dir", self.report_dir_var)).grid(
            row=row, column=3, padx=(2, 15))
        row += 1

        ctk.CTkLabel(tab, text="「恢复桌面」= 清除自定义路径，改用桌面作为默认保存位置",
                     font=ctk.CTkFont(size=11),
                     text_color=COLORS["text_dim"]).grid(
            row=row, column=0, columnspan=4, sticky="w", padx=15, pady=(0, 8))
        row += 1

        # ── WizTree ─────────────────────────────────────────────────
        ctk.CTkLabel(tab, text="🔧  WizTree 扫描引擎",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=row, column=0, columnspan=4, sticky="w", padx=15, pady=(18, 4))
        row += 1
        ctk.CTkFrame(tab, height=1, fg_color=("gray70", "gray35")).grid(
            row=row, column=0, columnspan=4, sticky="ew", padx=15, pady=(0, 10))
        row += 1

        info_box = ctk.CTkFrame(tab, fg_color=("gray88", "gray22"), corner_radius=8)
        info_box.grid(row=row, column=0, columnspan=4, sticky="ew", padx=15, pady=(0, 8))
        info_box.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(info_box,
                     text="本软件需要配合 WizTree 使用（免费工具）。如果还没有安装，请前往官网下载：",
                     font=ctk.CTkFont(size=12), justify="left").grid(
            row=0, column=0, sticky="w", padx=15, pady=(12, 4))
        ctk.CTkButton(info_box,
                      text=f"🔗  前往官网下载 WizTree（免费）  →  {WIZTREE_DOWNLOAD_URL}",
                      font=ctk.CTkFont(size=12), fg_color="transparent",
                      text_color=COLORS["info"], hover_color=("gray80", "gray30"), anchor="w",
                      command=lambda: webbrowser.open(WIZTREE_DOWNLOAD_URL)).grid(
            row=1, column=0, sticky="w", padx=10, pady=(0, 12))
        row += 1

        wt_row = ctk.CTkFrame(tab, fg_color="transparent")
        wt_row.grid(row=row, column=0, columnspan=4, sticky="ew", padx=15, pady=4)
        wt_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(wt_row, text="当前WizTree路径：", width=130, anchor="e").grid(
            row=0, column=0, padx=(0, 5))
        ctk.CTkEntry(wt_row, textvariable=self.wiztree_path_var,
                     state="readonly", height=32).grid(row=0, column=1, sticky="ew", padx=5)
        ctk.CTkButton(wt_row, text="更改路径", width=80, height=32,
                      command=self._on_change_wiztree_path,
                      fg_color="gray40", hover_color="gray50").grid(row=0, column=2)
        row += 1

        # ── 清理与备份 ──────────────────────────────────────────────
        ctk.CTkLabel(tab, text="🧹  清理与备份",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=row, column=0, columnspan=4, sticky="w", padx=15, pady=(20, 4))
        row += 1
        ctk.CTkFrame(tab, height=1, fg_color=("gray70", "gray35")).grid(
            row=row, column=0, columnspan=4, sticky="ew", padx=15, pady=(0, 10))
        row += 1

        admin_row = ctk.CTkFrame(tab, fg_color="transparent")
        admin_row.grid(row=row, column=0, columnspan=4, sticky="ew", padx=15, pady=4)
        admin_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(admin_row, text="运行权限：", width=130, anchor="e").grid(
            row=0, column=0, padx=(0, 5))
        self.admin_status_label = ctk.CTkLabel(
            admin_row, text="", anchor="w", font=ctk.CTkFont(size=12))
        self.admin_status_label.grid(row=0, column=1, sticky="w")
        self.admin_btn = ctk.CTkButton(
            admin_row, text="以管理员身份重启", width=130, height=32,
            command=self._on_restart_admin)
        self.admin_btn.grid(row=0, column=2, padx=4)
        row += 1
        self._refresh_admin_status()

        backup_row = ctk.CTkFrame(tab, fg_color="transparent")
        backup_row.grid(row=row, column=0, columnspan=4, sticky="ew", padx=15, pady=4)
        backup_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(backup_row, text="备份目录：", width=130, anchor="e").grid(
            row=0, column=0, padx=(0, 5))
        self.backup_dir_var = ctk.StringVar(
            value=self.config.get("backup_dir") or default_backup_root())
        ctk.CTkEntry(backup_row, textvariable=self.backup_dir_var,
                     state="readonly", height=32).grid(
            row=0, column=1, sticky="ew", padx=5)
        ctk.CTkButton(backup_row, text="浏览", width=65, height=32,
                      command=self._on_browse_backup_dir).grid(row=0, column=2, padx=4)
        ctk.CTkButton(backup_row, text="打开", width=65, height=32,
                      fg_color="gray40", hover_color="gray50",
                      command=lambda: _open_folder(self.backup_dir_var.get())).grid(
            row=0, column=3, padx=(2, 15))
        row += 1

        retention_row = ctk.CTkFrame(tab, fg_color="transparent")
        retention_row.grid(row=row, column=0, columnspan=4, sticky="ew", padx=15, pady=4)
        ctk.CTkLabel(retention_row, text="备份保留：", width=130, anchor="e").grid(
            row=0, column=0, padx=(0, 5))
        self.retention_menu = ctk.CTkOptionMenu(
            retention_row, values=["3 天", "7 天", "14 天", "30 天"],
            width=110, command=self._on_retention_change)
        self.retention_menu.grid(row=0, column=1, sticky="w")
        self.retention_menu.set("%d 天" % self.config.get("backup_retention_days", 7))
        ctk.CTkButton(retention_row, text="立即清理过期备份", width=150, height=32,
                      fg_color="gray40", hover_color="gray50",
                      command=self._on_prune_backups).grid(row=0, column=2, padx=4)
        row += 1
        ctk.CTkLabel(tab, text="备份目录用于存放「建议备份」类项目；"
                              "超过保留天数后会在启动时自动清理。",
                     font=ctk.CTkFont(size=11),
                     text_color=COLORS["text_dim"]).grid(
            row=row, column=0, columnspan=4, sticky="w", padx=15, pady=(0, 8))
        row += 1

        # ── 关于 / 知识库 ────────────────────────────────────────────
        ctk.CTkLabel(tab, text="ℹ️  关于本软件",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=row, column=0, columnspan=4, sticky="w", padx=15, pady=(20, 4))
        row += 1
        ctk.CTkFrame(tab, height=1, fg_color=("gray70", "gray35")).grid(
            row=row, column=0, columnspan=4, sticky="ew", padx=15, pady=(0, 10))
        row += 1

        about_box = ctk.CTkFrame(tab, fg_color=("gray88", "gray22"), corner_radius=8)
        about_box.grid(row=row, column=0, columnspan=4, sticky="ew", padx=15, pady=(0, 15))
        ctk.CTkLabel(about_box,
                     text=(
                         f"磁盘清理助手  v3.1          知识库更新日期：{KNOWLEDGE_DATE}\n\n"
                         f"v3.1 主要改动：\n"
                         f"  • 新增安全删除引擎（cleaner.py）：保护目录、junction 检测、真实释放量\n"
                         f"  • 重写全部规则，修掉会误删 Firefox 配置 / KOOK / Razer / 剪映草稿的宽匹配\n"
                         f"  • 系统组件（WinSxS、Installer、WindowsApps）改为「只建议」\n"
                         f"  • 新增：Windows\\Temp、传递优化、CBS 日志、崩溃转储、大文件榜\n"
                         f"  • CSV 流式解析，整盘导出不再吃满内存\n"
                         f"  • 扫描改用「CSV 写完」判定，支持管理员模式\n\n"
                         f"知识来源：{KNOWLEDGE_SOURCES}"
                     ),
                     font=ctk.CTkFont(size=11), justify="left", anchor="w").pack(
            padx=15, pady=12, anchor="w")

    def _build_statusbar(self):
        statusbar = ctk.CTkFrame(self, height=25, corner_radius=0,
                                 fg_color=("gray85", "gray15"))
        statusbar.grid(row=3, column=0, sticky="ew")
        statusbar.grid_propagate(False)
        self.status_label = ctk.CTkLabel(statusbar, text="就绪",
                                         font=ctk.CTkFont(size=11),
                                         text_color=COLORS["text_dim"])
        self.status_label.pack(side="left", padx=10)

    # ── 事件处理 ────────────────────────────────────────────────────

    def _show_wiztree_dialog(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("首次设置 - WizTree")
        dialog.geometry("500x300")
        dialog.resizable(False, False)
        dialog.grab_set()
        dialog.lift()

        ctk.CTkLabel(dialog, text="欢迎使用磁盘清理助手 v3.0",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(22, 6))
        ctk.CTkLabel(dialog,
                     text="本软件需要配合 WizTree 运行磁盘扫描。\n"
                          "请先确认已安装 WizTree，再点击「选择路径」。",
                     justify="center", font=ctk.CTkFont(size=12)).pack(pady=(0, 10))

        download_frame = ctk.CTkFrame(dialog, fg_color=("gray85", "gray25"), corner_radius=8)
        download_frame.pack(fill="x", padx=25, pady=4)
        ctk.CTkLabel(download_frame,
                     text="尚未安装 WizTree？点击下方按钮前往官网免费下载：",
                     font=ctk.CTkFont(size=11)).pack(pady=(10, 3))
        ctk.CTkButton(download_frame,
                      text=f"🔗  {WIZTREE_DOWNLOAD_URL}",
                      font=ctk.CTkFont(size=11), fg_color="transparent",
                      text_color=COLORS["info"], hover_color=("gray78", "gray32"),
                      command=lambda: webbrowser.open(WIZTREE_DOWNLOAD_URL)).pack(pady=(0, 10))

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(pady=14)

        def select_and_close():
            dialog.destroy()
            self._on_change_wiztree_path()

        ctk.CTkButton(btn_frame, text="✅  选择 WizTree 路径", width=170, height=38,
                      command=select_and_close).pack(side="left", padx=10)
        ctk.CTkButton(btn_frame, text="稍后设置", width=100, height=38,
                      fg_color="gray40", hover_color="gray50",
                      command=dialog.destroy).pack(side="left", padx=10)

    def _on_change_wiztree_path(self):
        path = filedialog.askopenfilename(
            title="选择 WizTree64.exe",
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")],
            initialdir=self.config.get("wiztree_path", "D:\\") or "D:\\")
        if path:
            self.config["wiztree_path"] = path
            save_config(self.config)
            self.wiztree_path_var.set(path)
            self._set_status(f"WizTree路径已更新：{path}")

    def _on_browse_csv_dir(self):
        path = filedialog.askdirectory(
            title="选择 CSV 扫描文件保存目录",
            initialdir=self.config.get("csv_dir", "") or get_desktop_path())
        if path:
            self.config["csv_dir"] = path
            save_config(self.config)
            self.csv_dir_var.set(path)
            self._set_status(f"CSV保存目录已更新：{path}")

    def _on_browse_report_dir(self):
        path = filedialog.askdirectory(
            title="选择报告输出目录",
            initialdir=self.config.get("report_dir", "") or get_desktop_path())
        if path:
            self.config["report_dir"] = path
            save_config(self.config)
            self.report_dir_var.set(path)
            self._set_status(f"报告输出目录已更新：{path}")

    def _reset_dir(self, key: str, var: ctk.StringVar):
        self.config[key] = ""
        save_config(self.config)
        var.set("（默认：桌面）")
        self._set_status(f"{key} 已重置为桌面")

    # ── 备份 / 管理员相关设置 ───────────────────────────────────────

    def _on_browse_backup_dir(self):
        path = filedialog.askdirectory(
            title="选择备份目录",
            initialdir=self.backup_dir_var.get() or get_desktop_path())
        if path:
            self.config["backup_dir"] = path
            save_config(self.config)
            self.backup_dir_var.set(path)
            self._set_status("备份目录已更新：%s" % path)

    def _on_retention_change(self, value: str):
        try:
            days = int(str(value).split()[0])
        except (TypeError, ValueError):
            return
        self.config["backup_retention_days"] = days
        save_config(self.config)
        self._set_status("备份保留期已设为 %d 天" % days)

    def _on_prune_backups(self):
        root = self.config.get("backup_dir") or default_backup_root()
        days = int(self.config.get("backup_retention_days", 7))
        try:
            removed = prune_backups(root, days)
        except OSError as e:
            messagebox.showerror("清理失败", str(e))
            return
        if removed:
            messagebox.showinfo("清理完成", "已删除 %d 个过期备份批次。" % len(removed))
        else:
            messagebox.showinfo("无需清理", "没有超过 %d 天的备份。" % days)

    def _refresh_admin_status(self):
        if is_elevated():
            self.admin_status_label.configure(
                text="管理员（MFT 快速扫描，可处理系统目录）",
                text_color=COLORS["success"])
            self.admin_btn.configure(state="disabled")
        else:
            self.admin_status_label.configure(
                text="普通用户（系统目录会被跳过，扫描较慢）",
                text_color=COLORS["warning"])
            self.admin_btn.configure(state="normal")

    def _on_restart_admin(self):
        if is_elevated():
            messagebox.showinfo("已经是管理员", "当前进程已具备管理员权限。")
            return
        if not messagebox.askyesno(
                "以管理员身份重启",
                "重启后会用管理员权限重新打开本程序，未保存的扫描结果会丢失。\n\n继续？"):
            return
        if restart_as_admin():
            self.destroy()
        else:
            messagebox.showwarning("未重启", "UAC 被取消，或启动失败。")

    def _prune_old_backups(self):
        root = self.config.get("backup_dir") or default_backup_root()
        try:
            removed = prune_backups(root, int(self.config.get("backup_retention_days", 7)))
        except OSError as e:
            self._set_status("过期备份清理未完成：%s" % e)
            return
        if removed:
            self._set_status("已自动清理 %d 个过期备份" % len(removed))

    def _toggle_theme(self):
        if self.theme_switch.get():
            ctk.set_appearance_mode("dark")
            self.theme_switch.configure(text="深色")
            self.config["theme"] = "dark"
        else:
            ctk.set_appearance_mode("light")
            self.theme_switch.configure(text="浅色")
            self.config["theme"] = "light"
        save_config(self.config)

    def _on_scan_click(self):
        wiztree_path = find_wiztree(self.config.get("wiztree_path", ""))
        if not wiztree_path:
            self._show_wiztree_dialog()
            return
        if not is_elevated():
            self._set_status("普通权限扫描（如需更完整结果，可在设置里以管理员身份重启）")
        self.scan_btn.configure(state="disabled", text="扫描中...")
        self.progress_bar.set(0)
        self._set_progress("正在准备扫描...", 0.05)
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        export_dir = self.config.get("csv_dir", "") or get_desktop_path()
        export_path = build_export_path(export_dir)

        self.after(0, lambda: self._set_progress("正在启动 WizTree 扫描...", 0.1))
        timeout = int(self.config.get("scan_timeout_sec", 900))
        export_files = bool(self.config.get("export_files", True))
        admin_mode = self.config.get("admin_scan", "auto") == "always"
        use_admin = is_elevated() and (admin_mode or self.config.get("admin_scan", "auto") == "auto")

        success, result = run_wiztree_scan(
            self.config.get("wiztree_path", ""), export_path,
            lambda msg: self.after(0, lambda: self._set_progress(msg, 0.35)),
            timeout=timeout, export_files=export_files, use_admin=use_admin)

        if not success:
            self.after(0, lambda: self._on_scan_error(result))
            return

        csv_path = result
        self.config["last_csv_file"] = csv_path
        save_config(self.config)

        self.after(0, lambda: self._set_progress("正在分析数据...", 0.7))
        analysis = run_analysis(csv_path)

        if analysis.get("error"):
            self.after(0, lambda: self._on_scan_error(analysis["error"]))
            return

        self.after(0, lambda: self._on_scan_complete(analysis, csv_path))

    def _on_scan_error(self, error_msg: str):
        self.scan_btn.configure(state="normal", text="🚀  开始扫描 C盘")
        self.progress_bar.set(0)
        self._set_status("扫描失败")
        messagebox.showerror("扫描失败", f"扫描过程中发生错误：\n\n{error_msg}")

    def _on_scan_complete(self, analysis: dict, csv_path: str):
        self.analysis_result = analysis
        self.guide_items = analysis.get("guide_items", [])
        self.big_files = analysis.get("big_files", [])
        self.session_csv_path = csv_path  # 记录本次会话的 CSV，关闭时询问

        self._set_progress("分析完成！", 1.0)
        self._set_status("分析完成 | CSV：%s" % os.path.basename(csv_path))

        self._update_tab_overview(analysis)
        self._update_tab_cleanup(analysis)
        self._update_tab_detail(analysis)
        self._update_tab_bigfiles()

        self.save_report_btn.configure(state="normal")
        self.exec_cleanup_btn.configure(state="normal")
        self._on_checkbox_change()
        self.scan_btn.configure(state="normal", text="🚀  开始扫描 C盘")
        self.last_csv_label.configure(text="最近扫描文件：%s" % csv_path)
        self.tabview.set("🧹 清理建议")

    # ── 界面更新 ────────────────────────────────────────────────────

    def _update_tab_overview(self, analysis: dict):
        disk_info = analysis.get("disk_info")
        if not disk_info:
            return

        for w in self.overview_cards.winfo_children():
            w.destroy()
        self.overview_hint.grid_forget()

        pct = disk_info.used_percent
        bar_color = (COLORS["danger"] if pct >= 80 else
                     COLORS["warning"] if pct >= 60 else COLORS["success"])

        self.overview_cards.grid_columnconfigure((0, 1, 2, 3, 4), weight=1)

        def make_card(parent, title, value, subtitle="", color=None):
            card = ctk.CTkFrame(parent, corner_radius=10)
            card.pack(side="left", padx=8, pady=8, expand=True, fill="both")
            ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=11),
                         text_color=COLORS["text_dim"]).pack(pady=(10, 0))
            ctk.CTkLabel(card, text=value,
                         font=ctk.CTkFont(size=22, weight="bold"),
                         text_color=color or "white").pack()
            if subtitle:
                ctk.CTkLabel(card, text=subtitle, font=ctk.CTkFont(size=10),
                             text_color=COLORS["text_dim"]).pack(pady=(0, 8))
            return card

        make_card(self.overview_cards, "总容量", f"{disk_info.total_gb} GB")
        make_card(self.overview_cards, "已用空间", f"{disk_info.used_gb} GB", color=bar_color)
        make_card(self.overview_cards, "剩余空间", f"{disk_info.free_gb} GB", color=COLORS["success"])
        make_card(self.overview_cards, "使用率", f"{disk_info.used_percent}%",
                  subtitle="建议低于75%", color=bar_color)
        reclaimable = analysis.get("total_reclaimable_bytes", 0)
        make_card(self.overview_cards, "可回收空间", bytes_to_human(reclaimable),
                  subtitle="优先级1+2合计", color=COLORS["info"])

        # ── 磁盘空间缩略图 ──────────────────────────────────────────
        for w in self.treemap_frame.winfo_children():
            w.destroy()
        self.treemap_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))

        top_level1 = analysis.get("top_level1", [])

        header = ctk.CTkFrame(self.treemap_frame, fg_color="transparent")
        header.pack(fill="x", padx=2)
        ctk.CTkLabel(header, text="💾  磁盘空间缩略图（点击色块可打开对应文件夹）",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")

        # Canvas 缩略图
        is_dark = ctk.get_appearance_mode().lower() == "dark"
        canvas_bg = "#1e1e2e" if is_dark else "#e8e8e8"
        self._treemap_canvas = tk.Canvas(
            self.treemap_frame, height=80, bg=canvas_bg,
            highlightthickness=1,
            highlightbackground="#444" if is_dark else "#ccc")
        self._treemap_canvas.pack(fill="x", padx=2, pady=(4, 2))

        # 图例行
        legend_frame = ctk.CTkFrame(self.treemap_frame, fg_color="transparent")
        legend_frame.pack(fill="x", padx=2, pady=(2, 4))

        for i, folder in enumerate(top_level1[:12]):
            color = TREEMAP_COLORS[i % len(TREEMAP_COLORS)]
            name = folder.path.rstrip("\\").split("\\")[-1][:10]
            dot = tk.Label(legend_frame, text="■", fg=color,
                           bg=canvas_bg if is_dark else "#f0f0f0",
                           font=("Arial", 10))
            dot.pack(side="left", padx=(6, 1))
            lbl = ctk.CTkLabel(legend_frame,
                               text=f"{name} {bytes_to_human(folder.size_bytes)}",
                               font=ctk.CTkFont(size=10))
            lbl.pack(side="left", padx=(0, 4))

        # 绑定 Configure 事件（窗口大小改变时重绘）
        self._treemap_canvas.bind(
            "<Configure>",
            lambda e: self._draw_treemap(self._treemap_canvas, top_level1, disk_info))
        self.after(80, lambda: self._draw_treemap(self._treemap_canvas, top_level1, disk_info))

        # ── 顶级目录表格 ────────────────────────────────────────────
        # 每次扫描重建前先销毁旧表，避免控件泄漏
        if self._overview_table is not None:
            try:
                self._overview_table.destroy()
            except Exception:
                pass
        table_frame = ctk.CTkScrollableFrame(
            self.tab_overview,
            label_text="C:\\ 下一级目录 Top %d（点击📂可打开文件夹）" % len(top_level1))
        self._overview_table = table_frame
        table_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
        self.tab_overview.grid_rowconfigure(2, weight=1)
        table_frame.grid_columnconfigure(1, weight=1)

        for col, (text, w) in enumerate([("排名", 40), ("路径", 0), ("大小", 80), ("占比", 60), ("", 38)]):
            ctk.CTkLabel(table_frame, text=text, width=w,
                         font=ctk.CTkFont(weight="bold")).grid(
                row=0, column=col, padx=5, pady=3, sticky="w" if col == 1 else "")

        for i, folder in enumerate(top_level1, 1):
            bg = ("gray90", "gray20") if i % 2 == 0 else ("gray85", "gray25")
            ctk.CTkLabel(table_frame, text=str(i), width=40,
                         fg_color=bg, corner_radius=3).grid(
                row=i, column=0, padx=5, pady=2, sticky="ew")
            ctk.CTkLabel(table_frame, text=folder.path,
                         fg_color=bg, corner_radius=3, anchor="w").grid(
                row=i, column=1, padx=5, pady=2, sticky="ew")
            ctk.CTkLabel(table_frame, text=bytes_to_human(folder.size_bytes),
                         width=80, fg_color=bg, corner_radius=3).grid(
                row=i, column=2, padx=5, pady=2)
            pct_str = f"{folder.size_bytes / disk_info.total_bytes * 100:.1f}%"
            ctk.CTkLabel(table_frame, text=pct_str,
                         width=60, fg_color=bg, corner_radius=3).grid(
                row=i, column=3, padx=5, pady=2)
            ctk.CTkButton(table_frame, text="📂", width=36, height=26,
                          fg_color="transparent", hover_color=("gray80", "gray30"),
                          command=lambda p=folder.path: _open_folder(p)).grid(
                row=i, column=4, padx=5, pady=2)

    def _draw_treemap(self, canvas, folders, disk_info):
        """在 Canvas 上绘制磁盘空间比例缩略图"""
        canvas.update_idletasks()
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 10 or h < 10:
            return

        canvas.delete("all")
        total = disk_info.total_bytes
        if total == 0:
            return

        x = 0
        tooltip_var = [None]  # 用于 hover 提示

        for i, folder in enumerate(folders):
            if folder.size_bytes <= 0:
                continue
            ratio = folder.size_bytes / total
            fw = max(int(w * ratio), 3)
            color = TREEMAP_COLORS[i % len(TREEMAP_COLORS)]

            rect = canvas.create_rectangle(x, 2, x + fw, h - 2,
                                           fill=color, outline="#000", width=1)
            short = folder.path.rstrip("\\").split("\\")[-1]
            size_str = bytes_to_human(folder.size_bytes)

            if fw >= 90:
                canvas.create_text(x + fw / 2, h / 2 - 10,
                                   text=short[:14], fill="white",
                                   font=("Arial", 8, "bold"), anchor="center")
                canvas.create_text(x + fw / 2, h / 2 + 8,
                                   text=size_str, fill="white",
                                   font=("Arial", 8), anchor="center")
            elif fw >= 45:
                canvas.create_text(x + fw / 2, h / 2,
                                   text=size_str, fill="white",
                                   font=("Arial", 7), anchor="center")

            path = folder.path
            canvas.tag_bind(rect, "<Button-1>", lambda e, p=path: _open_folder(p))
            canvas.tag_bind(rect, "<Enter>",
                            lambda e, p=path, s=size_str: (
                                canvas.configure(cursor="hand2"),
                                self._set_status(f"📂 {p}  —  {s}  （点击打开文件夹）")))
            canvas.tag_bind(rect, "<Leave>",
                            lambda e: (canvas.configure(cursor=""), self._set_status("就绪")))
            x += fw

        # 剩余空间（绿色）
        if x < w:
            free_w = w - x
            canvas.create_rectangle(x, 2, w, h - 2, fill="#27ae60", outline="#000", width=1)
            if free_w >= 70:
                canvas.create_text(x + free_w / 2, h / 2,
                                   text=f"空闲  {bytes_to_human(disk_info.free_bytes)}",
                                   fill="white", font=("Arial", 9, "bold"), anchor="center")

    def _update_tab_cleanup(self, analysis: dict):
        self._cleanup_checkboxes = {}
        cleanup_items = analysis.get("cleanup_items", [])
        guide_items = analysis.get("guide_items", [])
        reclaimable = analysis.get("total_reclaimable_bytes", 0)
        backup_bytes = analysis.get("backup_bytes", 0)
        p1_count = sum(1 for x in cleanup_items
                       if x.priority == 1 and x.action == "delete")
        admin_count = sum(1 for x in cleanup_items if x.requires_admin)

        summary = ("可执行 %d 项 | 立即清理 %d 项 | 可直接回收 %s"
                   % (len(cleanup_items), p1_count, bytes_to_human(reclaimable)))
        if backup_bytes:
            summary += " | 需备份 %s" % bytes_to_human(backup_bytes)
        if admin_count:
            summary += " | %d 项需管理员" % admin_count
        summary += " | 只建议 %d 项" % len(guide_items)
        self.cleanup_summary.configure(text=summary, text_color="white")

        self.cleanup_hint.grid_forget()
        for widget in self.cleanup_scroll.winfo_children():
            widget.destroy()

        if not cleanup_items and not guide_items:
            ctk.CTkLabel(self.cleanup_scroll, text="未发现明显的可清理项目。",
                         text_color=COLORS["success"]).grid(row=0, column=0, pady=30)
            return

        current_priority = 0
        row = 0
        for item in cleanup_items:
            if item.priority != current_priority:
                current_priority = item.priority
                color = PRIORITY_COLORS.get(current_priority, "gray")
                ctk.CTkLabel(self.cleanup_scroll,
                             text="  %s" % PRIORITY_LABELS.get(current_priority, ""),
                             font=ctk.CTkFont(size=14, weight="bold"),
                             text_color=color, anchor="w").grid(
                    row=row, column=0, sticky="ew", padx=5, pady=(12, 3))
                row += 1
            card = self._make_cleanup_card(self.cleanup_scroll, item)
            card.grid(row=row, column=0, sticky="ew", padx=5, pady=3)
            row += 1

        if guide_items:
            ctk.CTkLabel(self.cleanup_scroll,
                         text="  🟢 只建议（系统组件 / 真实数据，不会自动删除）",
                         font=ctk.CTkFont(size=14, weight="bold"),
                         text_color=COLORS["success"], anchor="w").grid(
                row=row, column=0, sticky="ew", padx=5, pady=(18, 3))
            row += 1
            for item in guide_items:
                card = self._make_cleanup_card(self.cleanup_scroll, item)
                card.grid(row=row, column=0, sticky="ew", padx=5, pady=3)
                row += 1

        self._on_checkbox_change()

    def _make_cleanup_card(self, parent, item: CleanupItem) -> ctk.CTkFrame:
        color = PRIORITY_COLORS.get(item.priority, "gray")
        card = ctk.CTkFrame(parent, corner_radius=8, border_width=1, border_color=color)
        card.grid_columnconfigure(2, weight=1)

        # 勾选框（只建议项不可勾选）
        if item.action == "guide":
            ctk.CTkLabel(card, text="只\n读", width=26,
                         font=ctk.CTkFont(size=10),
                         text_color=COLORS["text_dim"]).grid(
                row=0, column=0, rowspan=5, padx=(8, 0), pady=5)
        else:
            var = ctk.BooleanVar(value=False)
            self._cleanup_checkboxes[item.path] = var
            cb = ctk.CTkCheckBox(card, variable=var, text="", width=26, height=26,
                                 checkbox_width=20, checkbox_height=20,
                                 command=self._on_checkbox_change)
            cb.grid(row=0, column=0, rowspan=5, padx=(8, 0), pady=5)

        # 左侧彩色竖条
        ctk.CTkFrame(card, width=5, fg_color=color, corner_radius=4).grid(
            row=0, column=1, rowspan=5, sticky="ns", padx=(5, 8), pady=5)

        # 名称 + 大小
        name_frame = ctk.CTkFrame(card, fg_color="transparent")
        name_frame.grid(row=0, column=2, sticky="ew", pady=(8, 2))
        name_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(name_frame, text=item.name,
                     font=ctk.CTkFont(size=13, weight="bold"), anchor="w").grid(
            row=0, column=0, sticky="w")
        ctk.CTkLabel(name_frame, text=bytes_to_human(item.size_bytes),
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=color).grid(row=0, column=1, padx=10)

        # 路径 + 打开按钮（可点击访问）
        path_row = ctk.CTkFrame(card, fg_color="transparent")
        path_row.grid(row=1, column=2, sticky="ew", pady=2)
        path_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(path_row, text=f"路径：{item.path}",
                     font=ctk.CTkFont(size=11), text_color=COLORS["text_dim"],
                     anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkButton(path_row, text="📂 打开", width=72, height=24,
                      font=ctk.CTkFont(size=11),
                      fg_color=("gray80", "gray30"), hover_color=("gray70", "gray40"),
                      text_color=("gray20", "white"),
                      command=lambda p=item.path: _open_folder(p)).grid(
            row=0, column=1, padx=(6, 8))

        # 风险 + 操作方法 + 额外标记
        badges = []
        if item.action == "backup":
            badges.append("💾 移入备份")
        if item.action == "guide":
            badges.append("🟢 只建议")
        if item.requires_admin:
            badges.append("🔒 需管理员")
        if item.count_files:
            badges.append("📄 %d 个文件" % item.count_files)
        prefix = (" ｜ ".join(badges) + " ｜ ") if badges else ""
        ctk.CTkLabel(card, text="%s风险：%s  ｜  操作：%s" % (prefix, item.risk, item.method),
                     font=ctk.CTkFont(size=11), anchor="w",
                     wraplength=720, justify="left").grid(
            row=2, column=2, sticky="ew", pady=2)

        # 折叠的判断依据
        reason_text = ctk.CTkLabel(
            card, text=item.reason, font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"], wraplength=700, justify="left", anchor="w")
        expanded = [False]
        btn_holder = [None]

        def toggle():
            expanded[0] = not expanded[0]
            if expanded[0]:
                reason_text.grid(row=4, column=2, sticky="ew", padx=5, pady=(0, 8))
                btn_holder[0].configure(text="▼ 收起判断依据")
            else:
                reason_text.grid_forget()
                btn_holder[0].configure(text="▶ 查看判断依据")

        toggle_btn = ctk.CTkButton(
            card, text="▶ 查看判断依据", height=22, width=130,
            font=ctk.CTkFont(size=11), fg_color="transparent",
            text_color=COLORS["info"], hover_color=("gray85", "gray25"),
            command=toggle, anchor="w")
        toggle_btn.grid(row=3, column=2, sticky="w", pady=(0, 5))
        btn_holder[0] = toggle_btn
        return card

    def _update_tab_detail(self, analysis: dict):
        def fill_scroll(scroll_frame, folders, max_n=15):
            for w in scroll_frame.winfo_children():
                w.destroy()
            scroll_frame.grid_columnconfigure(0, weight=1)
            for i, folder in enumerate(folders[:max_n]):
                bg = ("gray90", "gray20") if i % 2 == 0 else ("gray85", "gray25")
                short = folder.path.rstrip("\\").split("\\")[-1]
                row_f = ctk.CTkFrame(scroll_frame, fg_color=bg, corner_radius=3)
                row_f.grid(row=i, column=0, sticky="ew", padx=3, pady=2)
                row_f.grid_columnconfigure(0, weight=1)
                ctk.CTkLabel(row_f, text=short, anchor="w",
                             font=ctk.CTkFont(size=11)).grid(row=0, column=0, sticky="w", padx=6)
                ctk.CTkLabel(row_f, text=bytes_to_human(folder.size_bytes),
                             width=70, font=ctk.CTkFont(size=11)).grid(row=0, column=1, padx=4)
                ctk.CTkButton(row_f, text="📂", width=32, height=24,
                              fg_color="transparent", hover_color=("gray75", "gray35"),
                              command=lambda p=folder.path: _open_folder(p)).grid(
                    row=0, column=2, padx=(2, 4))

        fill_scroll(self.local_scroll, analysis.get("top_appdata_local", []))
        fill_scroll(self.roaming_scroll, analysis.get("top_appdata_roaming", []))

    def _update_tab_bigfiles(self):
        for w in self.bigfiles_scroll.winfo_children():
            w.destroy()
        self.bigfiles_scroll.grid_columnconfigure(1, weight=1)

        if not self.big_files:
            ctk.CTkLabel(self.bigfiles_scroll,
                         text="没有发现 ≥100MB 的文件（或扫描时关闭了文件导出）。",
                         text_color=COLORS["text_dim"],
                         font=ctk.CTkFont(size=12)).grid(row=0, column=0, pady=30)
            return

        for col, text in enumerate(["#", "文件", "大小", "", ""]):
            ctk.CTkLabel(self.bigfiles_scroll, text=text,
                         font=ctk.CTkFont(weight="bold")).grid(
                row=0, column=col, padx=6, pady=3,
                sticky="w" if col == 1 else "")

        for i, entry in enumerate(self.big_files, 1):
            bg = ("gray90", "gray20") if i % 2 == 0 else ("gray85", "gray25")
            ctk.CTkLabel(self.bigfiles_scroll, text=str(i), width=28,
                         fg_color=bg, corner_radius=3).grid(
                row=i, column=0, padx=4, pady=2, sticky="ew")
            ctk.CTkLabel(self.bigfiles_scroll, text=entry.path, anchor="w",
                         fg_color=bg, corner_radius=3,
                         font=ctk.CTkFont(size=11)).grid(
                row=i, column=1, padx=4, pady=2, sticky="ew")
            ctk.CTkLabel(self.bigfiles_scroll, text=bytes_to_human(entry.size_bytes),
                         width=80, fg_color=bg, corner_radius=3).grid(
                row=i, column=2, padx=4, pady=2)
            hint = _big_file_hint(entry.path)
            ctk.CTkLabel(self.bigfiles_scroll, text=hint, width=210,
                         fg_color=bg, corner_radius=3,
                         font=ctk.CTkFont(size=10),
                         text_color=COLORS["text_dim"], anchor="w").grid(
                row=i, column=3, padx=4, pady=2, sticky="ew")
            ctk.CTkButton(self.bigfiles_scroll, text="📂", width=34, height=24,
                          fg_color="transparent", hover_color=("gray80", "gray30"),
                          command=lambda p=entry.path: _open_folder(p)).grid(
                row=i, column=4, padx=4, pady=2)

    # ── 报告和清理 ──────────────────────────────────────────────────

    def _on_save_report(self):
        if not self.analysis_result:
            messagebox.showwarning("无数据", "请先执行扫描。")
            return
        output_dir = self.config.get("report_dir", "") or get_desktop_path()
        success, result = generate_report(
            self.analysis_result, output_dir,
            meta={"admin": is_elevated(),
                  "backup_retention": self.config.get("backup_retention_days", 7),
                  "csv_path": self.session_csv_path or ""})
        if success:
            messagebox.showinfo("报告已保存",
                                f"报告已保存到：\n{result}\n\n可用记事本或Typora打开查看。")
            self._set_status(f"报告已保存：{os.path.basename(result)}")
        else:
            messagebox.showerror("保存失败", result)

    def _on_exec_cleanup(self):
        if not self.analysis_result:
            return
        items = [x for x in self.analysis_result.get("cleanup_items", [])
                 if x.priority == 1 and x.action == "delete"]
        if not items:
            messagebox.showinfo("无可清理项", "没有找到「立即清理」级别的可删除项目。")
            return
        self._start_execution(items, "优先级 1")

    def _on_exec_selected(self):
        if not self.analysis_result:
            return
        selected = [item for item in self.analysis_result.get("cleanup_items", [])
                    if self._cleanup_checkboxes.get(item.path) is not None
                    and self._cleanup_checkboxes[item.path].get()]
        if not selected:
            messagebox.showinfo("未选择", "请先勾选要处理的项目。")
            return
        self._start_execution(selected, "选中项")

    # ── 勾选框 ──────────────────────────────────────────────────────

    def _on_checkbox_change(self):
        any_checked = any(v.get() for v in self._cleanup_checkboxes.values())
        if not self._exec_running:
            self.exec_selected_btn.configure(
                state="normal" if any_checked else "disabled")

    # ── 执行清理（后台线程 + 真实结果）──────────────────────────────

    def _build_preview(self, items):
        lines = []
        for item in items[:10]:
            tags = []
            if item.action == "backup":
                tags.append("备份")
            if item.requires_admin:
                tags.append("需管理员")
            mark = ("[%s] " % "/".join(tags)) if tags else ""
            lines.append("  • %s%s（%s）\n    %s" % (
                mark, item.name, bytes_to_human(item.size_bytes), item.path))
        if len(items) > 10:
            lines.append("  … 以及另外 %d 项" % (len(items) - 10))
        return "\n".join(lines)

    def _start_execution(self, items, title):
        if self._exec_running:
            messagebox.showinfo("请稍候", "正在执行上一次清理，请等待完成。")
            return

        total = sum(x.size_bytes for x in items)
        backup_total = sum(x.size_bytes for x in items if x.action == "backup")
        note = ""
        if backup_total:
            note = ("\n\n其中约 %s 会移入备份目录，不会立刻释放空间。"
                    % bytes_to_human(backup_total))
        if any(x.requires_admin for x in items) and not is_elevated():
            note += "\n\n有项目需要管理员权限，当前是普通权限，这些项会被跳过或失败。"

        if not messagebox.askyesno(
                "确认执行（%s）" % title,
                "即将处理 %d 项，共 %s：\n\n%s%s\n\n确认继续？"
                % (len(items), bytes_to_human(total), self._build_preview(items), note)):
            return

        self._exec_running = True
        self._exec_cancel.clear()
        self.exec_cleanup_btn.configure(state="disabled")
        self.exec_selected_btn.configure(state="disabled")
        self.scan_btn.configure(state="disabled")
        self.cancel_exec_btn.configure(state="normal")
        self._set_progress("开始清理...", 0.0)
        self._set_status("正在清理...")

        threading.Thread(target=self._exec_worker, args=(items,), daemon=True).start()
        self.after(100, self._poll_exec_queue)

    def _exec_worker(self, items):
        try:
            results = execute_items(
                items,
                backup_root=self.config.get("backup_dir") or None,
                progress=lambda done, total, name: self._exec_queue.put(
                    ("progress", done, total, name)),
                cancel=self._exec_cancel,
                retention_days=int(self.config.get("backup_retention_days", 7)))
        except Exception as e:  # 后台线程不能把异常抛回 Tk
            results = []
            self._exec_queue.put(("error", str(e)))
        self._exec_queue.put(("done", results))

    def _poll_exec_queue(self):
        finished = False
        while True:
            try:
                msg = self._exec_queue.get_nowait()
            except queue.Empty:
                break
            if msg[0] == "progress":
                _kind, done_n, total_n, name = msg
                value = (done_n / total_n) if total_n else 0
                self._set_progress("清理中：%s（%d/%d）" % (name, done_n, total_n), value)
            elif msg[0] == "error":
                self._set_status("清理异常：%s" % msg[1])
            elif msg[0] == "done":
                finished = True
                self._finish_execution(msg[1])
        if not finished and self._exec_running:
            self.after(150, self._poll_exec_queue)

    def _on_cancel_exec(self):
        if self._exec_running:
            self._exec_cancel.set()
            self._set_status("正在停止（当前项完成后停止）...")

    def _finish_execution(self, results):
        self._exec_running = False
        self.exec_cleanup_btn.configure(state="normal")
        self.exec_selected_btn.configure(state="normal")
        self.scan_btn.configure(state="normal", text="🚀  开始扫描 C盘")
        self.cancel_exec_btn.configure(state="disabled")
        self._set_progress("清理结束", 1.0)

        if not results:
            self._set_status("没有执行任何清理")
            return

        freed = sum(r.bytes_freed for r in results)
        moved = sum(r.bytes_moved for r in results)
        deleted = [r for r in results if r.status == "deleted"]
        partial = [r for r in results if r.status == "partial"]
        moved_items = [r for r in results if r.status == "moved"]
        skipped = [r for r in results if r.status in ("skipped", "denied", "missing")]
        failed = [r for r in results if r.status == "failed"]

        self._save_history_record(results)
        self._update_tab_history()

        # 已处理的项不应再保持勾选
        for var in self._cleanup_checkboxes.values():
            try:
                var.set(False)
            except Exception:
                pass
        self._on_checkbox_change()

        lines = ["实际释放：%s" % bytes_to_human(freed)]
        if moved:
            lines.append("移入备份：%s（%d 项）" % (bytes_to_human(moved), len(moved_items)))
        lines.append("成功删除：%d 项" % len(deleted))
        if partial:
            lines.append("部分完成：%d 项（已删除 %d 个文件，失败操作 %d 次）" % (
                len(partial), sum(r.files for r in partial),
                sum(r.failed_operations for r in partial)))
        if skipped:
            lines.append("跳过/拒绝：%d 项" % len(skipped))
        if failed:
            lines.append("失败：%d 项" % len(failed))

        detail = []
        for r in results:
            if r.status in ("partial", "failed", "denied", "skipped") and r.errors:
                detail.append("· %s：%s（%s）" % (r.name, r.status_label, r.errors[0]))
        if detail:
            lines.append("")
            lines.append("需要注意：")
            lines.extend(detail[:8])

        if partial or failed or skipped:
            messagebox.showwarning("清理结束（有未完成项）", "\n".join(lines))
        else:
            messagebox.showinfo("清理完成", "\n".join(lines))
        state = "清理结束（有未完成项）" if partial or failed or skipped else "清理完成"
        self._set_status("%s：释放 %s" % (state, bytes_to_human(freed)))

    # ── 历史记录标签页 ───────────────────────────────────────────────

    def _build_tab_history(self):
        tab = self.tab_history
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        # 折线图区域
        chart_frame = ctk.CTkFrame(tab, height=180, corner_radius=8)
        chart_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        chart_frame.grid_propagate(False)
        chart_frame.grid_columnconfigure(0, weight=1)
        chart_frame.grid_rowconfigure(0, weight=1)

        ctk.CTkLabel(chart_frame, text="C盘使用率趋势",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=10, pady=(8, 0))

        is_dark = ctk.get_appearance_mode().lower() == "dark"
        bg = "#1e1e2e" if is_dark else "#f0f0f0"
        self._history_canvas = tk.Canvas(chart_frame, height=130, bg=bg,
                                         highlightthickness=1,
                                         highlightbackground="#444" if is_dark else "#ccc")
        self._history_canvas.pack(fill="x", padx=8, pady=(2, 8))

        # 记录列表区域
        self._history_scroll = ctk.CTkScrollableFrame(tab, label_text="清理历史记录")
        self._history_scroll.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self._history_scroll.grid_columnconfigure(0, weight=1)

        self._update_tab_history()

    def _load_history(self) -> list:
        path = HISTORY_FILE
        if not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_history_record(self, results):
        records = self._load_history()
        disk_pct = 0.0
        if self.analysis_result and self.analysis_result.get("disk_info"):
            disk_pct = self.analysis_result["disk_info"].used_percent
        counts = {}
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1
        records.append({
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "items": [r.name for r in results],
            "status": counts,
            "freed_bytes": sum(r.bytes_freed for r in results),
            "moved_bytes": sum(r.bytes_moved for r in results),
            "disk_used_percent": disk_pct,
        })
        records = records[-200:]
        try:
            os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _update_tab_history(self):
        records = self._load_history()
        # 重绘折线图
        self._draw_history_chart(self._history_canvas, records)
        # 重建列表
        for w in self._history_scroll.winfo_children():
            w.destroy()
        if not records:
            ctk.CTkLabel(self._history_scroll,
                         text="暂无清理记录。执行清理后将自动记录在这里。",
                         text_color=COLORS["text_dim"],
                         font=ctk.CTkFont(size=13)).grid(row=0, column=0, pady=30)
            return
        for i, rec in enumerate(reversed(records)):
            bg = ("gray90", "gray20") if i % 2 == 0 else ("gray85", "gray25")
            card = ctk.CTkFrame(self._history_scroll, fg_color=bg, corner_radius=6)
            card.grid(row=i, column=0, sticky="ew", padx=3, pady=3)
            card.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(card, text=rec.get("time", ""), width=130,
                         font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=0, padx=10, pady=6)
            items_str = "、".join(rec.get("items", []))
            ctk.CTkLabel(card, text=items_str, anchor="w",
                         font=ctk.CTkFont(size=11), wraplength=500).grid(row=0, column=1, sticky="ew", padx=5)
            freed = bytes_to_human(rec.get("freed_bytes", 0))
            pct = rec.get("disk_used_percent", 0)
            extra = ""
            if rec.get("moved_bytes"):
                extra = " | 备份 %s" % bytes_to_human(rec["moved_bytes"])
            ctk.CTkLabel(card, text="释放 %s%s | 使用率 %s%%" % (freed, extra, pct),
                         font=ctk.CTkFont(size=10), text_color=COLORS["success"]).grid(
                row=0, column=2, padx=10)

    def _draw_history_chart(self, canvas, records):
        canvas.update_idletasks()
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 10:
            w = 600
        if h < 10:
            h = 130
        canvas.delete("all")
        is_dark = ctk.get_appearance_mode().lower() == "dark"
        text_color = "white" if is_dark else "#333"
        grid_color = "#333" if is_dark else "#ccc"

        if len(records) < 2:
            canvas.create_text(w // 2, h // 2,
                               text="暂无足够数据绘制趋势图（至少需要2次清理记录）",
                               fill=text_color, font=("Arial", 10))
            return

        pad_l, pad_r, pad_t, pad_b = 50, 20, 15, 30
        chart_w = w - pad_l - pad_r
        chart_h = h - pad_t - pad_b

        pcts = [r.get("disk_used_percent", 0) for r in records]
        min_p = max(0, min(pcts) - 5)
        max_p = min(100, max(pcts) + 5)
        if max_p == min_p:
            max_p = min_p + 10

        # 网格线
        for pct_val in range(int(min_p // 10) * 10, int(max_p) + 10, 10):
            if min_p <= pct_val <= max_p:
                y = pad_t + chart_h - (pct_val - min_p) / (max_p - min_p) * chart_h
                canvas.create_line(pad_l, y, w - pad_r, y, fill=grid_color, dash=(2, 4))
                canvas.create_text(pad_l - 5, y, text=f"{pct_val}%",
                                   fill=text_color, font=("Arial", 8), anchor="e")

        # 折线
        n = len(records)
        points = []
        for i, r in enumerate(records):
            x = pad_l + i / (n - 1) * chart_w
            y = pad_t + chart_h - (r.get("disk_used_percent", 0) - min_p) / (max_p - min_p) * chart_h
            points.append((x, y))

        for i in range(len(points) - 1):
            canvas.create_line(points[i][0], points[i][1],
                               points[i+1][0], points[i+1][1],
                               fill=COLORS["info"], width=2)

        for i, (x, y) in enumerate(points):
            canvas.create_oval(x - 4, y - 4, x + 4, y + 4,
                               fill=COLORS["info"], outline="white", width=1)
            if i % max(1, n // 6) == 0 or i == n - 1:
                time_str = records[i].get("time", "")[-5:]
                canvas.create_text(x, h - pad_b + 8, text=time_str,
                                   fill=text_color, font=("Arial", 7))

    # ── 工具方法 ────────────────────────────────────────────────────

    def _set_progress(self, msg: str, value: float):
        self.progress_label.configure(text=msg)
        self.progress_bar.set(value)

    def _set_status(self, msg: str):
        self.status_label.configure(text=msg)

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import webbrowser
import threading
import subprocess
import os
import sys

from config import load_config, save_config, get_desktop_path
from scanner import run_wiztree_scan, build_export_path
from analyzer import run_analysis, CleanupItem, KNOWLEDGE_DATE, KNOWLEDGE_SOURCES
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


class DiskCleanerApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.session_csv_path = None  # 本次会话扫描生成的 CSV（用于关闭时询问）

        ctk.set_appearance_mode(self.config.get("theme", "dark"))
        ctk.set_default_color_theme("blue")

        self.title("磁盘清理助手 v2.0  |  C盘空间分析工具")
        self.geometry("1060x740")
        self.minsize(860, 620)

        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
        if os.path.exists(icon_path):
            self.iconbitmap(icon_path)

        self.analysis_result = None
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if not self.config.get("wiztree_path"):
            self.after(500, self._show_wiztree_dialog)

    # ── 关闭处理 ────────────────────────────────────────────────────

    def _on_close(self):
        """关闭窗口时：询问是否保留本次扫描的 CSV 文件"""
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

        ctk.CTkLabel(toolbar, text="🔍 磁盘清理助手 v2.0",
                     font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, padx=15, pady=15, sticky="w")

        path_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        path_frame.grid(row=0, column=1, padx=10, pady=10, sticky="ew")
        path_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(path_frame, text="WizTree:", width=65,
                     font=ctk.CTkFont(size=12)).grid(row=0, column=0, padx=5)
        self.wiztree_path_var = ctk.StringVar(
            value=self.config.get("wiztree_path", "") or "（未设置）")
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
        self.tab_report   = self.tabview.add("📄 报告操作")
        self.tab_settings = self.tabview.add("⚙️ 设置")

        self._build_tab_overview()
        self._build_tab_cleanup()
        self._build_tab_detail()
        self._build_tab_report()
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

    def _build_tab_report(self):
        tab = self.tab_report
        tab.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            tab,
            text=(
                "📄  报告操作说明\n\n"
                "• 点击「保存分析报告」将完整分析结果保存为 Markdown 文件\n"
                "• Markdown 文件可用记事本打开，也可用 Typora、VS Code 等查看格式化效果\n\n"
                "📁  生成的文件\n\n"
                "• WizTree 扫描文件：WizTree_scan_YYYYMMDD_HHMMSS.csv\n"
                "  → 保存目录在「⚙️ 设置」选项卡中配置（默认：桌面）\n"
                "• 分析报告：磁盘清理分析报告_YYYYMMDD_HHMM.md\n"
                "  → 保存目录在「⚙️ 设置」选项卡中配置（默认：桌面）\n\n"
                "🗑️  执行清理说明\n\n"
                "• 软件会列出将要删除的路径和大小，等待你确认后才会执行\n"
                "• 建议先从「立即清理」级别开始，风险最低"
            ),
            font=ctk.CTkFont(size=13), justify="left").grid(
            row=0, column=0, padx=20, pady=20, sticky="nw")

        btn_frame = ctk.CTkFrame(tab, fg_color="transparent")
        btn_frame.grid(row=1, column=0, padx=20, pady=10, sticky="w")

        self.save_report_btn = ctk.CTkButton(
            btn_frame, text="💾  保存分析报告", width=180, height=40,
            font=ctk.CTkFont(size=13), command=self._on_save_report,
            state="disabled", fg_color=COLORS["info"])
        self.save_report_btn.pack(side="left", padx=5)

        self.exec_cleanup_btn = ctk.CTkButton(
            btn_frame, text="🗑️  执行清理（优先级1）", width=200, height=40,
            font=ctk.CTkFont(size=13), command=self._on_exec_cleanup,
            state="disabled", fg_color=COLORS["danger"], hover_color="#c0392b")
        self.exec_cleanup_btn.pack(side="left", padx=5)

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
                         f"磁盘清理助手  v2.0          知识库更新日期：{KNOWLEDGE_DATE}\n\n"
                         f"本次联网核实新增/更新的清理规则：\n"
                         f"  • AMD GPU 着色器缓存（DxCache / GLCache）\n"
                         f"  • DirectX 系统着色器缓存（D3DSCache）\n"
                         f"  • 系统缩略图缓存（Thumbnail Cache）\n"
                         f"  • Windows 错误报告转储（WER）\n"
                         f"  • Windows 预读缓存（Prefetch，低风险）\n"
                         f"  • 核实：WinSxS 不可直接删除，已从规则中排除\n\n"
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

        ctk.CTkLabel(dialog, text="欢迎使用磁盘清理助手 v2.0",
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
        wiztree_path = self.config.get("wiztree_path", "")
        if not wiztree_path or not os.path.isfile(wiztree_path):
            self._show_wiztree_dialog()
            return
        self.scan_btn.configure(state="disabled", text="扫描中...")
        self.progress_bar.set(0)
        self._set_progress("正在准备扫描...", 0.05)
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        export_dir = self.config.get("csv_dir", "") or get_desktop_path()
        export_path = build_export_path(export_dir)

        self.after(0, lambda: self._set_progress("正在启动WizTree扫描...", 0.1))
        success, result = run_wiztree_scan(
            self.config["wiztree_path"], export_path,
            lambda msg: self.after(0, lambda: self._set_progress(msg, 0.3)))

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
        self.session_csv_path = csv_path  # 记录本次会话的 CSV，关闭时询问

        self._set_progress("分析完成！", 1.0)
        self._set_status(f"分析完成 | CSV：{os.path.basename(csv_path)}")

        self._update_tab_overview(analysis)
        self._update_tab_cleanup(analysis)
        self._update_tab_detail(analysis)

        self.save_report_btn.configure(state="normal")
        self.exec_cleanup_btn.configure(state="normal")
        self.scan_btn.configure(state="normal", text="🚀  开始扫描 C盘")
        self.last_csv_label.configure(text=f"最近扫描文件：{csv_path}")
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
        table_frame = ctk.CTkScrollableFrame(
            self.tab_overview,
            label_text=f"C:\\ 下一级目录 Top {len(top_level1)}（点击📂可打开文件夹）")
        table_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
        self.tab_overview.grid_rowconfigure(2, weight=1)
        table_frame.grid_columnconfigure(1, weight=1)

        for col, (text, w) in enumerate([("排名", 40), ("路径", 0), ("大小", 80), ("", 38)]):
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
            ctk.CTkButton(table_frame, text="📂", width=36, height=26,
                          fg_color="transparent", hover_color=("gray80", "gray30"),
                          command=lambda p=folder.path: _open_folder(p)).grid(
                row=i, column=3, padx=5, pady=2)

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
        cleanup_items = analysis.get("cleanup_items", [])
        reclaimable = analysis.get("total_reclaimable_bytes", 0)
        p1_count = sum(1 for x in cleanup_items if x.priority == 1)
        self.cleanup_summary.configure(
            text=f"共发现 {len(cleanup_items)} 个清理项 | "
                 f"立即可清理 {p1_count} 项 | "
                 f"无风险可回收 {bytes_to_human(reclaimable)}",
            text_color="white")

        self.cleanup_hint.grid_forget()
        for widget in self.cleanup_scroll.winfo_children():
            widget.destroy()

        if not cleanup_items:
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
                             text=f"  {PRIORITY_LABELS.get(current_priority, '')}",
                             font=ctk.CTkFont(size=14, weight="bold"),
                             text_color=color, anchor="w").grid(
                    row=row, column=0, sticky="ew", padx=5, pady=(12, 3))
                row += 1

            card = self._make_cleanup_card(self.cleanup_scroll, item)
            card.grid(row=row, column=0, sticky="ew", padx=5, pady=3)
            row += 1

    def _make_cleanup_card(self, parent, item: CleanupItem) -> ctk.CTkFrame:
        color = PRIORITY_COLORS.get(item.priority, "gray")
        card = ctk.CTkFrame(parent, corner_radius=8, border_width=1, border_color=color)
        card.grid_columnconfigure(1, weight=1)

        # 左侧彩色竖条
        ctk.CTkFrame(card, width=5, fg_color=color, corner_radius=4).grid(
            row=0, column=0, rowspan=5, sticky="ns", padx=(5, 8), pady=5)

        # 名称 + 大小
        name_frame = ctk.CTkFrame(card, fg_color="transparent")
        name_frame.grid(row=0, column=1, sticky="ew", pady=(8, 2))
        name_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(name_frame, text=item.name,
                     font=ctk.CTkFont(size=13, weight="bold"), anchor="w").grid(
            row=0, column=0, sticky="w")
        ctk.CTkLabel(name_frame, text=bytes_to_human(item.size_bytes),
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=color).grid(row=0, column=1, padx=10)

        # 路径 + 打开按钮（可点击访问）
        path_row = ctk.CTkFrame(card, fg_color="transparent")
        path_row.grid(row=1, column=1, sticky="ew", pady=2)
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

        # 风险 + 操作方法
        ctk.CTkLabel(card, text=f"风险：{item.risk}  ｜  操作：{item.method}",
                     font=ctk.CTkFont(size=11), anchor="w").grid(
            row=2, column=1, sticky="ew", pady=2)

        # 折叠的判断依据
        reason_text = ctk.CTkLabel(
            card, text=item.reason, font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"], wraplength=700, justify="left", anchor="w")
        expanded = [False]
        btn_holder = [None]

        def toggle():
            expanded[0] = not expanded[0]
            if expanded[0]:
                reason_text.grid(row=4, column=1, sticky="ew", padx=5, pady=(0, 8))
                btn_holder[0].configure(text="▼ 收起判断依据")
            else:
                reason_text.grid_forget()
                btn_holder[0].configure(text="▶ 查看判断依据")

        toggle_btn = ctk.CTkButton(
            card, text="▶ 查看判断依据", height=22, width=130,
            font=ctk.CTkFont(size=11), fg_color="transparent",
            text_color=COLORS["info"], hover_color=("gray85", "gray25"),
            command=toggle, anchor="w")
        toggle_btn.grid(row=3, column=1, sticky="w", pady=(0, 5))
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

    # ── 报告和清理 ──────────────────────────────────────────────────

    def _on_save_report(self):
        if not self.analysis_result:
            messagebox.showwarning("无数据", "请先执行扫描。")
            return
        output_dir = self.config.get("report_dir", "") or get_desktop_path()
        success, result = generate_report(self.analysis_result, output_dir)
        if success:
            messagebox.showinfo("报告已保存",
                                f"报告已保存到：\n{result}\n\n可用记事本或Typora打开查看。")
            self._set_status(f"报告已保存：{os.path.basename(result)}")
        else:
            messagebox.showerror("保存失败", result)

    def _on_exec_cleanup(self):
        if not self.analysis_result:
            return
        p1_items = [x for x in self.analysis_result.get("cleanup_items", [])
                    if x.priority == 1]
        if not p1_items:
            messagebox.showinfo("无可清理项", "没有找到「立即清理」级别的项目。")
            return

        preview = "\n".join(
            f"  • {item.name}\n    路径：{item.path}\n    大小：{bytes_to_human(item.size_bytes)}"
            for item in p1_items)
        total = bytes_to_human(sum(x.size_bytes for x in p1_items))

        if not messagebox.askyesno("确认清理",
                                   f"即将删除以下「立即清理」级别项目（共 {total}）：\n\n"
                                   f"{preview}\n\n确认删除？（此操作不可撤销）"):
            return

        self._set_status("正在执行清理...")
        errors = []
        for item in p1_items:
            path = item.path.strip('"')
            try:
                subprocess.run(
                    ["powershell", "-Command",
                     f'Remove-Item -Path "{path}" -Recurse -Force -ErrorAction SilentlyContinue'],
                    timeout=60, capture_output=True)
            except Exception as e:
                errors.append(f"{item.name}: {e}")

        if errors:
            messagebox.showwarning("部分清理失败", "以下项目清理失败：\n" + "\n".join(errors))
        else:
            messagebox.showinfo("清理完成", f"清理完成！共释放约 {total}。\n\n建议重新扫描查看最新状态。")
        self._set_status("清理完成")

    # ── 工具方法 ────────────────────────────────────────────────────

    def _set_progress(self, msg: str, value: float):
        self.progress_label.configure(text=msg)
        self.progress_bar.set(value)

    def _set_status(self, msg: str):
        self.status_label.configure(text=msg)

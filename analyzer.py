# ==============================================================
# analyzer.py —— 磁盘数据分析引擎 + 清理规则库
# ==============================================================
# 【职责】
#   1. 流式解析 WizTree 导出的 CSV（文件夹 + 大文件）
#   2. 用规则库判断哪些目录可以清理，以及"怎么清理"
#   3. 生成只读的"系统级建议"（WinSxS / Installer / 休眠文件等）
#
# 【规则设计原则】（2026-10 重写）
#   · 每条规则必须锚定到具体父目录，禁止用 `\\KOOK\\` 这种
#     会命中 Program Files 安装目录的宽匹配
#   · 规则分三种动作：delete（可直接删）/ backup（移入备份）/
#     guide（只给官方做法，永远不自动删）
#   · 系统目录（WinSxS、Installer、WindowsApps、Package Cache）
#     一律 guide，不提供删除按钮
#   · 规则只负责"发现"，真正的删除安全由 cleaner.py 兜底
# ==============================================================

import csv
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from cleaner import path_is_safe

# ── 单位换算 ────────────────────────────────────────────────────
GB = 1_073_741_824
MB = 1_048_576

# 小于 5MB 的目录不单独列出（避免报告被碎片淹没）
MIN_ITEM_BYTES = 5 * MB
# 大文件榜的入选门槛与数量上限
BIG_FILE_MIN_BYTES = 100 * MB
BIG_FILE_LIMIT = 300

KNOWLEDGE_DATE = "2026-10-21"
KNOWLEDGE_SOURCES = (
    "Microsoft Learn（存储感知 / 组件清理 / WinSxS 说明）、WizTree 4.25 官方命令行文档、"
    "ElevenForum、Microsoft Q&A、win11-storage-governance-kit（2026-10 复核）。\n"
    "结论：WinSxS 与 Windows\\Installer 只能用官方工具维护；Prefetch、缩略图、"
    "GPU 着色器缓存、WER 转储、浏览器缓存均可安全再生；聊天文件属于真实数据，只能迁移不能删。"
)

# 常用路径前缀，减少规则里的重复书写
_U = r"^[A-Za-z]:\\Users\\[^\\]+\\AppData\\"
_L = _U + r"Local\\"
_R = _U + r"Roaming\\"
_PF = r"^C:\\Program Files( \(x86\))?\\"
_STEAM = r"^C:\\(?:Program Files(?: \(x86\))?\\)?Steam\\"
_TEAMS = r"^[A-Za-z]:\\Users\\[^\\]+\\AppData\\(?:Local|Roaming)\\Microsoft\\Teams\\"
_ADOBE = r"^[A-Za-z]:\\Users\\[^\\]+\\AppData\\(?:Local|Roaming)\\Adobe\\"


# ==============================================================
# 数据结构
# ==============================================================

@dataclass
class DiskInfo:
    """磁盘总体信息（来自 CSV 根目录行）"""
    total_bytes: int
    used_bytes: int
    free_bytes: int
    scan_time: str

    @property
    def total_gb(self) -> float:
        return round(self.total_bytes / GB, 2)

    @property
    def used_gb(self) -> float:
        return round(self.used_bytes / GB, 2)

    @property
    def free_gb(self) -> float:
        return round(self.free_bytes / GB, 2)

    @property
    def used_percent(self) -> float:
        if self.total_bytes == 0:
            return 0
        return round(self.used_bytes / self.total_bytes * 100, 1)


@dataclass
class FolderEntry:
    """CSV 中的一行文件夹记录"""
    path: str
    size_bytes: int
    modified: str = ""
    file_count: int = 0
    folder_count: int = 0

    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / GB, 2)

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / MB, 1)

    @property
    def depth(self) -> int:
        return len([p for p in self.path.rstrip("\\").split("\\") if p]) - 1

    @property
    def name(self) -> str:
        return self.path.rstrip("\\").split("\\")[-1]


@dataclass
class FileEntry:
    """CSV 中的一行文件记录（只保留较大的文件）"""
    path: str
    size_bytes: int
    modified: str = ""

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


@dataclass
class CleanupItem:
    """一条清理建议"""
    path: str
    size_bytes: int
    priority: int
    name: str
    reason: str
    method: str
    risk: str
    action: str = "delete"          # delete / backup / guide
    target: str = "folder"          # folder / contents / files
    file_globs: Tuple[str, ...] = ()
    recursive: bool = True
    requires_admin: bool = False
    count_files: int = 0
    count_dirs: int = 0
    source: str = ""

    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / GB, 2)

    @property
    def priority_label(self) -> str:
        if self.action == "guide":
            return "🟢 仅建议"
        labels = {1: "🔴 立即清理", 2: "🟠 高收益缓存",
                  3: "🟡 需要重新生成", 4: "🟢 建议备份/迁移"}
        base = labels.get(self.priority, "未知")
        if self.action == "backup":
            return base + "（备份）"
        return base

    @property
    def admin_label(self) -> str:
        return "需要管理员" if self.requires_admin else ""


# ==============================================================
# 规则定义
# ==============================================================

def _rule(pattern: str, hint, priority: int, name: str, reason: str,
          method: str, risk: str, action: str = "delete",
          target: str = "folder", globs: Sequence[str] = (),
          recursive: bool = True, admin: bool = False,
          source: str = "2026-10 复核") -> dict:
    return {
        "pattern": pattern,
        "hint": hint,
        "priority": priority,
        "name": name,
        "reason": reason,
        "method": method,
        "risk": risk,
        "action": action,
        "target": target,
        "globs": tuple(globs),
        "recursive": recursive,
        "admin": admin,
        "source": source,
    }

CLEANUP_RULES: List[dict] = [
    # ── 优先级 1：无风险、可立即清理 ─────────────────────────────
    _rule(_L + r"NVIDIA\\DXCache$", "nvidia\\dxcache", 1,
          "NVIDIA 着色器缓存（DXCache）",
          "DirectX 着色器编译缓存。删除后 GPU 在下次运行游戏/3D 软件时自动重建，"
          "不修改任何游戏文件与设置。",
          "直接删除整个文件夹", "✅ 无风险（自动重建）",
          source="NVIDIA / Microsoft 文档"),
    _rule(_L + r"NVIDIA\\GLCache$", "nvidia\\glcache", 1,
          "NVIDIA OpenGL 着色器缓存（GLCache）",
          "OpenGL 着色器编译缓存，原理同 DXCache，通常体积较小。",
          "直接删除整个文件夹", "✅ 无风险（自动重建）"),
    _rule(r"^C:\\ESD$", "\\esd", 1,
          "Windows 升级源文件（C:\\ESD）",
          "Windows 大版本升级时下载的安装包，升级完成后不再使用。",
          "直接删除整个 C:\\ESD，或用「磁盘清理 → 清理系统文件」",
          "✅ 无风险（不影响当前系统）", admin=True),
    _rule(_L + r"Temp$", "\\appdata\\local\\temp", 1,
          "用户临时文件（Temp）",
          "软件运行时产生的临时文件，正常情况下退出时自动清理；崩溃后会有残留。"
          "被占用的文件会自动跳过。",
          "删除文件夹内的内容（保留 Temp 文件夹本身）",
          "✅ 无风险（被占用文件自动跳过）", target="contents"),
    _rule(r"^C:\\Windows\\Temp$", "\\windows\\temp", 1,
          "系统临时文件（Windows\\Temp）",
          "系统服务与安装程序共用的临时目录，长期积累可达数 GB。",
          "删除文件夹内的内容（保留目录本身）",
          "✅ 无风险（被占用文件自动跳过）", target="contents", admin=True),
    _rule(_L + r"AMD\\DxCache$", "amd\\dxcache", 1,
          "AMD 着色器缓存（DxCache）",
          "AMD 显卡的 DirectX 着色器缓存，原理与 NVIDIA DXCache 相同。",
          "直接删除整个文件夹", "✅ 无风险（自动重建）"),
    _rule(_L + r"AMD\\GLCache$", "amd\\glcache", 1,
          "AMD OpenGL 着色器缓存（GLCache）",
          "AMD 显卡的 OpenGL 着色器缓存，删除后自动重建。",
          "直接删除整个文件夹", "✅ 无风险（自动重建）"),
    _rule(_L + r"D3DSCache$", "d3dscache", 1,
          "DirectX 系统着色器缓存（D3DSCache）",
          "Windows 系统级着色器缓存，独立于显卡驱动。驱动或系统更新后清理它"
          "有时还能修复图形异常。",
          "直接删除整个文件夹", "✅ 无风险（自动重建）",
          source="Microsoft Q&A / ElevenForum"),
    _rule(_L + r"Microsoft\\Windows\\Explorer$", "\\windows\\explorer", 1,
          "缩略图与图标缓存",
          "资源管理器为图片/视频生成的预览缓存（thumbcache_*.db、iconcache_*.db）。"
          "只删这两类缓存文件，不动该目录下的其他数据。",
          "删除该目录下的 thumbcache_*.db 与 iconcache_*.db",
          "✅ 无风险（自动重建，首次浏览稍慢）",
          target="files", globs=("thumbcache_*.db", "iconcache_*.db"),
          recursive=False),
    _rule(r"^C:\\ProgramData\\Microsoft\\Windows\\WER\\(ReportArchive|ReportQueue|Temp)$",
          "\\wer\\", 1,
          "Windows 错误报告（WER）",
          "程序崩溃时生成的内存转储与诊断报告，普通用户几乎用不到。",
          "删除文件夹内的内容", "✅ 无风险（仅调试信息）",
          target="contents", admin=True),
    _rule(_R + r"discord\\(Cache|Code Cache|GPUCache)$", "discord", 1,
          "Discord 聊天缓存",
          "Discord 缓存的头像、图片、视频，数据都在云端。",
          "直接删除整个文件夹", "✅ 无风险（自动重建）"),
    _rule(_L + r"Spotify\\Storage$", "spotify", 1,
          "Spotify 音乐缓存",
          "Spotify 离线/播放缓存，删除后重新播放时自动下载。",
          "直接删除整个文件夹", "✅ 无风险"),
    _rule(_STEAM + r"htmlcache$", "steam\\htmlcache", 1,
          "Steam 内置浏览器缓存",
          "Steam 客户端内置 Chromium 的网页缓存，与游戏文件无关。",
          "直接删除整个文件夹", "✅ 无风险"),
    _rule(_L + r"EpicGamesLauncher\\Saved\\webcache$", "epicgameslauncher", 1,
          "Epic Games 启动器缓存",
          "Epic 启动器的网页缓存，删除后自动重建。",
          "直接删除整个文件夹", "✅ 无风险"),

    # ── 优先级 2：高收益缓存 ─────────────────────────────────────
    _rule(r"^C:\\Windows\\Prefetch$", "prefetch", 2,
          "Windows 预读缓存（Prefetch）",
          "Windows 为加速程序启动预读的数据（*.pf）。删除后会自动重建，"
          "但接下来 1~2 次开机会稍慢。",
          "只删除该目录下的 *.pf 文件", "🟡 低风险（下次启动略慢）",
          target="files", globs=("*.pf",), recursive=False, admin=True),
    _rule(_L + r"JianyingPro\\User Data\\(Cache|cache|temp|Temp)$", "jianyingpro", 2,
          "剪映缓存（User Data）",
          "剪映的预览、素材缓存。注意：草稿工程在 User Data\\Projects，"
          "本规则不会碰它。",
          "删除该缓存目录，或用「剪映 → 设置 → 缓存管理」",
          "🟡 低风险（未导出项目需重新生成预览）"),
    _rule(_L + r"Mozilla\\Firefox\\Profiles\\[^\\]+\\(cache2|startupCache|shader-cache|thumbnails|OfflineCache)$",
          "mozilla", 2,
          "Firefox 网页缓存",
          "Firefox 的磁盘缓存。书签、密码、历史保存在同级的其他文件中，"
          "本规则只匹配缓存子目录。",
          "删除该缓存目录，或用 Firefox 设置清除缓存",
          "✅ 无风险（不动书签/密码/历史）"),
    _rule(_R + r"Mozilla\\Firefox\\Profiles\\[^\\]+\\(cache2|startupCache|shader-cache)$",
          "mozilla", 2,
          "Firefox 缓存（Roaming）",
          "Firefox 在 Roaming 下的缓存子目录。绝不匹配整个 Mozilla 或 Profiles 目录。",
          "删除该缓存目录", "✅ 无风险（不动书签/密码/历史）"),
    _rule(_L + r"Google\\Chrome\\User Data\\[^\\]+\\(Cache|Code Cache|GPUCache|ShaderCache|DawnCache|GrShaderCache)$",
          "chrome", 2,
          "Chrome 浏览器缓存",
          "网页与脚本缓存，仅影响下次访问的加载速度；书签、密码、登录状态不受影响。",
          "删除该缓存目录，或 Chrome 设置里清除浏览数据",
          "✅ 无风险"),
    _rule(_L + r"Google\\Chrome\\User Data\\[^\\]+\\Service Worker\\CacheStorage$",
          "chrome", 2,
          "Chrome Service Worker 缓存",
          "离线网页缓存。删除后网页首次加载略慢，不影响账号数据。",
          "删除该缓存目录", "✅ 无风险"),
    _rule(_L + r"Microsoft\\Edge\\User Data\\[^\\]+\\(Cache|Code Cache|GPUCache|ShaderCache|DawnCache|GrShaderCache)$",
          "edge", 2,
          "Edge 浏览器缓存",
          "Edge 的网页与脚本缓存，原理同 Chrome。",
          "删除该缓存目录，或 Edge 设置里清除浏览数据",
          "✅ 无风险"),
    _rule(_L + r"Microsoft\\Edge\\User Data\\[^\\]+\\Service Worker\\CacheStorage$",
          "edge", 2, "Edge Service Worker 缓存",
          "Edge 的离线网页缓存，可安全删除。", "删除该缓存目录", "✅ 无风险"),
    _rule(_L + r"KOOK\\(Cache|Code Cache|GPUCache|logs|Logs)$", "kook", 2,
          "KOOK 缓存与日志",
          "KOOK 的界面渲染缓存和日志，只匹配 KOOK 的 AppData 子目录，不碰安装目录。",
          "删除该缓存目录", "✅ 无风险"),
    _rule(r"\\app_shell_cache$", "app_shell_cache", 2,
          "Electron 应用 Shell 缓存",
          "Electron 应用（KOOK、钉钉、飞书等）的界面缓存，应用启动时自动重建。",
          "删除该目录", "✅ 无风险"),
    _rule(_R + r"baidu\\[^\\]+\\(Cache|cache|temp)$", "baidu", 2,
          "百度系应用缓存",
          "百度输入法等软件的缓存子目录，不涉及账号数据。",
          "删除该缓存目录", "🟡 低风险"),
    _rule(_R + r"douyin\\(Cache|cache|Temp|temp)$", "douyin", 2,
          "抖音 PC 客户端缓存",
          "抖音缓存的视频封面与预览，内容均在云端。",
          "删除该缓存目录", "✅ 无风险"),
    _rule(_L + r"Razer\\[^\\]+\\(Logs|Log|Cache)$", "razer", 2,
          "Razer 软件缓存与日志",
          "雷蛇 Synapse 的日志/缓存子目录。注意：只匹配 AppData，不碰 Program Files 里的驱动本体。",
          "删除该缓存目录", "✅ 无风险"),
    _rule(_L + r"baidunetdisk\\[^\\]+\\(Cache|temp)$", "baidunetdisk", 2,
          "百度网盘缓存",
          "下载过程中产生的临时缓存，不是已保存到网盘或本地的文件。",
          "删除该缓存目录，或在百度网盘设置里清理缓存",
          "🟡 低风险（确认没有正在下载的任务）"),
    _rule(_STEAM + r"steamapps\\downloading$", "steam\\steamapps\\downloading", 2,
          "Steam 未完成的下载",
          "正在下载但未完成的游戏文件。删除后需要重新下载，已安装的游戏不受影响。",
          "先在 Steam 里暂停/取消下载，再清理",
          "🟡 低风险（丢失下载进度）", target="contents"),
    _rule(_STEAM + r"steamapps\\workshop\\downloads$",
          "steam\\steamapps\\workshop\\downloads", 2,
          "Steam 创意工坊下载缓存",
          "创意工坊内容的下载缓存，删除后可重新下载。",
          "删除文件夹内容", "✅ 无风险", target="contents"),
    _rule(_R + r"Code\\CachedData$", "\\code\\cacheddata", 2,
          "VS Code 扩展缓存",
          "VS Code 扩展的编译缓存，删除后首次启动会重新生成，稍慢。",
          "删除整个文件夹", "✅ 无风险（自动重建）"),
    _rule(_R + r"Code\\logs$", "\\code\\logs", 2,
          "VS Code 运行日志",
          "VS Code 的运行日志，对日常使用无用。",
          "删除整个文件夹", "✅ 无风险"),
    _rule(r"\\JetBrains\\[^\\]+\\caches$", ("jetbrains", "intellijidea"), 2,
          "JetBrains IDE 索引缓存",
          "IntelliJ IDEA / PyCharm / WebStorm 的索引缓存，删除后重启 IDE 会重建（需几分钟）。",
          "删除该目录，或用 IDE 的 File → Invalidate Caches",
          "🟡 低风险（需重建索引）"),
    _rule(_ADOBE + r"Common\\(Media Cache Files|Media Cache)$", "adobe", 2,
          "Adobe 媒体缓存",
          "Premiere / After Effects 的媒体预览缓存，删除后重新打开项目会重建。",
          "删除该目录，或用 Premiere 首选项清除媒体缓存",
          "🟡 低风险（项目需重新生成预览）"),
    _rule(_ADOBE + r"[^\\]+\\(Cache|Temp)$", "adobe", 2,
          "Adobe 应用缓存",
          "Photoshop 等 Adobe 软件的临时缓存。",
          "删除该目录", "✅ 无风险"),
    _rule(_R + r"Tencent\\QQMusic\\(Cache|cache)$", "qqmusic", 2,
          "QQ 音乐缓存",
          "QQ 音乐缓存的封面与试听文件，重新播放时自动下载。",
          "删除该缓存目录", "✅ 无风险"),
    _rule(_L + r"Netease\\CloudMusic\\Cache$", "cloudmusic", 2,
          "网易云音乐缓存",
          "网易云音乐的歌曲/封面缓存，删除后重新播放时自动下载。",
          "删除该目录，或在网易云音乐设置里清除缓存", "✅ 无风险"),
    _rule(_R + r"DingTalk\\(Cache|Temp|temp)$", "dingtalk", 2,
          "钉钉缓存",
          "钉钉的界面与文件预览缓存，聊天记录不在此目录。",
          "删除该缓存目录", "✅ 无风险"),
    _rule(_R + r"(Lark|feishu)\\(Cache|cache|sdk_storage)$", ("lark", "feishu"), 2,
          "飞书缓存",
          "飞书的界面缓存，删除后自动重建。",
          "删除该缓存目录", "✅ 无风险"),
    _rule(_TEAMS + r"(Cache|Code Cache|GPUCache|ShaderCache)$",
          "teams", 2,
          "Microsoft Teams 缓存",
          "Teams 客户端缓存，积累较快，删除后自动重建。",
          "删除该缓存目录", "✅ 无风险"),
    _rule(_R + r"Zoom\\data$", "zoom\\data", 4,
          "Zoom 本地数据",
          "Zoom 的本地缓存与配置，删除会重置部分客户端设置（会议录制在文档目录，不在此处）。",
          "移入备份目录，确认无影响后再删除", "🟡 低风险（会重置本地设置）",
          action="backup"),
    _rule(_R + r"npm-cache$", "npm-cache", 2,
          "npm 缓存",
          "npm 下载包的本地缓存，删除后重新安装时自动下载。",
          "执行 `npm cache clean --force`，或直接删除",
          "✅ 无风险"),
    _rule(_L + r"npm-cache$", "npm-cache", 2,
          "npm 缓存（Local）",
          "npm 的本地包缓存目录。", "直接删除", "✅ 无风险"),
    _rule(r"\\.npm\\_npx$", "_npx", 2,
          "npx 临时安装目录",
          "npx 临时安装的包，删除后再次使用 npx 会重新下载。",
          "直接删除", "✅ 无风险"),
    _rule(r"^C:\\Windows\\ServiceProfiles\\NetworkService\\AppData\\Local\\Microsoft\\Windows\\DeliveryOptimization\\Cache$",
          "deliveryoptimization", 2,
          "传递优化（Delivery Optimization）缓存",
          "Windows 更新与商店应用的 P2P 分发缓存，删除后系统会按需重新缓存。",
          "删除文件夹内容，或用「磁盘清理 → 传递优化文件」",
          "✅ 无风险", target="contents", admin=True),
    _rule(r"^C:\\Windows\\Logs\\CBS$", "\\logs\\cbs", 2,
          "Windows 组件安装日志（CBS）",
          "系统组件安装/修复日志，排障时才需要，长期积累可达数 GB。",
          "删除文件夹内容", "✅ 无风险（仅日志）",
          target="contents", admin=True),
    _rule(r"^C:\\Windows\\LiveKernelReports$", "livekernelreports", 2,
          "内核实时转储（LiveKernelReports）",
          "内核异常时生成的转储文件，常用于驱动问题排查，普通用户用不到。",
          "删除文件夹内容", "✅ 无风险（仅诊断数据）",
          target="contents", admin=True),
    _rule(r"^C:\\Windows\\Minidump$", "minidump", 2,
          "蓝屏小内存转储（Minidump）",
          "蓝屏时生成的 .dmp 文件，若不是正在排查蓝屏问题可直接清理。",
          "删除文件夹内容", "✅ 无风险（仅诊断数据）",
          target="contents", admin=True),
    _rule(_L + r"CrashDumps$", "crashdumps", 2,
          "应用崩溃转储（CrashDumps）",
          "程序崩溃时生成的转储文件，普通用户用不到。",
          "删除文件夹内容", "✅ 无风险（仅诊断数据）",
          target="contents"),

    # ── 优先级 3：需要重新生成/下载 ─────────────────────────────
    _rule(r"\\Riot Games\\League of Legends\\(Logs|Cache)$", "riot games", 3,
          "英雄联盟缓存与日志",
          "游戏日志与缓存，删除后下次启动会重新生成/校验资源。",
          "删除该目录", "🟡 中等（首次启动需校验资源）"),
    _rule(r"\\.minecraft\\logs$", "minecraft", 3,
          "Minecraft 日志",
          "游戏运行日志，删除不影响存档。",
          "删除该目录", "✅ 无风险（仅日志）"),

    # ── 优先级 4：真实数据，只备份/迁移，不直接删 ───────────────
    _rule(r"\\Documents\\WeChat Files$", "wechat files", 4,
          "微信聊天文件（真实数据）",
          "微信接收的文件、图片、视频，属于真实数据。删除不可恢复，"
          "正确做法是迁移到 D 盘。",
          "微信 → 设置 → 文件管理 → 更改存储位置，或手动迁移到 D 盘",
          "⚠️ 高风险（真实文件，禁止直接删除）", action="guide"),
    _rule(_R + r"Tencent$", "tencent", 4,
          "腾讯软件数据（微信/QQ）",
          "微信、QQ、腾讯会议的本地数据，可能包含聊天记录与登录状态。",
          "用软件自带的「清理存储空间」功能，不要直接删目录",
          "⚠️ 中等风险（可能丢聊天记录）", action="guide"),
    _rule(_L + r"Tencent$", "tencent", 4,
          "腾讯软件本地缓存（Local）",
          "腾讯系软件的本地缓存，也可能含已下载的聊天文件。",
          "优先用软件自带清理功能；确认后再删",
          "🟡 低-中风险", action="guide"),

    # ── 只建议：系统组件，只能用官方工具 ───────────────────────
    _rule(r"^C:\\Windows\\WinSxS$", "winsxs", 2,
          "组件存储（WinSxS）",
          "系统组件存储，直接删除会导致更新失败甚至系统无法启动。"
          "正确做法是用 DISM 清理旧组件。",
          "管理员运行：DISM /Online /Cleanup-Image /StartComponentCleanup",
          "🚫 禁止手动删除", action="guide", admin=True,
          source="Microsoft Learn"),
    _rule(r"^C:\\Windows\\Installer$", "\\windows\\installer", 2,
          "MSI 安装缓存（Windows\\Installer）",
          "已安装程序的 MSI/MSP 缓存，删除后无法修复/卸载对应软件。",
          "不要手动删除；用「磁盘清理 → 清理系统文件」或专用工具分析",
          "🚫 禁止手动删除", action="guide", admin=True),
    _rule(r"^C:\\Program Files\\WindowsApps$", "windowsapps", 2,
          "商店应用目录（WindowsApps）",
          "Microsoft Store 应用安装目录，权限由系统管理。",
          "通过「设置 → 应用」卸载不需要的应用",
          "🚫 禁止手动删除", action="guide", admin=True),
    _rule(r"^C:\\ProgramData\\Package Cache$", "package cache", 2,
          "安装包缓存（Package Cache）",
          "Visual Studio、VC++ 运行库等安装器的缓存，删除会导致无法修复安装。",
          "用 Visual Studio Installer 或对应的卸载/修复工具处理",
          "🚫 不建议手动删除", action="guide", admin=True),
    _rule(r"^C:\\Windows\\SoftwareDistribution\\Download$", "softwaredistribution", 2,
          "Windows 更新下载缓存",
          "已下载的更新包。直接删可能损坏正在进行的更新。",
          "先停止 wuauserv 服务，或用「磁盘清理 → Windows 更新清理」",
          "🟡 低风险（需先停服务）", action="guide", admin=True),
    _rule(r"^C:\\\$Recycle\.Bin$", "recycle.bin", 2,
          "回收站",
          "回收站中的文件，按用户 SID 分目录存放。",
          "右键回收站 → 清空，或执行 Clear-RecycleBin -Force",
          "⚠️ 清空后不可恢复", action="guide"),
    _rule(r"\\AppData\\Local\\Packages\\[^\\]+\\LocalCache$", "localcache", 2,
          "UWP 应用缓存（LocalCache）",
          "商店应用（含新版 Teams、照片等）的本地缓存。",
          "用「设置 → 应用 → 高级选项 → 重置/修复」，不要直接删",
          "🟡 低风险（可能重置应用状态）", action="guide"),
    _rule(r"\\AppData\\Local\\Docker\\wsl\\(data|distro)$", "docker\\wsl", 4,
          "Docker / WSL 虚拟磁盘",
          "Docker Desktop 的 WSL 数据盘，可能是几十 GB 的 ext4.vhdx。"
          "删除会丢失镜像、容器和卷。",
          "Docker Desktop → Settings → Resources → Disk image location，迁到 D 盘",
          "⚠️ 高风险（删除会丢容器数据）", action="guide"),
]


# ==============================================================
# 规则编译与匹配
# ==============================================================

_SEGMENT_INDEX = None
_REGEX_CACHE: List["re.Pattern"] = []


def _rule_hint_keys(rule: dict) -> Tuple[str, ...]:
    """把 hint 收敛成路径段名，用于建立"段 → 规则"倒排索引"""
    keys = []
    for hint in _hints(rule):
        parts = [p for p in hint.split("\\") if p]
        if parts:
            keys.append(parts[-1].lstrip("$"))
    return tuple(keys)


def _segment_index():
    global _SEGMENT_INDEX
    if _SEGMENT_INDEX is None:
        index = {}
        for rule_index, rule in enumerate(CLEANUP_RULES):
            for key in _rule_hint_keys(rule):
                index.setdefault(key, []).append(rule_index)
        _SEGMENT_INDEX = {k: tuple(sorted(set(v))) for k, v in index.items()}
    return _SEGMENT_INDEX


def _rule_regex(rule_index: int):
    if len(_REGEX_CACHE) < len(CLEANUP_RULES):
        _REGEX_CACHE[:] = [re.compile(r["pattern"], re.IGNORECASE)
                           for r in CLEANUP_RULES]
    return _REGEX_CACHE[rule_index]


def _hints(rule: dict) -> Tuple[str, ...]:
    hint = rule.get("hint", "")
    if isinstance(hint, str):
        return (hint.lower(),) if hint else ()
    return tuple(h.lower() for h in hint if h)


def _is_child(path: str, parent: str) -> bool:
    p = path.rstrip("\\").lower() + "\\"
    q = parent.rstrip("\\").lower() + "\\"
    return p.startswith(q) and p != q


def apply_cleanup_rules(folders: Sequence[FolderEntry]) -> List[CleanupItem]:
    """
    对所有文件夹应用规则，返回清理建议（含 guide 项）。

    实现要点：先用"路径段 → 规则"倒排索引把候选规则缩到个位数，
    再跑正则确认。20 万行从原来的 12 秒降到 1 秒级。
    """
    candidates: List[Tuple[str, str, FolderEntry]] = []
    for folder in folders:
        if folder.size_bytes < MIN_ITEM_BYTES:
            continue
        norm = folder.path.rstrip("\\")
        candidates.append((norm, norm.lower(), folder))

    if not candidates:
        return []

    index = _segment_index()
    raw: List[Tuple[int, str, dict, FolderEntry]] = []
    for norm, low, folder in candidates:
        hits = set()
        for segment in low.split("\\"):
            rule_ids = index.get(segment.lstrip("$"))
            if rule_ids:
                hits.update(rule_ids)
        if not hits:
            continue
        for rule_index in sorted(hits):
            rule = CLEANUP_RULES[rule_index]
            if not _rule_regex(rule_index).search(norm):
                continue
            if rule["action"] != "guide":
                safe, _reason = path_is_safe(norm)
                if not safe:
                    continue
            raw.append((folder.depth, norm, rule, folder))
            break

    # 浅层优先：父目录已命中的，子目录不再重复计量（按动作类型分别处理）
    raw.sort(key=lambda x: (x[0], x[1]))
    kept_action: List[str] = []
    kept_guide: List[str] = []
    items: List[CleanupItem] = []

    for _depth, norm, rule, folder in raw:
        is_guide = rule["action"] == "guide"
        bucket = kept_guide if is_guide else kept_action
        if any(_is_child(norm, parent) or norm.lower() == parent.lower()
               for parent in bucket):
            continue
        bucket.append(norm)
        items.append(CleanupItem(
            path=norm,
            size_bytes=folder.size_bytes,
            priority=rule["priority"],
            name=rule["name"],
            reason=rule["reason"],
            method=rule["method"],
            risk=rule["risk"],
            action=rule["action"],
            target=rule["target"],
            file_globs=tuple(rule["globs"]),
            recursive=rule["recursive"],
            requires_admin=rule["admin"],
            count_files=folder.file_count,
            count_dirs=folder.folder_count,
            source=rule.get("source", ""),
        ))

    items.sort(key=lambda x: (x.action == "guide", x.priority, -x.size_bytes))
    return items


# ==============================================================
# CSV 解析
# ==============================================================

def _detect_encoding(path: str) -> str:
    """WizTree 默认导出 UTF-8 BOM，但中文系统上也可能遇到 GBK"""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            sample = head + f.read(8192)
    except OSError:
        return "utf-8-sig"

    if sample.startswith(b"\xff\xfe") or sample.startswith(b"\xfe\xff"):
        return "utf-16"

    # 去掉末尾几个字节，避免正好截断在多字节字符中间导致误判
    probe = sample[:-4] if len(sample) > 16 else sample
    try:
        probe.decode("utf-8-sig")
        return "utf-8-sig"
    except UnicodeDecodeError:
        pass
    try:
        probe.decode("gbk")
        return "gbk"
    except UnicodeDecodeError:
        pass
    # 两种都不行时按系统 ANSI 读取，并使用 errors=replace 兜底
    return "mbcs"


def _safe_int(value: str) -> int:
    try:
        return int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0


def parse_csv(csv_path: str,
              big_file_min_bytes: int = BIG_FILE_MIN_BYTES,
              big_file_limit: int = BIG_FILE_LIMIT
              ) -> Tuple[Optional[DiskInfo], List[FolderEntry], List[FileEntry]]:
    """
    流式解析 WizTree CSV（不把整个文件读进内存）。

    CSV 结构：
      第 1 行  软件信息
      第 2 行  列标题（文件名称,大小,分配,修改时间,属性,文件,文件夹,
                        DRIVECAPACITY,FREESPACE,USEDSPACE,RESERVEDSPACE）
      第 3 行  根目录行（带磁盘容量）
      之后     文件夹行（以 \\ 结尾）与文件行
    """
    if not os.path.isfile(csv_path):
        return None, [], []

    encoding = _detect_encoding(csv_path)
    folders: List[FolderEntry] = []
    files: List[FileEntry] = []
    disk_info: Optional[DiskInfo] = None
    scan_time = ""

    try:
        with open(csv_path, "r", encoding=encoding, errors="replace", newline="") as f:
            scan_time = f.readline().strip()
            f.readline()  # 列标题

            reader = csv.reader(f)
            for row_num, row in enumerate(reader):
                if len(row) < 7:
                    continue
                raw_path = row[0].strip().strip('"')
                if not raw_path:
                    continue

                size = _safe_int(row[1])
                modified = row[3].strip() if len(row) > 3 else ""
                file_count = _safe_int(row[5]) if len(row) > 5 else 0
                folder_count = _safe_int(row[6]) if len(row) > 6 else 0

                # 根目录行：带容量字段
                if row_num == 0:
                    if len(row) >= 10:
                        total = _safe_int(row[7])
                        free = _safe_int(row[8])
                        used = _safe_int(row[9])
                        if total > 0:
                            disk_info = DiskInfo(total, used, free, scan_time)
                    continue

                if raw_path.endswith("\\"):
                    folders.append(FolderEntry(
                        path=raw_path, size_bytes=size, modified=modified,
                        file_count=file_count, folder_count=folder_count))
                elif size >= big_file_min_bytes and len(files) < big_file_limit:
                    files.append(FileEntry(path=raw_path, size_bytes=size,
                                           modified=modified))
    except (OSError, UnicodeError) as e:
        print("[分析] 读取 CSV 失败：%s" % e)
        return None, [], []

    return disk_info, folders, files


# ==============================================================
# 统计
# ==============================================================

def get_top_folders(folders: Sequence[FolderEntry], depth: int = 1,
                    top_n: int = 15) -> List[FolderEntry]:
    filtered = [f for f in folders if f.depth == depth and f.size_bytes > 0]
    filtered.sort(key=lambda x: x.size_bytes, reverse=True)
    return filtered[:top_n]


def build_system_items(files: Sequence[FileEntry]) -> List[CleanupItem]:
    """从文件清单里识别休眠/页面文件等，生成只读建议"""
    items: List[CleanupItem] = []
    system_drive = os.environ.get("SystemDrive", "C:").upper()
    for entry in files:
        name = entry.name.lower()
        parent = os.path.dirname(entry.path).rstrip("\\").upper()
        if parent != system_drive:
            continue
        if name == "hiberfil.sys":
            items.append(CleanupItem(
                path=entry.path, size_bytes=entry.size_bytes, priority=3,
                name="休眠文件（hiberfil.sys）",
                reason="系统休眠时保存内存镜像的文件，大小通常等于物理内存的 40%~100%。"
                       "如果从不使用「休眠」功能，可以关闭它来释放空间。",
                method="管理员运行：powercfg /h off（关闭休眠）",
                risk="🟡 会关闭休眠与快速启动，可随时重新开启",
                action="guide", target="files", requires_admin=True))
        elif name == "pagefile.sys":
            items.append(CleanupItem(
                path=entry.path, size_bytes=entry.size_bytes, priority=3,
                name="虚拟内存文件（pagefile.sys）",
                reason="系统页面文件大小通常为内存的 1~1.5 倍。可以把它移到 D 盘，"
                       "但不建议直接删除。",
                method="系统属性 → 高级 → 性能设置 → 高级 → 虚拟内存 → 更改到 D 盘",
                risk="⚠️ 操作不当可能影响系统稳定性",
                action="guide", target="files", requires_admin=True))
        elif name == "swapfile.sys":
            items.append(CleanupItem(
                path=entry.path, size_bytes=entry.size_bytes, priority=3,
                name="交换文件（swapfile.sys）",
                reason="供 UWP 应用使用的交换文件，大小通常较小，由系统管理。",
                method="无需手动处理；关闭休眠后可能一并缩小",
                risk="✅ 不建议手动删除", action="guide", target="files"))
    return items


def run_analysis(csv_path: str) -> dict:
    """
    完整分析流程，返回：
      disk_info / top_level1 / top_appdata_local / top_appdata_roaming /
      cleanup_items（可执行）/ guide_items（只建议）/ big_files /
      total_reclaimable_bytes / backup_bytes / error
    """
    disk_info, folders, files = parse_csv(csv_path)

    if not folders:
        return {"error": "无法从 CSV 中解析出文件夹数据。\n"
                         "请确认文件是 WizTree 导出的 CSV，且扫描没有中途中断。",
                "disk_info": disk_info}

    top_level1 = get_top_folders(folders, depth=1, top_n=15)

    local_folders = [f for f in folders if re.search(
        r"AppData\\Local\\[^\\]+\\?$", f.path, re.IGNORECASE)]
    local_folders.sort(key=lambda x: x.size_bytes, reverse=True)

    roaming_folders = [f for f in folders if re.search(
        r"AppData\\Roaming\\[^\\]+\\?$", f.path, re.IGNORECASE)]
    roaming_folders.sort(key=lambda x: x.size_bytes, reverse=True)

    all_items = apply_cleanup_rules(folders)
    cleanup_items = [i for i in all_items if i.action != "guide"]
    guide_items = [i for i in all_items if i.action == "guide"]
    guide_items.extend(build_system_items(files))

    reclaimable = sum(i.size_bytes for i in cleanup_items
                      if i.action == "delete" and i.priority <= 2)
    backup_bytes = sum(i.size_bytes for i in cleanup_items if i.action == "backup")

    big_files = sorted(files, key=lambda x: x.size_bytes, reverse=True)[:15]

    return {
        "disk_info": disk_info,
        "top_level1": top_level1[:10],
        "top_appdata_local": local_folders[:15],
        "top_appdata_roaming": roaming_folders[:15],
        "cleanup_items": cleanup_items,
        "guide_items": guide_items,
        "big_files": big_files,
        "total_reclaimable_bytes": reclaimable,
        "backup_bytes": backup_bytes,
        "error": None,
    }

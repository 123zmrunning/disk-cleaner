# ==============================================================
# analyzer.py —— 磁盘数据分析引擎
# ==============================================================
# 【作用】读取WizTree导出的CSV文件，解析出每个文件夹的大小，
#         然后用内置的"清理规则"判断哪些文件夹可以清理。
#
# 【最重要的部分】CLEANUP_RULES 列表 —— 这就是软件判断
#   "哪些能清理、为什么能清理"的全部依据。
#   每条规则包含：匹配模式、清理理由、操作方法、风险评估。
#   如果你想添加新的清理规则，只需在这个列表里加一项即可。
# ==============================================================

import csv
import re
import os
from dataclasses import dataclass   # dataclass让定义数据结构的代码更简洁
from typing import List, Optional, Tuple
from datetime import datetime

# ── 单位换算常数 ────────────────────────────────────────────────
GB = 1_073_741_824   # 1 GB = 2^30 字节（注意：1GB≠10亿字节）
MB = 1_048_576       # 1 MB = 2^20 字节


# ==============================================================
# 数据结构定义
# @dataclass 是Python的装饰器，自动为类生成 __init__、__repr__ 等方法
# 把它理解为"有字段的结构体"即可
# ==============================================================

@dataclass
class DiskInfo:
    """磁盘总体信息（从CSV第3行的根目录条目中读取）"""
    total_bytes: int    # 总容量（字节）
    used_bytes: int     # 已用空间
    free_bytes: int     # 剩余空间
    scan_time: str      # 扫描时间（从CSV第1行读取）

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
    """CSV中一行文件夹记录"""
    path: str           # 文件夹路径
    size_bytes: int     # 大小（字节）
    modified: str       # 最后修改时间

    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / GB, 2)

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / MB, 1)

    @property
    def depth(self) -> int:
        """路径深度：C:\\xxx\\ 是1级，C:\\xxx\\yyy\\ 是2级"""
        # 去掉开头的盘符，数反斜杠个数
        parts = self.path.strip('"').rstrip('\\').split('\\')
        return len(parts) - 1   # 减去盘符那一级


@dataclass
class CleanupItem:
    """一个清理建议项"""
    path: str           # 文件夹路径
    size_bytes: int     # 大小（字节）
    priority: int       # 优先级 1=立即清理 2=高收益缓存 3=游戏缓存 4=谨慎操作
    name: str           # 清理项名称
    reason: str         # 判断依据（为什么可以清理）
    method: str         # 操作方法
    risk: str           # 风险评估

    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / GB, 2)

    @property
    def priority_label(self) -> str:
        labels = {1: "🔴 立即清理", 2: "🟠 高收益缓存", 3: "🟡 游戏缓存", 4: "🟢 谨慎操作"}
        return labels.get(self.priority, "未知")


# ==============================================================
# 清理规则定义 —— 软件的判断核心
# ==============================================================
# 每条规则是一个字典，字段含义：
#   pattern  : 正则表达式，用于匹配文件夹路径（不区分大小写）
#   priority : 优先级 1~4（数字越小越建议清理）
#   name     : 显示给用户的清理项名称
#   reason   : 为什么可以清理——这是最重要的，让用户理解判断依据
#   method   : 推荐的操作方式
#   risk     : 风险说明
#
# 【正则表达式简单说明】
#   \\  代表一个反斜杠 \（在Python字符串里反斜杠要写两个）
#   .   匹配任意字符
#   .*  匹配任意数量的任意字符（零个或更多）
#   不懂正则也没关系，把它理解为"模糊匹配路径中包含某段文字"即可
# ==============================================================
# 知识库来源及更新日期（联网核实）
KNOWLEDGE_DATE = "2026-05-26"
KNOWLEDGE_SOURCES = (
    "Microsoft Learn · ElevenForum · MakeUseOf · GeekChamp · WindowsForum（2025-2026年核实）\n"
    "核实结论：WinSxS 不可直接删除；Prefetch 可删但影响启动速度；\n"
    "AMD/NVIDIA GPU 缓存、D3DSCache、缩略图缓存、WER 转储均可安全删除。"
)

CLEANUP_RULES = [
    # ── 优先级1：立即清理（无风险）────────────────────────────────
    {
        "pattern": r"\\NVIDIA\\DXCache",
        "priority": 1,
        "name": "NVIDIA GPU 着色器缓存（DXCache）",
        "reason": (
            "【是什么】DirectX着色器编译缓存。GPU运行游戏/3D软件时，会把着色器程序编译成\n"
            "适合本机显卡的格式并缓存，避免每次都重新编译。\n"
            "【为何可删】删除后GPU会在下次运行时自动重新生成，完全无副作用。\n"
            "这类缓存随着游戏和驱动更新会不断积累，定期清理很有必要。"
        ),
        "method": "直接删除整个文件夹",
        "risk": "✅ 无风险（会自动重建）",
    },
    {
        "pattern": r"\\NVIDIA\\GLCache",
        "priority": 1,
        "name": "NVIDIA OpenGL 着色器缓存（GLCache）",
        "reason": (
            "【是什么】OpenGL着色器编译缓存，原理与DXCache完全相同。\n"
            "【为何可删】同样会自动重建，积累量较小但也可以清理。"
        ),
        "method": "直接删除整个文件夹",
        "risk": "✅ 无风险（会自动重建）",
    },
    {
        "pattern": r"^\"?C:\\ESD",
        "priority": 1,
        "name": "Windows 系统升级源文件（C:\\ESD）",
        "reason": (
            "【是什么】Windows进行大版本升级（如Win10→Win11）时下载到本地的安装包。\n"
            "【为何可删】升级完成后这些文件完全没用了，是系统遗留的「装修废料」。\n"
            "修改时间越久（超过半年），越可以放心删除。"
        ),
        "method": "直接删除整个C:\\ESD文件夹，或通过「磁盘清理→清理系统文件」删除",
        "risk": "✅ 无风险（不影响当前系统）",
    },
    {
        "pattern": r"\\AppData\\Local\\Temp",
        "priority": 1,
        "name": "系统临时文件（Temp）",
        "reason": (
            "【是什么】系统和各软件运行时产生的临时文件，正常情况下软件退出后会自动删除。\n"
            "【为何可删】软件崩溃或异常退出后遗留的残骸。长期积累会占用大量空间。\n"
            "正在使用的临时文件会被系统锁定，删除时会自动跳过，不会影响正在运行的程序。"
        ),
        "method": "删除文件夹内的所有内容（不要删除Temp文件夹本身）",
        "risk": "✅ 无风险（被占用的文件会自动跳过）",
    },
    {
        "pattern": r"C:\\Windows\.old",
        "priority": 1,
        "name": "旧版 Windows 备份（Windows.old）",
        "reason": (
            "【是什么】Windows大版本升级时，系统自动保留的旧版本备份，用于回滚。\n"
            "【为何可删】Windows只会保留10天的回滚窗口，超过后这个备份已无法使用。\n"
            "如果你已经稳定运行新版本超过一个月，完全可以删除。"
        ),
        "method": "控制面板 → 磁盘清理 → 清理系统文件 → 以前的Windows安装",
        "risk": "⚠️ 删除后无法回滚到旧版Windows（但新系统完全不受影响）",
    },

    # AMD GPU 缓存（联网核实：原理同 NVIDIA，安全删除）
    {
        "pattern": r"\\AMD\\DxCache",
        "priority": 1,
        "name": "AMD GPU 着色器缓存（DxCache）",
        "reason": (
            "【是什么】AMD 显卡的 DirectX 着色器编译缓存，原理与 NVIDIA DXCache 完全相同。\n"
            "【为何可删】删除后 GPU 下次运行时自动重新生成，无任何副作用。\n"
            "来源：AMD 官方 & Microsoft Learn（2025年联网核实）"
        ),
        "method": "直接删除整个文件夹",
        "risk": "✅ 无风险（会自动重建）",
    },
    {
        "pattern": r"\\AMD\\GLCache",
        "priority": 1,
        "name": "AMD OpenGL 着色器缓存（GLCache）",
        "reason": "AMD 显卡 OpenGL 着色器缓存，原理同 DxCache，删除后自动重建。\n来源：AMD 官方（2025年联网核实）",
        "method": "直接删除整个文件夹",
        "risk": "✅ 无风险（会自动重建）",
    },
    # DirectX 系统级着色器缓存（联网核实）
    {
        "pattern": r"\\Local\\D3DSCache",
        "priority": 1,
        "name": "DirectX 系统着色器缓存（D3DSCache）",
        "reason": (
            "【是什么】Windows 系统级的 DirectX 着色器缓存，独立于显卡驱动缓存。\n"
            "【为何可删】经 Microsoft Q&A 与多个技术社区证实，删除后自动重建。\n"
            "驱动或 Windows 更新后清理此目录可解决部分图形异常。\n"
            "来源：Microsoft Q&A、GeekChamp（2025年联网核实）"
        ),
        "method": "直接删除整个文件夹",
        "risk": "✅ 无风险（会自动重建）",
    },
    # 系统缩略图缓存（联网核实）
    {
        "pattern": r"\\Microsoft\\Windows\\Explorer",
        "priority": 1,
        "name": "系统缩略图缓存（Thumbnail Cache）",
        "reason": (
            "【是什么】Windows 资源管理器为图片、视频等文件生成的预览缩略图（thumbcache_*.db）。\n"
            "【为何可删】删除后 Windows 在下次打开文件夹时自动重新生成，不影响任何文件。\n"
            "长期积累可达数 GB，是无痛清理的好目标。\n"
            "来源：ElevenForum、WinAero（2025年联网核实）"
        ),
        "method": "直接删除整个文件夹（会自动重建）",
        "risk": "✅ 无风险（会自动重建）",
    },
    # Windows 错误报告转储（联网核实）
    {
        "pattern": r"\\WER\\Report",
        "priority": 1,
        "name": "Windows 错误报告转储（WER）",
        "reason": (
            "【是什么】程序崩溃时 Windows 自动生成的错误报告和内存转储文件（.dmp），\n"
            "用于微软收集诊断信息，普通用户几乎用不到。\n"
            "【为何可删】这些文件已上报或从未被查阅，删除不影响系统运行。\n"
            "来源：Microsoft Learn、WindowsForum（2025年联网核实）"
        ),
        "method": "直接删除文件夹内容",
        "risk": "✅ 无风险（仅调试信息）",
    },

    # ── 优先级2：高收益缓存（低风险）──────────────────────────────
    # Windows 预读缓存（联网核实：可删但有轻微代价）
    {
        "pattern": r"C:\\Windows\\Prefetch",
        "priority": 2,
        "name": "Windows 预读缓存（Prefetch）",
        "reason": (
            "【是什么】Windows 为加速程序启动而预先读取并缓存的数据，文件扩展名 .pf。\n"
            "【为何可删】可以安全删除，Windows 会自动重新建立。\n"
            "⚠️ 注意：删除后头 1~2 次开机和程序启动会稍慢（之后恢复正常）。\n"
            "不推荐作为常规操作，只在确实需要腾出空间时清理。\n"
            "来源：Microsoft Q&A、EaseUS（2025年联网核实）"
        ),
        "method": "删除文件夹内的 .pf 文件（不要删除 Prefetch 文件夹本身）",
        "risk": "🟡 低风险（删后头1~2次启动速度暂时变慢）",
    },
    {
        "pattern": r"\\JianyingPro\\",
        "priority": 2,
        "name": "剪映视频编辑缓存（JianyingPro）",
        "reason": (
            "【是什么】剪映在编辑视频时产生的预览缓存、素材缓存、代理文件等。\n"
            "【为何可删】已导出的成品视频不受影响；但删除后，重新打开未导出的\n"
            "项目时剪映需要重新生成预览，可能需要等待几分钟。"
        ),
        "method": "推荐：剪映 → 设置 → 缓存管理 → 清除缓存（更安全）",
        "risk": "🟡 低风险（已导出视频不受影响，未完成项目需重新生成预览）",
    },
    {
        "pattern": r"\\Mozilla\\",
        "priority": 2,
        "name": "Firefox 浏览器缓存（Mozilla）",
        "reason": (
            "【是什么】Firefox为加速网页加载而保存的本地副本（HTML、图片、视频等）。\n"
            "【为何可删】缓存只是加速用的，删除后浏览器会重新从网站下载，速度暂时慢一点。\n"
            "书签、历史记录、密码完全不受影响。"
        ),
        "method": "Firefox → 设置 → 隐私与安全 → 清除数据",
        "risk": "✅ 无风险（书签/历史/密码不受影响）",
    },
    {
        "pattern": r"\\Google\\Chrome\\.*\\Cache",
        "priority": 2,
        "name": "Chrome 浏览器缓存",
        "reason": (
            "【是什么】Chrome浏览器的网页缓存，原理与Firefox相同。\n"
            "【为何可删】仅影响下次访问已缓存网页的加载速度，个人数据不受影响。"
        ),
        "method": "Chrome → 设置 → 隐私安全 → 清除浏览数据",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\\Microsoft\\Edge\\.*\\Cache",
        "priority": 2,
        "name": "Edge 浏览器缓存",
        "reason": "Edge浏览器网页缓存，原理同Chrome。",
        "method": "Edge → 设置 → 隐私搜索 → 清除浏览数据",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\\KOOK\\",
        "priority": 2,
        "name": "KOOK 语音应用缓存",
        "reason": (
            "【是什么】KOOK（原网易云音乐旗下的语音软件）缓存的头像、表情、媒体文件等。\n"
            "【为何可删】这些资源都存储在服务器，重新打开KOOK会自动重新下载。\n"
            "聊天记录存在服务器端，不受影响。"
        ),
        "method": "直接删除整个文件夹",
        "risk": "✅ 无风险（数据存储在云端）",
    },
    {
        "pattern": r"\\Roaming\\baidu\\",
        "priority": 2,
        "name": "百度系应用缓存（baidu）",
        "reason": (
            "【是什么】百度输入法、百度浏览器等百度系软件产生的缓存数据。\n"
            "【为何可删】以缓存为主，重新打开对应软件会重新生成。"
        ),
        "method": "直接删除",
        "risk": "🟡 低风险",
    },
    {
        "pattern": r"\\Roaming\\douyin\\",
        "priority": 2,
        "name": "抖音 PC 客户端缓存",
        "reason": (
            "【是什么】抖音PC版缓存的视频封面、评论图片等本地数据。\n"
            "【为何可删】内容均来自云端，删除后重新打开抖音会重新加载。"
        ),
        "method": "直接删除",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"app_shell_cache",
        "priority": 2,
        "name": "Electron 应用 Shell 缓存",
        "reason": (
            "【是什么】基于Electron框架开发的应用（如KOOK、钉钉、飞书等）产生的\n"
            "界面渲染缓存，用于加快软件启动速度。\n"
            "【为何可删】应用重新打开时会自动重建，不影响软件功能。"
        ),
        "method": "直接删除",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\\Razer\\",
        "priority": 2,
        "name": "Razer 雷蛇软件缓存",
        "reason": (
            "【是什么】Razer Synapse等雷蛇外设管理软件产生的日志和缓存文件。\n"
            "【为何可删】主要是日志文件，对外设正常使用无影响。"
        ),
        "method": "直接删除缓存和日志子文件夹",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\\baidunetdisk\\",
        "priority": 2,
        "name": "百度网盘本地缓存",
        "reason": (
            "【是什么】百度网盘下载文件时产生的临时缓存，不是你实际保存的文件。\n"
            "【为何可删】实际文件存储在云端或你指定的下载文件夹，这里只是缓存。"
        ),
        "method": "直接删除，或通过百度网盘→设置→清除缓存",
        "risk": "🟡 低风险（确认你的文件在云端已备份）",
    },

    # ── 优先级3：游戏着色器缓存（需重新编译）──────────────────────
    {
        "pattern": r"\\BattlefieldGameData",
        "priority": 3,
        "name": "战地系列游戏 Shader 缓存",
        "reason": (
            "【是什么】战地游戏（Battlefield）预编译的图形着色器缓存。\n"
            "游戏第一次运行时需要花较长时间编译着色器，之后把结果缓存在这里，\n"
            "下次进入游戏就不需要重新编译了。\n"
            "【为何可删】可以删除，但下次启动游戏时会触发一次重新编译（约5~20分钟）。\n"
            "建议在不打算近期玩游戏时再清理。"
        ),
        "method": "直接删除",
        "risk": "🟡 中等（下次启动游戏需重新编译Shader，耗时较长）",
    },
    {
        "pattern": r"\\TslGame\\",
        "priority": 3,
        "name": "PUBG 绝地求生 Shader 缓存",
        "reason": (
            "【是什么】PUBG（绝地求生）的图形着色器缓存，原理与战地游戏相同。\n"
            "【为何可删】同样可以删除，下次进入游戏会重新编译。"
        ),
        "method": "直接删除",
        "risk": "🟡 中等（下次启动游戏需重新编译Shader）",
    },

    # ── 优先级4：谨慎操作（中风险，建议用软件自带清理）──────────
    {
        "pattern": r"\\AppData\\Roaming\\Tencent\\",
        "priority": 4,
        "name": "腾讯软件数据（微信/QQ）",
        "reason": (
            "【是什么】微信、QQ、腾讯会议等软件的本地数据，可能包含：\n"
            "  · 聊天图片/视频/文件的本地缓存\n"
            "  · 部分聊天记录（如果未开启云同步）\n"
            "  · 软件缓存和临时文件\n"
            "【为何谨慎】直接删除整个文件夹有可能丢失本地聊天记录！\n"
            "建议先通过软件内的「清理存储空间」功能，它只清理缓存，不动记录。"
        ),
        "method": "微信：设置 → 聊天 → 清理微信存储空间\nQQ：设置 → 基本设置 → 清理空间",
        "risk": "⚠️ 中等风险（可能含本地聊天记录，请先用软件自带清理功能）",
    },
    {
        "pattern": r"\\AppData\\Local\\Tencent\\",
        "priority": 4,
        "name": "腾讯软件本地缓存（Local）",
        "reason": (
            "【是什么】腾讯系软件的本地缓存（头像、表情包、文件缓存等）。\n"
            "相比Roaming目录，这里的内容更多是缓存，聊天记录风险较低。\n"
            "【为何谨慎】部分用户的微信文件下载可能在此目录，建议先检查再删。"
        ),
        "method": "同上，建议通过软件自带功能清理",
        "risk": "🟡 低-中风险",
    },

    # ── 优先级1新增：主流应用缓存（无风险）─────────────────────────
    {
        "pattern": r"\\discord\\[Cc]ache",
        "priority": 1,
        "name": "Discord 聊天缓存",
        "reason": "Discord缓存的头像、图片、视频等，删除后自动重建。",
        "method": "直接删除",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\\Spotify\\[Ss]torage",
        "priority": 1,
        "name": "Spotify 音乐缓存",
        "reason": "Spotify离线缓存文件，删除后重新播放时自动下载。",
        "method": "直接删除",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\\steam\\htmlcache",
        "priority": 1,
        "name": "Steam 内置浏览器缓存",
        "reason": "Steam客户端内置Chromium浏览器的缓存文件。",
        "method": "直接删除",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\\EpicGamesLauncher\\.*[Cc]ache",
        "priority": 1,
        "name": "Epic Games 启动器缓存",
        "reason": "Epic Games启动器缓存，删除后自动重建。",
        "method": "直接删除",
        "risk": "✅ 无风险",
    },

    # ── 优先级2新增：各类软件缓存（低风险）─────────────────────────
    {
        "pattern": r"\\Steam\\steamapps\\downloading",
        "priority": 2,
        "name": "Steam 游戏下载临时文件",
        "reason": "Steam正在下载但未完成的游戏文件，删除后需重新下载。",
        "method": "通过Steam暂停并取消下载后删除",
        "risk": "🟡 低风险（取消下载后才可删）",
    },
    {
        "pattern": r"\\Steam\\steamapps\\workshop\\downloads",
        "priority": 2,
        "name": "Steam Workshop 下载缓存",
        "reason": "Steam创意工坊的下载缓存，删除后可重新订阅下载。",
        "method": "直接删除",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\\Code\\CachedData",
        "priority": 2,
        "name": "VS Code 扩展缓存（CachedData）",
        "reason": "VS Code扩展的编译缓存，删除后首次启动会重新生成，稍慢。",
        "method": "直接删除",
        "risk": "✅ 无风险（会自动重建）",
    },
    {
        "pattern": r"\\Code\\logs",
        "priority": 2,
        "name": "VS Code 运行日志",
        "reason": "VS Code产生的运行日志文件，对日常使用无用。",
        "method": "直接删除",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\.IntelliJIdea.*\\system\\caches|\.idea\\.*[Cc]ache|\\JetBrains\\.*\\caches",
        "priority": 2,
        "name": "JetBrains IDE 缓存",
        "reason": "IntelliJ IDEA、PyCharm、WebStorm等JetBrains系IDE的索引缓存，删除后重启IDE自动重建（会花几分钟重建索引）。",
        "method": "直接删除，或通过IDE菜单 File→Invalidate Caches",
        "risk": "🟡 低风险（重启IDE需重建索引）",
    },
    {
        "pattern": r"\\Adobe\\.*[Cc]ache|\\Adobe\\.*[Tt]emp",
        "priority": 2,
        "name": "Adobe 软件缓存",
        "reason": "Photoshop、Premiere、After Effects等Adobe软件的临时缓存文件。",
        "method": "各软件设置内清除，或直接删除",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\\Adobe Media Cache",
        "priority": 2,
        "name": "Adobe Media Cache（视频预览缓存）",
        "reason": "Premiere Pro为视频文件生成的预览缓存，删除后Premiere重新处理时自动重建（会花时间）。",
        "method": "Premiere Pro→首选项→媒体→清除媒体缓存文件",
        "risk": "🟡 低风险（重新打开项目需等待重建）",
    },
    {
        "pattern": r"\\Tencent\\QQMusic",
        "priority": 2,
        "name": "QQ音乐缓存",
        "reason": "QQ音乐缓存的音乐文件封面等，删除后重新播放时自动下载。",
        "method": "直接删除",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\\netease\\cloudmusic\\.*[Cc]ache",
        "priority": 2,
        "name": "网易云音乐缓存",
        "reason": "网易云音乐本地缓存，删除后重新播放时自动下载。",
        "method": "直接删除或通过网易云音乐设置→清除缓存",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\\DingTalk\\.*[Cc]ache",
        "priority": 2,
        "name": "钉钉缓存",
        "reason": "钉钉客户端缓存的头像、文件预览等，删除后自动重新下载。",
        "method": "直接删除",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\\Lark\\.*[Cc]ache|\\feishu\\.*[Cc]ache",
        "priority": 2,
        "name": "飞书缓存",
        "reason": "飞书客户端缓存，删除后自动重建。",
        "method": "直接删除",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\\Microsoft\\Teams\\.*[Cc]ache",
        "priority": 2,
        "name": "Microsoft Teams 缓存",
        "reason": "Teams客户端缓存，积累较快，删除后自动重建。",
        "method": "直接删除",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\\AppData\\Roaming\\Zoom\\data",
        "priority": 2,
        "name": "Zoom 会议缓存",
        "reason": "Zoom缓存的会议背景、头像等数据。",
        "method": "直接删除",
        "risk": "✅ 无风险",
    },
    {
        "pattern": r"\.npm\\_npx|npm-cache",
        "priority": 2,
        "name": "npm 包管理器缓存",
        "reason": "npm下载包时产生的本地缓存，删除后npm重新下载时自动重建。",
        "method": "`npm cache clean --force` 或直接删除",
        "risk": "✅ 无风险",
    },

    # ── 优先级3新增：游戏缓存（中等影响）───────────────────────────
    {
        "pattern": r"\\League of Legends\\.*[Cc]ache",
        "priority": 3,
        "name": "英雄联盟游戏缓存",
        "reason": "英雄联盟缓存的游戏资源，删除后下次启动游戏重新下载（时间较长）。",
        "method": "直接删除",
        "risk": "🟡 中等（下次启动需重新下载资源）",
    },
    {
        "pattern": r"\.minecraft\\.*[Cc]ache|\\minecraft\\.*[Cc]ache",
        "priority": 3,
        "name": "Minecraft 游戏缓存",
        "reason": "Minecraft的着色器和资源缓存。",
        "method": "直接删除",
        "risk": "🟡 低风险",
    },

    # ── 优先级4新增：实际文件（高风险，建议迁移而非删除）──────────
    {
        "pattern": r"\\WeChat Files\\.*\\FileStorage\\File",
        "priority": 4,
        "name": "微信接收文件（建议移至D盘）",
        "reason": "微信聊天中接收的文件。注意：这是实际文件，不是缓存！删除后无法恢复。建议移动到D盘而非删除。",
        "method": "微信→设置→聊天→迁移聊天记录，或手动移动文件夹到D盘",
        "risk": "⚠️ 高风险（实际文件，删除不可恢复，建议移动而非删除）",
    },
]


# ==============================================================
# 核心分析函数
# ==============================================================

def parse_csv(csv_path: str) -> Tuple[Optional[DiskInfo], List[FolderEntry]]:
    """
    解析WizTree导出的CSV文件。

    CSV文件结构（重要）：
      第1行：软件信息，如"生成由 WizTree 4.25 2026/5/24 ..."
      第2行：列标题，如"文件名称,大小,分配,修改时间,属性,文件,文件夹,DRIVECAPACITY..."
      第3行：C:\根目录，包含磁盘总容量等信息
      第4行起：各子文件夹数据

    返回值：(磁盘信息, 文件夹列表)
    """
    if not os.path.isfile(csv_path):
        return None, []

    folders = []
    disk_info = None
    scan_time = ""

    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            # utf-8-sig 会自动处理BOM（字节顺序标记），避免第一行有乱码

            lines = f.readlines()
            if len(lines) < 3:
                return None, []

            # 第1行：提取扫描时间
            # 格式例：生成由 WizTree 4.25 2026/5/24 17:03:54 ...
            first_line = lines[0].strip()
            scan_time = first_line

            # 从第3行开始用csv.reader解析（跳过第1行信息行和第2行标题行）
            reader = csv.reader(lines[2:])

            for row_num, row in enumerate(reader):
                if len(row) < 4:
                    continue   # 跳过字段不足的行

                path = row[0].strip('"')   # 去掉路径两端的引号

                # 安全地把字符串转成整数（失败时用0）
                try:
                    size_bytes = int(row[1]) if row[1].strip() else 0
                except ValueError:
                    size_bytes = 0

                modified = row[3] if len(row) > 3 else ""

                # 第1行（row_num==0）是C:\根目录，包含磁盘总体信息
                if row_num == 0 and len(row) >= 10:
                    try:
                        total = int(row[7]) if row[7].strip() else 0
                        free = int(row[8]) if row[8].strip() else 0
                        used = int(row[9]) if row[9].strip() else 0
                        disk_info = DiskInfo(
                            total_bytes=total,
                            used_bytes=used,
                            free_bytes=free,
                            scan_time=scan_time,
                        )
                    except (ValueError, IndexError):
                        pass
                    continue   # 根目录行不加入文件夹列表

                # 只保留有实际大小的文件夹条目
                if size_bytes > 0:
                    folders.append(FolderEntry(
                        path=path,
                        size_bytes=size_bytes,
                        modified=modified,
                    ))

    except (IOError, UnicodeDecodeError) as e:
        print(f"[分析] 读取CSV失败：{e}")
        return None, []

    return disk_info, folders


def get_top_folders(folders: List[FolderEntry], depth: int = 1,
                    top_n: int = 15) -> List[FolderEntry]:
    """
    获取指定路径深度下占用空间最大的文件夹。
    depth：路径深度（1=C:\\xxx，2=C:\\xxx\\yyy）
    top_n：返回前几名
    """
    filtered = [f for f in folders if f.depth == depth]
    filtered.sort(key=lambda x: x.size_bytes, reverse=True)
    return filtered[:top_n]


def apply_cleanup_rules(folders: List[FolderEntry]) -> List[CleanupItem]:
    """
    对所有文件夹应用清理规则，找出符合条件的清理项。
    每个文件夹只匹配第一条符合的规则（避免重复显示）。

    返回值：CleanupItem列表，按优先级排序
    """
    results = []
    matched_paths = set()   # 记录已匹配的完整路径，避免重复

    def is_child_of_matched(path: str) -> bool:
        """检查某路径是否是已匹配路径的子目录（避免父子目录重复统计）"""
        normalized = path.rstrip("\\") + "\\"
        for matched in matched_paths:
            parent = matched.rstrip("\\") + "\\"
            if normalized.startswith(parent) and normalized != parent:
                return True
        return False

    for rule in CLEANUP_RULES:
        pattern = re.compile(rule["pattern"], re.IGNORECASE)
        # re.IGNORECASE 让匹配不区分大小写，
        # 比如"cache"和"Cache"和"CACHE"都能匹配

        for folder in folders:
            # 跳过已经精确匹配过的路径
            if folder.path in matched_paths:
                continue

            # 跳过太小的目录（小于5MB，不值得清理）
            if folder.size_bytes < 5 * MB:
                continue

            # 跳过已匹配目录的���目录（避免 C:\ESD\ 和 C:\ESD\Windows\ 都出现）
            if is_child_of_matched(folder.path):
                continue

            if pattern.search(folder.path):
                results.append(CleanupItem(
                    path=folder.path,
                    size_bytes=folder.size_bytes,
                    priority=rule["priority"],
                    name=rule["name"],
                    reason=rule["reason"],
                    method=rule["method"],
                    risk=rule["risk"],
                ))
                matched_paths.add(folder.path)

    # 按优先级排序，同优先级内按大小排序
    results.sort(key=lambda x: (x.priority, -x.size_bytes))
    return results


def run_analysis(csv_path: str) -> dict:
    """
    完整分析流程的入口函数。
    接受CSV路径，返回包含所有分析结果的字典。

    返回的字典结构：
    {
        "disk_info": DiskInfo 或 None,
        "top_level1": [FolderEntry, ...],   # C:\ 下一级目录
        "top_appdata_local": [...],          # AppData\Local 下子目录
        "top_appdata_roaming": [...],        # AppData\Roaming 下子目录
        "cleanup_items": [CleanupItem, ...], # 清理建议列表
        "total_reclaimable_bytes": int,      # 可回收的总字节数
        "error": str 或 None,               # 错误信息
    }
    """
    disk_info, folders = parse_csv(csv_path)

    if not folders:
        return {"error": "无法解析CSV文件，或文件为空。", "disk_info": None}

    # 获取各层级的大目录
    top_level1 = get_top_folders(folders, depth=1, top_n=15)

    # AppData\Local 和 Roaming 的子目录（深度3：C:\Users\用户名\AppData\Local\xxx）
    local_folders = [f for f in folders if
                     re.search(r"AppData\\Local\\[^\\]+$", f.path, re.IGNORECASE)]
    local_folders.sort(key=lambda x: x.size_bytes, reverse=True)

    roaming_folders = [f for f in folders if
                       re.search(r"AppData\\Roaming\\[^\\]+$", f.path, re.IGNORECASE)]
    roaming_folders.sort(key=lambda x: x.size_bytes, reverse=True)

    # 应用清理规则
    cleanup_items = apply_cleanup_rules(folders)

    # 计算可回收空间总量（只算优先级1和2的无风险项）
    reclaimable = sum(
        item.size_bytes for item in cleanup_items if item.priority <= 2
    )

    return {
        "disk_info": disk_info,
        "top_level1": top_level1[:10],
        "top_appdata_local": local_folders[:15],
        "top_appdata_roaming": roaming_folders[:15],
        "cleanup_items": cleanup_items,
        "total_reclaimable_bytes": reclaimable,
        "error": None,
    }

# ==============================================================
# reporter.py —— Markdown 报告生成
# ==============================================================
# 报告只做展示，不执行任何删除。分四类内容：
#   1. 磁盘概况与占用排行
#   2. 可执行清理（delete / backup）
#   3. 只建议（guide：WinSxS、Installer、休眠文件…）
#   4. 长期治理建议
# ==============================================================

import os
import time
from typing import List, Optional

from analyzer import CleanupItem, DiskInfo, GB
from config import get_desktop_path


def bytes_to_human(b: int) -> str:
    """字节数转人类易读格式"""
    if b >= GB:
        return "%.2f GB" % (b / GB)
    if b >= 1_048_576:
        return "%.1f MB" % (b / 1_048_576)
    if b >= 1024:
        return "%.0f KB" % (b / 1024)
    return "%d B" % b


_ACTION_HEADERS = {
    1: "### 🔴 优先级 1：立即清理（无风险）",
    2: "### 🟠 优先级 2：高收益缓存（低风险）",
    3: "### 🟡 优先级 3：需要重新生成/下载",
    4: "### 🟢 优先级 4：建议备份或迁移",
}


def _item_block(lines: List[str], item: CleanupItem) -> None:
    lines.append("#### %s" % item.name)
    lines.append("")
    lines.append("| 字段 | 内容 |")
    lines.append("|------|------|")
    lines.append("| 路径 | `%s` |" % item.path)
    lines.append("| 大小 | %s |" % bytes_to_human(item.size_bytes))
    if item.count_files:
        lines.append("| 文件数 | %d |" % item.count_files)
    lines.append("| 动作 | %s |" % ("移入备份" if item.action == "backup" else "删除"))
    lines.append("| 风险 | %s |" % item.risk)
    if item.requires_admin:
        lines.append("| 权限 | 需要管理员 |")
    lines.append("| 操作 | %s |" % item.method)
    if item.source:
        lines.append("| 依据 | %s |" % item.source)
    lines.append("")
    lines.append("**判断依据：**")
    for reason_line in item.reason.strip().splitlines():
        lines.append("> %s" % reason_line)
    lines.append("")


def generate_report(analysis: dict, output_dir: str = "",
                    meta: Optional[dict] = None) -> tuple:
    """
    生成 Markdown 报告。
    meta 可包含：admin(bool)、backup_retention(int)、csv_path(str)
    """
    meta = meta or {}
    if not output_dir:
        output_dir = get_desktop_path()

    date_str = time.strftime("%Y%m%d_%H%M")
    output_path = os.path.join(output_dir, "磁盘清理分析报告_%s.md" % date_str)

    disk_info: Optional[DiskInfo] = analysis.get("disk_info")
    top_level1 = analysis.get("top_level1", [])
    top_local = analysis.get("top_appdata_local", [])
    top_roaming = analysis.get("top_appdata_roaming", [])
    cleanup_items: List[CleanupItem] = analysis.get("cleanup_items", [])
    guide_items: List[CleanupItem] = analysis.get("guide_items", [])
    big_files = analysis.get("big_files", [])
    reclaimable = analysis.get("total_reclaimable_bytes", 0)
    backup_bytes = analysis.get("backup_bytes", 0)

    lines: List[str] = []
    lines.append("# 磁盘清理分析报告")
    lines.append("")
    if disk_info:
        lines.append("> 扫描时间：%s" % disk_info.scan_time)
    lines.append("> 报告生成：%s" % time.strftime("%Y年%m月%d日 %H:%M:%S"))
    lines.append("> 运行权限：%s" % ("管理员" if meta.get("admin") else "普通用户"))
    if meta.get("csv_path"):
        lines.append("> 数据来源：`%s`" % meta["csv_path"])
    lines.append("")
    lines.append("> 本报告不执行任何删除操作，仅用于展示分析结果。")
    lines.append("")

    # ── 一、磁盘概况 ────────────────────────────────────────────
    lines.append("## 一、磁盘概况（C 盘）")
    lines.append("")
    if disk_info:
        warning = " ⚠️ 空间紧张" if disk_info.used_percent >= 80 else ""
        lines.append("| 项目 | 数值 |")
        lines.append("|------|------|")
        lines.append("| 总容量 | %s GB |" % disk_info.total_gb)
        lines.append("| 已用空间 | %s GB |" % disk_info.used_gb)
        lines.append("| 剩余空间 | **%s GB** |" % disk_info.free_gb)
        lines.append("| 使用率 | **%s%%**%s |" % (disk_info.used_percent, warning))
    else:
        lines.append("_（CSV 中没有读取到磁盘容量信息）_")
    lines.append("")

    # ── 二、占用排行 ────────────────────────────────────────────
    lines.append("## 二、空间占用排行")
    lines.append("")
    if top_level1:
        lines.append("### C:\\ 下一级目录")
        lines.append("")
        lines.append("| 排名 | 路径 | 大小 |")
        lines.append("|------|------|------|")
        for i, folder in enumerate(top_level1, 1):
            lines.append("| %d | `%s` | %s |" % (i, folder.path, bytes_to_human(folder.size_bytes)))
        lines.append("")
    if big_files:
        lines.append("### 大文件 TOP %d（≥100MB）" % len(big_files))
        lines.append("")
        lines.append("| 文件 | 大小 | 说明 |")
        lines.append("|------|------|------|")
        for entry in big_files:
            lines.append("| `%s` | %s | %s |" % (
                entry.path, bytes_to_human(entry.size_bytes),
                _file_hint(entry.path)))
        lines.append("")

    # ── 三、可执行清理 ──────────────────────────────────────────
    lines.append("## 三、可执行清理（按优先级）")
    lines.append("")
    if not cleanup_items:
        lines.append("_没有发现符合规则的清理项。_")
        lines.append("")
    current_priority = -1
    for item in cleanup_items:
        if item.priority != current_priority:
            current_priority = item.priority
            lines.append("")
            lines.append(_ACTION_HEADERS.get(current_priority, "### 其他"))
            lines.append("")
        _item_block(lines, item)

    # ── 四、只建议 ──────────────────────────────────────────────
    lines.append("## 四、只建议（不自动删除）")
    lines.append("")
    lines.append("这些目录要么属于系统组件、要么是真实数据，"
                 "**不应该由清理工具直接删除**，请按下面的官方方法处理。")
    lines.append("")
    if not guide_items:
        lines.append("_没有需要特别提示的项目。_")
        lines.append("")
    for item in guide_items:
        lines.append("### %s" % item.name)
        lines.append("")
        lines.append("- **路径**：`%s`" % item.path)
        lines.append("- **大小**：%s" % bytes_to_human(item.size_bytes))
        lines.append("- **做法**：%s" % item.method)
        lines.append("- **风险**：%s" % item.risk)
        for reason_line in item.reason.strip().splitlines():
            lines.append("> %s" % reason_line)
        lines.append("")

    # ── 五、预期效果 ────────────────────────────────────────────
    lines.append("## 五、预期效果")
    lines.append("")
    lines.append("| 类别 | 大小 |")
    lines.append("|------|------|")
    lines.append("| 可直接删除（优先级 1+2） | **%s** |" % bytes_to_human(reclaimable))
    if backup_bytes:
        lines.append("| 需移入备份 | %s |" % bytes_to_human(backup_bytes))
    lines.append("")
    if reclaimable and disk_info and disk_info.total_bytes:
        after = disk_info.used_percent - reclaimable / disk_info.total_bytes * 100
        lines.append("清理后 C 盘使用率预计从 **%s%%** 降到 **%.1f%%**。"
                     % (disk_info.used_percent, max(0.0, after)))
        lines.append("")

    # ── 六、AppData 详情 ────────────────────────────────────────
    lines.append("## 六、AppData 目录占用")
    lines.append("")
    if top_local:
        lines.append("### AppData\\Local（前 %d 名）" % min(10, len(top_local)))
        lines.append("")
        lines.append("| 路径 | 大小 |")
        lines.append("|------|------|")
        for folder in top_local[:10]:
            lines.append("| `%s` | %s |" % (folder.path, bytes_to_human(folder.size_bytes)))
        lines.append("")
    if top_roaming:
        lines.append("### AppData\\Roaming（前 %d 名）" % min(10, len(top_roaming)))
        lines.append("")
        lines.append("| 路径 | 大小 |")
        lines.append("|------|------|")
        for folder in top_roaming[:10]:
            lines.append("| `%s` | %s |" % (folder.path, bytes_to_human(folder.size_bytes)))
        lines.append("")

    # ── 七、长期建议 ────────────────────────────────────────────
    lines.append("## 七、长期建议")
    lines.append("")
    advice = [
        "**把会再长出来的东西迁走**：游戏库、Docker/WSL 磁盘、npm/pip 缓存、"
        "微信文件目录都应放在 D 盘，而不是每次满了再清。",
        "**系统组件用官方工具**：`DISM /Online /Cleanup-Image /StartComponentCleanup`"
        "（清理 WinSxS 旧组件）、「设置 → 系统 → 存储 → 临时文件」、Storage Sense 自动清理。",
        "**保持 15% 以上空闲**：C 盘使用率超过 85% 后，Windows 更新和虚拟内存都会受影响。",
        "**大文件优先于小缓存**：先看本报告的大文件榜，再决定要不要清缓存。",
    ]
    if not meta.get("admin"):
        advice.append("**以管理员身份运行**：可用 MFT 快速扫描，也能覆盖 `C:\\ESD`、"
                      "`Windows\\Temp`、WER 等受保护目录。")
    if backup_bytes:
        advice.append("**备份会保留 %s 天**：确认无影响后可提前手动删除，"
                      "或在设置里调整保留天数。" % meta.get("backup_retention", 7))
    for i, text in enumerate(advice, 1):
        lines.append("%d. %s" % (i, text))
    lines.append("")
    lines.append("---")
    lines.append("_本报告由「磁盘清理助手」生成；清理规则基于公开的 Windows 系统知识，"
                 "仅供参考，执行前请自行确认路径。_")

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return True, output_path
    except OSError as e:
        return False, "报告保存失败：%s" % e


def _file_hint(path: str) -> str:
    name = os.path.basename(path).lower()
    if name == "hiberfil.sys":
        return "休眠文件，可用 powercfg /h off 关闭"
    if name == "pagefile.sys":
        return "虚拟内存文件，建议移到 D 盘"
    if name == "swapfile.sys":
        return "系统交换文件，由系统管理"
    if name.endswith(".vhdx") or name.endswith(".vhd"):
        return "虚拟磁盘（WSL/Docker/虚拟机），建议迁移"
    if name.endswith(".iso"):
        return "系统镜像，可移到 D 盘"
    if name.endswith(".dmp"):
        return "内存转储，排障后可删"
    return ""

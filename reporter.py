# ==============================================================
# reporter.py —— 报告生成模块
# ==============================================================
# 【作用】把分析结果转换成人类可读的Markdown报告文件，
#         保存到桌面或用户指定目录。
#
# 【Markdown是什么】一种轻量级文本格式，用 # 表示标题，
#   ** 表示加粗，| 表示表格。可以用记事本打开，
#   也可以用 Typora、VS Code、语雀等工具渲染成好看的格式。
# ==============================================================

import os
import time
from analyzer import DiskInfo, CleanupItem, GB
from typing import List, Optional
from config import get_desktop_path


def bytes_to_human(b: int) -> str:
    """把字节数转换成人类易读的格式，如 4.77 GB 或 512 MB"""
    if b >= GB:
        return f"{b / GB:.2f} GB"
    elif b >= 1_048_576:
        return f"{b / 1_048_576:.1f} MB"
    else:
        return f"{b / 1024:.0f} KB"


def generate_report(analysis: dict, output_dir: str = ""):  # -> Tuple[bool, str]
    """
    生成Markdown格式的磁盘清理分析报告。

    参数：
      analysis   run_analysis()返回的分析结果字典
      output_dir 报告保存目录，空字符串时保存到桌面

    返回值：(成功: bool, 文件路径或错误信息: str)
    """
    if not output_dir:
        output_dir = get_desktop_path()

    # 报告文件名包含日期，方便区分
    date_str = time.strftime("%Y%m%d_%H%M")
    filename = f"磁盘清理分析报告_{date_str}.md"
    output_path = os.path.join(output_dir, filename)

    # 从分析结果中取出各部分数据
    disk_info: Optional[DiskInfo] = analysis.get("disk_info")
    top_level1 = analysis.get("top_level1", [])
    top_local = analysis.get("top_appdata_local", [])
    top_roaming = analysis.get("top_appdata_roaming", [])
    cleanup_items: List[CleanupItem] = analysis.get("cleanup_items", [])
    reclaimable = analysis.get("total_reclaimable_bytes", 0)

    # ── 开始拼接报告内容 ──────────────────────────────────────────
    lines = []   # 用列表收集每一行，最后join成字符串（比字符串拼接效率高）

    lines.append(f"# 磁盘清理分析报告")
    lines.append(f"")
    if disk_info:
        lines.append(f"> 扫描时间：{disk_info.scan_time}")
    lines.append(f"> 报告生成：{time.strftime('%Y年%m月%d日 %H:%M:%S')}")
    lines.append(f"> 由「磁盘清理助手」自动生成")
    lines.append(f"")

    # ── 第一部分：磁盘概况 ────────────────────────────────────────
    lines.append(f"## 一、磁盘概况（C 盘）")
    lines.append(f"")
    if disk_info:
        used_pct = disk_info.used_percent
        warning = " ⚠️ 空间较紧张，建议清理" if used_pct >= 80 else ""
        lines.append(f"| 项目 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 总容量 | {disk_info.total_gb} GB |")
        lines.append(f"| 已用空间 | {disk_info.used_gb} GB |")
        lines.append(f"| 剩余空间 | **{disk_info.free_gb} GB** |")
        lines.append(f"| 使用率 | **{used_pct}%**{warning} |")
    else:
        lines.append("_（无法获取磁盘信息）_")
    lines.append(f"")

    # ── 第二部分：空间占用总览 ────────────────────────────────────
    lines.append(f"## 二、C 盘空间占用总览（前 {len(top_level1)} 大目录）")
    lines.append(f"")
    if top_level1:
        lines.append(f"| 排名 | 路径 | 大小 |")
        lines.append(f"|------|------|------|")
        for i, f in enumerate(top_level1, 1):
            lines.append(f"| {i} | `{f.path}` | {bytes_to_human(f.size_bytes)} |")
    lines.append(f"")

    # ── 第三部分：清理建议 ────────────────────────────────────────
    lines.append(f"## 三、清理建议（按优先级）")
    lines.append(f"")

    priority_headers = {
        1: "### 🔴 优先级1：立即清理（无风险）",
        2: "### 🟠 优先级2：高收益缓存（低风险）",
        3: "### 🟡 优先级3：游戏着色器缓存（需重新编译）",
        4: "### 🟢 优先级4：谨慎操作（建议用软件自带功能）",
    }

    # 按优先级分组输出
    current_priority = 0
    for item in cleanup_items:
        if item.priority != current_priority:
            current_priority = item.priority
            lines.append(f"")
            lines.append(priority_headers.get(item.priority, f"### 优先级{item.priority}"))
            lines.append(f"")

        lines.append(f"#### {item.name}")
        lines.append(f"")
        lines.append(f"- **路径**：`{item.path}`")
        lines.append(f"- **大小**：{bytes_to_human(item.size_bytes)}")
        lines.append(f"- **风险评估**：{item.risk}")
        lines.append(f"- **操作方法**：{item.method}")
        lines.append(f"")
        lines.append(f"**判断依据：**")
        # 把换行符转成Markdown换行（两个空格+换行）
        for reason_line in item.reason.strip().split('\n'):
            lines.append(f"> {reason_line}")
        lines.append(f"")

    # ── 第四部分：预期效果汇总 ────────────────────────────────────
    lines.append(f"## 四、预期效果汇总")
    lines.append(f"")
    if cleanup_items:
        lines.append(f"| 清理项 | 大小 | 优先级 | 风险评估 |")
        lines.append(f"|--------|------|--------|----------|")
        for item in cleanup_items:
            lines.append(
                f"| {item.name} | {bytes_to_human(item.size_bytes)} "
                f"| {item.priority_label} | {item.risk} |"
            )
        lines.append(f"")
        lines.append(f"**预计最大可回收空间（优先级1+2）：{bytes_to_human(reclaimable)}**")
    lines.append(f"")

    # ── 第五部分：AppData 详细占用 ───────────────────────────────
    lines.append(f"## 五、AppData 目录详细占用")
    lines.append(f"")

    if top_local:
        lines.append(f"### AppData\\Local（前 {min(10, len(top_local))} 名）")
        lines.append(f"")
        lines.append(f"| 路径 | 大小 |")
        lines.append(f"|------|------|")
        for f in top_local[:10]:
            lines.append(f"| `{f.path}` | {bytes_to_human(f.size_bytes)} |")
        lines.append(f"")

    if top_roaming:
        lines.append(f"### AppData\\Roaming（前 {min(10, len(top_roaming))} 名）")
        lines.append(f"")
        lines.append(f"| 路径 | 大小 |")
        lines.append(f"|------|------|")
        for f in top_roaming[:10]:
            lines.append(f"| `{f.path}` | {bytes_to_human(f.size_bytes)} |")
        lines.append(f"")

    # ── 第六部分：长期建议 ────────────────────────────────────────
    lines.append(f"## 六、长期建议")
    lines.append(f"")
    lines.append("1. **定期清理NVIDIA DXCache**：每1~2个月清理一次，每次可回收3~5 GB，无任何副作用。")
    lines.append("2. **将游戏和大文件迁移到D盘**：这是长期保持C盘健康的最有效方法。")
    lines.append("3. **微信聊天记录迁移**：微信→设置→聊天→迁移/备份聊天记录 → 选择D盘。")
    lines.append("4. **关注C盘使用率**：建议保持在75%以下，低于这个值系统性能最佳。")
    lines.append("5. **每次大型游戏更新后**：可以清理一次Shader缓存，避免旧缓存积累。")
    lines.append(f"")
    lines.append(f"---")
    lines.append(f"_本报告由「磁盘清理助手」生成，清理规则基于公开的Windows系统知识，仅供参考。_")

    # ── 写入文件 ──────────────────────────────────────────────────
    try:
        report_content = "\n".join(lines)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report_content)
        return True, output_path
    except IOError as e:
        return False, f"报告保存失败：{e}"

# 磁盘清理助手（Disk Cleaner）

Windows C 盘空间分析与**安全**清理工具。基于 WizTree 的扫描结果，按风险分级给出建议，
并提供一套带保护清单、可审计、可回退的清理引擎。

> 设计目标不是"删得更多"，而是"**删得对、删不坏、删了能查**"。

## 功能

- **磁盘概况**：容量、使用率、一级目录排行、空间缩略图
- **大文件榜**：列出 ≥100MB 的大文件（`pagefile.sys`、`hiberfil.sys`、`.vhdx`、ISO 往往才是真凶）
- **清理建议**：50+ 条规则，分「立即清理 / 高收益缓存 / 需要重新生成 / 建议备份」四级
- **只读建议**：WinSxS、Windows\Installer、WindowsApps、休眠文件等只给官方做法，不提供删除按钮
- **安全执行**：删除前预览真实路径、文件数、可回收量；执行后报告**实际**释放量与失败项
- **备份与回退**：标记为「建议备份」的项目移动到备份目录，按保留天数自动清理
- **历史记录**：每次清理的真实释放量、使用率变化
- **Markdown 报告**：完整分析结果可导出，便于存档或请教别人

## 安全模型

真正执行删除的是 `cleaner.py`，它对每个路径做四道检查：

1. **盘符与根目录**：只允许系统盘，拒绝盘符根
2. **系统保护清单**：`C:\Windows\System32`、`WinSxS`、`Installer`、`Program Files`、
   `WindowsApps`、`Package Cache`、用户 `Documents/Desktop/Downloads` 等一律拒绝
3. **重解析点检查**：目标及其所有上级目录不能是 junction / symlink（防止穿透到 D 盘）
4. **路径形态**：必须是绝对路径，且只能按「整个目录 / 目录内容 / 指定文件名」三种方式处理

此外：

- 不调用 PowerShell 拼接命令，避免路径注入
- 删除只读文件前自动去属性；被占用文件跳过并记录，不中断整批
- 释放量按真正删掉的文件字节累计，不使用扫描时的估算值
- 需要管理员的项目会明确标注；普通权限下失败会如实报告，不再"假成功"

## 环境要求

- Windows 10/11
- Python 3.8+
- [WizTree](https://www.diskanalyzer.com/download) 4.0+（免费版即可）
- `customtkinter`（`pip install -r requirements.txt`）

## 使用

```bat
启动.bat
```

或：

```bat
pip install -r requirements.txt
python main.py
```

首次启动会自动查找 `WizTree64.exe`（程序目录 → Program Files → 注册表卸载表）。
找不到时可在「设置」里手动指定，或把 `WizTree64.exe` 放到程序目录。

### 推荐流程

1. 扫描 → 看「磁盘概况」和「大文件」
2. 看「清理建议」：先处理 🔴 优先级 1
3. 勾选后点「清理选中项」→ **先看预览**，确认路径无误
4. 清理完重新扫描，对比使用率

## 规则分类

| 动作 | 含义 | 处理方式 |
|---|---|---|
| `delete` | 可再生缓存（着色器、浏览器缓存、Temp、日志…） | 直接删除 |
| `backup` | 会重置本地设置的缓存（如 Zoom data） | 移入备份目录，保留 N 天 |
| `guide` | 系统组件或真实数据（WinSxS、Installer、微信文件、休眠文件…） | 只展示，给官方命令，绝不自动删 |

规则定义在 `analyzer.py` 的 `CLEANUP_RULES`，每条包含：正则、路径段提示、优先级、
动作、处理目标、风险说明、参考来源。

## 文件结构

```
analyzer.py    CSV 解析 + 规则库 + 分析结果
cleaner.py     安全删除/备份引擎（可独立测试）
scanner.py     WizTree 调用、完成判定、自动发现
config.py      配置读写（%APPDATA%\DiskCleaner）、已知文件夹
reporter.py    Markdown 报告
app.py         customtkinter 界面
tests/         单元测试（29 个用例，含危险路径回归）
```

## 配置与数据

- 配置：`%APPDATA%\DiskCleaner\config.json`（原子写入）
- 历史：`%APPDATA%\DiskCleaner\cleanup_history.json`（最多 200 条）
- 备份：默认 `C:\DiskCleaner_Backup\<时间戳>\`，可在设置中修改
- 扫描 CSV 默认写桌面，可在设置中修改

## 测试

```bat
python -m unittest discover -s tests -t .
```

测试覆盖：

- 危险路径（Firefox 配置、KOOK/Razer 安装目录、剪映草稿、系统目录）**必须不产生可执行清理项**
- 已知缓存路径**必须被识别**
- 删除/内容删除/按文件名删除、只读文件、受保护路径拒绝
- 备份移动 + 清单 + 过期清理 + 取消
- CSV 解析（UTF-8 / GBK、容量列、大文件过滤）

## 已知限制

- 扫描 C 盘需要管理员权限才能使用 WizTree 的 MFT 快速模式；普通权限下较慢且会跳过无权限目录
- 被其他进程占用的文件会跳过（这是设计行为，不是失败）
- 规则基于公开资料与实测，不保证覆盖所有软件；发现误报请改 `CLEANUP_RULES`
- 删除操作没有"撤销"；只有 `backup` 类项目可回退

## 变更记录

### v3.1（2026-10）

- 新增 `cleaner.py` 安全删除引擎（保护清单、junction 检测、真实释放量、备份/清单/过期清理）
- 重写全部规则：修复 `\Mozilla\`、`\KOOK\`、`\Razer\`、`\JianyingPro\` 等宽匹配会误删
  Firefox 配置 / 安装目录 / 剪映草稿的问题
- 系统组件（WinSxS、Installer、WindowsApps、Package Cache）改为只读建议
- 新增：`Windows\Temp`、传递优化缓存、CBS 日志、内核转储、崩溃转储、大文件榜
- CSV 改为流式解析；规则匹配加"路径段倒排索引"，20 万行从 12 秒降到 1 秒级
- 扫描改用「CSV 写完」判定，支持 `/admin=1`；显式 `/exportfiles`，避免 CSV 体积失控
- 配置迁到 `%APPDATA%`、原子写入；桌面路径兼容 OneDrive 重定向
- 清理改后台线程，带进度与停止；失败/跳过如实展示
- 新增 29 个单元测试

### v3.0

- 46 条规则、勾选清理、历史记录、UI 优化、WizTree 自动检测

## 免责声明

本工具只做文件删除/移动，**不修改注册表、不调用系统优化脚本**。清理前请确认路径；
因误删造成的数据丢失需自行承担。重要数据请先备份。

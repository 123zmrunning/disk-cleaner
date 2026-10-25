# ==============================================================
# cleaner.py —— 安全清理引擎
# ==============================================================
# 【为什么单独一个文件】
#   analyzer.py 只负责"判断哪些能清理"，真正的删除/备份必须有
#   一套独立、可测试、有兜底的安全逻辑。把两者分开，规则写错时
#   仍然有一道闸门拦着。
#
# 【四道安全闸】
#   1. 路径必须在本机系统盘（C:），且不是盘符根、不是系统保护目录
#   2. 路径及其所有上级目录都不能是 junction / symlink（防止穿透到别的盘）
#   3. 系统保护目录（WinSxS、Installer、Program Files、文档、桌面…）一律拒绝
#   4. 删除只按"目录 / 目录内容 / 指定文件名"三种方式，不做模糊递归
#
# 【删除结果如实上报】
#   deleted / moved / skipped / denied / failed 分别返回，
#   释放量按"真正删掉的文件字节数"累计，而不是扫描时的大小。
# ==============================================================

import ctypes
import json
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

# Windows 文件属性：重解析点（junction / symlink / mount point）
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

SYSTEM_DRIVE = (os.environ.get("SystemDrive") or "C:") + "\\"

# ── 系统保护目录 ────────────────────────────────────────────────
# 精确匹配（这些目录本身绝不允许被整体删除）
PROTECTED_EXACT = {
    SYSTEM_DRIVE.rstrip("\\"),
    os.path.join(SYSTEM_DRIVE, "Windows"),
    os.path.join(SYSTEM_DRIVE, "Program Files"),
    os.path.join(SYSTEM_DRIVE, "Program Files (x86)"),
    os.path.join(SYSTEM_DRIVE, "ProgramData"),
    os.path.join(SYSTEM_DRIVE, "Users"),
    os.path.join(SYSTEM_DRIVE, "Recovery"),
    os.path.join(SYSTEM_DRIVE, "System Volume Information"),
    os.path.join(SYSTEM_DRIVE, "$Recycle.Bin"),
    os.path.join(SYSTEM_DRIVE, "Windows", "WinSxS"),
    os.path.join(SYSTEM_DRIVE, "Windows", "Installer"),
    os.path.join(SYSTEM_DRIVE, "Windows", "System32"),
    os.path.join(SYSTEM_DRIVE, "Windows", "SysWOW64"),
}

# 子树保护（这些目录下的任何东西都不允许删）
PROTECTED_PREFIXES = [
    os.path.join(SYSTEM_DRIVE, "Windows", "System32"),
    os.path.join(SYSTEM_DRIVE, "Windows", "SysWOW64"),
    os.path.join(SYSTEM_DRIVE, "Windows", "WinSxS"),
    os.path.join(SYSTEM_DRIVE, "Windows", "Installer"),
    os.path.join(SYSTEM_DRIVE, "Windows", "servicing"),
    os.path.join(SYSTEM_DRIVE, "Windows", "Boot"),
    os.path.join(SYSTEM_DRIVE, "Windows", "Fonts"),
    os.path.join(SYSTEM_DRIVE, "Windows", "assembly"),
    os.path.join(SYSTEM_DRIVE, "Windows", "Microsoft.NET"),
    os.path.join(SYSTEM_DRIVE, "Program Files"),
    os.path.join(SYSTEM_DRIVE, "Program Files (x86)"),
    os.path.join(SYSTEM_DRIVE, "ProgramData", "Package Cache"),
    os.path.join(SYSTEM_DRIVE, "System Volume Information"),
    os.path.join(SYSTEM_DRIVE, "Recovery"),
]

# 这些目录本身不允许删，但里面某个具体缓存子目录可以（由规则精确指定）
USER_PROTECTED_RE = re.compile(
    r"^[A-Za-z]:\\Users\\[^\\]+\\(Documents|Desktop|Downloads|Pictures|Videos|"
    r"Music|OneDrive|Favorites|Links|Contacts|Saved Games|Searches|3D Objects)"
    r"(\\|$)",
    re.IGNORECASE,
)
USER_PROFILE_PROTECTED_RE = re.compile(
    r"^[A-Za-z]:\\Users\\[^\\]+\\(AppData|AppData\\Local|AppData\\Roaming|"
    r"AppData\\LocalLow|AppData\\Roaming\\Microsoft\\Windows\\Start Menu|"
    r"AppData\\Local\\Packages)$",
    re.IGNORECASE,
)
USER_ROOT_RE = re.compile(r"^[A-Za-z]:\\Users\\[^\\]+$", re.IGNORECASE)
STORE_PACKAGE_RE = re.compile(r"\\AppData\\Local\\Packages\\[^\\]+$", re.IGNORECASE)
FIREFOX_PROFILE_RE = re.compile(
    r"\\AppData\\Roaming\\Mozilla\\Firefox\\Profiles(\\|$)", re.IGNORECASE
)

# 仅允许标准安装位置中的这三个 Steam 缓存目录例外。
# 不能凭路径后缀跳过 System32、个人文档等保护。
ALLOW_UNDER_PROTECTED = [
    re.compile(
        r"^%sProgram Files(?: \(x86\))?\\Steam\\"
        r"(?:htmlcache|steamapps\\downloading|steamapps\\workshop\\downloads)$"
        % re.escape(SYSTEM_DRIVE), re.IGNORECASE),
]

MAX_ERRORS_KEPT = 8


# ==============================================================
# 数据结构
# ==============================================================

@dataclass
class ItemResult:
    """单个清理项的执行结果"""
    name: str
    path: str
    action: str                     # delete / backup / guide
    status: str                     # deleted / moved / skipped / denied / failed / guide / missing
    bytes_freed: int = 0            # 真正释放的字节数（backup 时为 0）
    bytes_moved: int = 0            # 移动到备份目录的字节数
    files: int = 0                  # 成功删除的文件数
    dest: str = ""                  # 备份后的新位置
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("deleted", "moved")

    @property
    def status_label(self) -> str:
        return {
            "deleted": "已删除",
            "moved": "已移入备份",
            "skipped": "已跳过",
            "denied": "被安全策略拒绝",
            "failed": "失败",
            "guide": "仅建议",
            "missing": "路径已不存在",
        }.get(self.status, self.status)


# ==============================================================
# 基础工具
# ==============================================================

def is_elevated() -> bool:
    """当前进程是否以管理员身份运行"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def normalize(path: str) -> str:
    """统一路径写法：去引号、去尾部反斜杠、转绝对路径"""
    p = (path or "").strip().strip('"').strip()
    if not p:
        return ""
    p = os.path.abspath(p)
    if len(p) > 3:
        p = p.rstrip("\\")
    return p


def _long(path: str) -> str:
    """超过 240 字符时加 \\?\ 前缀，绕过 MAX_PATH 限制"""
    if os.name != "nt":
        return path
    if len(path) < 240 or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path.lstrip("\\")
    return "\\\\?\\" + path


def _is_reparse(path: str) -> bool:
    try:
        st = os.stat(_long(path), follow_symlinks=False)
        return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False


def _has_reparse_ancestor(path: str) -> Optional[str]:
    """检查目标本身及所有上级目录是否 junction/symlink"""
    current = normalize(path)
    while len(current) > 3:
        if _is_reparse(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _under(path: str, prefix: str) -> bool:
    p = path.lower().rstrip("\\")
    q = prefix.lower().rstrip("\\")
    return p == q or p.startswith(q + "\\")


def path_is_safe(path: str) -> Tuple[bool, str]:
    """
    判断某路径是否允许被清理。
    返回 (是否安全, 拒绝原因)。
    """
    p = normalize(path)
    if not p:
        return False, "路径为空"
    if len(p) <= 3:
        return False, "不允许操作磁盘根目录"
    drive = os.path.splitdrive(p)[0] + "\\"
    if drive.upper() != SYSTEM_DRIVE.upper():
        return False, "只允许清理系统盘（%s）" % SYSTEM_DRIVE

    if p.upper() in {x.upper() for x in PROTECTED_EXACT}:
        return False, "系统保护目录：%s" % p
    steam_cache = any(pattern.fullmatch(p) for pattern in ALLOW_UNDER_PROTECTED)
    program_dirs = {os.path.join(SYSTEM_DRIVE, name).lower()
                    for name in ("Program Files", "Program Files (x86)")}
    for prefix in PROTECTED_PREFIXES:
        if _under(p, prefix):
            if steam_cache and prefix.lower() in program_dirs:
                continue
            return False, "系统保护目录：%s" % prefix
    if USER_PROTECTED_RE.search(p):
        return False, "个人文档 / 桌面等目录受保护"
    if USER_ROOT_RE.match(p):
        return False, "用户配置根目录受保护"
    if USER_PROFILE_PROTECTED_RE.search(p):
        return False, "用户配置根目录受保护"
    if STORE_PACKAGE_RE.search(p):
        return False, "商店应用数据目录受保护"

    if FIREFOX_PROFILE_RE.search(p):
        return False, "Firefox 配置目录（书签/密码）受保护"

    bad = _has_reparse_ancestor(p)
    if bad:
        return False, "路径经过 junction/symlink：%s" % bad
    return True, ""


def measure(path: str) -> Tuple[int, int]:
    """快速统计目录/文件的 (字节数, 文件数)，不跟随 junction"""
    total = 0
    files = 0
    path = normalize(path)
    try:
        st = os.stat(_long(path), follow_symlinks=False)
    except OSError:
        return 0, 0
    if stat.S_ISREG(st.st_mode):
        return st.st_size, 1
    if _is_reparse(path):
        return 0, 0
    for root, dirs, names in os.walk(_long(path), topdown=True, followlinks=False):
        dirs[:] = [d for d in dirs if not _is_reparse(os.path.join(root, d))]
        for name in names:
            try:
                total += os.stat(os.path.join(root, name), follow_symlinks=False).st_size
                files += 1
            except OSError:
                continue
    return total, files


def default_backup_root() -> str:
    """备份目录：优先 C:\\DiskCleaner_Backup，不可写则退回 LOCALAPPDATA"""
    candidates = [
        os.path.join(SYSTEM_DRIVE, "DiskCleaner_Backup"),
        os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                     "DiskCleaner", "Backup"),
    ]
    for candidate in candidates:
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".write_test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            return candidate
        except OSError:
            continue
    return candidates[-1]


# ==============================================================
# 删除 / 备份
# ==============================================================

class _Collector:
    """执行过程中的统计与错误收集"""

    def __init__(self):
        self.bytes = 0
        self.files = 0
        self.skipped = 0
        self.errors: List[str] = []

    def error(self, path: str, exc: BaseException):
        if len(self.errors) < MAX_ERRORS_KEPT:
            self.errors.append("%s: %s" % (path, exc))
        self.skipped += 1


def _remove_file(path: str, col: _Collector) -> bool:
    """删除单个文件；只读属性、临时占用会自动重试一次"""
    try:
        st = os.stat(_long(path), follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as e:
        col.error(path, e)
        return False
    try:
        os.unlink(_long(path))
    except PermissionError:
        try:
            os.chmod(_long(path), stat.S_IWRITE | stat.S_IREAD)
            os.unlink(_long(path))
        except OSError as e:
            col.error(path, e)
            return False
    except OSError as e:
        col.error(path, e)
        return False
    col.bytes += st.st_size
    col.files += 1
    return True


def _remove_tree(path: str, col: _Collector) -> bool:
    """
    自底向上删除目录。遇到 junction 只删除链接本身，绝不进入目标。
    """
    path = normalize(path)
    if _is_reparse(path):
        try:
            os.rmdir(_long(path))
            return True
        except OSError as e:
            col.error(path, e)
            return False

    try:
        entries = list(os.scandir(_long(path)))
    except FileNotFoundError:
        return True
    except OSError as e:
        col.error(path, e)
        return False

    for entry in entries:
        child = entry.path
        try:
            if entry.is_dir(follow_symlinks=False):
                _remove_tree(child, col)
            else:
                _remove_file(child, col)
        except OSError as e:
            col.error(child, e)

    for attempt in (1, 2):
        try:
            os.rmdir(_long(path))
            return True
        except FileNotFoundError:
            return True
        except OSError as e:
            if attempt == 1:
                try:
                    os.chmod(_long(path), stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
                continue
            col.error(path, e)
            return False
    return False


def _resolve_targets(item) -> List[str]:
    """把清理项展开成真正要处理的路径列表"""
    base = normalize(item.path)
    target = getattr(item, "target", "folder")
    globs = tuple(getattr(item, "file_globs", ()) or ())
    recursive = bool(getattr(item, "recursive", True))

    if target == "folder":
        return [base]
    if not os.path.isdir(_long(base)):
        return []

    if target == "contents":
        try:
            names = [e.path for e in os.scandir(_long(base))]
        except OSError:
            return []
        return names

    # target == "files"：只删指定文件名模式
    import fnmatch
    matched: List[str] = []
    if recursive:
        for root, dirs, names in os.walk(_long(base), followlinks=False):
            dirs[:] = [d for d in dirs if not _is_reparse(os.path.join(root, d))]
            for name in names:
                if any(fnmatch.fnmatch(name.lower(), g.lower()) for g in globs):
                    matched.append(os.path.join(root, name))
    else:
        try:
            for entry in os.scandir(_long(base)):
                if entry.is_file(follow_symlinks=False) and any(
                        fnmatch.fnmatch(entry.name.lower(), g.lower()) for g in globs):
                    matched.append(entry.path)
        except OSError:
            return []
    return matched


def create_backup_run(root: str) -> str:
    """为本次执行创建带时间戳的备份批次目录（一次执行共用一个）"""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(root, stamp)
    suffix = 1
    while os.path.exists(run_dir):
        run_dir = os.path.join(root, "%s_%d" % (stamp, suffix))
        suffix += 1
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def _flatten_name(path: str) -> str:
    clean = normalize(path).replace(":", "").replace("\\", "__").strip("_")
    return clean[:120] or "item"


def execute_item(item, backup_root: Optional[str] = None,
                 backup_run: Optional[str] = None,
                 progress: Optional[Callable[[str], None]] = None) -> ItemResult:
    """
    执行单个清理项。
    item 需要具备：name / path / action / target / file_globs / recursive
    """
    result = ItemResult(name=item.name, path=item.path,
                        action=getattr(item, "action", "delete"),
                        status="skipped")

    if progress:
        progress("正在处理：%s" % item.name)

    if getattr(item, "action", "delete") == "guide":
        result.status = "guide"
        return result

    if not os.path.exists(_long(normalize(item.path))):
        result.status = "missing"
        return result

    safe, reason = path_is_safe(item.path)
    if not safe:
        result.status = "denied"
        result.errors.append(reason)
        return result

    if getattr(item, "action", "delete") == "backup":
        root = backup_run or backup_root or default_backup_root()
        try:
            dest = os.path.join(root, _flatten_name(item.path))
            size, files = measure(item.path)
            shutil.move(normalize(item.path), dest)
            result.status = "moved"
            result.bytes_moved = size
            result.files = files
            result.dest = dest
            return result
        except OSError as e:
            result.status = "failed"
            result.errors.append(str(e))
            return result

    targets = _resolve_targets(item)
    if not targets:
        result.status = "skipped"
        return result

    col = _Collector()
    for target in targets:
        if os.path.isdir(_long(target)):
            _remove_tree(target, col)
        else:
            _remove_file(target, col)

    result.bytes_freed = col.bytes
    result.files = col.files
    result.errors = col.errors[:MAX_ERRORS_KEPT]
    if col.errors and col.bytes == 0:
        result.status = "failed"
    elif col.errors:
        result.status = "deleted"
    else:
        result.status = "deleted"
    return result


def execute_items(items: Sequence, backup_root: Optional[str] = None,
                  progress: Optional[Callable[[int, int, str], None]] = None,
                  cancel: Optional[object] = None,
                  on_result: Optional[Callable[[ItemResult], None]] = None,
                  retention_days: int = 7
                  ) -> List[ItemResult]:
    """批量执行；progress(已完成, 总数, 当前项名称)，cancel 提供 is_set()"""
    results: List[ItemResult] = []
    total = len(items)
    run_dir = ""
    for index, item in enumerate(items, 1):
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            break
        if progress:
            progress(index - 1, total, item.name)
        if getattr(item, "action", "delete") == "backup" and not run_dir:
            run_dir = create_backup_run(backup_root or default_backup_root())
        result = execute_item(item, backup_root=backup_root, backup_run=run_dir or None)
        results.append(result)
        if on_result:
            on_result(result)
        if progress:
            progress(index, total, item.name)

    if run_dir and any(r.status == "moved" for r in results):
        write_backup_manifest(run_dir, results, retention_days)
        try:
            prune_backups(os.path.dirname(run_dir), retention_days)
        except OSError:
            pass
    return results


# ==============================================================
# 备份批次清单与清理
# ==============================================================

def write_backup_manifest(run_dir: str, results: Sequence[ItemResult],
                          retention_days: int) -> None:
    """在备份批次目录里写清单，记录来源、时间和可清理期限"""
    manifest = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "expires_after_days": retention_days,
        "prunable": True,
        "items": [
            {"name": r.name, "original_path": r.path, "bytes": r.bytes_moved}
            for r in results if r.status == "moved"
        ],
    }
    try:
        with open(os.path.join(run_dir, "_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def prune_backups(root: str, retention_days: int) -> List[str]:
    """删除超过保留期的备份批次（只删除带清单且 prunable 的批次）"""
    removed: List[str] = []
    if not root or not os.path.isdir(root) or retention_days <= 0:
        return removed
    deadline = time.time() - retention_days * 86400
    for name in os.listdir(root):
        run_dir = os.path.join(root, name)
        if not os.path.isdir(run_dir):
            continue
        manifest_path = os.path.join(run_dir, "_manifest.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            if not manifest.get("prunable", False):
                continue
            if os.path.getmtime(manifest_path) > deadline:
                continue
            shutil.rmtree(run_dir, ignore_errors=True)
            removed.append(run_dir)
        except (OSError, ValueError):
            continue
    return removed


def restart_as_admin() -> bool:
    """以管理员身份重新启动本程序；用户取消 UAC 时返回 False"""
    if is_elevated():
        return False
    try:
        if getattr(sys, "frozen", False):
            exe, params = sys.executable, ""
        else:
            exe = sys.executable
            params = '"%s"' % os.path.abspath(sys.argv[0])
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, os.getcwd(), 1)
        return rc > 32
    except Exception:
        return False

"""不创建窗口，验证清理结果在界面中的提示。"""
import importlib.util
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from cleaner import ItemResult

GUI_AVAILABLE = importlib.util.find_spec("customtkinter") is not None
if GUI_AVAILABLE:
    from app import DiskCleanerApp


@unittest.skipUnless(GUI_AVAILABLE, "界面测试需要 requirements.txt 中的 customtkinter")
class CleanupFeedbackTest(unittest.TestCase):
    def setUp(self):
        self.view = SimpleNamespace(
            _exec_running=True, _cleanup_checkboxes={},
            exec_cleanup_btn=MagicMock(), exec_selected_btn=MagicMock(),
            scan_btn=MagicMock(), cancel_exec_btn=MagicMock(),
            _set_progress=MagicMock(), _set_status=MagicMock(),
            _save_history_record=MagicMock(), _update_tab_history=MagicMock(),
            _on_checkbox_change=MagicMock(),
        )

    def test_partial_result_shows_warning_and_error(self):
        result = ItemResult("测试缓存", r"C:\Cache", "delete", "partial",
                            bytes_freed=100, files=1,
                            errors=["文件被占用"], failed_operations=2)
        with patch("app.messagebox") as box:
            DiskCleanerApp._finish_execution(self.view, [result])
        box.showinfo.assert_not_called()
        text = box.showwarning.call_args[0][1]
        self.assertIn("部分完成：1 项", text)
        self.assertIn("成功删除：0 项", text)
        self.assertIn("失败操作 2 次", text)
        self.assertIn("文件被占用", text)
        self.assertIn("有未完成项", self.view._set_status.call_args[0][0])
        self.view._save_history_record.assert_called_once_with([result])

    def test_complete_result_shows_success(self):
        result = ItemResult("测试缓存", r"C:\Cache", "delete", "deleted", files=1)
        with patch("app.messagebox") as box:
            DiskCleanerApp._finish_execution(self.view, [result])
        box.showwarning.assert_not_called()
        self.assertIn("成功删除：1 项", box.showinfo.call_args[0][1])

    def test_failed_result_shows_reason(self):
        result = ItemResult("测试缓存", r"C:\Cache", "delete", "failed",
                            errors=["权限不足"])
        with patch("app.messagebox") as box:
            DiskCleanerApp._finish_execution(self.view, [result])
        self.assertIn("权限不足", box.showwarning.call_args[0][1])


if __name__ == "__main__":
    unittest.main()

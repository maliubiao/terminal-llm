#!/usr/bin/env python
"""
llm_query 模块的单元测试
"""

import os
import pdb
import tempfile
import unittest
from unittest.mock import patch

import llm_query
from llm_query import (
    MAX_PROMPT_SIZE,
    BlockPatchResponse,
    CmdNode,
    GPTContextProcessor,
    _fetch_symbol_data,
    get_symbol_detail,
    patch_symbol_with_prompt,
)


class TestGPTContextProcessor(unittest.TestCase):
    """GPTContextProcessor 的单元测试类"""

    def setUp(self):
        """初始化测试环境"""
        self.processor = GPTContextProcessor()
        self.test_dir = tempfile.mkdtemp()
        os.chdir(self.test_dir)

    def tearDown(self):
        """清理测试环境"""
        os.chdir(os.path.dirname(self.test_dir))
        os.rmdir(self.test_dir)

    def test_basic_text_processing(self):
        """测试基本文本处理"""
        text = "这是一个普通文本"
        result = self.processor.process_text_with_file_path(text)
        self.assertEqual(result, text)

    def test_single_command_processing(self):
        """测试单个命令处理"""
        text = "@clipboard"
        with patch.dict(self.processor.cmd_map, {"clipboard": lambda x: "剪贴板内容"}):
            result = self.processor.process_text_with_file_path(text)
            self.assertIn("剪贴板内容", result)

    def test_escaped_at_symbol(self):
        """测试转义的@符号"""
        text = "这是一个转义符号\\@test"
        result = self.processor.process_text_with_file_path(text)
        self.assertEqual(result, "这是一个转义符号@test")

    def test_mixed_escaped_and_commands(self):
        """测试混合转义符号和命令"""
        text = "开始\\@test 中间 @clipboard 结束"
        with patch.dict(self.processor.cmd_map, {"clipboard": lambda x: "剪贴板内容"}):
            result = self.processor.process_text_with_file_path(text)
            self.assertEqual(result, "开始@test 中间 剪贴板内容 结束")

    def test_multiple_commands_processing(self):
        """测试多个命令处理"""
        text = "开始 @clipboard 中间 @last 结束"
        with patch.dict(self.processor.cmd_map, {"clipboard": lambda x: "剪贴板内容", "last": lambda x: "上次查询"}):
            result = self.processor.process_text_with_file_path(text)
            self.assertIn("剪贴板内容", result)
            self.assertIn("上次查询", result)

    def test_template_processing(self):
        """测试模板处理"""
        text = "{@clipboard @last}"
        with patch.dict(self.processor.cmd_map, {"clipboard": lambda x: "剪贴板内容 {}", "last": lambda x: "上次查询"}):
            result = self.processor.process_text_with_file_path(text)
            self.assertIn("剪贴板内容 上次查询", result)

    def test_command_with_args(self):
        """测试带参数的命令"""
        text = "@symbol:test"
        with patch.dict(self.processor.cmd_map, {"symbol": lambda x: "符号补丁"}):
            result = self.processor.process_text_with_file_path(text)
            self.assertIn("符号补丁", result)

    def test_mixed_content_processing(self):
        """测试混合内容处理"""
        text = "开始 {@clipboard @last} 中间 @symbol:test 结束"
        with patch.dict(
            self.processor.cmd_map,
            {"clipboard": lambda x: "剪贴板内容 {}", "last": lambda x: "上次查询", "symbol": lambda x: "符号补丁"},
        ):
            result = self.processor.process_text_with_file_path(text)
            self.assertIn("剪贴板内容 上次查询", result)
            self.assertIn("符号补丁", result)

    def test_command_not_found(self):
        """测试未找到命令的情况"""
        text = "@unknown"
        result = self.processor.process_text_with_file_path(text)
        self.assertEqual(result, "")

    def test_max_length_truncation(self):
        """测试最大长度截断"""
        long_text = "a" * (MAX_PROMPT_SIZE + 100)
        result = self.processor.process_text_with_file_path(long_text)
        self.assertTrue(len(result) <= MAX_PROMPT_SIZE)
        self.assertIn("输入太长内容已自动截断", result)

    def test_multiple_symbol_args(self):
        """测试多个符号参数合并"""
        text = "@symbol:a @symbol:b"
        with patch.dict(self.processor.cmd_map, {"symbol": lambda x: f"符号补丁 {x.args}"}):
            result = self.processor.process_text_with_file_path(text)
            self.assertIn("符号补丁 ['a', 'b']", result)

    def test_url_processing(self):
        """测试URL处理"""
        text = "@https://example.com"
        with patch("llm_query._handle_url") as mock_handle_url:
            mock_handle_url.return_value = "URL处理结果"
            result = self.processor.process_text_with_file_path(text)
            self.assertIn("URL处理结果", result)
            mock_handle_url.assert_called_once_with(
                CmdNode(command="https://example.com", command_type=None, args=None)
            )

    def test_multiple_urls(self):
        """测试多个URL处理"""
        text = "@https://example.com @https://another.com"
        with patch("llm_query._handle_url") as mock_handle_url:
            mock_handle_url.side_effect = ["URL1结果", "URL2结果"]
            result = self.processor.process_text_with_file_path(text)
            self.assertIn("URL1结果", result)
            self.assertIn("URL2结果", result)
            self.assertEqual(mock_handle_url.call_count, 2)
            mock_handle_url.assert_any_call(CmdNode(command="https://example.com", command_type=None, args=None))
            mock_handle_url.assert_any_call(CmdNode(command="https://another.com", command_type=None, args=None))

    def test_mixed_url_and_commands(self):
        """测试混合URL和命令处理"""
        text = "开始 @https://example.com 中间 @clipboard 结束"
        with (
            patch("llm_query._handle_url") as mock_handle_url,
            patch.dict(self.processor.cmd_map, {"clipboard": lambda x: "剪贴板内容"}),
        ):
            mock_handle_url.return_value = "URL处理结果"
            result = self.processor.process_text_with_file_path(text)
            self.assertIn("URL处理结果", result)
            self.assertIn("剪贴板内容", result)
            mock_handle_url.assert_called_once_with(
                CmdNode(command="https://example.com", command_type=None, args=None)
            )

    def test_patch_symbol_with_prompt(self):
        """测试生成符号补丁提示词"""

        # 模拟CmdNode对象
        class MockCmdNode:
            def __init__(self, args):
                self.args = args

        # 测试单个符号
        symbol_names = MockCmdNode(["test_symbol"])
        with patch("llm_query.get_symbol_detail") as mock_get_detail:
            mock_get_detail.return_value = {
                "file_path": "test.py",
                "code_range": ((1, 0), (10, 0)),
                "block_range": "1-10",
                "block_content": b"test content",
            }
            result = patch_symbol_with_prompt(symbol_names)
            self.assertIn("test_symbol", result)
            self.assertIn("test.py", result)
            self.assertIn("test content", result)

        # 测试多个符号
        symbol_names = MockCmdNode(["symbol1", "symbol2"])
        with patch("llm_query.get_symbol_detail") as mock_get_detail:
            mock_get_detail.side_effect = [
                {
                    "file_path": "file1.py",
                    "code_range": ((1, 0), (5, 0)),
                    "block_range": "1-5",
                    "block_content": b"content1",
                },
                {
                    "file_path": "file2.py",
                    "code_range": ((10, 0), (15, 0)),
                    "block_range": "10-15",
                    "block_content": b"content2",
                },
            ]
            result = patch_symbol_with_prompt(symbol_names)
            self.assertIn("symbol1", result)
            self.assertIn("symbol2", result)
            self.assertIn("content1", result)
            self.assertIn("content2", result)

    def test_get_symbol_detail(self):
        """测试获取符号详细信息"""
        with patch("llm_query._send_http_request") as mock_request:
            mock_request.return_value = {
                "content": "test content",
                "location": {"start_line": 1, "start_col": 0, "end_line": 10, "end_col": 0, "block_range": "1-10"},
                "file_path": "test.py",
            }
            result = get_symbol_detail("test_symbol")
            self.assertEqual(result["file_path"], "test.py")
            self.assertEqual(result["code_range"], ((1, 0), (10, 0)))
            self.assertEqual(result["block_content"], b"test content")

    def test_fetch_symbol_data(self):
        """测试获取符号上下文数据"""
        with patch("llm_query._send_http_request") as mock_request:
            mock_request.return_value = {"symbol_name": "test", "definitions": [], "references": []}
            result = _fetch_symbol_data("test_symbol")
            self.assertEqual(result["symbol_name"], "test")
            self.assertIsInstance(result["definitions"], list)
            self.assertIsInstance(result["references"], list)


class TestSymbolLocation(unittest.TestCase):
    def setUp(self):
        self.symbol_name = "test_symbol"
        self.file_path = "test_file.py"
        self.original_content = "\n\ndef test_symbol():\n    pass"
        self.block_range = (1, len(self.original_content))
        self.content = self.original_content[self.block_range[0] : self.block_range[1]]
        self.code_range = ((1, 0), (2, 4))
        self.flags = None
        self.whole_content = self.original_content + "\n"
        # 创建测试文件
        with open(self.file_path, "w") as f:
            f.write(self.whole_content)

        # 模拟API响应
        self.symbol_data = {
            "content": self.content,
            "location": {"block_range": self.block_range, "start_line": 1, "start_col": 0, "end_line": 2, "end_col": 4},
            "file_path": self.file_path,
        }

        # 模拟http请求
        self.original_send_http_request = llm_query._send_http_request
        llm_query._send_http_request = lambda url: self.symbol_data

    def tearDown(self):
        # 删除测试文件
        if os.path.exists(self.file_path):
            os.remove(self.file_path)

        # 恢复原始http请求函数
        llm_query._send_http_request = self.original_send_http_request

    def test_basic_symbol(self):
        result = llm_query.get_symbol_detail(self.symbol_name)
        self.assertIsNotNone(result)
        self.assertEqual(result["symbol_name"], self.symbol_name)
        self.assertEqual(result["file_path"], self.file_path)
        self.assertEqual(result["code_range"], self.code_range)
        self.assertEqual(result["block_range"], self.block_range)
        self.assertEqual(result["block_content"], self.content.encode("utf-8"))
        self.assertIsNone(result["flags"])

    def test_symbol_with_before_flag(self):
        self.symbol_name = "test_symbol^"
        result = llm_query.get_symbol_detail(self.symbol_name)
        self.assertIsNotNone(result)
        self.assertEqual(result["symbol_name"], "test_symbol")
        self.assertIsNotNone(result["flags"])
        self.assertEqual(result["flags"]["position"], "before")
        # 动态查找换行符位置
        self.assertEqual(result["flags"]["newline_pos"], 1)

    def test_symbol_with_after_flag(self):
        self.symbol_name = "test_symbol$"
        result = llm_query.get_symbol_detail(self.symbol_name)
        self.assertIsNotNone(result)
        self.assertEqual(result["symbol_name"], "test_symbol")
        self.assertIsNotNone(result["flags"])
        self.assertEqual(result["flags"]["position"], "after")
        # 动态查找换行符位置
        expected_newline_pos = self.whole_content.rfind("\n", 0)
        self.assertEqual(result["flags"]["newline_pos"], expected_newline_pos)

    def test_file_content_mismatch(self):
        self.symbol_name = "test_symbol^"  # 添加标志触发文件验证
        # 修改文件内容
        with open(self.file_path, "w") as f:
            f.write("modified content")

        result = llm_query.get_symbol_detail(self.symbol_name)
        self.assertIsNone(result)

    def test_missing_file(self):
        self.symbol_name = "test_symbol^"  # 添加标志触发文件验证
        os.remove(self.file_path)
        result = llm_query.get_symbol_detail(self.symbol_name)
        self.assertIsNone(result)

    def test_invalid_symbol_data(self):
        # 模拟无效的symbol_data
        self.symbol_data["content"] = "invalid content"
        result = llm_query.get_symbol_detail(self.symbol_name + "$")
        self.assertIsNone(result)

    def test_multiline_symbol(self):
        # 测试多行符号
        self.content = "def test_symbol():\n    pass\n    pass\n"
        self.block_range = (0, len(self.content))
        self.code_range = ((1, 0), (3, 4))

        # 更新测试文件
        with open(self.file_path, "w") as f:
            f.write(self.content)

        # 更新模拟数据
        self.symbol_data["content"] = self.content
        self.symbol_data["location"]["block_range"] = self.block_range
        self.symbol_data["location"]["end_line"] = 3

        result = llm_query.get_symbol_detail(self.symbol_name)
        self.assertIsNotNone(result)
        self.assertEqual(result["block_range"], self.block_range)
        self.assertEqual(result["code_range"], self.code_range)

    def test_empty_symbol(self):
        # 测试空符号
        self.content = ""
        self.block_range = (0, 0)
        self.code_range = ((1, 0), (1, 0))

        # 更新测试文件
        with open(self.file_path, "w") as f:
            f.write(self.content)

        # 更新模拟数据
        self.symbol_data["content"] = self.content
        self.symbol_data["location"]["block_range"] = self.block_range
        self.symbol_data["location"]["end_line"] = 1
        self.symbol_data["location"]["end_col"] = 0  # 修复结束列位置

        result = llm_query.get_symbol_detail(self.symbol_name)
        self.assertIsNotNone(result)
        self.assertEqual(result["block_range"], self.block_range)
        self.assertEqual(result["code_range"], self.code_range)


class TestFileRange(unittest.TestCase):
    def test_file_range_patch(self):
        """测试文件范围补丁解析"""
        # 模拟包含文件范围的响应内容
        response = """
[modified block]: example.py:10-20
[source code start]
def new_function():
    print("Added by patch")
[source code end]
        """
        parser = BlockPatchResponse()
        results = parser.parse(response)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "example.py:10-20")
        self.assertIn("new_function", results[0][1])


if __name__ == "__main__":
    unittest.main()

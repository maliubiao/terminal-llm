import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch
from urllib.parse import unquote, urlparse

from fastapi.testclient import TestClient

import tree
from lsp import GenericLSPClient
from tree import (
    BlockPatch,
    CodeMapBuilder,
    NodeProcessor,
    NodeTypes,
    ParserLoader,
    ParserUtil,
    RipgrepSearcher,
    SearchConfig,
    SourceSkeleton,
    SymbolTrie,
    app,
    get_symbol_context_api,
    init_symbol_database,
    insert_symbol,
    search_symbols_api,
    symbol_completion,
    symbol_completion_realtime,
    symbol_completion_simple,
)


class TestSourceFrameworkParser(unittest.TestCase):
    def setUp(self):
        self.parser_loader = ParserLoader()
        self.parser = SourceSkeleton(self.parser_loader)

    def create_temp_file(self, code: str) -> str:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".py") as f:
            f.write(code)
            return f.name

    def test_class_with_decorated_method(self):
        code = dedent(
            """
        class MyClass:
            @decorator1
            @decorator2
            def my_method(self):
                \"\"\"Method docstring\"\"\"
                a = 1
                b = 2
                return a + b
        """
        )
        expected = dedent(
            """
        # Auto-generated code skeleton

        class MyClass:
            @decorator1
            @decorator2
            def my_method(self):
                \"\"\"Method docstring\"\"\"
                pass  # Placeholder
        """
        ).strip()

        path = self.create_temp_file(code)
        result = self.parser.generate_framework(path).strip()
        os.unlink(path)

        self.assertEqual(result, expected)

    def test_module_level_elements(self):
        code = dedent(
            """
        \"\"\"Module docstring\"\"\"

        import os
        from sys import path

        VALUE = 100

        @class_decorator
        class MyClass:
            pass
        """
        )
        expected = dedent(
            """
        # Auto-generated code skeleton

        \"\"\"Module docstring\"\"\"
        import os
        from sys import path
        VALUE = 100
        @class_decorator
        class MyClass:
            pass  # Placeholder
        """
        ).strip()

        path = self.create_temp_file(code)
        result = self.parser.generate_framework(path).strip()
        os.unlink(path)

        self.assertEqual(result, expected)


class TestBlockPatch(unittest.TestCase):
    def setUp(self):
        self.temp_files = []

    def tearDown(self):
        for file in self.temp_files:
            if os.path.exists(file):
                os.unlink(file)

    def create_temp_file(self, code: str) -> str:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".py") as f:
            f.write(code)
            self.temp_files.append(f.name)
            return f.name

    def _find_byte_range(self, content: bytes, target: bytes) -> tuple[int, int]:
        """通过字符串查找确定字节范围"""
        start = content.find(target)
        if start == -1:
            raise ValueError(f"未找到目标字符串: {target}")
        return (start, start + len(target))

    def test_basic_patch(self):
        code = dedent(
            """
            def foo():
                return 1

            def bar():
                return 2
            """
        )
        file_path = self.create_temp_file(code)

        with open(file_path, "rb") as f:
            content = f.read()
            patch_range = self._find_byte_range(content, b"return 1")

        basic_patch = BlockPatch(
            file_paths=[file_path],
            patch_ranges=[patch_range],
            block_contents=[b"return 1"],
            update_contents=[b"return 10"],
        )

        diff = basic_patch.generate_diff()
        self.assertIn("-    return 1", diff)
        self.assertIn("+    return 10", diff)

        patched_files = basic_patch.apply_patch()
        self.assertIn(file_path, patched_files)
        self.assertIn(b"return 10", patched_files[file_path])

    def test_multiple_patches(self):
        code = dedent(
            """
            def foo():
                return 1

            def bar():
                return 2
            """
        )
        file_path = self.create_temp_file(code)

        with open(file_path, "rb") as f:
            content = f.read()
            patch_range1 = self._find_byte_range(content, b"return 1")
            patch_range2 = self._find_byte_range(content, b"return 2")

        multiple_patch = BlockPatch(
            file_paths=[file_path, file_path],
            patch_ranges=[patch_range1, patch_range2],
            block_contents=[b"return 1", b"return 2"],
            update_contents=[b"return 10", b"return 20"],
        )

        diff = multiple_patch.generate_diff()
        self.assertIn("-    return 1", diff)
        self.assertIn("+    return 10", diff)
        self.assertIn("-    return 2", diff)
        self.assertIn("+    return 20", diff)

        patched_files = multiple_patch.apply_patch()
        self.assertIn(file_path, patched_files)
        self.assertIn(b"return 10", patched_files[file_path])
        self.assertIn(b"return 20", patched_files[file_path])

    def test_invalid_patch(self):
        code = dedent(
            """
            def foo():
                return 1
            """
        )
        file_path = self.create_temp_file(code)

        with open(file_path, "rb") as f:
            content = f.read()
            patch_range = self._find_byte_range(content, b"return 1")

        invalid_patch = BlockPatch(
            file_paths=[file_path],
            patch_ranges=[patch_range],
            block_contents=[b"return 2"],
            update_contents=[b"return 10"],
        )
        with self.assertRaises(ValueError):
            invalid_patch.generate_diff()

        overlap_patch = BlockPatch(
            file_paths=[file_path, file_path],
            patch_ranges=[(patch_range[0], patch_range[1] - 2), (patch_range[0] + 2, patch_range[1])],
            block_contents=[b"return 1", b"return 1"],
            update_contents=[b"return 10", b"return 10"],
        )
        with self.assertRaises(ValueError):
            overlap_patch.generate_diff()

    def test_no_changes(self):
        code = dedent(
            """
            def foo():
                return 1
            """
        )
        file_path = self.create_temp_file(code)

        with open(file_path, "rb") as f:
            content = f.read()
            patch_range = self._find_byte_range(content, b"return 1")

        nochange_patch = BlockPatch(
            file_paths=[file_path],
            patch_ranges=[patch_range],
            block_contents=[b"return 1"],
            update_contents=[b"return 1"],
        )

        self.assertEqual(nochange_patch.generate_diff(), "")
        self.assertEqual(nochange_patch.apply_patch(), {})

    def test_multiple_files(self):
        code1 = dedent(
            """
            def foo():
                return 1
            """
        )
        code2 = dedent(
            """
            def bar():
                return 2
            """
        )
        file1 = self.create_temp_file(code1)
        file2 = self.create_temp_file(code2)

        with open(file1, "rb") as f:
            content = f.read()
            patch_range1 = self._find_byte_range(content, b"return 1")

        with open(file2, "rb") as f:
            content = f.read()
            patch_range2 = self._find_byte_range(content, b"return 2")

        multifile_patch = BlockPatch(
            file_paths=[file1, file2],
            patch_ranges=[patch_range1, patch_range2],
            block_contents=[b"return 1", b"return 2"],
            update_contents=[b"return 10", b"return 20"],
        )

        diff = multifile_patch.generate_diff()
        self.assertIn("-    return 1", diff)
        self.assertIn("+    return 10", diff)
        self.assertIn("-    return 2", diff)
        self.assertIn("+    return 20", diff)

        patched_files = multifile_patch.apply_patch()
        self.assertIn(file1, patched_files)
        self.assertIn(file2, patched_files)
        self.assertIn(b"return 10", patched_files[file1])
        self.assertIn(b"return 20", patched_files[file2])

    def test_insert_patch(self):
        code = dedent(
            """
            def foo():
                return 1
            """
        )
        file_path = self.create_temp_file(code)

        with open(file_path, "rb") as f:
            content = f.read()
            insert_pos = content.find(b"return 1")

        insert_patch = BlockPatch(
            file_paths=[file_path],
            patch_ranges=[(insert_pos, insert_pos)],
            block_contents=[b""],
            update_contents=[b"print('inserted')\n    "],
        )

        diff = insert_patch.generate_diff()
        self.assertIn("+    print('inserted')", diff)

        patched_files = insert_patch.apply_patch()
        self.assertIn(file_path, patched_files)
        self.assertIn(b"print('inserted')", patched_files[file_path])
        self.assertIn(b"return 1", patched_files[file_path])


class TestParserUtil(unittest.TestCase):
    def setUp(self):
        self.parser_loader = ParserLoader()
        self.node_processor = NodeProcessor()
        self.code_map_builder = CodeMapBuilder(self.node_processor)
        self.parser_util = ParserUtil(self.parser_loader)
        self.lsp_client = GenericLSPClient(
            lsp_command=["pylsp"],
            workspace_path=os.path.dirname(__file__),
            init_params={"rootUri": f"file://{os.path.dirname(__file__)}"},
        )
        self.lsp_client.start()

    def tearDown(self):
        if self.lsp_client.running:
            asyncio.run(self.lsp_client.shutdown())

    def create_temp_file(self, code: str, suffix=".py") -> str:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=suffix) as f:
            f.write(code)
            return f.name

    def test_go_method_extraction(self):
        """测试Go方法符号提取"""
        code = """
        package main

        type MyStruct struct {}

        func (m MyStruct) Method1() {}
        func (m *MyStruct) Method2() {}
        func (_ MyStruct) Method3() {}
        func (MyStruct) Method4() {}
        """
        file_path = self.create_temp_file(code, suffix=".go")
        paths, code_map = self.parser_util.get_symbol_paths(file_path)

        expected_symbols = [
            "main.MyStruct",
            "main.MyStruct.Method1",
            "main.MyStruct.Method2",
            "main.MyStruct.Method3",
            "main.MyStruct.Method4",
        ]
        self.assertCountEqual([s for s in paths if s.startswith("main.MyStruct")], expected_symbols)

    def test_go_function_extraction(self):
        """测试Go函数符号提取"""
        code = """
        package main

        func Function1() {}
        func Function2() int { return 0 }
        func Function3(param string) {}
        """
        file_path = self.create_temp_file(code, suffix=".go")
        paths, code_map = self.parser_util.get_symbol_paths(file_path)

        expected_symbols = [
            "main.Function1",
            "main.Function2",
            "main.Function3",
        ]
        self.assertCountEqual([s for s in paths if s.startswith("main.Function")], expected_symbols)

    def test_go_nested_receiver_extraction(self):
        """测试嵌套接收器方法符号提取"""
        code = """
        package main

        type OuterStruct struct {
            InnerStruct struct {
                Value int
            }
        }

        func (o OuterStruct) Method1() {}
        func (o *OuterStruct.InnerStruct) Method2() {}
        """
        file_path = self.create_temp_file(code, suffix=".go")
        paths, code_map = self.parser_util.get_symbol_paths(file_path)

        expected_symbols = [
            "main.OuterStruct",
            "main.OuterStruct.Method1",
            "main.OuterStruct.InnerStruct.Method2",
        ]
        self.assertCountEqual([s for s in paths if s.startswith("main.OuterStruct")], expected_symbols)

    def test_go_anonymous_function_extraction(self):
        """测试匿名函数符号提取"""
        code = """
        package main

        var FuncVar = func() {}
        """
        file_path = self.create_temp_file(code, suffix=".go")
        paths, code_map = self.parser_util.get_symbol_paths(file_path)

        # 匿名函数不应被提取为符号
        self.assertNotIn("main.FuncVar", paths)

    def test_go_empty_receiver_extraction(self):
        """测试空接收器方法符号提取"""
        code = """
        package main

        type MyStruct struct {}

        func () Method1() {}
        """
        file_path = self.create_temp_file(code, suffix=".go")
        paths, code_map = self.parser_util.get_symbol_paths(file_path)

        # 空接收器方法不应被提取为符号
        self.assertNotIn("main.Method1", paths)

    def test_go_type_declaration_extraction(self):
        """测试Go类型声明符号提取"""
        code = """
        package main

        type MyInt int
        type MyStruct struct {
            Field1 int
            Field2 string
        }
        """
        file_path = self.create_temp_file(code, suffix=".go")
        paths, code_map = self.parser_util.get_symbol_paths(file_path)

        expected_symbols = [
            "main.MyInt",
            "main.MyStruct",
        ]
        self.assertCountEqual([s for s in paths if s.startswith("main.My")], expected_symbols)

    def test_go_import_declaration_extraction(self):
        """测试Go导入声明符号提取"""
        code = """
        package main

        import (
            "fmt"
            "math"
        )
        """
        file_path = self.create_temp_file(code, suffix=".go")
        paths, code_map = self.parser_util.get_symbol_paths(file_path)

        self.assertIn("__import__", paths)
        self.assertIn("fmt", code_map["__import__"]["code"])
        self.assertIn("math", code_map["__import__"]["code"])

    def test_go_package_clause_extraction(self):
        """测试Go包声明符号提取"""
        code = """
        package main
        """
        file_path = self.create_temp_file(code, suffix=".go")
        paths, code_map = self.parser_util.get_symbol_paths(file_path)

        self.assertIn("__import__", paths)
        self.assertIn("package main", code_map["__import__"]["code"])


class TestSymbolPaths(TestParserUtil):
    def test_get_symbol_paths(self):
        code = dedent(
            """
            class MyClass:
                def my_method(self):
                    pass
            """
        )
        path = self.create_temp_file(code)
        paths, code_map = self.parser_util.get_symbol_paths(path)
        os.unlink(path)

        expected_paths = ["MyClass", "MyClass.my_method"]
        self.assertEqual(sorted(paths), sorted(expected_paths))

        for path_key in expected_paths:
            self.assertIn(path_key, code_map)
            self.assertIn("code", code_map[path_key])
            self.assertIn("block_range", code_map[path_key])
        self.assertIn("start_line", code_map[path_key])
        self.assertIn("end_line", code_map[path_key])

    def test_main_block_detection(self):
        code = dedent(
            """
            if __name__ == "__main__":
                print("Hello World")
            """
        )
        path = self.create_temp_file(code)
        paths, code_map = self.parser_util.get_symbol_paths(path)
        os.unlink(path)

        self.assertIn("__main__", paths)
        self.assertIn("__main__", code_map)
        self.assertIn("code", code_map["__main__"])
        self.assertEqual(code_map["__main__"]["code"].strip(), 'if __name__ == "__main__":\n    print("Hello World")')


class TestGoTypeAndFunctionAndMethod(TestParserUtil):
    def test_go_type_struct_definition(self):
        code = dedent(
            """  
            package main  
  
            type MyStruct struct {
                Field1 string
                Field2 int
            }
            """
        )
        path = self.create_temp_file(code, suffix=".go")
        paths, code_map = self.parser_util.get_symbol_paths(path)
        os.unlink(path)

        self.assertIn("main.MyStruct", paths)
        self.assertIn("main.MyStruct", code_map)
        self.assertEqual(
            code_map["main.MyStruct"]["code"].strip(), "type MyStruct struct {\n    Field1 string\n    Field2 int\n}"
        )

    def test_go_commented_function_extraction(self):
        """测试带注释的Go函数符号提取"""
        code = dedent(
            """  
            package main  
  
            // Function1的注释  
            func Function1() {}  
  
            /* 
            Function2的多行注释 
            */  
            func Function2() int { return 0 }  
  
            // 带参数的函数注释  
            func Function3(param string) {}  
        """
        )
        path = self.create_temp_file(code, suffix=".go")
        paths, code_map = self.parser_util.get_symbol_paths(path)
        os.unlink(path)

        # 验证符号提取
        expected_symbols = ["main.Function1", "main.Function2", "main.Function3"]
        self.assertCountEqual([s for s in paths if s.startswith("main.Function")], expected_symbols)

        # 验证注释包含在代码块中
        self.assertIn("// Function1的注释", code_map["main.Function1"]["code"])
        self.assertIn("/* \nFunction2的多行注释 \n*/", code_map["main.Function2"]["code"])
        self.assertIn("// 带参数的函数注释", code_map["main.Function3"]["code"])

        # 验证函数体完整性
        self.assertIn("func Function1() {}", code_map["main.Function1"]["code"])
        self.assertIn("func Function2() int { return 0 }", code_map["main.Function2"]["code"])
        self.assertIn("func Function3(param string) {}", code_map["main.Function3"]["code"])


class TestImportBlocks(TestParserUtil):
    def test_import_block_detection(self):
        code = dedent(
            """
            # This is a comment
            import os
            import sys
            """
        )
        path = self.create_temp_file(code)
        paths, code_map = self.parser_util.get_symbol_paths(path)
        os.unlink(path)

        self.assertIn("__import__", paths)
        self.assertIn("__import__", code_map)
        self.assertIn("code", code_map["__import__"])
        self.assertEqual(code_map["__import__"]["code"].strip(), "# This is a comment\nimport os\nimport sys")

    def test_import_block_with_strings_and_from_import(self):
        code = dedent(
            """
            # 模块注释
            "文档字符串"
            """
            """\n            aaa\n            bbb\n            """
            """
            import os
            from sys import version
            import sys as sys1
        """
        )
        path = self.create_temp_file(code)
        paths, code_map = self.parser_util.get_symbol_paths(path)
        os.unlink(path)

        self.assertIn("__import__", paths)
        import_entry = code_map["__import__"]
        self.assertIn("# 模块注释", import_entry["code"])
        self.assertIn('"文档字符串"', import_entry["code"])
        self.assertIn("aaa\nbbb", import_entry["code"])
        self.assertIn("import os", import_entry["code"])
        self.assertIn("from sys import version", import_entry["code"])
        self.assertIn("import sys as sys1", import_entry["code"])

    def test_go_import_block_detection(self):
        code = dedent(
            """
            package main

            import (
                "fmt"
                "math/rand"
                "os"
            )
            """
        )
        path = self.create_temp_file(code, suffix=".go")
        paths, code_map = self.parser_util.get_symbol_paths(path)
        os.unlink(path)

        self.assertIn("__import__", paths)
        import_entry = code_map["__import__"]
        self.assertIn('"fmt"', import_entry["code"])
        self.assertIn('"math/rand"', import_entry["code"])
        self.assertIn('"os"', import_entry["code"])


class TestCallAnalysis(TestParserUtil):
    def _find_byte_position(self, code_bytes: bytes, substring: str) -> tuple:
        start = code_bytes.find(substring.encode("utf8"))
        if start == -1:
            return (0, 0)
        return (start, start + len(substring.encode("utf8")))

    def _convert_bytes_to_points(self, code_bytes: bytes, start_byte: int, end_byte: int) -> tuple:
        before_start = code_bytes[:start_byte]
        start_line = before_start.count(b"\n")
        start_col = start_byte - (before_start.rfind(b"\n") + 1) if b"\n" in before_start else start_byte

        before_end = code_bytes[:end_byte]
        end_line = before_end.count(b"\n")
        end_col = end_byte - (before_end.rfind(b"\n") + 1) if b"\n" in before_end else end_byte

        return (start_line, start_col, end_line, end_col)

    def test_function_call_extraction(self):
        code = dedent(
            """
            def some_function():
                pass

            class A:
                class B:
                    @staticmethod
                    def f():
                        pass

            class MyClass:
                def other_method(self):
                    pass

                @A.B.f()
                def my_method(self):
                    A.B.f()
                    self.other_method()
                    some_function()
                    self.attr
                    A.B.f
            """
        )
        path = self.create_temp_file(code)
        paths, code_map = self.parser_util.get_symbol_paths(path)

        with open(path, "rb") as f:
            code_bytes = f.read()
        os.unlink(path)

        expected_paths = ["A", "A.B", "A.B.f", "MyClass", "MyClass.other_method", "MyClass.my_method", "some_function"]
        method_entry = code_map["MyClass.my_method"]

        self.assertEqual(sorted(paths), sorted(expected_paths))

        actual_calls = method_entry["calls"]
        expected_call_names = {"A.B", "A.B.f", "self.other_method", "some_function", "self.attr"}
        actual_call_names = {call["name"] for call in actual_calls}
        self.assertEqual(actual_call_names, expected_call_names)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            temp_path = f.name

        try:
            self.lsp_client.send_notification(
                "textDocument/didOpen",
                {"textDocument": {"uri": f"file://{temp_path}", "languageId": "python", "version": 1, "text": code}},
            )

            for call in actual_calls:
                line = call["start_point"][0] + 1
                char = call["start_point"][1] + 1

                definition = asyncio.run(self.lsp_client.get_definition(temp_path, line, char))
                self.assertTrue(definition is not None, f"未找到 {call['name']} 的定义")

                definitions = definition if isinstance(definition, list) else [definition]
                found_valid = False
                for d in definitions:
                    uri = d.get("uri", "")
                    if uri.startswith("file://"):
                        def_path = unquote(urlparse(uri).path)
                        if os.path.exists(def_path):
                            found_valid = True
                            break
                self.assertTrue(found_valid, f"未找到有效的文件路径定义: {call['name']}")
        finally:
            os.unlink(temp_path)
            if self.lsp_client.running:
                asyncio.run(self.lsp_client.shutdown())

    def test_parameter_type_calls(self):
        code = dedent(
            """
            from typing import List, Optional
            
            class MyType:
                pass
            
            def example(
                a: int,
                b: MyType,
                c: List[MyType],
                d: Optional[List[int]],
                e: "Optional[MyType]",
                f: dict[str, MyType],
                g: tuple[MyType, int],
                h: Sequence[MyType]
            ):
                pass
        """
        )
        path = self.create_temp_file(code)
        _, code_map = self.parser_util.get_symbol_paths(path)
        os.unlink(path)

        entry = code_map["example"]
        call_names = {call["name"] for call in entry["calls"]}
        expected_calls = {"MyType"}
        self.assertEqual(call_names, expected_calls)

    def test_find_symbol_by_location(self):
        code = dedent(
            """
            class Outer:
                class Inner:
                    def nested_method(self):
                        def local_function():
                            pass
                        local_variable = 42
            """
        )
        path = self.create_temp_file(code)
        _, code_map = self.parser_util.get_symbol_paths(path)

        expected_symbols = [
            "Outer",
            "Outer.Inner",
            "Outer.Inner.nested_method",
            "Outer.Inner.nested_method.local_function",
            "Outer.Inner.nested_method.local_variable",
        ]
        for symbol in expected_symbols:
            self.assertIn(symbol, code_map, f"符号 {symbol} 未在code_map中找到")

        def test_symbol_position(symbol_path):
            info = code_map[symbol_path]
            symbols = self.parser_util.find_symbols_by_location(code_map, info["start_line"], info["start_col"])
            found_symbols = [s["symbol"] for s in symbols]
            self.assertIn(symbol_path, found_symbols, f"在起始位置未找到符号 {symbol_path}")
            mid_line = (info["start_line"] + info["end_line"]) // 2
            mid_col = (info["start_col"] + info["end_col"]) // 2
            symbols = self.parser_util.find_symbols_by_location(code_map, mid_line, mid_col)
            found_symbols = [s["symbol"] for s in symbols]
            self.assertIn(symbol_path, found_symbols, f"在中间位置未找到符号 {symbol_path}")

        test_symbol_position("Outer")
        test_symbol_position("Outer.Inner")
        test_symbol_position("Outer.Inner.nested_method")
        test_symbol_position("Outer.Inner.nested_method.local_function")

        nested_info = code_map["Outer.Inner.nested_method.local_function"]
        symbols = self.parser_util.find_symbols_by_location(
            code_map, nested_info["start_line"], nested_info["start_col"]
        )
        found_symbols = [s["symbol"] for s in symbols]
        expected_symbols = [
            "Outer.Inner.nested_method.local_function",
            "Outer.Inner.nested_method",
            "Outer.Inner",
            "Outer",
        ]
        self.assertEqual(found_symbols, expected_symbols)

        os.unlink(path)

    def test_batch_find_symbols(self):
        code = dedent(
            """
            class Alpha:
                def method_a(self):
                    pass

            class Beta:
                def method_b(self):
                    pass
        """
        )
        path = self.create_temp_file(code)
        _, code_map = self.parser_util.get_symbol_paths(path)

        with open(path, "rb") as f:
            code_bytes = f.read()

        alpha_pos = self._find_byte_position(code_bytes, "class Alpha:")
        alpha_start_line, alpha_start_col, _, _ = self._convert_bytes_to_points(
            code_bytes, alpha_pos[0], alpha_pos[0] + 1
        )

        method_a_pos = self._find_byte_position(code_bytes, "def method_a(self):")
        method_a_start_line, method_a_start_col, _, _ = self._convert_bytes_to_points(
            code_bytes, method_a_pos[0], method_a_pos[0] + 1
        )

        beta_pos = self._find_byte_position(code_bytes, "class Beta:")
        beta_start_line, beta_start_col, _, _ = self._convert_bytes_to_points(code_bytes, beta_pos[0], beta_pos[0] + 1)

        method_b_pos = self._find_byte_position(code_bytes, "def method_b(self):")
        method_b_start_line, method_b_start_col, _, _ = self._convert_bytes_to_points(
            code_bytes, method_b_pos[0], method_b_pos[0] + 1
        )

        test_locations = [
            (alpha_start_line, alpha_start_col),
            (alpha_start_line, alpha_start_col + 5),
            (method_a_start_line, method_a_start_col),
            (method_a_start_line, method_a_start_col + 3),
            (beta_start_line, beta_start_col),
            (beta_start_line, beta_start_col + 4),
            (method_b_start_line, method_b_start_col),
            (method_b_start_line, method_b_start_col + 3),
        ]

        symbols = self.parser_util.find_symbols_for_locations(code_map, test_locations)

        expected_symbols = {
            "Alpha": code_map["Alpha"],
            "Beta": code_map["Beta"],
        }
        self.assertEqual(symbols.keys(), expected_symbols.keys())

        os.unlink(path)


class TestNodeType(TestParserUtil):
    def test_node_type_checks(self):
        """测试NodeTypes中的类型检查方法"""
        test_cases = [
            (
                NodeTypes.is_module,
                [
                    (NodeTypes.MODULE, True),
                    (NodeTypes.TRANSLATION_UNIT, True),
                    (NodeTypes.GO_SOURCE_FILE, True),
                    (NodeTypes.CLASS_DEFINITION, False),
                ],
            ),
            (
                NodeTypes.is_import,
                [
                    (NodeTypes.IMPORT_STATEMENT, True),
                    (NodeTypes.IMPORT_FROM_STATEMENT, True),
                    (NodeTypes.GO_IMPORT_DECLARATION, True),
                    (NodeTypes.MODULE, False),
                ],
            ),
            (
                NodeTypes.is_definition,
                [
                    (NodeTypes.CLASS_DEFINITION, True),
                    (NodeTypes.FUNCTION_DEFINITION, True),
                    (NodeTypes.DECORATED_DEFINITION, True),
                    (NodeTypes.GO_FUNC_DECLARATION, True),
                    (NodeTypes.IMPORT_STATEMENT, False),
                ],
            ),
            (
                NodeTypes.is_statement,
                [
                    (NodeTypes.EXPRESSION_STATEMENT, True),
                    (NodeTypes.IF_STATEMENT, True),
                    (NodeTypes.CALL, True),
                    (NodeTypes.ASSIGNMENT, True),
                    (NodeTypes.MODULE, False),
                ],
            ),
            (
                NodeTypes.is_identifier,
                [
                    (NodeTypes.IDENTIFIER, True),
                    (NodeTypes.NAME, True),
                    (NodeTypes.WORD, True),
                    (NodeTypes.GO_PACKAGE_IDENTIFIER, True),
                    (NodeTypes.MODULE, False),
                ],
            ),
            (
                NodeTypes.is_type,
                [
                    (NodeTypes.TYPED_PARAMETER, True),
                    (NodeTypes.TYPED_DEFAULT_PARAMETER, True),
                    (NodeTypes.GENERIC_TYPE, True),
                    (NodeTypes.UNION_TYPE, True),
                    (NodeTypes.MODULE, False),
                ],
            ),
        ]

        for checker, cases in test_cases:
            for node_type, expected in cases:
                with self.subTest(checker=checker.__name__, node_type=node_type):
                    self.assertEqual(checker(node_type), expected)


class TestRipGrepSearch(unittest.TestCase):
    def setUp(self):
        self.base_config = SearchConfig(
            root_dir=Path.cwd(),
            exclude_dirs=["vendor", "node_modules"],
            exclude_files=["temp.txt"],
            include_dirs=["src"],
            include_files=[".py", ".md"],
            file_types=["py", "md"],
        )
        self.searcher = RipgrepSearcher(self.base_config)

    @patch("subprocess.run")
    def test_basic_search(self, mock_run):
        mock_output = """
{"type":"begin","data":{"path":{"text":"src/main.py"}}}
{"type":"match","data":{"path":{"text":"src/main.py"},"lines":{"text":"def test():"},"line_number":42,"submatches":[{"start":4,"end":8}]}}
{"type":"end","data":{"path":{"text":"src/main.py"},"stats":{"elapsed":{"secs":0,"nanos":12345},"matched_lines":1,"matches":1}}}
""".strip()
        mock_run.return_value = MagicMock(stdout=mock_output, stderr="", returncode=0)

        results = self.searcher.search(["test"], Path("src"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].file_path.name, "main.py")
        self.assertEqual(len(results[0].matches), 1)
        self.assertEqual(results[0].matches[0].line, 42)
        self.assertEqual(results[0].stats.get("matched_lines"), 1)

    @patch("subprocess.run")
    def test_command_generation(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)

        expected = [
            "rg",
            "--json",
            "--smart-case",
            "--trim",
            "--type-add",
            "custom:*.{py,md}",
            "-t",
            "custom",
            "--no-ignore",
            "--regexp",
            "test",
            "--glob",
            "!vendor/**",
            "--glob",
            "!node_modules/**",
            "--glob",
            "!temp.txt",
            "--glob",
            "src/**",
            str(Path.cwd()),
        ]

        self.searcher.search(["test"])
        actual_command = mock_run.call_args[0][0]
        self.assertListEqual(actual_command, expected)

    def test_invalid_search_root(self):
        with self.assertRaises(ValueError):
            self.searcher.search(["test"], Path("/nonexistent"))

    @patch("subprocess.run")
    def test_error_handling(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="invalid pattern", returncode=2)
        with self.assertRaises(RuntimeError):
            self.searcher.search(["[invalid-regex"], Path("."))

    @patch("subprocess.run")
    def test_multiple_patterns(self, mock_run):
        mock_output = """
{"type":"begin","data":{"path":{"text":"src/test.py"}}}
{"type":"match","data":{"path":{"text":"src/test.py"},"lines":{"text":"class ParserUtil:"},"line_number":5,"submatches":[{"start":6,"end":16}]}}
{"type":"end","data":{"path":{"text":"src/test.py"},"stats":{"elapsed":{"secs":0,"nanos":34567},"matched_lines":1,"matches":1}}}
""".strip()
        mock_run.return_value = MagicMock(stdout=mock_output, stderr="", returncode=0)

        results = self.searcher.search(["ParserUtil", "Validator"], Path("src"))
        self.assertEqual(len(results), 1)
        self.assertIn("--regexp ParserUtil", " ".join(mock_run.call_args[0][0]))
        self.assertIn("--regexp Validator", " ".join(mock_run.call_args[0][0]))


class TestSymbolsComplete(unittest.TestCase):
    """
    symbol:a.c/main,d 可补全symbol:a.c/main,debug
    symbol:a.c/m 可补全symbol:a.c/main
    symbol:a.c/main,debug,pr 可补全symbol:a.c/main,debug,print
    symbol:a.c/symbol_a,symbol_ 可补全symbol:a.c/symbol_a,symbol_b
    symbol:multi/level,path,test_ 可补全symbol:multi/level,path,test_case
    """

    def setUp(self):
        """初始化测试环境"""
        # 初始化测试数据
        self.temp_files = []  # 保存临时文件引用
        symbols_dict = {
            "symbol:a.c/debug": [("a.c", "debug()", "debug_hash")],
            "symbol:a.c/main": [("a.c", "main()", "main_hash")],
            "symbol:a.c/print": [("a.c", "print()", "print_hash")],
            "symbol:a.c/symbol_a": [("a.c", "symbol_a", "symbol_b_hash")],
            "symbol:a.c/symbol_b": [("a.c", "symbol_b", "symbol_b_hash")],
        }

        # 创建临时文件并更新路径
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".c", delete=False) as tmp:
            tmp.write("void debug() {}\nvoid main() {}\n")
            self.temp_files.append(tmp)
            symbols_dict = {
                f"symbol:{tmp.name}/debug": [(tmp.name, "debug()", "debug_hash")],
                f"symbol:{tmp.name}/main": [(tmp.name, "main()", "main_hash")],
                f"symbol:{tmp.name}/print": [(tmp.name, "print()", "print_hash")],
                f"symbol:{tmp.name}/symbol_a": [(tmp.name, "symbol_a", "symbol_b_hash")],
                f"symbol:{tmp.name}/symbol_b": [(tmp.name, "symbol_b", "symbol_b_hash")],
            }

        app.state.file_symbol_trie = SymbolTrie.from_symbols(symbols_dict)
        app.state.symbol_trie = SymbolTrie.from_symbols({})
        app.state.file_mtime_cache = {}

    def tearDown(self):
        """清理临时文件"""
        for tmp in self.temp_files:
            try:
                os.unlink(tmp.name)
            except:
                pass

    def test_complete_debug_from_main_d(self):
        tmp = self.temp_files[0]
        prefix = f"symbol:{tmp.name}/main,d"
        expected = f"symbol:{tmp.name}/main,debug"
        results = self._get_completions(prefix)
        self.assertIn(expected, results)

    def test_complete_main_from_m(self):
        tmp = self.temp_files[0]
        prefix = f"symbol:{tmp.name}/m"
        expected = f"symbol:{tmp.name}/main"
        results = self._get_completions(prefix)
        self.assertIn(expected, results)

    def test_complete_print_in_multi_symbol_context(self):
        tmp = self.temp_files[0]
        prefix = f"symbol:{tmp.name}/main,debug,pr"
        expected = f"symbol:{tmp.name}/main,debug,print"
        results = self._get_completions(prefix)
        self.assertIn(expected, results)

    def test_complete_symbol_b_after_symbol_a(self):
        tmp = self.temp_files[0]
        prefix = f"symbol:{tmp.name}/symbol_a,symbol_"
        expected = f"symbol:{tmp.name}/symbol_a,symbol_b"
        results = self._get_completions(prefix)
        self.assertIn(expected, results)

    # 修改后的测试用例使用实际文件路径
    def test_get_valid_symbol_content(self):
        """测试正常获取符号内容"""
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".c", delete=False) as tmp:
            tmp.write("void main() {\n  // main function\n}\n")
            tmp.flush()
            tmp.seek(0)
            self.temp_files.append(tmp)

            # 使用实际文件路径构造symbol路径
            symbol_path = f"symbol:{tmp.name}/main"
            app.state.file_symbol_trie.insert(
                symbol_path,
                {
                    "file_path": tmp.name,
                    "location": ((1, 0), (3, 1), (0, len(tmp.read()))),
                },
            )

            test_client = TestClient(app)
            response = test_client.get(f"/symbol_content?symbol_path={symbol_path}")
            self.assertEqual(response.status_code, 200)
            self.assertIn("void main()", response.text)

    def test_get_multiple_symbols_content(self):
        """测试获取多个符号内容"""
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".c", delete=False) as tmp:
            tmp.write("void main() {}\nvoid debug() {}\n")
            tmp.flush()
            self.temp_files.append(tmp)

            # 使用实际文件路径构造symbol路径
            main_symbol = f"symbol:{tmp.name}/main"
            debug_symbol = f"symbol:{tmp.name}/debug"

            app.state.file_symbol_trie.insert(
                main_symbol,
                {
                    "file_path": tmp.name,
                    "location": ((1, 0), (1, 13), (0, 13)),
                },
            )
            app.state.file_symbol_trie.insert(
                debug_symbol,
                {
                    "file_path": tmp.name,
                    "location": ((2, 0), (2, 14), (14, 28)),
                },
            )

            test_client = TestClient(app)
            response = test_client.get(f"/symbol_content?symbol_path=symbol:{tmp.name}/main,debug")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text.count("\n\n"), 1)
            self.assertIn("void main()", response.text)
            self.assertIn("void debug()", response.text)

    def test_json_response_format(self):
        """测试JSON响应格式"""
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".c", delete=False) as tmp:
            tmp.write("void main() {\n  // test json\n}\n")
            tmp.flush()
            tmp.seek(0)
            self.temp_files.append(tmp)

            full_content = tmp.read()
            start = 0
            end = len(full_content)
            symbol_path = f"symbol:{tmp.name}/main"

            app.state.file_symbol_trie.insert(
                symbol_path,
                {
                    "file_path": tmp.name,
                    "location": ((1, 0), (3, 1), (start, end)),
                },
            )

            test_client = TestClient(app)
            response = test_client.get(f"/symbol_content?symbol_path={symbol_path}&json_format=true")
            self.assertEqual(response.status_code, 200)
            json_data = response.json()
            self.assertEqual(json_data[0]["location"]["start_line"], 1)
            self.assertEqual(json_data[0]["location"]["end_line"], 3)
            self.assertIn("void main()", json_data[0]["content"])

    def _get_completions(self, prefix: str) -> list:
        test_client = TestClient(app)
        response = test_client.get(f"/complete_realtime?prefix={prefix}")
        return response.text.splitlines()


class TestSymbolsAPI(unittest.TestCase):
    def setUp(self):
        """初始化测试环境"""
        self.conn = sqlite3.connect(":memory:")
        tree.get_db_connection = lambda: self.conn
        init_symbol_database(self.conn)
        self.test_symbols = [
            {
                "name": "main_function",
                "file_path": "file",  # 修改为相对路径
                "type": "function",
                "signature": "def main_function()",
                "body": "pass",
                "full_definition": "def main_function(): pass",
                "calls": ["helper_function", "undefined_function"],
            },
            {
                "name": "helper_function",
                "file_path": "file",  # 修改为相对路径
                "type": "function",
                "signature": "def helper_function()",
                "body": "pass",
                "full_definition": "def helper_function(): pass",
                "calls": [],
            },
            {
                "name": "calculate_sum",
                "file_path": "file",  # 修改为相对路径
                "type": "function",
                "signature": "def calculate_sum(a, b)",
                "body": "return a + b",
                "full_definition": "def calculate_sum(a, b): return a + b",
                "calls": [],
            },
            {
                "name": "compute_average",
                "file_path": "file",  # 修改为相对路径
                "type": "function",
                "signature": "def compute_average(values)",
                "body": "return sum(values) / len(values)",
                "full_definition": "def compute_average(values): return sum(values) / len(values)",
                "calls": [],
            },
            {
                "name": "init_module",
                "file_path": "file",  # 修改为相对路径
                "type": "module",
                "signature": "",
                "body": "",
                "full_definition": "",
                "calls": [],
            },
            {
                "name": "symbol:test/symbol",
                "file_path": "symbol.py",  # 修改为相对路径
                "type": "symbol",
                "signature": "",
                "body": "",
                "full_definition": "",
                "calls": [],
            },
        ]

        self.trie = SymbolTrie.from_symbols({})
        app.state.symbol_trie = self.trie
        app.state.file_symbol_trie = self.trie
        app.state.file_mtime_cache = {}

        for symbol in self.test_symbols:
            insert_symbol(self.conn, symbol)

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        """清理测试环境"""
        self.loop.close()
        self.conn.execute("DELETE FROM symbols WHERE file_path = ?", ("file",))  # 同步修改清理路径
        self.conn.commit()
        self.conn.close()

    def test_search_functionality(self):
        """测试符号搜索功能"""

        async def run_tests():
            response = await search_symbols_api("main", 10)
            self.assertEqual(len(response["results"]), 1)

            response = await search_symbols_api("main_function", 10)
            self.assertEqual(len(response["results"]), 1)
            self.assertEqual(response["results"][0]["name"], "main_function")

        self.loop.run_until_complete(run_tests())

    def test_symbol_context(self):
        """测试获取符号上下文"""

        async def run_tests():
            response = await get_symbol_context_api("main_function", "file")  # 同步修改路径参数
            self.assertEqual(response["symbol_name"], "main_function")
            self.assertGreaterEqual(len(response["definitions"]), 2)

            response = await get_symbol_context_api("nonexistent", "file")
            self.assertIn("error", response)

        self.loop.run_until_complete(run_tests())

    def test_completion_features(self):
        """测试补全功能"""

        async def run_tests():
            # 标准补全
            response = await symbol_completion("calc")
            self.assertEqual(len(response["completions"]), 1)
            self.assertEqual(response["completions"][0]["name"], "calculate_sum")

            # 空结果补全
            response = await symbol_completion("xyz")
            self.assertEqual(len(response["completions"]), 0)

            # 简单补全
            response = await symbol_completion_simple("calc")
            self.assertIn(b"symbol:file/calculate_sum", response.body)

            # 路径补全
            response = await symbol_completion_simple("symbol:test/")
            self.assertIn(b"symbol:test/symbol", response.body)

            # 实时补全
            response = await symbol_completion_realtime("symbol:file", 10)
            self.assertIn(b"main_function", response.body)
            self.assertIn(b"helper_function", response.body)

            # 无效路径补全
            response = await symbol_completion_realtime("symbol:nonexistent", 10)
            self.assertEqual(response.body, b"")

        self.loop.run_until_complete(run_tests())


class TestExtractIdentifiablePath(unittest.TestCase):
    def setUp(self):
        self.cur_dir = os.path.dirname(os.path.abspath(__file__))

    def test_relative_within_current_dir(self):
        rel_path = "test_data/sample.py"
        result = tree.extract_identifiable_path(rel_path)
        self.assertEqual(result, rel_path)

    def test_absolute_within_current_dir(self):
        abs_path = os.path.join(self.cur_dir, "utils/helper.py")
        expected = os.path.join("utils", "helper.py")
        self.assertEqual(tree.extract_identifiable_path(abs_path), expected)

    def test_relative_outside_current_dir(self):
        rel_path = "../../external/module.py"
        expected = os.path.abspath(os.path.join(self.cur_dir, rel_path))
        self.assertEqual(tree.extract_identifiable_path(rel_path), expected)

    def test_absolute_outside_current_dir(self):
        abs_path = "/tmp/another_project/main.py"
        self.assertEqual(tree.extract_identifiable_path(abs_path), abs_path)

    def test_init_py_normal_handling(self):
        rel_path = "package/__init__.py"
        self.assertEqual(tree.extract_identifiable_path(rel_path), rel_path)


class TestLSPIntegration(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.temp_files = []
        self.lsp_client = GenericLSPClient(
            lsp_command=["pylsp"],
            workspace_path=os.path.dirname(__file__),
            init_params={"rootUri": f"file://{os.path.dirname(__file__)}"},
        )
        app.state.LSP_CLIENT = self.lsp_client
        self.lsp_client.start()
        # 确保初始化完成
        if not self.lsp_client.initialized_event.wait(timeout=5):
            raise RuntimeError("LSP client failed to initialize")
        # 强制设置textDocumentSync能力为Full模式用于测试
        self.lsp_client.capabilities.text_document_sync = {"change": 1}

    def tearDown(self):
        if self.lsp_client.running:
            asyncio.run(self.lsp_client.shutdown())
        for tmp in self.temp_files:
            try:
                os.unlink(tmp.name)
            except:
                pass

    def test_did_change_success(self):
        """测试正常文档变更通知"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
            tmp.write("def test(): pass")
            tmp.flush()  # 确保内容写入磁盘
            self.temp_files.append(tmp)

        # 先发送didOpen通知初始化文档
        self.lsp_client.send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": f"file://{tmp.name}",
                    "languageId": "python",
                    "version": 1,
                    "text": "def test(): pass",
                }
            },
        )

        response = self.client.post(
            "/lsp/didChange", data={"file_path": tmp.name, "content": "def test(): pass\nprint('updated')"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("success", response.json()["status"])

    def test_missing_parameters(self):
        """测试缺少必要参数"""
        response = self.client.post("/lsp/didChange", data={})
        self.assertEqual(response.status_code, 422)
        # 检查错误详情结构
        detail = response.json()["detail"]
        missing_fields = {err["loc"][-1] for err in detail if err["type"] == "missing"}
        self.assertIn("file_path", missing_fields)
        self.assertIn("content", missing_fields)

    def test_client_not_initialized(self):
        """测试客户端未初始化场景"""
        app.state.LSP_CLIENT = None
        response = self.client.post("/lsp/didChange", data={"file_path": "test.py", "content": "content"})
        self.assertEqual(response.status_code, 501)
        self.assertIn("not initialized", response.json()["message"])

    def test_unsupported_feature(self):
        """测试不支持的文档同步功能"""
        # 修改能力对象以模拟不支持Full模式
        original_capabilities = self.lsp_client.capabilities
        self.lsp_client.capabilities.text_document_sync = 2  # 设置为非Full模式

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
            self.temp_files.append(tmp)

        response = self.client.post("/lsp/didChange", data={"file_path": tmp.name, "content": "content"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("textDocumentSync Full", response.json()["message"])

        # 恢复原始能力对象
        self.lsp_client.capabilities = original_capabilities

    def test_server_error_handling(self):
        """测试服务端异常处理"""
        with patch.object(GenericLSPClient, "did_change", side_effect=Exception("mock error")):
            response = self.client.post("/lsp/didChange", data={"file_path": "test.py", "content": "content"})
            self.assertEqual(response.status_code, 500)
            self.assertIn("Internal server error", response.json()["message"])


class TestSentenceSegments:
    client = TestClient(app)

    def test_should_extract_identifiers(self):
        response = self.client.get("/extract_identifier?text=我们试试看ParserUtil Python TestCase")
        assert sorted(response.json()) == ["ParserUtil", "Python", "TestCase"]

    def test_should_handle_empty_input(self):
        response = self.client.get("/extract_identifier?text=")
        assert response.status_code == 422

    def test_should_filter_non_identifiers(self):
        response = self.client.get("/extract_identifier?text=百度是高科技公司")
        assert response.json() == []

    def test_should_ignore_spaces_and_symbols(self):
        response = self.client.get("/extract_identifier?text=hello_world x123 _temp var-2")
        assert response.json() == ["hello_world", "x123", "_temp"]


if __name__ == "__main__":
    unittest.main()

import argparse
import datetime
import json
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import uuid

from markitdown import MarkItDown
from tornado import gen, ioloop, web, websocket
from tornado.httpclient import AsyncHTTPClient

if os.name == "nt":
    import msvcrt
else:
    import fcntl


# 调试模式配置
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# 配置日志
logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

connected_clients = {}
pending_requests = {}


def init_cache_db():
    """初始化SQLite缓存数据库"""
    with sqlite3.connect("url_cache.db") as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS url_cache (
                url TEXT PRIMARY KEY,
                markdown_content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """
        )
        conn.commit()
        logger.info("✅ 初始化URL缓存数据库完成")


# 跨平台文件锁
class ProcessLock:
    def __init__(self, lock_file="server.lock"):
        logger.info("🔒 初始化进程锁，文件: %s", lock_file)
        self.lock_file = lock_file
        self.locking = threading.Lock()
        self.fd = None
        self.file = None

    def acquire(self):
        logger.info("🔐 尝试获取进程锁")
        try:
            self.file = open(self.lock_file, "w", encoding="utf-8")
            self.fd = self.file.fileno()
            if os.name == "nt":  # Windows
                logger.info("🪟 检测到Windows系统，使用msvcrt锁定")
                try:
                    msvcrt.locking(self.fd.fileno(), msvcrt.LK_NBLCK, 1)
                    logger.info("✅ 成功获取Windows进程锁")
                    return True
                except IOError:
                    logger.warning("⚠️ Windows进程锁已被占用")
                    return False
            else:  # Unix/Linux/Mac
                logger.info("🐧 检测到Unix/Linux/Mac系统，使用fcntl锁定")
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    logger.info("✅ 成功获取Unix进程锁")
                    return True
                except (IOError, BlockingIOError):
                    logger.warning("⚠️ Unix进程锁已被占用")
                    return False
        except (OSError, IOError) as e:
            logger.error("🚨 获取锁失败: %s", str(e))
            return False

    def release(self):
        with self.locking:
            logger.info("🔓 尝试释放进程锁")
            try:
                if self.fd:
                    if os.name == "nt":
                        logger.info("🪟 释放Windows进程锁")

                        msvcrt.locking(self.fd.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        logger.info("🐧 释放Unix进程锁")
                        fcntl.flock(self.fd, fcntl.LOCK_UN)
                    self.file.close()
                    os.unlink(self.lock_file)
                    self.fd = None
                    logger.info("✅ 成功释放进程锁")
            except (OSError, IOError) as e:
                logger.error("🚨 释放锁失败: %s", str(e))


class BrowserWebSocketHandler(websocket.WebSocketHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client_id = None
        logger.info("🛠 初始化WebSocket处理器")

    def check_origin(self, origin):
        logger.info("🌐 检查来源: %s", origin)
        return (
            origin.startswith("chrome-extension://")
            or origin.startswith("http://localhost:")
            or origin.startswith("http://127.0.0.1:")
        )

    def open(self, *args, **kwargs):
        self.client_id = str(uuid.uuid4())
        connected_clients[self.client_id] = self
        logger.info("🎮 浏览器客户端连接成功，ID: %s", self.client_id)
        logger.info("📊 当前连接客户端数: %d", len(connected_clients))

    def on_message(self, message):
        logger.info("📨 收到浏览器消息: %s...", message[:200])
        ioloop.IOLoop.current().add_callback(self._process_message, message)

    async def _process_message(self, message):
        try:
            data = json.loads(message)
            logger.info("📝 解析消息类型: %s", data.get("type"))
            if data.get("type") == "htmlResponse":
                request_id = data.get("requestId")
                logger.info("🆔 处理请求ID: %s", request_id)
                if request_id in pending_requests:
                    pending_requests[request_id].set_result(data["content"])
                    logger.info("✅ 请求 %s 已设置结果", request_id)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error("🚨 处理消息出错: %s", str(e))

    def data_received(self, chunk):
        pass

    def on_close(self):
        if self.client_id in connected_clients:
            del connected_clients[self.client_id]
        logger.info("❌ 浏览器客户端断开，ID: %s", self.client_id)
        logger.info("📊 当前连接客户端数: %d", len(connected_clients))


class ConvertHandler(web.RequestHandler):
    def data_received(self, chunk):
        pass

    async def _process_html(self, html, is_news):
        if is_news:
            logger.info("🛠 正在使用Readability净化内容...")
            try:
                http_client = AsyncHTTPClient()
                logger.info("🌐 向Readability服务发送请求")
                response = await http_client.fetch(
                    "http://localhost:3000/html_reader",
                    method="POST",
                    headers={"Content-Type": "application/json"},
                    body=json.dumps({"content": html}),
                    connect_timeout=10,
                    request_timeout=30,
                )
                if response.code == 200:
                    result = json.loads(response.body)
                    if "content" in result:
                        html = result["content"]
                        logger.info("✅ 净化完成，新长度: %s 字符", len(html))
                    else:
                        logger.warning("⚠️ 净化服务未返回有效内容，使用原始HTML")
                else:
                    logger.error("⚠️ 净化服务返回错误状态码: %s", response.code)
            except (OSError, IOError, ValueError) as e:
                logger.error("🚨 净化服务调用失败: %s，继续使用原始HTML", str(e))
        return html

    async def _convert_to_markdown(self, html):
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=True, encoding="utf-8") as f:
                f.write(html)
                f.flush()
                logger.info("🔄 开始转换，临时文件: %s", f.name)
                md = MarkItDown()
                result = md.convert(f.name)
                logger.info("✅ 转换完成，Markdown长度: %s 字符", len(result.text_content))
                return result.text_content
        except (OSError, IOError):
            logger.warning("⚠️ 无法创建临时文件，尝试普通文件")
            temp_file = "temp_conversion.html"
            try:
                with open(temp_file, "w", encoding="utf-8") as f:
                    f.write(html)
                logger.info("🔄 开始转换，临时文件: %s", temp_file)
                md = MarkItDown()
                result = md.convert(temp_file)
                logger.info("✅ 转换完成，Markdown长度: %s 字符", len(result.text_content))
                return result.text_content
            finally:
                try:
                    os.remove(temp_file)
                except OSError:
                    logger.warning("⚠️ 无法删除临时文件: %s", temp_file)

    async def get(self):
        try:
            url = self.get_query_argument("url")
            is_news = self.get_query_argument("is_news", "false").lower() == "true"
            logger.info("🌐 收到转换请求，URL: %s", url)
            logger.info("📰 新闻模式: %s", is_news)

            # 检查缓存
            try:
                with sqlite3.connect("url_cache.db") as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT markdown_content FROM url_cache WHERE url = ?", (url,))
                    if row := cursor.fetchone():
                        logger.info("💾 命中缓存，直接返回结果")
                        return self.write(row[0])
            except sqlite3.Error as e:
                logger.error("🚨 缓存查询失败: %s", str(e))

            if not connected_clients:
                logger.error("🚫 没有连接的浏览器客户端")
                self.set_status(503)
                return self.write({"error": "No browser connected"})

            client = next(iter(connected_clients.values()))
            request_id = str(uuid.uuid4())
            fut = gen.Future()
            pending_requests[request_id] = fut
            logger.info("🆔 生成请求ID: %s", request_id)

            try:
                logger.info("📤 发送提取请求到浏览器，请求ID: %s", request_id)
                await client.write_message(json.dumps({"type": "extract", "url": url, "requestId": request_id}))

                html = await gen.with_timeout(ioloop.IOLoop.current().time() + 60, fut)
                logger.info("📥 收到HTML响应，长度: %s 字符", len(html))

                html = await self._process_html(html, is_news)
                markdown = await self._convert_to_markdown(html)

                # 写入缓存
                try:
                    with sqlite3.connect("url_cache.db") as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            """
                            INSERT OR REPLACE INTO url_cache
                            (url, markdown_content, created_at)
                            VALUES (?, ?, ?)
                        """,
                            (url, markdown, datetime.datetime.now().isoformat()),
                        )
                        conn.commit()
                        logger.info("💾 缓存写入成功")
                except sqlite3.Error as e:
                    logger.error("🚨 缓存写入失败: %s", str(e))

                self.write(markdown)

            except gen.TimeoutError:
                logger.error("⏰ 请求超时，请求ID: %s", request_id)
                self.set_status(504)
                self.write({"error": "Request timeout"})
            finally:
                pending_requests.pop(request_id, None)

        except web.MissingArgumentError:
            self.set_status(400)
            self.write({"error": "Missing url parameter"})
        except (OSError, IOError, ValueError) as e:
            logger.error("处理请求出错: %s", str(e))
            self.set_status(500)
            self.write({"error": "Internal server error"})


def make_app():
    return web.Application(
        [
            (r"/convert", ConvertHandler),
            (r"/ws", BrowserWebSocketHandler),
        ]
    )


if __name__ == "__main__":
    # 添加参数解析
    parser = argparse.ArgumentParser(description="启动服务器。")
    parser.add_argument("--addr", default="127.0.0.1", help="服务器监听地址 (默认: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="服务器监听端口 (默认: 8000)")
    parsed_args = parser.parse_args()

    # 创建进程锁
    process_lock = ProcessLock()
    if not process_lock.acquire():
        logger.error("🚫 已有服务器实例在运行，请先停止当前实例")
        sys.exit(1)

    try:
        init_cache_db()  # 初始化缓存数据库
        app = make_app()
        # 使用参数中的地址和端口
        app.listen(parsed_args.port, address=parsed_args.addr)
        logger.info("%s", f"🚀 服务器已启动，监听 {parsed_args.addr}:{parsed_args.port}")

        ioloop.IOLoop.current().start()
    finally:
        process_lock.release()

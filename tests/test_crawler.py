import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# 添加项目根目录到 sys.path
sys.path.append(os.getcwd())

# Mock 掉 app.core.database 的依赖，防止 import 时连接数据库
sys.modules['app.core.database'] = MagicMock()
sys.modules['app.services.vector_service'] = MagicMock()

# 现在可以 import crawler 了
from app.services.crawler import CrawlerService

class TestCrawler(unittest.TestCase):
    def setUp(self):
        self.crawler = CrawlerService()

    def test_chunk_text(self):
        text = "1234567890" * 10  # 100 chars
        # Chunk size 10, overlap 2 -> [0:10], [8:18], [16:26]...
        chunks = self.crawler._chunk_text(text, chunk_size=10, overlap=2)
        
        self.assertEqual(len(chunks[0]), 10)
        self.assertEqual(chunks[0], "1234567890")
        self.assertEqual(chunks[1], "9012345678") # overlap 90
        
    def test_clean_text(self):
        raw = "Hello   World\n\nTest   Me"
        cleaned = self.crawler._clean_text(raw)
        self.assertEqual(cleaned, "Hello World Test Me")

    def test_is_valid_url(self):
        # 同域名 + 同路径前缀才算有效，防止从大站子目录爬到全站
        self.crawler.base_domain = "cloud.tencent.com"
        self.crawler.base_path_prefix = "/document/product"
        self.assertTrue(self.crawler._is_valid_url("https://cloud.tencent.com/document/product/213/495"))
        self.assertFalse(self.crawler._is_valid_url("https://cloud.tencent.com/document/api/213"))  # 同域名但前缀不符
        self.assertFalse(self.crawler._is_valid_url("https://google.com/document/product"))         # 跨域名
        self.assertFalse(self.crawler._is_valid_url("ftp://cloud.tencent.com/document/product/x"))  # 非 http(s)

if __name__ == '__main__':
    unittest.main()

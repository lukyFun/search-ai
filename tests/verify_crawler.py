import asyncio
import sys
import os
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# -------------------------------------------------------------------------
# CRITICAL: Mock dependencies BEFORE importing app.services.crawler
# This prevents the singleton instantiation at the bottom of crawler.py 
# from triggering real DB/VectorService initialization.
# -------------------------------------------------------------------------

# Mock vector_service module
mock_vs_module = MagicMock()
mock_vs_module.get_vector_service.return_value = MagicMock()
sys.modules['app.services.vector_service'] = mock_vs_module

# Mock crawl_store module（dry_run 不写库，但 import 不能失败）
mock_cs_module = MagicMock()
mock_cs_module.crawl_db = MagicMock()
sys.modules['app.core.crawl_store'] = mock_cs_module

# Now it is safe to import CrawlerService
from app.services.crawler import CrawlerService

async def main():
    print("🚀 Starting Crawler Verification (Dry Run)...")
    
    # Initialize service (dependencies are already mocked via sys.modules)
    crawler = CrawlerService()
    
    # Target URL (add trailing slash to be safe, though follow_redirects handles it)
    start_url = "https://cloud.tencent.com/document/product"
    print(f"Target: {start_url}")
    
    try:
        # Run crawl
        # Limit to 3 pages for quick verification
        result = await crawler.crawl_site(start_url, max_pages=3, dry_run=True)
        
        print("\n✅ Verification Complete!")
        print(f"Pages Crawled: {result['pages_crawled']}")
        print("\n📄 Detailed Results:")
        
        if not result.get('results'):
            print("⚠️ No pages were successfully parsed. Check logs/network.")
        
        for idx, item in enumerate(result.get('results', [])):
            print(f"\n--- Page {idx+1} ---")
            print(f"URL: {item['url']}")
            print(f"Title: {item['title']}")
            print(f"Chunks: {item['chunks_count']}")
            print(f"Preview: {item['content_preview']}")
            print("-" * 30)
            
    except Exception as e:
        print(f"\n❌ Error during verification: {e}")

if __name__ == "__main__":
    asyncio.run(main())

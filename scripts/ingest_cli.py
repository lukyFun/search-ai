import asyncio
import sys
import os
import argparse
from datetime import datetime

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.crawl_store import crawl_db
from app.services.crawler import crawler_service
from app.core.logger import logger

async def run_ingest(mode: str, limit: int, url: str):
    print(f"🚀 Starting Ingestion Tool")
    print(f"Target: {url}")
    print(f"Mode: {mode.upper()}")
    print(f"Page Limit: {limit}")
    print("-" * 40)

    try:
        # Initialize SQLite store（爬虫元数据）
        await crawl_db.init()
        
        # Determine if dry_run
        dry_run = (mode == "preview")
        
        start_time = datetime.now()
        
        # Execute Crawl
        # Note: crawler_service is already instantiated, but its internal 
        # VectorService singleton lazy-loads the model on first use (if not dry_run).
        result = await crawler_service.crawl_site(url, max_pages=limit, dry_run=dry_run)
        
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        print("-" * 40)
        print(f"✅ Operation Complete in {duration:.2f}s")
        print(f"Pages Processed: {result['pages_crawled']}")
        
        if mode == "preview":
            print("\n📋 URL List Preview:")
            for idx, item in enumerate(result.get('results', [])):
                print(f"[{idx+1}] {item['url']} ({item['chunks_count']} chunks)")
                print(f"    Title: {item['title']}")
        else:
            print("\n💾 Ingestion Summary:")
            print(f"- Documents saved to MongoDB")
            print(f"- Vectors indexed in ChromaDB (using BGE-M3)")
            print(f"- Metadata updated")
            
    except Exception as e:
        print(f"\n❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        await crawl_db.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DocMind Doc Ingestion Tool")
    parser.add_argument("--mode", choices=["preview", "ingest"], default="preview", help="Mode: preview (dry-run) or ingest (real)")
    parser.add_argument("--limit", type=int, default=10, help="Max pages to crawl")
    parser.add_argument("--url", type=str, default="https://cloud.tencent.com/document/product", help="Start URL")
    
    args = parser.parse_args()
    
    asyncio.run(run_ingest(args.mode, args.limit, args.url))

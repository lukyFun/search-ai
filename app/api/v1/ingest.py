from fastapi import APIRouter, BackgroundTasks, HTTPException
from app.models.schemas import IngestRequest, IngestResponse
from app.services.crawler import crawler_service
from app.core.logger import logger

router = APIRouter()

# 1. 定义后台任务函数
# 这是一个普通的异步函数，将在 HTTP 响应发送后在后台执行
async def run_crawler_task(url: str, max_pages: int):
    try:
        logger.info(f"Background task started: Crawling {url}")
        # 调用爬虫服务执行实际的爬取逻辑
        await crawler_service.crawl_site(url, max_pages)
        logger.info(f"Background task finished: Crawling {url}")
    except Exception as e:
        logger.error(f"Background task failed: {str(e)}")

# 2. 注入 BackgroundTasks
# BackgroundTasks 是 FastAPI 的一个神奇特性，允许你在返回 Response 后继续执行任务
# 适合耗时但不要求即时返回结果的操作 (如发邮件、处理文件、爬虫)
@router.post("/ingest", response_model=IngestResponse)
async def ingest_content(request: IngestRequest, background_tasks: BackgroundTasks):
    """
    提交一个 URL 开始爬取并建立索引 (异步后台任务)
    """
    # 简单的 URL 校验
    if not str(request.url).startswith("http"):
        # HTTPException: FastAPI 推荐的错误抛出方式，会自动转为 JSON 错误响应
        raise HTTPException(status_code=400, detail="Invalid URL scheme")
        
    # 将任务添加到后台队列
    # 注意：这里只是"添加"，任务会在当前函数 return 后才开始执行
    background_tasks.add_task(run_crawler_task, str(request.url), request.max_pages)
    
    # 立即返回响应给用户，不需要等待爬虫结束
    return IngestResponse(
        message=f"Crawling started for {request.url}",
        task_id="background_task"
    )

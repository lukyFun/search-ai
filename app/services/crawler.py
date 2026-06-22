import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, quote
from app.services.vector_service import get_vector_service
from app.core.crawl_store import crawl_db
from app.core.logger import logger
import hashlib
import time
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception


def _now_ms() -> int:
    return int(time.time() * 1000)


class CrawlerService:
    def __init__(self):
        self.vector_service = get_vector_service()
        self.visited = set()
        self.base_domain = ""
        self.base_path_prefix = ""


    def _is_valid_url(self, url: str) -> bool:
        parsed = urlparse(url)
        # 同域名 + 同路径前缀下的 http/https 链接才抓。
        # 路径前缀限定很关键：起始 URL 常常是大站点的子目录
        # （如 cloud.tencent.com/document/product），只按域名过滤会把整个站点都爬进来。
        return (
            parsed.netloc == self.base_domain
            and parsed.path.startswith(self.base_path_prefix)
            and parsed.scheme in ["http", "https"]
        )

    def _clean_text(self, text: str) -> str:
        # 简单的清洗：去除多余空白
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        return " ".join(chunk for chunk in chunks if chunk)

    def _chunk_text(self, text: str, chunk_size: int = 800, overlap: int = 100) -> list[str]:
        """
        简单的滑动窗口切片
        """
        if not text:
            return []
        
        chunks = []
        start = 0
        text_len = len(text)
        
        while start < text_len:
            end = start + chunk_size
            chunk = text[start:end]
            chunks.append(chunk)
            # 移动窗口，如果有重叠
            start += chunk_size - overlap
            
        return chunks

    def _is_retryable_error(exception):
        """
        判断异常是否需要重试。
        404 Not Found 不需要重试。
        """
        if isinstance(exception, httpx.HTTPStatusError):
            if exception.response.status_code == 404:
                return False
        return True

    @retry(
        stop=stop_after_attempt(3), 
        wait=wait_fixed(2),
        retry=retry_if_exception(_is_retryable_error)
    )
    async def _fetch_page(self, client: httpx.AsyncClient, url: str):
        response = await client.get(url, timeout=10.0, follow_redirects=True)
        response.raise_for_status()
        return response.text

    async def crawl_site(self, start_url: str, max_pages: int = 50, dry_run: bool = False):
        """
        BFS 抓取站点
        :param start_url: 起始 URL
        :param max_pages: 最大抓取页数
        :param dry_run: 如果为 True，仅执行抓取和解析，不保存到数据库和向量库，并返回抓取结果列表
        """
        self.base_domain = urlparse(start_url).netloc
        # 抓取范围限定在起始 URL 的路径前缀内（去掉末尾斜杠便于 startswith 匹配）
        self.base_path_prefix = urlparse(start_url).path.rstrip("/") or "/"
        queue = [start_url]
        self.visited = {start_url}
        count = 0
        results = []
        
        # 记录任务开始时间，用于后续清理过时数据（epoch ms）
        task_start_ms = _now_ms()
        logger.info(f"Starting crawl task for {start_url} (dry_run={dry_run})")
        
        async with httpx.AsyncClient() as client:
            while queue and count < max_pages:
                current_url = queue.pop(0)
                try:
                    logger.info(f"Crawling: {current_url}")
                    html = await self._fetch_page(client, current_url)
                    
                    # 解析
                    soup = BeautifulSoup(html, "html.parser")
                    
                    # 提取标题
                    title = soup.title.string if soup.title else current_url
                    
                    # 1. 先提取所有链接 (在清理之前，防止移除导航栏里的链接)
                    links_found = soup.find_all("a", href=True)
                    logger.info(f"Found {len(links_found)} raw links on {current_url}")
                    
                    for link in links_found:
                        href = link["href"]
                        # 简单的 URL 编码处理 (防止空格导致的问题)
                        # 保留常见字符，只编码空格和特殊符号
                        href = quote(href, safe="/:?=&%#") 
                        
                        full_url = urljoin(current_url, href)
                        # 去除 fragment
                        full_url = full_url.split("#")[0]
                        
                        if full_url not in self.visited and self._is_valid_url(full_url):
                            self.visited.add(full_url)
                            queue.append(full_url)

                    # 2. 先移除全站级别的无用标签（这些绝不可能是正文）
                    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                        tag.decompose()

                    # 3. 定位正文容器：先试语义标签，再退回常见文档站的正文 class。
                    # Docusaurus 用 <article>/<main>；腾讯云文档没有这些标签，
                    # 正文在 div.J-mainDetail / .J-markdown-box；很多 markdown 主题用 .markdown-body。
                    main_content = (
                        soup.find("article")
                        or soup.find("main")
                        or soup.find(class_=lambda x: x and any(
                            k in x for k in ("J-mainDetail", "J-markdown-box", "markdown-body")
                        ))
                    )

                    # 4. 清理 TOC 目录等干扰元素——必须限定在正文容器内（找不到才全局）。
                    # 某些站点的两栏布局外层 class 也含 "toc"（如腾讯云的 rno-toc-layout），
                    # 全局按 "toc" 删会连正文一起删掉；限定在正文容器内则只删真正的目录组件。
                    cleanup_scope = main_content if main_content else soup
                    for tag in cleanup_scope.find_all(class_=lambda x: x and "toc" in x.lower()):
                        tag.decompose()
                    for tag in cleanup_scope.find_all(string=lambda text: text and "On this page" in text):
                        if tag.parent and tag.parent.name not in ['body', 'html', 'article', 'main']:
                            tag.parent.decompose()
                    
                    if main_content:
                        # 3. 在提取文本前，将链接转换为 Markdown 格式 [text](url)
                        # 为了不破坏原 soup 结构用于后续操作（虽然这里其实不需要了），可以先处理
                        
                        # 使用 copy 避免影响 soup 结构（可选，但这里 main_content 就是我们要用的）
                        # 直接在 main_content 上操作
                        
                        base_url_str = current_url
                        for a in main_content.find_all("a", href=True):

                            href = a['href']
                            text = a.get_text(strip=True)
                            
                            # 跳过锚点链接(#)和空文本
                            if href.startswith('#') or not text:
                                continue
                                
                            try:
                                # 转换为绝对路径
                                absolute_url = urljoin(base_url_str, href)
                                # 替换为 Markdown 格式，注意前后加空格避免粘连
                                new_string = f" [{text}]({absolute_url}) "
                                a.replace_with(new_string)
                            except Exception as e:
                                # 如果转换失败，保留原样
                                logger.warning(f"Failed to convert link {href}: {e}")
                                pass

                        raw_text = main_content.get_text()
                    else:
                        # 否则回退到整个 body，但要移除干扰链接
                        # 移除包含 "Skip to main content" 的链接
                        for a in soup.find_all("a"):
                            if "Skip to main content" in a.get_text():
                                a.decompose()
                        raw_text = soup.get_text()

                        
                    text_content = self._clean_text(raw_text)
                    
                    if not text_content:
                        logger.warning(f"No text content found for {current_url} (marking as success)")
                        # 内容为空（可能只有图片），视为抓取成功
                        if not dry_run:
                            await crawl_db.upsert_empty(url=current_url)
                        continue

                    # 切片
                    chunks = self._chunk_text(text_content)
                    # 修正：应该对内容进行哈希，而不是 URL
                    doc_hash = hashlib.md5(text_content.encode("utf-8")).hexdigest()

                    if dry_run:
                        # Dry run 模式：收集结果但不保存
                        results.append({
                            "url": current_url,
                            "title": title,
                            "content_preview": text_content[:200] + "...",
                            "chunks_count": len(chunks)
                        })
                        logger.info(f"[Dry Run] Parsed {current_url}: {len(chunks)} chunks")
                    else:
                        # 正常模式：存入 SQLite 和 ChromaDB

                        # 1. 检查是否需要更新 (Hash 对比)
                        existing_doc = await crawl_db.get_by_url(current_url)
                        is_content_changed = not existing_doc or existing_doc.get("hash") != doc_hash

                        await crawl_db.upsert_success(
                            url=current_url, title=title, content=text_content, doc_hash=doc_hash,
                        )

                        # 2. 只有内容变了，才更新向量库
                        if is_content_changed and chunks:
                            # 先删除旧向量 (避免切片策略变化导致 ID 残留)
                            self.vector_service.delete_documents(where={"url": current_url})
                            
                            ids = [f"{doc_hash}_{i}" for i in range(len(chunks))]
                            metadatas = [{"url": current_url, "title": title, "chunk_index": i} for i in range(len(chunks))]
                            
                            self.vector_service.add_documents(
                                documents=chunks,
                                metadatas=metadatas,
                                ids=ids
                            )
                            logger.info(f"Indexed {len(chunks)} chunks for {current_url}")
                        else:
                            logger.info(f"Content unchanged for {current_url}, skipping vector update.")

                    count += 1
                    
                    # 发现新链接 (已移至上方处理)
                    # links_found = soup.find_all("a", href=True) ...
                            
                except Exception as e:
                    error_msg = str(e)
                    # 优化 404 错误日志
                    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
                        logger.warning(f"Page not found (404): {current_url}")
                        error_msg = "Page not found (404)"
                    else:
                        logger.error(f"Failed to crawl {current_url}", error=error_msg)
                    
                    if not dry_run:
                        await crawl_db.upsert_failed(url=current_url, error_msg=error_msg)
        
        logger.info(f"Crawl finished. Processed {count} pages.")
        
        # 清理阶段：将本次任务未爬取到（last_crawled_at < task_start_ms）且属于当前域名的文档标记为已删除
        if not dry_run:
            await self._cleanup_outdated_docs(task_start_ms)

        if dry_run:
            return {"pages_crawled": count, "results": results}
        return {"pages_crawled": count}

    async def _cleanup_outdated_docs(self, threshold_ms: int):
        """
        清理过时文档：标记为 deleted 并从向量库移除。
        """
        logger.info(f"Starting cleanup for docs not updated since ms={threshold_ms} (domain={self.base_domain})")

        deleted_count = 0
        async for doc in crawl_db.iter_outdated_active(
            domain=self.base_domain, last_crawled_before_ms=threshold_ms,
        ):
            url = doc["url"]
            logger.info(f"Marking {url} as deleted (outdated)")
            await crawl_db.mark_deleted(url)
            self.vector_service.delete_documents(where={"url": url})
            deleted_count += 1

        logger.info(f"Cleanup finished. Deleted {deleted_count} outdated documents.")

# 单例
crawler_service = CrawlerService()

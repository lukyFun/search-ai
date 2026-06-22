from pydantic_settings import BaseSettings
from functools import lru_cache

# 1. 定义配置类
# BaseSettings 是 Pydantic 提供的强大工具，可以自动从环境变量 (.env 文件或系统变量) 读取配置
# 它会自动进行类型转换（比如把字符串 "true" 转为 bool True）
class Settings(BaseSettings):
    PROJECT_NAME: str = "DocMind"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"
    DEBUG: bool = False

    # 助手品牌 / 知识边界（全部可经 .env 覆盖，换站点只改这几行）
    # ASSISTANT_NAME：出现在系统提示词与前端标题中的助手人设
    # KNOWLEDGE_SCOPE：拒答文案里用来描述"我只回答 X 范围内的问题"
    # EXAMPLE_QUESTIONS：拒答时给用户的示例提问（逗号分隔）
    ASSISTANT_NAME: str = "云文档智能问答助手"
    KNOWLEDGE_SCOPE: str = "人脸识别产品文档"
    EXAMPLE_QUESTIONS: str = "人脸识别是什么？,人脸识别的计费方式是怎样的？"

    # Vector DB 配置
    # 默认相对路径（相对仓库根目录），本地直接跑脚本即可命中；
    # Docker 里 WORKDIR=/app，相对路径同样解析到 /app/data/chromadb。
    # 容器部署务必把该目录挂 volume，避免重启丢失知识库。
    CHROMA_PERSIST_DIRECTORY: str = "data/chromadb"

    # Model 配置
    # 本地 embedding 模型目录（相对仓库根目录）；不存在时运行期会自动从
    # HuggingFace 拉取 BAAI/bge-m3。建议先用 scripts/download_model*.py 预下载。
    MODEL_PATH: str = "models/bge-m3"
    
    # LLM (OpenAI Compatible) 配置
    # 这里设置默认值，实际运行会优先读取 .env 文件中的值
    LLM_API_KEY: str = "dummy-key"
    LLM_BASE_URL: str = "https://api.deepseek.com" # 默认为 DeepSeek
    LLM_MODEL_NAME: str = "deepseek-chat"
    
    # Crawler 配置
    # 默认爬取入口 = 腾讯云开发者社区文档中心；爬虫按该 URL 的路径前缀限定抓取范围
    TARGET_URL: str = "https://cloud.tencent.com/document/product"

    # 审计 SQLite（qa_audit / user_feedback）
    AUDIT_DB_PATH: str = "data/audit.db"
    AUDIT_RETENTION_DAYS: int = 30  # 超过该天数的审计/反馈记录会被 janitor 删除
    AUDIT_JANITOR_INTERVAL_SECONDS: int = 3600  # 1 小时清一次

    # 爬虫元数据 SQLite（documents 表）
    CRAWL_DB_PATH: str = "data/crawl_meta.db"

    # 日志滚动
    LOG_FILE: str = ".run/app.log"
    LOG_MAX_BYTES: int = 100 * 1024 * 1024
    LOG_BACKUP_COUNT: int = 10

    # 内部诊断 API 的访问控制
    INTERNAL_API_ALLOWED_IPS: str = "127.0.0.1,::1"  # 逗号分隔；支持 CIDR，例 10.0.0.0/8

    # 反向代理 / 真实客户端 IP 解析
    TRUSTED_PROXIES: str = ""  # 逗号分隔的可信代理 IP/CIDR；空字符串 = 不信任任何 XFF

    # Security & Quota Limits
    MAX_INPUT_LENGTH: int = 500  # 用户提问最大字符数
    RATE_LIMIT_PER_MINUTE: int = 12          # 单 IP 每分钟请求
    RATE_LIMIT_PER_MINUTE_PER_SESSION: int = 20  # 单 session_id 每分钟请求（防 IP 切换保留 session）
    RATE_LIMIT_PER_MINUTE_PER_SUBNET: int = 60   # /24 子网每分钟请求（防代理池）
    GLOBAL_RATE_LIMIT_PER_MINUTE: int = 300      # 全实例每分钟请求兜底
    MAX_CONCURRENT_QA: int = 20
    MAX_CONCURRENT_QA_PER_IP: int = 3
    MAX_SESSIONS_PER_IP: int = 30
    MAX_NEW_SESSIONS_PER_MINUTE: int = 10
    # RAG 检索相关度阈值（cosine distance，越小越相关；超过即拒答不调 LLM）。
    # 0.55 留给跨语言查询（如中/阿语提问英文文档站）足够余地，又能挡掉明显无关问题。
    # 同语种纯英文场景可调到 0.45；噪音多的小知识库可调到 0.6。
    RAG_MAX_DISTANCE: float = 0.55
    MAX_CONTEXT_LENGTH: int = 4000

    # LLM 每日 token 预算（OpenAI compatible 返回 usage.total_tokens 时累加）
    DAILY_TOKEN_BUDGET: int = 0  # 0 = 不启用预算限制
    # SSE 流式接口的 chunk 间隔超时（秒）；超过即主动断开，防"挂连接耗资源"
    SSE_CHUNK_TIMEOUT_SECONDS: float = 60.0
    # 同 session 同问题去重窗口（秒）
    DEDUP_WINDOW_SECONDS: float = 5.0
    # 拒答恒定耗时下限（秒）：触发限流或被拒时强制 sleep 到该下限，防响应时间侧信道
    REJECT_RESPONSE_FLOOR_SECONDS: float = 0.2

    # 内部配置类，指定读取 .env 文件
    class Config:
        env_file = ".env"
        case_sensitive = True

# 2. 使用 lru_cache 缓存配置实例
# 这样无论调用多少次 get_settings()，都只会读取一次环境变量，提高性能
@lru_cache()
def get_settings():
    return Settings()

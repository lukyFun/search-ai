# DocMind

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.9+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688.svg)
![Deps](https://img.shields.io/badge/deps-no%20PG%2FRedis%2FMongo-success.svg)

[English](./README.en.md) · **简体中文**

面向开源社区/官网文档的 AI 问答助手：给一个站点 URL + 一个 LLM Key，就能起一个"自己的 AI 帮助中心"。

适用场景：
- 个人网站 / 产品文档 / 团队 Wiki 的智能问答
- 企业内部知识库快速检索
- 私有化部署的 RAG Demo / 基础框架

> **服务定位说明**：本服务面向外网开放，作为"轻量级 AI 助手"对外提供。安全防控做了重点投入（见下文），但不依赖任何外部中间件（无 Redis、无 MongoDB）。单进程嵌入式存储，docker 都不需要。

---

## ✨ 核心亮点

- 🌍 **跨语言问答 · 国际化开箱即用**：知识库文档是中文，用户照样能用 **英语 / 日语 / 阿拉伯语** 等任意语言提问，系统自动识别提问语种并用**同语种**作答。靠 BGE-M3 多语言向量做检索，**一份中文文档即可服务全球用户**——无需翻译文档、无需维护多套知识库。这是面向海外/多语种用户的文档中心最实用的能力。
- ⚡ **秒级响应 · 重复问题不重复烧钱**：本地向量检索 + SSE 流式输出，界面实时显示**首字耗时**；完全相同的问题（同一上下文）直接命中缓存返回，**不再重复调用大模型**，省 token 又快。
- 🛡️ **面向公网的安全防控**：15 层防护（多维限流 / 每日 token 预算 / 提示注入拦截 / 拒答恒定耗时 …），详见下文。
- 🪶 **极致轻量**：无 Redis、无 MongoDB、无需 Docker；两个 SQLite 文件 + 一个 ChromaDB 目录即可跑起来。

---

## 🛡️ 安全防控能力（重点）

DocMind 直接暴露在公网时面临的典型威胁：滥刷接口、烧 LLM token（账单攻击）、提示注入、爬库脚本、代理池切 IP、挂连接耗资源、数据泄露。下面按"攻击手法 → 防御层"组织。

### 1. 多维度限流（任一维度超阈值即 429）

单维度限流（例如只看 IP）很容易被绕过。本服务叠加 4 个维度，攻击者要同时换 IP + 换 session + 换子网 + 换 UA 才能持续打，成本陡升：

| 维度 | 配置项 | 默认 | 防什么 |
|---|---|---|---|
| 单 IP RPM | `RATE_LIMIT_PER_MINUTE` | 12 | 单点滥用 |
| 单 session_id RPM | `RATE_LIMIT_PER_MINUTE_PER_SESSION` | 20 | 切 IP 但复用 session 的脚本 |
| 单 /24 子网 RPM（IPv6 用 /64） | `RATE_LIMIT_PER_MINUTE_PER_SUBNET` | 60 | 代理池常集中在少数 ASN/子网 |
| 全实例 RPM 兜底 | `GLOBAL_RATE_LIMIT_PER_MINUTE` | 300 | 分布式攻击 |

所有维度走"滑动窗口"算法（deque-of-timestamps），无固定窗口边界的"卡点突袭"漏洞。

### 2. 防"账单攻击"：每日 LLM token 预算

攻击者的目的不是拿数据，是烧公司钱：开 1 万次请求把 LLM 调用费打爆。防御：

- 每次 LLM 调用后累加 `usage.total_tokens` 到当日计数
- 调 LLM 前先检查"今日预算是否已耗尽"，**超额直接拒答，不再调 LLM**
- 按 UTC 日自动 rollover，跨日归零
- 配置：`DAILY_TOKEN_BUDGET`（默认 0 = 不限；生产建议按月度预算 ÷ 30 设上限）
- 监测：`/api/v1/internal/token-budget` 查当日已用/预算

### 3. 同问题短时去重（防爬库脚本）

5 秒内同 `(session_id|ip, query)` 第二次出现直接 429：

- 攻击者写脚本反复打同样问题想压榨知识 → 第二次起被拒
- 用 SHA-256 截前 16 字节做 key，不存储原始问题
- 配置：`DEDUP_WINDOW_SECONDS`（默认 5）

### 4. SSE 长连接超时（防挂连接耗资源）

流式接口最容易被滥用：客户端打开连接就不读，占着资源。本服务给每个 SSE chunk 加 `asyncio.wait_for` 超时：

- 默认 60 秒没有新 chunk → 主动 `server_error` + 断流
- 配置：`SSE_CHUNK_TIMEOUT_SECONDS`（默认 60）

### 5. 拒答恒定耗时（抹平响应时间侧信道）

所有"被拦截"路径（任一维度限流触发、去重命中、session 配额耗尽）都强制 sleep 到下限再返回 429：

- 默认下限 200ms（`REJECT_RESPONSE_FLOOR_SECONDS`）
- 攻击者无法通过响应耗时快速区分"拦截"和"真处理"，提高试探成本

### 6. 真实客户端 IP（反向代理感知）

如果前面挂 nginx / 网关，`request.client.host` 永远是网关 IP，所有限流形同虚设。本服务从 `X-Forwarded-For` 解析真实 IP：

- **必须配可信代理白名单** 才信 XFF，否则攻击者塞 `X-Forwarded-For: 1.2.3.4` 即可伪造身份
- 直连客户端不在白名单 → 完全忽略 XFF，回落到 transport IP
- 从 XFF 链最右往左剥离可信代理，第一个非可信地址即真实客户端
- 配置：`TRUSTED_PROXIES`（逗号分隔，支持 CIDR；空字符串 = 不信任任何 XFF）

### 7. 提示注入与"越权指令"拦截

- prompt 中明确声明"上下文不可信、禁止泄露机密"
- 对典型越权 / 泄密指令（如"忽略前面的指令"、"输出系统提示"等）模式拦截，**直接拒答不调 LLM**
- 命中时返回友好提示，并写入审计日志

### 8. 检索相关度阈值（防无关提问 + 防幻觉 + 省钱）

- 向量检索结果距离 > `RAG_MAX_DISTANCE`（默认 0.55）→ 礼貌拒绝
- **不调用 LLM**，避免成本浪费与"瞎编"
- 同时也是一种安全控制：缺乏上下文证据的回答不输出

### 9. 输入合规

- 单次提问最大 `MAX_INPUT_LENGTH`（默认 500 字符）双重校验（前端 + 后端）
- 反馈"原因"字段最多 500 字符
- 超长直接 4xx 拦截，不进 LLM

### 10. 内存配额与防泄漏（无 OOM 风险）

所有按 IP / session_id 为键的内存容器都遵循 (a) 上限 + (b) TTL + (c) 后台主动清理"三重保护"：

- 通用 `TtlLruCache` + `SlidingWindowCounter`：写入超 `max_size` 立即 LRU 淘汰最老
- 后台 janitor task 每 60s 全扫一次主动清过期（不依赖被动 get 触发）
- 监测：`/api/v1/internal/cache-stats` 看每个缓存当前 size
- 单实例可承载 10000 个 IP 维度键，超出自动 LRU 淘汰，攻击者多 IP 攻击也涨不死内存

### 11. 审计 + 反馈结构化存储 + 本地查询 API

每次问答和用户反馈都"日志双写"：

- **stdout JSON 日志**（关键字 `event=qa_audit` / `event=user_feedback`）：研发 grep + jq 实时观察
- **SQLite 表**（独立 `audit.db` 文件，不入 repo）：支持分页查询 + 过滤 + 关键字搜索
- 自动滚动删除超过 `AUDIT_RETENTION_DAYS`（默认 30 天）的旧记录
- 审计数据**绝不进向量库**，无法通过 RAG 提问获取"其他人问了什么"

### 12. 内部诊断 API 严格本地化

`/api/v1/internal/*` 路由（查反馈 / 审计 / 缓存 size / token 预算）：

- IP 白名单中间件，默认仅 `127.0.0.1, ::1`
- 配置项 `INTERNAL_API_ALLOWED_IPS`（支持 CIDR，例 `10.0.0.0/8`）
- **故意不信任 X-Forwarded-For**：否则攻击者伪造 XFF 即可绕过白名单
- 不在 `/docs` OpenAPI 暴露

### 13. 全局并发治理

- 全局并发问答上限 `MAX_CONCURRENT_QA`（默认 20，含 SSE 连接）
- 每 IP 并发问答上限 `MAX_CONCURRENT_QA_PER_IP`（默认 3）
- 每 IP session 总数 `MAX_SESSIONS_PER_IP`（默认 30）
- 每 IP 每分钟新建 session `MAX_NEW_SESSIONS_PER_MINUTE`（默认 10）

### 14. 会话隔离与隐私

- 前端为每个浏览器标签生成 `session_id` 存 `sessionStorage`
- 后端按 `session_id` 维护对话历史（内存 LRU + 30 分钟 TTL）
- **不同 session 严格隔离**：你的 session 拿不到别人的提问 / 历史 / 缓存答案
- session 过期或显式 delete 时联动清掉该 session 关联的所有答案缓存

### 15. 日志滚动（无磁盘炸盘风险）

- `.run/app.log` 由 Python `RotatingFileHandler` 自管理
- 默认 `100MB × 10` 文件循环（`LOG_MAX_BYTES` + `LOG_BACKUP_COUNT`）
- shell `nohup` 重定向到独立 `app.stderr.log`，避免与 Python 日志双写覆盖

### 安全控制速查表

| 攻击手法 | 防御层 | 配置项 |
|---|---|---|
| 单 IP 暴刷 | IP RPM 限流 | `RATE_LIMIT_PER_MINUTE` |
| 切 IP 保留 session | session RPM | `RATE_LIMIT_PER_MINUTE_PER_SESSION` |
| 代理池 / 同 ASN | /24 子网 RPM | `RATE_LIMIT_PER_MINUTE_PER_SUBNET` |
| 分布式 RPM 拉爆 | 全局 RPM 兜底 | `GLOBAL_RATE_LIMIT_PER_MINUTE` |
| 烧 token 账单攻击 | 每日预算 + 拒答 | `DAILY_TOKEN_BUDGET` |
| 爬库脚本短时重放 | 同问题去重 | `DEDUP_WINDOW_SECONDS` |
| 挂 SSE 连接耗资源 | chunk 间隔超时 | `SSE_CHUNK_TIMEOUT_SECONDS` |
| 响应时间侧信道试探 | 拒答恒定耗时下限 | `REJECT_RESPONSE_FLOOR_SECONDS` |
| 反代下 IP 失真 | XFF + 可信代理 | `TRUSTED_PROXIES` |
| 提示注入 / 越权 | 模式拦截 + prompt 声明 | （内置） |
| 无关 / 越界提问 | 检索距离阈值 | `RAG_MAX_DISTANCE` |
| 内存被多 IP 撑爆 | LRU + TTL + janitor | （内置） |
| 内部接口被外网摸 | IP 白名单 + 不信 XFF | `INTERNAL_API_ALLOWED_IPS` |
| 审计/反馈数据被反查 | 审计不入向量库 | （架构隔离） |
| 日志撑爆磁盘 | RotatingFileHandler | `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` |

---

## 核心能力

- 站点爬取与清洗：从起始 URL 出发抓取网页，提取正文做文本清洗
- 自动切片与向量化：写入 ChromaDB，支持语义检索
- 检索增强问答：基于上下文调用 OpenAI 兼容 LLM 生成回答
- 会话记忆：同 `session_id` 可追问，上下文连续
- 来源溯源：回答返回参考 URL
- 多语言自适应（国际化）：自动识别提问语种并用同语种回复，中文文档也能用英 / 日 / 阿语等多国语言问答；同样支持中文提问检索英文文档（BGE-M3 多语言向量能力）
- SSE 流式输出：打字机效果 + 建议追问
- 反馈机制：每条回答支持 👍/👎，👎 可填原因（最多 500 字），用于持续改进

---

## 技术栈

- **后端**：FastAPI + Uvicorn（异步）
- **向量库**：ChromaDB（嵌入式持久化）
- **Embedding**：BAAI/bge-m3 本地模型
- **LLM**：OpenAI 兼容 API（默认 DeepSeek）
- **存储**：两个 SQLite 文件（`audit.db` + `crawl_meta.db`），**无 Redis、无 MongoDB**
- **日志**：structlog JSON + RotatingFileHandler
- **前端**：原生 HTML/JS + TailwindCSS

整体瘦身：**不依赖 docker，不依赖任何外部数据库**。`run.sh` 直接拉起 uvicorn。

---

## 快速开始

### 环境要求

- Python 3.9+（开发用 3.11）
- 内存 ≥ 4 GB（BGE-M3 模型常驻约 2.3 GB）
- 磁盘 ≥ 5 GB（模型 ~2.1 GB + 知识库 + 日志）
- 一个 OpenAI 兼容的 LLM API Key（默认 DeepSeek，也可换任意兼容网关）

> 全程**不需要 Docker、不需要任何外部数据库**。下面是裸机本地起服务的完整步骤。

### 1) 克隆 + 安装依赖

```bash
git clone https://github.com/lukyFun/search-ai.git docmind && cd docmind
pip install -r requirements.txt
```

> 模型文件（约 2.1 GB）不在仓库里（已 gitignore），下一步用脚本下载。

### 2) 下载 embedding 模型（BAAI/bge-m3）

二选一：

```bash
# 方式 A：海外/能连 HuggingFace
python3 scripts/download_model.py

# 方式 B：国内推荐，走 ModelScope（阿里云镜像，速度快）
pip install modelscope
python3 scripts/download_model_cn.py
```

两个脚本都会把模型下载到 `models/bge-m3/`。

> 也可以跳过这步：模型目录不存在时，服务首次启动会自动从 HuggingFace 拉取 `BAAI/bge-m3`。
> 但国内网络下自动拉取很慢甚至失败，**强烈建议先用方式 B 预下载**。

### 3) 配置 `.env`

```bash
cp .env.example .env
# 然后编辑 .env，至少填上 LLM_API_KEY
```

```env
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL_NAME=deepseek-chat

# 换文档站只改这几行（助手人设 / 知识边界 / 示例问题 / 爬取入口）
ASSISTANT_NAME=云文档智能问答助手
KNOWLEDGE_SCOPE=人脸识别产品文档
EXAMPLE_QUESTIONS=人脸识别是什么？,人脸识别的计费方式是怎样的？
TARGET_URL=https://cloud.tencent.com/document/product/867
```

> 仓库默认演示对象是**腾讯云人脸识别产品文档**（一个公开文档示例）。
> 换成你自己的站点：把 `TARGET_URL` 指向目标文档入口，并改 `ASSISTANT_NAME` / `KNOWLEDGE_SCOPE` / `EXAMPLE_QUESTIONS` 三个品牌项即可，**无需改代码**。

### 4) 启动

```bash
bash run.sh          # dev 模式（--reload），后台
bash run.sh --prod   # prod 模式
bash run.sh --fg     # 前台
APP_PORT=8101 bash run.sh   # 改端口（默认 8100）
```

停止：`bash stop.sh`。首次启动会加载 BGE-M3 模型，约需 5~10 秒。

### 5) 构建知识库

服务起来后，灌一批文档进去（先用小页数验证链路）：

**CLI 方式**：

```bash
# 先 dry-run 预览抓取效果，不写库
python3 scripts/ingest_cli.py --mode preview --url "https://cloud.tencent.com/document/product/867" --limit 5
# 确认没问题后正式写入
python3 scripts/ingest_cli.py --mode ingest --url "https://cloud.tencent.com/document/product/867" --limit 50
```

**API 方式**：

```bash
curl -X POST "http://localhost:8100/api/v1/ingest" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://cloud.tencent.com/document/product/867", "max_pages": 50}'
```

### 6) 访问

- Web UI：`http://localhost:8100`
- OpenAPI：`http://localhost:8100/docs`
- 健康检查：`http://localhost:8100/health`

---

## 适配你自己的文档站

仓库默认演示对象是腾讯云人脸识别产品文档。换成别的站点通常**只需改配置**：

1. `.env` 改三个品牌项 + 爬取入口：`ASSISTANT_NAME` / `KNOWLEDGE_SCOPE` / `EXAMPLE_QUESTIONS` / `TARGET_URL`
2. 前端文案在 `app/static/index.html` 顶部的 `UI_CONFIG` 一处改完（标题 / 欢迎语 / 预设问题 / 输入框提示）
3. 重新 ingest 一次知识库

爬虫的两个关键行为：

- **抓取范围按路径前缀限定**：以 `TARGET_URL` 的 path 为前缀，只抓该目录下的页面。
  起始 URL 设成 `cloud.tencent.com/document/product/867` 就只爬人脸识别文档，不会顺着链接爬到整个站点。
- **正文容器自动识别**：依次尝试 `<article>` → `<main>` → `.J-mainDetail` / `.J-markdown-box`（腾讯云文档）/ `.markdown-body`（常见 markdown 主题），找不到才回退整个 `<body>`。

> ⚠️ 适用前提：目标站点的正文要在**静态 HTML** 里（服务端渲染）。腾讯云文档、Docusaurus 等都满足。
> 若是正文完全靠 JS 异步渲染的纯 SPA，httpx 抓到的是空壳，需要自行接入无头浏览器或站点的内容 API——这是本项目当前不覆盖的场景。
> 正文容器若是上述选择器都命中不了的自定义结构，可在 `app/services/crawler.py` 的正文容器列表里加一条 class。

---

## 环境变量

### 基础

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROJECT_NAME` | `DocMind` | 项目名（OpenAPI 标题） |
| `VERSION` | `1.0.0` | 版本号 |
| `API_V1_STR` | `/api/v1` | API 前缀 |
| `DEBUG` | `False` | 调试开关 |

### 品牌 / 知识边界（换站点只改这三项）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ASSISTANT_NAME` | `云文档智能问答助手` | 系统提示词与前端标题里的助手人设 |
| `KNOWLEDGE_SCOPE` | `人脸识别产品文档` | 拒答文案里"我只回答 X 范围内问题"的 X |
| `EXAMPLE_QUESTIONS` | `人脸识别是什么？,…` | 拒答时给用户的示例提问（逗号分隔） |

### LLM

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_API_KEY` | `dummy-key` | LLM API Key（**必须改**） |
| `LLM_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容网关 |
| `LLM_MODEL_NAME` | `deepseek-chat` | 模型名 |

### 向量库 / 模型

| 变量 | 默认 | 说明 |
|---|---|---|
| `CHROMA_PERSIST_DIRECTORY` | `data/chromadb` | Chroma 持久化目录 |
| `MODEL_PATH` | `models/bge-m3` | 本地 embedding 模型 |
| `RAG_MAX_DISTANCE` | `0.55` | 检索相关度阈值 |
| `MAX_CONTEXT_LENGTH` | `4000` | RAG 上下文最大字符 |

### 爬虫

| 变量 | 默认 | 说明 |
|---|---|---|
| `TARGET_URL` | `https://cloud.tencent.com/document/product/867` | 默认爬取入口；抓取范围按其**路径前缀**限定 |

### 数据存储

| 变量 | 默认 | 说明 |
|---|---|---|
| `AUDIT_DB_PATH` | `data/audit.db` | 审计 / 反馈 SQLite（不入 repo） |
| `CRAWL_DB_PATH` | `data/crawl_meta.db` | 爬虫元数据 SQLite（可入 repo） |
| `AUDIT_RETENTION_DAYS` | `30` | 审计数据保留天数 |
| `AUDIT_JANITOR_INTERVAL_SECONDS` | `3600` | 审计清理周期 |

### 日志

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOG_FILE` | `.run/app.log` | 主日志路径 |
| `LOG_MAX_BYTES` | `100MB` | 单文件大小上限 |
| `LOG_BACKUP_COUNT` | `10` | 滚动备份数 |

### 安全 / 限流 / 反代

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_INPUT_LENGTH` | `500` | 提问最大字符 |
| `RATE_LIMIT_PER_MINUTE` | `12` | 单 IP RPM |
| `RATE_LIMIT_PER_MINUTE_PER_SESSION` | `20` | 单 session RPM |
| `RATE_LIMIT_PER_MINUTE_PER_SUBNET` | `60` | /24 子网 RPM |
| `GLOBAL_RATE_LIMIT_PER_MINUTE` | `300` | 全实例 RPM 兜底 |
| `MAX_CONCURRENT_QA` | `20` | 全局并发问答 |
| `MAX_CONCURRENT_QA_PER_IP` | `3` | 每 IP 并发问答 |
| `MAX_SESSIONS_PER_IP` | `30` | 每 IP session 总数 |
| `MAX_NEW_SESSIONS_PER_MINUTE` | `10` | 每 IP 每分钟新建 session |
| `DAILY_TOKEN_BUDGET` | `0` | 每日 LLM token 预算，0=不限 |
| `DEDUP_WINDOW_SECONDS` | `5` | 同问题去重窗口 |
| `SSE_CHUNK_TIMEOUT_SECONDS` | `60` | SSE chunk 超时 |
| `REJECT_RESPONSE_FLOOR_SECONDS` | `0.2` | 拒答恒定耗时下限 |
| `INTERNAL_API_ALLOWED_IPS` | `127.0.0.1,::1` | 内部 API 白名单（CIDR） |
| `TRUSTED_PROXIES` | （空） | 可信反代白名单；空 = 不信任 XFF |

---

## 运维查询接口（仅限本地 / 内网）

所有 `/api/v1/internal/*` 路由都受 IP 白名单中间件保护，**不接受 X-Forwarded-For**（防伪造）。

```bash
# 查最近 50 条反馈（默认按时间倒序）
curl "http://localhost:8100/api/v1/internal/feedback?limit=50"

# 只看差评 + 关键字
curl "http://localhost:8100/api/v1/internal/feedback?vote=down&keyword=不准确"

# 查审计日志（QA 记录）
curl "http://localhost:8100/api/v1/internal/audit?session_id=xxx"
curl "http://localhost:8100/api/v1/internal/audit?client_ip=1.2.3.4&since_ms=1700000000000"

# 看每个内存缓存当前 size（排查"内存有没有涨"）
curl "http://localhost:8100/api/v1/internal/cache-stats"

# 看今日已用 / 预算 token
curl "http://localhost:8100/api/v1/internal/token-budget"
```

研发也可直接 `grep` 关键字检索结构化日志（不依赖 API）：

```bash
# 看所有 QA 审计
grep '"qa_audit"' .run/app.log | jq

# 看某 IP 的所有反馈
grep '"user_feedback"' .run/app.log | jq 'select(.client_ip=="1.2.3.4")'

# 看限流触发记录
grep -E '"rate_limit_(ip|subnet|session|global)"' .run/app.log | jq
```

---

## 反馈机制（👍 / 👎）

- Web UI 每条 AI 回答下方提供 👍 / 👎
- 👎 可填"不满意原因"（最多 500 字）
- 后端：`POST /api/v1/feedback` 双写：
  - structlog 日志 `event=user_feedback`
  - SQLite `user_feedback` 表（可通过 `/internal/feedback` 查询）

---

## 多模态（未实现）

当前版本只支持文本型 RAG。如果要扩展 PDF / 图片 / 音视频：

- PDF / Office：解析文本 → 切片 → 向量化
- 图片：OCR 抽文字 / 图像向量模型
- 音视频：ASR 转写（可按时间轴切片）

爬虫 / 切片 / 索引链路可复用，扩展点是"内容抽取"层。

---

## 项目结构

```text
app/
├── api/v1/
│   ├── chat.py        # 问答接口（含 SSE）
│   ├── ingest.py      # 知识库构建
│   └── internal.py    # 运维查询接口（本地白名单）
├── core/
│   ├── config.py        # 配置（pydantic-settings）
│   ├── cache.py         # TtlLruCache + SlidingWindowCounter + janitor
│   ├── security.py      # 多维度限流 + 配额 + 去重 + 拒答恒定耗时
│   ├── net.py           # IP 白名单 / XFF 解析
│   ├── sqlite_store.py  # audit.db（qa_audit + user_feedback）
│   ├── crawl_store.py   # crawl_meta.db（爬虫 documents）
│   ├── token_budget.py  # 每日 LLM token 预算
│   ├── logger.py        # structlog + RotatingFileHandler
│   └── exceptions.py    # 全局异常
├── services/
│   ├── crawler.py       # 爬虫
│   ├── llm_service.py   # RAG 编排 + LLM 调用
│   └── vector_service.py
├── static/              # Web UI
└── main.py

data/
├── audit.db             # 运行时生成（不入 repo）
├── crawl_meta.db        # 可入 repo（随镜像分发知识库）
└── chromadb/            # ChromaDB 持久化

scripts/
└── ingest_cli.py        # 爬取 CLI
```

---

## 常见问题

### 1) 聊天 401 / 鉴权失败

检查 `.env` 中的 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_NAME`。

### 2) 看日志

```bash
tail -f .run/app.log         # 结构化 JSON 日志
tail -f .run/app.stderr.log  # 启动期 stderr 兜底
```

每条日志都带 `request_id`，可用 jq 串接全链路追踪。

### 3) 会话记忆怎么实现？

- 前端：每个浏览器标签 `sessionStorage` 存一个 UUID 当 `session_id`
- 后端：内存 LRU + TTL 30 分钟；不同 session 严格隔离，无法查询他人记录
- session 过期 / 显式 delete 时联动清答案缓存

### 4) 没有 docker / 没有外部数据库还能持久化吗？

可以。两个 SQLite 文件 + 一个 ChromaDB 目录，全在 `data/` 下。
镜像构建可以把 `crawl_meta.db` 和 `chromadb/` 一起带进去，git clone + `bash run.sh` 即可启动带知识库的服务。

---

## License

[MIT](LICENSE) © 2026 lukyFun

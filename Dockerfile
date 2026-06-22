# 使用轻量级 Python 基础镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODEL_PATH=/app/models/bge-m3 \
    TZ=Asia/Shanghai

# 安装系统依赖 (如果需要)
# RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制下载脚本并预下载模型 (构建阶段执行，实现 All-in-One)
# 注意：这会增加镜像构建时间和大小。
# 如果构建环境没有网络，需要提前下载好通过 COPY 指令拷入。
COPY scripts/download_model.py /app/scripts/
RUN python /app/scripts/download_model.py

# 复制应用代码
COPY . .

# 创建非 root 用户运行 (安全最佳实践)
RUN adduser --disabled-password --gecos '' appuser && \
    chown -R appuser:appuser /app
USER appuser

# 暴露端口
EXPOSE 8100

# 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]

import os
from sentence_transformers import SentenceTransformer

# BGE-M3 模型 ID（从 HuggingFace 下载；国内网络慢/不通时改用 download_model_cn.py 走 ModelScope）
MODEL_NAME = "BAAI/bge-m3"
# 模型存储路径：优先用 MODEL_PATH 环境变量，否则默认到仓库内 models/bge-m3
# （相对脚本位置计算，从任意目录运行都对）
DEFAULT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../models/bge-m3"))
MODEL_PATH = os.getenv("MODEL_PATH", DEFAULT_PATH)

def download_model():
    print(f"Downloading model {MODEL_NAME} to {MODEL_PATH}...")
    # 这会将模型下载并保存到指定目录
    model = SentenceTransformer(MODEL_NAME)
    model.save(MODEL_PATH)
    print("Model downloaded successfully.")

if __name__ == "__main__":
    download_model()

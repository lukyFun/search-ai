import os
import sys

# 尝试导入 modelscope，如果不存在则提示安装
try:
    from modelscope.hub.snapshot_download import snapshot_download
except ImportError:
    print("Error: 'modelscope' library is not installed.")
    print("Please run: pip install modelscope")
    sys.exit(1)

# 模型 ID (ModelScope 上的 BGE-M3 镜像)
MODEL_ID = "Xorbits/bge-m3"

# 本地保存路径 (与原脚本保持一致)
CACHE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../models/bge-m3"))

def download_model():
    print(f"Downloading model {MODEL_ID} from ModelScope (Aliyun) to {CACHE_DIR}...")
    
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)
        
    try:
        # local_dir 指定直接下载到该目录，而不是缓存目录
        path = snapshot_download(MODEL_ID, local_dir=CACHE_DIR)
        print(f"\n✅ Model downloaded successfully to: {path}")
        print("You can now restart the Docker container or application.")
    except Exception as e:
        print(f"\n❌ Download failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    download_model()

import chromadb
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from chromadb.config import Settings as ChromaSettings
from sentence_transformers import SentenceTransformer
from app.core.config import get_settings
from app.core.logger import logger
import os
import logging

settings = get_settings()
logging.getLogger("chromadb.telemetry.product.posthog").disabled = True

class BGEM3EmbeddingFunction(EmbeddingFunction):
    def __init__(self):
        # 检查模型路径是否存在
        if os.path.exists(settings.MODEL_PATH) and os.listdir(settings.MODEL_PATH):
             logger.info(f"Loading BGE-M3 model from local path: {settings.MODEL_PATH}")
             self.model = SentenceTransformer(settings.MODEL_PATH)
        else:
             logger.warning(f"Model path {settings.MODEL_PATH} not found or empty. Downloading from HuggingFace Hub (BAAI/bge-m3)...")
             self.model = SentenceTransformer("BAAI/bge-m3")
             # 如果是开发环境，可能希望保存下来
             # self.model.save(settings.MODEL_PATH)

    def __call__(self, input: Documents) -> Embeddings:
        # BGE-M3 返回的是 ndarray，需要转 list
        # normalize_embeddings=True 对余弦相似度搜索很重要
        embeddings = self.model.encode(input, normalize_embeddings=True, show_progress_bar=False)
        return embeddings.tolist()

class VectorService:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(VectorService, cls).__new__(cls)
            cls._instance.initialize()
        return cls._instance

    def initialize(self):
        logger.info(f"Initializing ChromaDB at {settings.CHROMA_PERSIST_DIRECTORY}")
        self.client = chromadb.PersistentClient(
            path=settings.CHROMA_PERSIST_DIRECTORY,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self.embedding_fn = BGEM3EmbeddingFunction()
        self.collection = self.client.get_or_create_collection(
            name="docs",
            embedding_function=self.embedding_fn,
            metadata={"hnsw:space": "cosine"} # 使用余弦相似度
        )
        logger.info("VectorService initialized successfully")
    
    def add_documents(self, documents: list[str], metadatas: list[dict], ids: list[str]):
        """
        添加或更新文档向量
        """
        if not documents:
            return
            
        logger.info(f"Upserting {len(documents)} documents to vector store")
        self.collection.upsert(
            documents=documents,
            metadatas=metadatas,
            ids=ids
        )

    def delete_documents(self, where: dict = None, ids: list[str] = None):
        """
        删除文档
        :param where: metadata 过滤条件，例如 {"url": "http://..."}
        :param ids: 指定 ID 列表删除
        """
        if not where and not ids:
            logger.warning("delete_documents called without where or ids, ignoring.")
            return

        try:
            self.collection.delete(
                ids=ids,
                where=where
            )
            logger.info(f"Deleted documents from vector store. where={where}, ids={len(ids) if ids else 0}")
        except Exception as e:
            logger.error("Failed to delete documents from vector store", error=str(e))
        
    def query(self, query_text: str, n_results: int = 5):
        """
        语义搜索
        """
        results = self.collection.query(
            query_texts=[query_text],
            n_results=n_results
        )
        return results

# 全局实例获取函数
def get_vector_service():
    return VectorService()

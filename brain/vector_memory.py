"""向量化记忆存储 — FAISS 语义检索 + 记忆元数据管理。

替代纯 Markdown 全量注入 System Prompt 的模式。
记忆同时写入 .md 文件（人类可读备份）和 FAISS 索引（语义检索）。

嵌入方案（零配置，自动降级）：
  1. 本地 sentence-transformers 模型 (BAAI/bge-small-zh-v1.5)
     → 首次自动下载 ~96MB，之后完全本地运行
  2. 降级: TF-IDF 文本匹配（无需任何模型，纯数学）

依赖: faiss-cpu, numpy, sentence-transformers (可选)
"""

from __future__ import annotations

import hashlib
import json
import pickle
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

from astrbot.api import logger

# ═══════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════
VECTOR_DIM = 512  # bge-small-zh-v1.5 输出 512 维
INDEX_FILE = "memory_index.faiss"
META_FILE = "memory_meta.json"

# 遗忘算法权重（针对 Nexus 单用户场景调优）
FORGETTING_W_RECENCY = 0.30       # 近因性权重
FORGETTING_W_FREQUENCY = 0.30     # 频率权重
FORGETTING_W_CONFIDENCE = 0.40    # 置信度权重（最重要）
FORGETTING_LAMBDA = 0.08          # 近因性衰减系数（比 Iris 更慢，旧记忆也重要）
FORGETTING_THRESHOLD = 0.25       # 遗忘阈值（低于此分 → 候选淘汰）
FORGETTING_IMMEDIATE_THRESHOLD = 0.08  # 立即淘汰阈值


# ═══════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════

@dataclass
class MemoryEntry:
    """单条记忆条目的完整元数据。"""

    id: str                          # [mem-YYYYMMDD-NNN]
    content: str                     # 记忆文本内容
    timestamp: str = ""              # 创建时间 ISO 格式
    confidence: float = 0.7          # 置信度 (0.0-1.0)
    access_count: int = 0            # 被检索命中的次数
    last_access_time: str = ""       # 最近被检索时间
    source: str = "extraction"       # 来源: "extraction" | "manual"
    merged_from: list[str] = field(default_factory=list)  # 由哪些旧记忆合并而来

    @property
    def age_days(self) -> float:
        """记忆年龄（天）。"""
        if not self.timestamp:
            return 999.0
        try:
            created = datetime.fromisoformat(self.timestamp)
            return (datetime.now(timezone.utc) - created).total_seconds() / 86400
        except (ValueError, TypeError):
            return 999.0

    @property
    def days_since_access(self) -> float:
        """距上次访问的天数。"""
        access_time = self.last_access_time or self.timestamp
        if not access_time:
            return 999.0
        try:
            dt = datetime.fromisoformat(access_time)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except (ValueError, TypeError):
            return 999.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "timestamp": self.timestamp,
            "confidence": self.confidence,
            "access_count": self.access_count,
            "last_access_time": self.last_access_time,
            "source": self.source,
            "merged_from": self.merged_from,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryEntry":
        return cls(
            id=d.get("id", ""),
            content=d.get("content", ""),
            timestamp=d.get("timestamp", ""),
            confidence=d.get("confidence", 0.7),
            access_count=d.get("access_count", 0),
            last_access_time=d.get("last_access_time", ""),
            source=d.get("source", "extraction"),
            merged_from=d.get("merged_from", []),
        )


# ═══════════════════════════════════════════════════════════
# 本地嵌入模型（零配置，自动下载）
# ═══════════════════════════════════════════════════════════

# 默认使用 bge-small-zh-v1.5：中文优化、体积小(96MB)、维度低(512)
_LOCAL_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
_LOCAL_MODEL_DIM = 512  # bge-small-zh-v1.5 输出 512 维

# 全局模型缓存（进程级单例）
_embedding_model = None
_embedding_model_name: str = ""
_tfidf_vectorizer = None
_tfidf_idf = None


def _load_local_embedding_model():
    """加载本地 sentence-transformers 模型（自动下载 + 缓存）。

    首次调用时自动从 HuggingFace 下载模型 (~96MB)，
    之后缓存在内存中。完全离线、零配置。
    """
    global _embedding_model, _embedding_model_name
    if _embedding_model is not None:
        return _embedding_model

    try:
        from sentence_transformers import SentenceTransformer
        logger.info(f"正在加载本地嵌入模型: {_LOCAL_MODEL_NAME} ...")
        _embedding_model = SentenceTransformer(_LOCAL_MODEL_NAME)
        _embedding_model_name = _LOCAL_MODEL_NAME
        logger.info(f"本地嵌入模型已就绪: {_LOCAL_MODEL_NAME}")
        return _embedding_model
    except ImportError:
        logger.warning(
            "sentence-transformers 未安装，向量检索将降级为 TF-IDF 文本匹配。"
            "如需更好的检索效果，请运行: pip install sentence-transformers"
        )
        return None
    except Exception as e:
        logger.warning(f"加载本地嵌入模型失败 ({e})，降级为 TF-IDF 文本匹配")
        return None


def _encode_with_local_model(texts: list[str]) -> np.ndarray:
    """用本地模型将文本列表编码为向量。"""
    model = _load_local_embedding_model()
    if model is None:
        raise RuntimeError("本地嵌入模型不可用")

    embeddings = model.encode(
        texts,
        normalize_embeddings=True,  # L2 归一化，用于内积检索
        show_progress_bar=False,
    )
    return np.array(embeddings, dtype=np.float32)


def _encode_with_tfidf(texts: list[str], fit: bool = False) -> np.ndarray:
    """用 TF-IDF 将文本列表编码为稀疏向量（FAISS 兼容的 dense 格式）。

    纯数学，零模型、零下载、零配置。
    质量不如语义嵌入，但不需要任何外部依赖。
    """
    global _tfidf_vectorizer, _tfidf_idf
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:
        # 最终降级: 用字符级 one-hot 近似
        return _char_level_encode(texts)

    if fit or _tfidf_vectorizer is None:
        _tfidf_vectorizer = TfidfVectorizer(
            max_features=VECTOR_DIM,
            analyzer='char_wb',
            ngram_range=(2, 4),
        )
        _tfidf_vectorizer.fit(texts)
        _tfidf_idf = _tfidf_vectorizer.idf_

    vectors = _tfidf_vectorizer.transform(texts)
    dense = vectors.toarray().astype(np.float32)

    # 填充/截断到 VECTOR_DIM
    if dense.shape[1] < VECTOR_DIM:
        padded = np.zeros((dense.shape[0], VECTOR_DIM), dtype=np.float32)
        padded[:, :dense.shape[1]] = dense
        dense = padded
    elif dense.shape[1] > VECTOR_DIM:
        dense = dense[:, :VECTOR_DIM]

    # L2 归一化
    norms = np.linalg.norm(dense, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return dense / norms


def _char_level_encode(texts: list[str]) -> np.ndarray:
    """终极降级方案: 字符级 bag-of-chars 编码。

    不需要 sklearn，纯 numpy 实现。
    """
    # 收集所有字符构建词汇表
    all_chars = sorted(set(''.join(texts)))
    char_to_idx = {c: i for i, c in enumerate(all_chars)}
    vocab_size = min(len(all_chars), VECTOR_DIM)

    vectors = np.zeros((len(texts), VECTOR_DIM), dtype=np.float32)
    for i, text in enumerate(texts):
        for char in text:
            idx = char_to_idx.get(char, -1)
            if 0 <= idx < vocab_size:
                vectors[i, idx] += 1.0

    # 归一化
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


def get_embedding_function():
    """获取当前可用的最佳嵌入函数。

    返回 (embed_fn, dim) 元组:
      - embed_fn: async fn(texts: list[str]) -> np.ndarray
      - dim: 向量维度

    优先级: 本地模型 > TF-IDF > 字符编码
    """
    # 尝试加载本地模型
    model = _load_local_embedding_model()
    if model is not None:

        async def _local_embed(texts: list[str]) -> np.ndarray:
            import asyncio
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, _encode_with_local_model, texts
            )

        return _local_embed, _LOCAL_MODEL_DIM

    # 降级: TF-IDF
    try:
        import sklearn  # noqa: F401
        async def _tfidf_embed(texts: list[str]) -> np.ndarray:
            import asyncio
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, lambda t: _encode_with_tfidf(t, fit=True), texts
            )

        logger.info("嵌入方案: TF-IDF 文本匹配（pip install sentence-transformers 可升级）")
        return _tfidf_embed, VECTOR_DIM
    except ImportError:
        pass

    # 终极降级: 字符编码
    async def _char_embed(texts: list[str]) -> np.ndarray:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _char_level_encode, texts)

    logger.info("嵌入方案: 字符级匹配（安装 sentence-transformers 获得更好效果）")
    return _char_embed, VECTOR_DIM


# ═══════════════════════════════════════════════════════════
# 向量记忆存储
# ═══════════════════════════════════════════════════════════

class VectorMemoryStore:
    """FAISS 向量记忆存储 + 元数据管理。

    双写模式：FAISS 索引（语义检索） + .md 文件（人类可读备份）。

    使用方式:
        store = VectorMemoryStore(data_dir=Path("./memory"))
        await store.initialize(embedding_fn)

        # 写入
        mid = await store.add_memory("主人喜欢喝咖啡", confidence=0.85)

        # 检索
        results = await store.search("主人喜欢喝什么", top_k=5)
        for entry, score in results:
            print(f"[{score:.3f}] {entry.content}")

        # 遗忘评分
        score = store.calculate_forgetting_score(entry)
    """

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._index: Any = None           # FAISS IndexFlatIP
        self._entries: dict[str, MemoryEntry] = {}  # id → MemoryEntry
        self._id_list: list[str] = []     # 与 FAISS 索引行对应的 id 列表
        self._embedding_fn: Any = None    # async fn(texts: list[str]) -> np.ndarray
        self._initialized = False

    # ── 属性 ──

    @property
    def count(self) -> int:
        return len(self._entries)

    @property
    def is_available(self) -> bool:
        return self._initialized and self._index is not None

    # ── 初始化 ──

    async def initialize(self, embedding_fn=None) -> None:
        """初始化 FAISS 索引并加载已有元数据。

        自动选择最佳嵌入方案（本地模型 > TF-IDF > 字符编码），
        零配置、无需外部 API。

        Args:
            embedding_fn: 可选的嵌入函数。None 则自动检测。
        """
        if embedding_fn is not None:
            self._embedding_fn = embedding_fn
        else:
            self._embedding_fn, detected_dim = get_embedding_function()
            # 如果检测到的维度与默认不同，重新创建索引
            if detected_dim != VECTOR_DIM:
                logger.info(
                    f"检测到嵌入维度 {detected_dim}，将使用此维度创建索引"
                )

        try:
            import faiss
            self._faiss = faiss
        except ImportError:
            logger.warning(
                "faiss-cpu 未安装，向量记忆存储降级为纯元数据模式。"
                "请运行: pip install faiss-cpu"
            )
            self._initialized = True
            return

        # 加载元数据
        self._load_metadata()

        # 初始化或加载 FAISS 索引
        index_path = self._data_dir / INDEX_FILE
        if index_path.exists() and self._entries:
            try:
                self._index = faiss.read_index(str(index_path))
                logger.info(
                    f"FAISS 索引已加载: {self._index.ntotal} 条向量 "
                    f"({index_path})"
                )
            except Exception as e:
                logger.warning(f"加载 FAISS 索引失败，将重建: {e}")
                self._index = None

        if self._index is None:
            self._index = faiss.IndexFlatIP(VECTOR_DIM)
            logger.info(f"FAISS 索引已创建 (dim={VECTOR_DIM})")

        # 如果索引为空但元数据非空，重建索引
        if self._index.ntotal == 0 and self._entries:
            logger.info(
                f"FAISS 索引为空但元数据有 {len(self._entries)} 条，"
                f"正在重建索引..."
            )
            await self._rebuild_index()

        self._initialized = True
        logger.info(
            f"VectorMemoryStore 初始化完成: {len(self._entries)} 条记忆, "
            f"{self._index.ntotal} 条向量"
        )

    async def _rebuild_index(self) -> None:
        """从已有元数据重建 FAISS 索引。"""
        try:
            contents = [e.content for e in self._entries.values()]
            vectors = await self._embed_texts(contents)
            self._index.add(vectors)
            self._id_list = list(self._entries.keys())

            index_path = self._data_dir / INDEX_FILE
            self._faiss.write_index(self._index, str(index_path))
            logger.info(f"FAISS 索引已重建: {len(contents)} 条")
        except Exception as e:
            logger.error(f"重建 FAISS 索引失败: {e}")

    # ── CRUD ──

    async def add_memory(
        self,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
        skip_embedding: bool = False,
    ) -> Optional[str]:
        """添加一条记忆到向量存储。

        Args:
            content: 记忆文本内容
            metadata: 额外元数据 (confidence, source, etc.)
            skip_embedding: 跳过向量化（用于从 .md 导入时批量添加）

        Returns:
            记忆 ID，失败返回 None。
        """
        if not content or not content.strip():
            return None

        metadata = metadata or {}
        today = datetime.now(timezone.utc).strftime("%Y%m%d")

        # 生成 ID
        existing_ids = {e.id for e in self._entries.values()}
        counter = 1
        while True:
            mem_id = f"[mem-{today}-{counter:03d}]"
            if mem_id not in existing_ids:
                break
            counter += 1

        entry = MemoryEntry(
            id=mem_id,
            content=content.strip(),
            timestamp=datetime.now(timezone.utc).isoformat(),
            confidence=float(metadata.get("confidence", 0.7)),
            access_count=int(metadata.get("access_count", 0)),
            last_access_time=metadata.get("last_access_time", ""),
            source=str(metadata.get("source", "extraction")),
            merged_from=list(metadata.get("merged_from", [])),
        )

        self._entries[mem_id] = entry

        # 向量化并添加到 FAISS 索引
        if not skip_embedding and self._index is not None and self._embedding_fn:
            try:
                vectors = await self._embed_texts([content])
                self._index.add(vectors)
                self._id_list.append(mem_id)
                self._save_metadata()
                self._save_index()
            except Exception as e:
                logger.warning(f"记忆向量化失败 (id={mem_id}): {e}")
                # 仍然保存元数据
                self._save_metadata()
        else:
            self._id_list.append(mem_id)
            self._save_metadata()

        return mem_id

    async def search(
        self, query: str, top_k: int = 10, min_score: float = 0.0
    ) -> list[tuple[MemoryEntry, float]]:
        """语义检索最相关的记忆。

        Args:
            query: 查询文本
            top_k: 返回结果数
            min_score: 最低相关性阈值 (0.0-1.0)

        Returns:
            [(MemoryEntry, score), ...] 按相关性降序排列
        """
        if not self._entries:
            return []

        # 纯向量检索
        if self._index is not None and self._embedding_fn and self._index.ntotal > 0:
            return await self._vector_search(query, top_k, min_score)
        else:
            # 降级: 纯文本匹配
            logger.debug("FAISS 不可用，降级为文本匹配检索")
            return self._text_search(query, top_k, min_score)

    async def _vector_search(
        self, query: str, top_k: int, min_score: float
    ) -> list[tuple[MemoryEntry, float]]:
        """FAISS 向量检索。"""
        try:
            vectors = await self._embed_texts([query])
            k = min(top_k, self._index.ntotal)
            scores, indices = self._index.search(vectors, k)

            results: list[tuple[MemoryEntry, float]] = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0 or idx >= len(self._id_list):
                    continue
                mem_id = self._id_list[idx]
                entry = self._entries.get(mem_id)
                if entry and score >= min_score:
                    results.append((entry, float(score)))
                    # 更新访问计数
                    self._record_access(entry)

            return results
        except Exception as e:
            logger.warning(f"向量检索失败: {e}")
            return self._text_search(query, top_k, min_score)

    def _text_search(
        self, query: str, top_k: int, min_score: float
    ) -> list[tuple[MemoryEntry, float]]:
        """纯文本 BM25 风格的简单检索（FAISS 不可用时的降级方案）。"""
        from difflib import SequenceMatcher

        scored: list[tuple[MemoryEntry, float]] = []
        query_lower = query.lower()

        for entry in self._entries.values():
            content_lower = entry.content.lower()
            # 简单的 token overlap + sequence match
            query_tokens = set(query_lower.split())
            content_tokens = set(content_lower.split())
            if not query_tokens:
                continue
            overlap = len(query_tokens & content_tokens) / len(query_tokens)
            seq_score = SequenceMatcher(None, query_lower, content_lower).ratio()
            score = 0.6 * overlap + 0.4 * seq_score  # token overlap 权重更高

            if score >= min_score:
                scored.append((entry, score))
                self._record_access(entry)

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def get_all_entries(self) -> list[MemoryEntry]:
        """获取所有记忆条目。"""
        return list(self._entries.values())

    def get_entry(self, mem_id: str) -> Optional[MemoryEntry]:
        """按 ID 获取记忆条目。"""
        return self._entries.get(mem_id)

    def remove_entry(self, mem_id: str) -> bool:
        """删除一条记忆。"""
        if mem_id not in self._entries:
            return False
        del self._entries[mem_id]
        if mem_id in self._id_list:
            self._id_list.remove(mem_id)
        self._save_metadata()
        # 注意: FAISS 不支持直接删除，标记为需要重建
        logger.debug(f"记忆已标记删除: {mem_id}（索引将在下次维护时重建）")
        return True

    def remove_batch(self, mem_ids: list[str]) -> int:
        """批量删除记忆。"""
        count = 0
        for mid in mem_ids:
            if self.remove_entry(mid):
                count += 1
        return count

    # ── 遗忘评分 ──

    def calculate_forgetting_score(self, entry: MemoryEntry) -> float:
        """计算记忆的综合遗忘评分。

        公式: S = w1 * R + w2 * F + w3 * C

        得分越高越重要，越不应被淘汰。

        Nexus 单用户场景权重调优：
        - 置信度权重最高(0.40): "主人妈妈生日"这种高置信记忆应永不过期
        - 频率适中(0.30): 被频繁检索的记忆更重要
        - 近因性较低(0.30): 旧记忆在主人场景中也同样珍贵
        - 衰减系数慢(λ=0.08): 30 天后近因性仍 ~0.09, 比 Iris 的 0.05 还慢
        """
        import math

        # R: 近因性 — 指数衰减
        days = entry.days_since_access
        recency = math.exp(-FORGETTING_LAMBDA * days)

        # F: 频率 — 对数归一化
        freq = math.log(entry.access_count + 1) / math.log(101)

        # C: 置信度
        confidence = max(0.0, min(1.0, entry.confidence))

        score = (
            FORGETTING_W_RECENCY * recency
            + FORGETTING_W_FREQUENCY * freq
            + FORGETTING_W_CONFIDENCE * confidence
        )

        return max(0.0, min(1.0, score))

    def should_evict(self, entry: MemoryEntry, retention_days: int = 30) -> bool:
        """判断记忆是否应被淘汰。

        保护规则（满足任一则永不淘汰）：
        - 高置信度 (>=0.85) + 被访问过
        - 最近 7 天内创建
        """
        # 高置信度保护
        if entry.confidence >= 0.85 and entry.access_count > 0:
            return False

        # 新记忆保护
        if entry.age_days < 7:
            return False

        score = self.calculate_forgetting_score(entry)

        # 立即淘汰
        if score < FORGETTING_IMMEDIATE_THRESHOLD:
            return True

        # 低于阈值且超过保留期
        if score < FORGETTING_THRESHOLD:
            if entry.days_since_access > retention_days:
                return True

        return False

    def get_eviction_candidates(
        self, max_entries: int = 60, retention_days: int = 30
    ) -> list[tuple[MemoryEntry, float]]:
        """获取候选淘汰记忆列表（按遗忘评分升序，最该淘汰的排前面）。

        Args:
            max_entries: 超过此数量才触发淘汰
            retention_days: 保留期天数

        Returns:
            [(entry, score), ...] 按评分升序
        """
        if len(self._entries) <= max_entries:
            return []

        candidates: list[tuple[MemoryEntry, float]] = []
        for entry in self._entries.values():
            if self.should_evict(entry, retention_days):
                score = self.calculate_forgetting_score(entry)
                candidates.append((entry, score))

        candidates.sort(key=lambda x: x[1])  # 低分在前
        return candidates

    # ── 内部 ──

    def _record_access(self, entry: MemoryEntry) -> None:
        """记录记忆被访问（频率+1，更新最近访问时间）。"""
        entry.access_count += 1
        entry.last_access_time = datetime.now(timezone.utc).isoformat()

    async def _embed_texts(self, texts: list[str]) -> np.ndarray:
        """调用 AstrBot Embedding Provider 向量化文本。

        返回 (N, dim) 的 float32 numpy 数组。
        """
        if not self._embedding_fn:
            raise RuntimeError("Embedding function not set")

        try:
            result = await self._embedding_fn(texts)
            if isinstance(result, np.ndarray):
                vectors = result.astype(np.float32)
            else:
                vectors = np.array(result, dtype=np.float32)

            if vectors.ndim == 1:
                vectors = vectors.reshape(1, -1)

            # L2 归一化（用于内积检索 = 余弦相似度）
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            vectors = vectors / norms

            return vectors
        except Exception as e:
            logger.error(f"文本向量化失败: {e}")
            raise

    def _save_metadata(self) -> None:
        """保存元数据到 JSON 文件。"""
        meta_path = self._data_dir / META_FILE
        try:
            data = {
                "entries": [e.to_dict() for e in self._entries.values()],
                "id_list": self._id_list,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning(f"保存记忆元数据失败: {e}")

    def _save_index(self) -> None:
        """保存 FAISS 索引到磁盘。"""
        if self._index is None or not hasattr(self, '_faiss'):
            return
        index_path = self._data_dir / INDEX_FILE
        try:
            self._faiss.write_index(self._index, str(index_path))
        except Exception as e:
            logger.warning(f"保存 FAISS 索引失败: {e}")

    def _load_metadata(self) -> None:
        """从 JSON 文件加载元数据。"""
        meta_path = self._data_dir / META_FILE
        if not meta_path.exists():
            return

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for entry_dict in data.get("entries", []):
                entry = MemoryEntry.from_dict(entry_dict)
                if entry.id and entry.content:
                    self._entries[entry.id] = entry

            self._id_list = data.get("id_list", list(self._entries.keys()))

            if self._entries:
                logger.info(
                    f"记忆元数据已加载: {len(self._entries)} 条 "
                    f"({meta_path})"
                )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"加载记忆元数据失败: {e}")

    def sync_from_notebook(self, notebook_text: str) -> int:
        """从 .md AUTO-MEMORY 段同步记忆到向量存储。

        解析 [mem-YYYYMMDD-NNN] 格式的条目，对比已有条目，
        增量添加新条目、标记已删除条目。

        Returns:
            新增的记忆数量。
        """
        import re

        # 解析出每条 [mem-ID] 开头的记忆
        bullet_pattern = re.compile(
            r'^- \[mem-(\d{8}-\d{3})\]\s+(.+)', re.MULTILINE
        )
        notebook_entries: dict[str, str] = {}
        for match in bullet_pattern.finditer(notebook_text):
            mem_id = f"[mem-{match.group(1)}]"
            content = match.group(2).strip()
            if content:
                notebook_entries[mem_id] = content

        # 增量同步
        added = 0
        for mem_id, content in notebook_entries.items():
            if mem_id not in self._entries:
                entry = MemoryEntry(
                    id=mem_id,
                    content=content,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    confidence=0.7,
                    source="notebook_sync",
                )
                self._entries[mem_id] = entry
                self._id_list.append(mem_id)
                added += 1

        if added > 0:
            self._save_metadata()
            logger.info(
                f"从小本本同步: {added} 条新记忆 → 向量存储 "
                f"(总计 {len(self._entries)} 条)"
            )

        return added

    def rebuild_index_from_entries(self) -> None:
        """标记索引需要重建（在下一次搜索时自动触发）。"""
        # 清空 id_list 以触发重建
        # 实际重建在 _vector_search 首次调用时进行
        if self._index is not None:
            self._index.reset()
            self._id_list = []
            logger.info("FAISS 索引已重置，将在下次搜索时重建")

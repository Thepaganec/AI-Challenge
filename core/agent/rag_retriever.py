import json
import math
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib import request as urllib_request


class RagError(RuntimeError):
    pass


class RagRetriever:
    def __init__(
        self,
        *,
        project_root: str,
        rag_dir: str = "RAG",
        ollama_url: str = "http://127.0.0.1:11434",
        model: str = "nomic-embed-text",
        timeout_sec: int = 30,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.rag_dir = (self.project_root / rag_dir).resolve()
        self.ollama_url = str(ollama_url).rstrip("/")
        self.model = str(model).strip()
        self.timeout_sec = int(timeout_sec)
        self._cache: Dict[str, Dict[str, Any]] = {}

    def _strategy_paths(self, strategy: str) -> Tuple[Path, Path]:
        clean = str(strategy or "").strip().lower()
        if clean not in {"fixed", "structural"}:
            raise RagError(f"Unsupported RAG strategy: {strategy}")
        return (
            self.rag_dir / f"chunks_{clean}.jsonl",
            self.rag_dir / f"embeddings_{clean}.jsonl",
        )

    def _read_jsonl(self, path: Path) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                clean = line.strip()
                if not clean:
                    continue
                try:
                    row = json.loads(clean)
                except Exception as e:
                    raise RagError(f"Invalid JSONL in {path} at line {line_no}: {e}") from e
                if isinstance(row, dict):
                    rows.append(row)
        return rows

    def _load_index(self, strategy: str) -> List[Dict[str, Any]]:
        chunks_path, embeddings_path = self._strategy_paths(strategy)
        if not chunks_path.exists() or not embeddings_path.exists():
            raise RagError(
                f"RAG index files are missing for '{strategy}'. "
                f"Expected: {chunks_path.name}, {embeddings_path.name}"
            )

        mtime_sig = (
            chunks_path.stat().st_mtime_ns,
            embeddings_path.stat().st_mtime_ns,
        )
        cached = self._cache.get(strategy)
        if isinstance(cached, dict) and cached.get("mtime_sig") == mtime_sig:
            rows = cached.get("rows")
            if isinstance(rows, list):
                return rows

        chunk_rows = self._read_jsonl(chunks_path)
        emb_rows = self._read_jsonl(embeddings_path)

        chunk_by_id: Dict[str, Dict[str, Any]] = {}
        for item in chunk_rows:
            chunk_id = str(item.get("chunk_id") or "").strip()
            if chunk_id:
                chunk_by_id[chunk_id] = item

        joined: List[Dict[str, Any]] = []
        for item in emb_rows:
            chunk_id = str(item.get("chunk_id") or "").strip()
            vector = item.get("embedding")
            if not chunk_id or not isinstance(vector, list) or not vector:
                continue
            chunk = chunk_by_id.get(chunk_id) or {}
            content = str(chunk.get("content") or "").strip()
            if not content:
                continue
            joined.append(
                {
                    "chunk_id": chunk_id,
                    "file": str(chunk.get("file") or item.get("file") or ""),
                    "section": str(chunk.get("section") or item.get("section") or ""),
                    "start_line": int(chunk.get("start_line") or 0),
                    "end_line": int(chunk.get("end_line") or 0),
                    "content": content,
                    "vector": [float(x) for x in vector],
                }
            )

        if not joined:
            raise RagError(f"RAG index is empty for strategy '{strategy}'. Rebuild index files in {self.rag_dir}.")

        self._cache[strategy] = {"mtime_sig": mtime_sig, "rows": joined}
        return joined

    def _embed_query(self, text: str) -> List[float]:
        clean = str(text or "").strip()
        if not clean:
            raise RagError("Empty query for RAG embedding.")
        headers = {"Content-Type": "application/json"}

        def _post(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            url = f"{self.ollama_url}{endpoint}"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib_request.Request(url, data=body, headers=headers, method="POST")
            with urllib_request.urlopen(req, timeout=self.timeout_sec) as resp:
                data = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(data) if data else {}
            return parsed if isinstance(parsed, dict) else {}

        try:
            data = _post("/api/embed", {"model": self.model, "input": [clean]})
            embeds = data.get("embeddings")
            if isinstance(embeds, list) and embeds and isinstance(embeds[0], list):
                return [float(x) for x in embeds[0]]
        except Exception:
            pass
        try:
            data = _post("/api/embeddings", {"model": self.model, "prompt": clean})
        except Exception as e:
            raise RagError(f"Failed to embed query via Ollama at {self.ollama_url}: {e}") from e
        emb = data.get("embedding")
        if not isinstance(emb, list) or not emb:
            raise RagError("Ollama returned no embedding vector for query.")
        return [float(x) for x in emb]

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        n = min(len(a), len(b))
        if n <= 0:
            return 0.0
        dot = 0.0
        na = 0.0
        nb = 0.0
        for i in range(n):
            x = float(a[i])
            y = float(b[i])
            dot += x * y
            na += x * x
            nb += y * y
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        return dot / (math.sqrt(na) * math.sqrt(nb))

    def retrieve(self, *, query: str, strategy: str, top_k: int) -> List[Dict[str, Any]]:
        rows = self._load_index(strategy)
        qvec = self._embed_query(query)
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for row in rows:
            score = self._cosine(qvec, row.get("vector") or [])
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        k = max(1, int(top_k or 4))
        best = scored[:k]
        if not best:
            raise RagError("No relevant chunks found for this query.")
        out: List[Dict[str, Any]] = []
        for score, row in best:
            item = dict(row)
            item["score"] = float(score)
            out.append(item)
        return out

    @staticmethod
    def build_rag_context(chunks: List[Dict[str, Any]]) -> str:
        parts: List[str] = [
            "RAG_CONTEXT: Используй релевантные фрагменты кода ниже как основной источник фактов.",
            "Если фрагментов не хватает, явно обозначь ограничение.",
        ]
        for idx, item in enumerate(chunks, start=1):
            file_path = str(item.get("file") or "")
            section = str(item.get("section") or "")
            start_ln = int(item.get("start_line") or 0)
            end_ln = int(item.get("end_line") or 0)
            score = float(item.get("score") or 0.0)
            header = (
                f"[RAG #{idx}] file={file_path} | section={section} | "
                f"lines={start_ln}-{end_ln} | score={score:.4f}"
            )
            parts.append(header)
            parts.append(str(item.get("content") or ""))
        return "\n\n".join(parts).strip()

    @staticmethod
    def build_sources_block(chunks: List[Dict[str, Any]]) -> str:
        lines: List[str] = ["Источники:"]
        for idx, item in enumerate(chunks, start=1):
            file_path = str(item.get("file") or "")
            section = str(item.get("section") or "")
            start_ln = int(item.get("start_line") or 0)
            end_ln = int(item.get("end_line") or 0)
            score = float(item.get("score") or 0.0)
            lines.append(
                f"{idx}) {file_path} | {section} | lines {start_ln}-{end_ln} | score={score:.4f}"
            )
        return "\n".join(lines).strip()

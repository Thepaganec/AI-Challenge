import json
import math
import os
import re
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
        llm_base_url: str = "https://openai.api.proxyapi.ru/v1",
        llm_api_key_env: str = "PROXYAPI_KEY",
        llm_model: str = "gpt-4o-mini",
        llm_timeout_sec: int = 40,
        heuristic_w_semantic: float = 0.8,
        heuristic_w_lexical: float = 0.2,
        llm_rerank_max_chunks: int = 8,
        llm_rerank_max_chars_per_chunk: int = 1200,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.rag_dir = (self.project_root / rag_dir).resolve()
        self.ollama_url = str(ollama_url).rstrip("/")
        self.model = str(model).strip()
        self.timeout_sec = int(timeout_sec)
        self._cache: Dict[str, Dict[str, Any]] = {}

        self.llm_base_url = str(llm_base_url).rstrip("/")
        self.llm_api_key_env = str(llm_api_key_env).strip() or "PROXYAPI_KEY"
        self.llm_model = str(llm_model).strip() or "gpt-4o-mini"
        self.llm_timeout_sec = max(5, int(llm_timeout_sec))
        self.heuristic_w_semantic = max(0.0, float(heuristic_w_semantic))
        self.heuristic_w_lexical = max(0.0, float(heuristic_w_lexical))
        self.llm_rerank_max_chunks = max(1, int(llm_rerank_max_chunks))
        self.llm_rerank_max_chars_per_chunk = max(200, int(llm_rerank_max_chars_per_chunk))
        self.last_run_meta: Dict[str, Any] = {}

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
                    "source": str(chunk.get("source") or item.get("source") or "repo"),
                    "title": str(chunk.get("title") or item.get("title") or chunk.get("file") or item.get("file") or ""),
                    "file": str(chunk.get("file") or item.get("file") or ""),
                    "path": str(chunk.get("path") or item.get("path") or ""),
                    "section": str(chunk.get("section") or item.get("section") or ""),
                    "strategy": str(chunk.get("strategy") or item.get("strategy") or strategy),
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

    @staticmethod
    def _tokenize_for_lexical(text: str) -> List[str]:
        clean = str(text or "").lower()
        return [t for t in re.split(r"[^a-zA-Z0-9_а-яА-Я]+", clean) if len(t) >= 2]

    @classmethod
    def _lexical_overlap_score(cls, query: str, content: str) -> float:
        q_tokens = set(cls._tokenize_for_lexical(query))
        if not q_tokens:
            return 0.0
        c_tokens = set(cls._tokenize_for_lexical(content))
        if not c_tokens:
            return 0.0
        return float(len(q_tokens & c_tokens) / len(q_tokens))

    def _llm_api_key(self) -> str:
        return str(os.getenv(self.llm_api_key_env) or "").strip()

    def _chat_completion_text(self, *, messages: List[Dict[str, str]], max_tokens: int = 256) -> str:
        api_key = self._llm_api_key()
        if not api_key:
            raise RagError(f"LLM API key is missing in env '{self.llm_api_key_env}'.")

        payload = {
            "model": self.llm_model,
            "messages": messages,
            "stream": False,
            "max_completion_tokens": int(max_tokens),
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.llm_base_url}/chat/completions"
        req = urllib_request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib_request.urlopen(req, timeout=self.llm_timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            raise RagError(f"LLM call failed at {self.llm_base_url}: {e}") from e

        try:
            parsed = json.loads(raw) if raw else {}
        except Exception as e:
            raise RagError(f"Invalid LLM JSON response: {e}") from e

        choices = parsed.get("choices") if isinstance(parsed.get("choices"), list) else []
        if not choices:
            return ""
        first = choices[0] if isinstance(choices[0], dict) else {}
        msg = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, dict) and str(item.get("type") or "") in {"output_text", "text"}:
                    part = str(item.get("text") or "").strip()
                    if part:
                        parts.append(part)
            return "\n".join(parts).strip()
        return ""

    @staticmethod
    def _parse_json_object(text: str) -> Dict[str, Any]:
        raw = str(text or "").strip()
        if not raw:
            return {}
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z0-9_\-]*", "", raw).strip()
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if 0 <= start < end:
            snippet = raw[start : end + 1]
            try:
                data2 = json.loads(snippet)
                return data2 if isinstance(data2, dict) else {}
            except Exception:
                return {}
        return {}

    def _rewrite_query(self, query: str) -> Tuple[str, str]:
        prompt_system = (
            "You rewrite user queries for codebase retrieval. "
            "Keep meaning unchanged, add key technical terms, stay concise. "
            "Return plain text only."
        )
        prompt_user = (
            "Rewrite this query for semantic search in repository chunks. "
            "Output one line only.\n\n"
            f"Query: {query}"
        )
        text = self._chat_completion_text(
            messages=[
                {"role": "system", "content": prompt_system},
                {"role": "user", "content": prompt_user},
            ],
            max_tokens=96,
        )
        rewritten = str(text or "").strip()
        if not rewritten:
            return query, "empty_rewrite_result"
        return rewritten, ""

    @staticmethod
    def _to_output_rows(scored_rows: List[Tuple[float, Dict[str, Any]]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for rank, (score, row) in enumerate(scored_rows, start=1):
            item = dict(row)
            item["score"] = float(score)
            item["rank"] = rank
            out.append(item)
        return out

    def _apply_threshold_filter(
        self,
        *,
        rows: List[Dict[str, Any]],
        similarity_threshold: float,
        top_k_after: int,
    ) -> List[Dict[str, Any]]:
        filtered = [row for row in rows if float(row.get("score") or 0.0) >= similarity_threshold]
        return filtered[:top_k_after]

    def _apply_heuristic_rerank(
        self,
        *,
        rows: List[Dict[str, Any]],
        query: str,
        similarity_threshold: float,
        top_k_after: int,
    ) -> List[Dict[str, Any]]:
        reranked: List[Tuple[float, Dict[str, Any]]] = []
        total_weight = self.heuristic_w_semantic + self.heuristic_w_lexical
        if total_weight <= 0.0:
            total_weight = 1.0

        for row in rows:
            base_score = float(row.get("score") or 0.0)
            lexical = self._lexical_overlap_score(query, str(row.get("content") or ""))
            combined = (
                self.heuristic_w_semantic * base_score + self.heuristic_w_lexical * lexical
            ) / total_weight
            item = dict(row)
            item["lexical_score"] = float(lexical)
            item["combined_score"] = float(combined)
            reranked.append((combined, item))

        reranked.sort(key=lambda x: x[0], reverse=True)
        rows2 = [item for _, item in reranked]
        rows2 = [row for row in rows2 if float(row.get("score") or 0.0) >= similarity_threshold]
        return rows2[:top_k_after]

    def _apply_llm_rerank(
        self,
        *,
        rows: List[Dict[str, Any]],
        query: str,
        similarity_threshold: float,
        top_k_after: int,
    ) -> Tuple[List[Dict[str, Any]], str]:
        if not rows:
            return [], ""

        limited = rows[: self.llm_rerank_max_chunks]
        compact_rows: List[Dict[str, Any]] = []
        for row in limited:
            compact_rows.append(
                {
                    "chunk_id": str(row.get("chunk_id") or ""),
                    "file": str(row.get("file") or ""),
                    "section": str(row.get("section") or ""),
                    "score": float(row.get("score") or 0.0),
                    "content": str(row.get("content") or "")[: self.llm_rerank_max_chars_per_chunk],
                }
            )

        user_payload = {
            "query": query,
            "chunks": compact_rows,
            "instruction": "Return JSON with ranked_ids array sorted by relevance desc.",
        }
        prompt_system = (
            "You are a retrieval reranker. Return strict JSON only: "
            "{\"ranked_ids\":[\"id1\",\"id2\"],\"notes\":\"optional\"}. "
            "Never include ids that are not in input."
        )
        answer = self._chat_completion_text(
            messages=[
                {"role": "system", "content": prompt_system},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            max_tokens=220,
        )
        parsed = self._parse_json_object(answer)
        ranked_ids = parsed.get("ranked_ids") if isinstance(parsed.get("ranked_ids"), list) else []
        clean_ids = [str(x).strip() for x in ranked_ids if str(x).strip()]
        if not clean_ids:
            return [], "llm_rerank_empty_ranked_ids"

        by_id = {str(row.get("chunk_id") or ""): row for row in rows}
        used = set()
        reranked_rows: List[Dict[str, Any]] = []
        for cid in clean_ids:
            row = by_id.get(cid)
            if row is None or cid in used:
                continue
            used.add(cid)
            reranked_rows.append(dict(row))

        for row in rows:
            cid2 = str(row.get("chunk_id") or "")
            if cid2 and cid2 not in used:
                reranked_rows.append(dict(row))

        reranked_rows = [row for row in reranked_rows if float(row.get("score") or 0.0) >= similarity_threshold]
        return reranked_rows[:top_k_after], ""

    def retrieve(
        self,
        *,
        query: str,
        strategy: str,
        top_k: int,
        rewrite_enabled: bool = False,
        rerank_mode: str = "none",
        top_k_before: int = 0,
        similarity_threshold: float = 0.0,
        top_k_after: int = 0,
    ) -> List[Dict[str, Any]]:
        clean_query = str(query or "").strip()
        if not clean_query:
            raise RagError("Empty query for RAG retrieval.")

        mode = str(rerank_mode or "none").strip().lower()
        if mode not in {"none", "threshold", "heuristic", "llm"}:
            mode = "none"

        k_before = max(1, int(top_k_before or top_k or 4))
        k_after = max(1, int(top_k_after or top_k or 4))
        threshold = max(0.0, min(1.0, float(similarity_threshold or 0.0)))

        effective_query = clean_query
        rewrite_applied = False
        rewrite_error = ""
        if bool(rewrite_enabled):
            try:
                rewritten, rewrite_err = self._rewrite_query(clean_query)
                rewrite_error = rewrite_err
                if rewritten and rewritten.strip() and rewritten.strip() != clean_query:
                    effective_query = rewritten.strip()
                    rewrite_applied = True
            except Exception as e:
                rewrite_error = str(e)

        rows = self._load_index(strategy)
        qvec = self._embed_query(effective_query)
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for row in rows:
            score = self._cosine(qvec, row.get("vector") or [])
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)

        first_stage = self._to_output_rows(scored[:k_before])
        if not first_stage:
            raise RagError("No relevant chunks found for this query.")

        rerank_error = ""
        if mode == "threshold":
            final_rows = self._apply_threshold_filter(
                rows=first_stage,
                similarity_threshold=threshold,
                top_k_after=k_after,
            )
        elif mode == "heuristic":
            final_rows = self._apply_heuristic_rerank(
                rows=first_stage,
                query=effective_query,
                similarity_threshold=threshold,
                top_k_after=k_after,
            )
        elif mode == "llm":
            try:
                final_rows, rerank_error = self._apply_llm_rerank(
                    rows=first_stage,
                    query=effective_query,
                    similarity_threshold=threshold,
                    top_k_after=k_after,
                )
            except Exception as e:
                rerank_error = str(e)
                final_rows = first_stage[:k_after]
        else:
            final_rows = first_stage[:k_after]

        if not final_rows:
            final_rows = first_stage[: min(len(first_stage), k_after)]

        self.last_run_meta = {
            "original_query": clean_query,
            "effective_query": effective_query,
            "rewrite_enabled": bool(rewrite_enabled),
            "rewrite_applied": bool(rewrite_applied),
            "rewrite_error": rewrite_error,
            "rerank_mode": mode,
            "top_k_before": k_before,
            "similarity_threshold": threshold,
            "top_k_after": k_after,
            "initial_candidates_count": len(first_stage),
            "final_candidates_count": len(final_rows),
            "rerank_error": rerank_error,
        }
        return final_rows

    @staticmethod
    def build_rag_context(chunks: List[Dict[str, Any]]) -> str:
        parts: List[str] = [
            "RAG_CONTEXT: Use the relevant code fragments below as the primary source of facts.",
            "If context is insufficient, explicitly mention this limitation.",
        ]
        for idx, item in enumerate(chunks, start=1):
            file_path = str(item.get("file") or "")
            section = str(item.get("section") or "")
            start_ln = int(item.get("start_line") or 0)
            end_ln = int(item.get("end_line") or 0)
            score = float(item.get("score") or 0.0)
            combined_score = item.get("combined_score")
            if combined_score is None:
                header = (
                    f"[RAG #{idx}] file={file_path} | section={section} | "
                    f"lines={start_ln}-{end_ln} | score={score:.4f}"
                )
            else:
                header = (
                    f"[RAG #{idx}] file={file_path} | section={section} | "
                    f"lines={start_ln}-{end_ln} | score={score:.4f} | combined={float(combined_score):.4f}"
                )
            parts.append(header)
            parts.append(str(item.get("content") or ""))
        return "\n\n".join(parts).strip()

    @staticmethod
    def build_sources_block(chunks: List[Dict[str, Any]]) -> str:
        lines: List[str] = ["Источники:"]
        for idx, item in enumerate(chunks, start=1):
            chunk_id = str(item.get("chunk_id") or "")
            source = str(item.get("source") or "repo")
            file_path = str(item.get("file") or "")
            title = str(item.get("title") or file_path)
            section = str(item.get("section") or "")
            strategy = str(item.get("strategy") or "")
            start_ln = int(item.get("start_line") or 0)
            end_ln = int(item.get("end_line") or 0)
            score = float(item.get("score") or 0.0)
            lines.append(
                f"{idx}) {source} | chunk_id={chunk_id} | {file_path} | {title} | {section} | "
                f"{strategy} | lines {start_ln}-{end_ln} | score={score:.4f}"
            )
        return "\n".join(lines).strip()

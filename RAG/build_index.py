import argparse
import ast
import hashlib
import json
import os
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests


DEFAULT_INCLUDE_EXTS = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
}

DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".idea",
    ".vscode",
    "RAG",
}


@dataclass
class RunConfig:
    root: Path
    out_dir: Path
    model: str
    ollama_url: str
    fixed_chunk_size: int
    fixed_overlap: int
    min_chunk_chars: int
    batch_size: int
    include_exts: Sequence[str]
    exclude_dirs: Sequence[str]
    max_file_chars: int
    embedding_max_chars: int
    timeout_sec: int


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def estimate_tokens(text: str) -> int:
    clean = (text or "").strip()
    if not clean:
        return 0
    return max(1, len(clean) // 4)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def is_binary_file(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            sample = f.read(8192)
        return b"\x00" in sample
    except Exception:
        return True


def read_text(path: Path, max_chars: int) -> Optional[str]:
    if is_binary_file(path):
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if not text.strip():
        return None
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


def collect_files(root: Path, include_exts: Sequence[str], exclude_dirs: Sequence[str]) -> List[Path]:
    include_set = {ext.lower().strip() for ext in include_exts}
    exclude_set = {name.strip() for name in exclude_dirs}
    results: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_set]
        for filename in filenames:
            p = Path(dirpath) / filename
            ext = p.suffix.lower()
            if ext in include_set:
                results.append(p)
    results.sort()
    return results


def line_starts(text: str) -> List[int]:
    starts = [0]
    for idx, ch in enumerate(text):
        if ch == "\n":
            starts.append(idx + 1)
    return starts


def char_pos_to_line(starts: Sequence[int], pos: int) -> int:
    left, right = 0, len(starts) - 1
    while left <= right:
        mid = (left + right) // 2
        if starts[mid] <= pos:
            left = mid + 1
        else:
            right = mid - 1
    return max(1, right + 1)


def detect_language(path: Path) -> str:
    ext = path.suffix.lower()
    mapping = {
        ".py": "python",
        ".md": "markdown",
        ".txt": "text",
        ".json": "json",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".toml": "toml",
        ".ini": "ini",
    }
    return mapping.get(ext, "text")


def chunk_fixed(text: str, size: int, overlap: int, min_chunk_chars: int) -> List[Tuple[int, int, str, str]]:
    chunks: List[Tuple[int, int, str, str]] = []
    if not text:
        return chunks
    step = max(1, size - overlap)
    start = 0
    n = len(text)
    idx = 0
    while start < n:
        end = min(n, start + size)
        chunk = text[start:end].strip()
        if len(chunk) >= min_chunk_chars or (start == 0 and chunk):
            section = f"fixed_block_{idx}"
            chunks.append((start, end, section, chunk))
            idx += 1
        if end >= n:
            break
        start += step
    return chunks


def chunk_structural_python(text: str, fallback_size: int, min_chunk_chars: int) -> List[Tuple[int, int, str, str, int, int]]:
    lines = text.splitlines()
    n_lines = len(lines)
    if n_lines == 0:
        return []

    chunks: List[Tuple[int, int, str, str, int, int]] = []
    try:
        tree = ast.parse(text)
    except Exception:
        return chunk_structural_text(text, fallback_size, min_chunk_chars)

    entities: List[Tuple[int, int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            start = int(getattr(node, "lineno", 1))
            end = int(getattr(node, "end_lineno", start))
            entities.append((start, end, f"class:{node.name}"))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = int(getattr(node, "lineno", 1))
            end = int(getattr(node, "end_lineno", start))
            kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            entities.append((start, end, f"{kind}:{node.name}"))

    entities.sort(key=lambda item: item[0])
    used_ranges: List[Tuple[int, int]] = []
    for start, end, section in entities:
        start = max(1, min(start, n_lines))
        end = max(start, min(end, n_lines))
        body = "\n".join(lines[start - 1:end]).strip()
        if len(body) < min_chunk_chars:
            continue
        chunks.append((0, 0, section, body, start, end))
        used_ranges.append((start, end))

    prev_end = 1
    module_idx = 0
    for start, end in used_ranges:
        if start > prev_end:
            block = "\n".join(lines[prev_end - 1:start - 1]).strip()
            if len(block) >= min_chunk_chars:
                chunks.append((0, 0, f"module_block_{module_idx}", block, prev_end, start - 1))
                module_idx += 1
        prev_end = max(prev_end, end + 1)
    if prev_end <= n_lines:
        block = "\n".join(lines[prev_end - 1:]).strip()
        if len(block) >= min_chunk_chars:
            chunks.append((0, 0, f"module_block_{module_idx}", block, prev_end, n_lines))

    return chunks


def chunk_structural_text(text: str, fallback_size: int, min_chunk_chars: int) -> List[Tuple[int, int, str, str, int, int]]:
    lines = text.splitlines()
    n = len(lines)
    chunks: List[Tuple[int, int, str, str, int, int]] = []
    if n == 0:
        return chunks

    start_line = 1
    buff: List[str] = []
    section = "text_block_0"
    sec_idx = 0

    def flush(end_line: int) -> None:
        nonlocal buff, sec_idx, section
        text_block = "\n".join(buff).strip()
        if len(text_block) >= min_chunk_chars:
            chunks.append((0, 0, section, text_block, start_line, end_line))
        buff = []
        sec_idx += 1
        section = f"text_block_{sec_idx}"

    for i, line in enumerate(lines, start=1):
        is_header = line.strip().startswith("#")
        if is_header and buff:
            flush(i - 1)
            start_line = i
            buff = [line]
            section = f"header:{line.strip()[:60]}"
            continue
        buff.append(line)
        current = "\n".join(buff)
        if len(current) >= fallback_size and i > start_line:
            flush(i)
            start_line = i + 1
    if buff and start_line <= n:
        flush(n)
    return chunks


def build_chunks_for_file(
    *,
    path: Path,
    rel_path: str,
    text: str,
    strategy: str,
    cfg: RunConfig,
    global_index_start: int,
) -> List[Dict]:
    lang = detect_language(path)
    starts = line_starts(text)
    created = now_iso()
    title = path.name
    out: List[Dict] = []

    if strategy == "fixed":
        pieces = chunk_fixed(text, cfg.fixed_chunk_size, cfg.fixed_overlap, cfg.min_chunk_chars)
        for local_idx, (char_start, char_end, section, content) in enumerate(pieces):
            start_ln = char_pos_to_line(starts, char_start)
            end_ln = char_pos_to_line(starts, max(char_start, char_end - 1))
            chunk_idx = global_index_start + local_idx
            out.append(
                {
                    "chunk_id": f"{strategy}:{chunk_idx:06d}",
                    "source": "repo",
                    "title": title,
                    "file": rel_path,
                    "path": rel_path,
                    "section": section,
                    "strategy": strategy,
                    "language": lang,
                    "start_line": start_ln,
                    "end_line": end_ln,
                    "char_count": len(content),
                    "token_est": estimate_tokens(content),
                    "created_at": created,
                    "content_hash": sha256_text(content),
                    "content": content,
                }
            )
        return out

    if lang == "python":
        pieces2 = chunk_structural_python(text, cfg.fixed_chunk_size, cfg.min_chunk_chars)
    else:
        pieces2 = chunk_structural_text(text, cfg.fixed_chunk_size, cfg.min_chunk_chars)

    for local_idx, (_, _, section, content, start_ln, end_ln) in enumerate(pieces2):
        chunk_idx = global_index_start + local_idx
        out.append(
            {
                "chunk_id": f"{strategy}:{chunk_idx:06d}",
                "source": "repo",
                "title": title,
                "file": rel_path,
                "path": rel_path,
                "section": section,
                "strategy": strategy,
                "language": lang,
                "start_line": start_ln,
                "end_line": end_ln,
                "char_count": len(content),
                "token_est": estimate_tokens(content),
                "created_at": created,
                "content_hash": sha256_text(content),
                "content": content,
            }
        )
    return out


def write_jsonl(path: Path, rows: Iterable[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, timeout_sec: int = 60):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_sec = timeout_sec
        self.session = requests.Session()
        self._batch_supported: Optional[bool] = None

    def _post(self, endpoint: str, payload: Dict) -> Dict:
        url = f"{self.base_url}{endpoint}"
        resp = self.session.post(url, json=payload, timeout=self.timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected response from Ollama endpoint {endpoint}")
        return data

    def _get(self, endpoint: str) -> Dict:
        url = f"{self.base_url}{endpoint}"
        resp = self.session.get(url, timeout=self.timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected response from Ollama endpoint {endpoint}")
        return data

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        if self._batch_supported is not False:
            try:
                data = self._post("/api/embed", {"model": self.model, "input": texts})
                embeds = data.get("embeddings")
                if isinstance(embeds, list) and embeds and isinstance(embeds[0], list):
                    self._batch_supported = True
                    return embeds
            except Exception:
                self._batch_supported = False

        vectors: List[List[float]] = []
        for t in texts:
            vectors.append(self._embed_single(t))
        return vectors

    def _embed_single(self, text: str) -> List[float]:
        data = self._post("/api/embeddings", {"model": self.model, "prompt": text})
        emb = data.get("embedding")
        if not isinstance(emb, list):
            raise RuntimeError("Ollama /api/embeddings returned no embedding vector")
        return emb

    def embed_one_with_fallback(self, text: str, max_chars: int) -> List[float]:
        clean = str(text or "").strip()
        if not clean:
            return self._embed_single(" ")

        current = clean[:max_chars] if max_chars > 0 else clean
        while True:
            try:
                return self._embed_single(current)
            except Exception as e:
                msg = str(e).lower()
                too_long = ("input length exceeds the context length" in msg) or ("context length" in msg)
                if (not too_long) or len(current) <= 400:
                    raise
                current = current[: max(400, int(len(current) * 0.7))]

    def healthcheck(self) -> None:
        try:
            self._get("/api/tags")
        except Exception as e:
            raise RuntimeError(
                f"Ollama is not reachable at {self.base_url}. "
                f"Start Ollama and pull model '{self.model}'. Details: {e}"
            ) from e


def median_safe(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return float(statistics.median(values))


def p95(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * 0.95))
    return float(ordered[idx])


def compute_chunk_metrics(chunks: Sequence[Dict], total_files: int, elapsed_chunk_sec: float, elapsed_embed_sec: float, embed_error_count: int) -> Dict:
    chars = [int(item.get("char_count") or 0) for item in chunks]
    tokens = [int(item.get("token_est") or 0) for item in chunks]
    line_spans = [max(0, int(item.get("end_line") or 0) - int(item.get("start_line") or 0) + 1) for item in chunks]
    hashes = [str(item.get("content_hash") or "") for item in chunks if str(item.get("content_hash") or "")]
    unique_hashes = set(hashes)
    duplicates = max(0, len(hashes) - len(unique_hashes))
    dedup_ratio = (duplicates / len(hashes)) if hashes else 0.0
    avg_embed_sec = (elapsed_embed_sec / len(chunks)) if chunks else 0.0

    return {
        "files_total": int(total_files),
        "chunks_total": int(len(chunks)),
        "char_avg": float(sum(chars) / len(chars)) if chars else 0.0,
        "char_median": median_safe(chars),
        "char_p95": p95(chars),
        "token_avg": float(sum(tokens) / len(tokens)) if tokens else 0.0,
        "token_median": median_safe(tokens),
        "token_p95": p95(tokens),
        "line_span_avg": float(sum(line_spans) / len(line_spans)) if line_spans else 0.0,
        "chunking_time_sec": round(elapsed_chunk_sec, 4),
        "embedding_time_sec": round(elapsed_embed_sec, 4),
        "embedding_avg_per_chunk_sec": round(avg_embed_sec, 6),
        "embedding_error_count": int(embed_error_count),
        "duplicate_chunks": int(duplicates),
        "duplicate_ratio": round(dedup_ratio, 4),
    }


def compute_coverage(chunks: Sequence[Dict]) -> Dict[str, Dict[str, int]]:
    coverage: Dict[str, set] = {}
    for item in chunks:
        file_path = str(item.get("file") or "")
        if not file_path:
            continue
        start_ln = int(item.get("start_line") or 0)
        end_ln = int(item.get("end_line") or 0)
        if start_ln <= 0 or end_ln <= 0 or end_ln < start_ln:
            continue
        coverage.setdefault(file_path, set()).update(range(start_ln, end_ln + 1))
    summary: Dict[str, Dict[str, int]] = {}
    for file_path, lines in coverage.items():
        summary[file_path] = {"covered_lines": len(lines)}
    return summary


def write_report(path: Path, fixed_metrics: Dict, structural_metrics: Dict, fixed_files: Dict, structural_files: Dict, files_total: int) -> None:
    def fmt_value(value: object) -> str:
        if isinstance(value, float):
            return f"{value:.4f}"
        return str(value)

    metric_descriptions_ru = {
        "chunks_total": "Общее количество чанков после разбиения.",
        "char_avg": "Средняя длина чанка в символах.",
        "char_median": "Медианная длина чанка в символах.",
        "char_p95": "95-й перцентиль длины чанка в символах.",
        "token_avg": "Средняя оценка числа токенов на чанк.",
        "token_median": "Медианная оценка числа токенов на чанк.",
        "token_p95": "95-й перцентиль оценки токенов на чанк.",
        "line_span_avg": "Среднее количество строк кода/текста в чанке.",
        "chunking_time_sec": "Время выполнения этапа chunking в секундах.",
        "embedding_time_sec": "Время генерации эмбеддингов в секундах.",
        "embedding_avg_per_chunk_sec": "Среднее время эмбеддинга одного чанка в секундах.",
        "embedding_error_count": "Количество чанков с ошибкой эмбеддинга.",
        "duplicate_chunks": "Количество чанков с дублирующимся content_hash.",
        "duplicate_ratio": "Доля дублей среди всех чанков.",
    }

    keys = list(metric_descriptions_ru.keys())
    rows: List[Tuple[str, str, str, str]] = []
    for key in keys:
        rows.append(
            (
                key,
                fmt_value(fixed_metrics.get(key, 0)),
                fmt_value(structural_metrics.get(key, 0)),
                metric_descriptions_ru.get(key, ""),
            )
        )

    w_metric = max(len("Metric"), max((len(r[0]) for r in rows), default=0))
    w_fixed = max(len("Fixed"), max((len(r[1]) for r in rows), default=0))
    w_struct = max(len("Structural"), max((len(r[2]) for r in rows), default=0))
    w_desc = max(len("Описание"), max((len(r[3]) for r in rows), default=0))

    lines = [
        "# Chunking Comparison Report",
        "",
        f"- Generated at: {now_iso()}",
        f"- Corpus files: {files_total}",
        "",
        "## Metrics",
        "",
        f"| {'Metric':<{w_metric}} | {'Fixed':<{w_fixed}} | {'Structural':<{w_struct}} | {'Описание':<{w_desc}} |",
        f"|{'-' * (w_metric + 2)}|{'-' * (w_fixed + 2)}|{'-' * (w_struct + 2)}|{'-' * (w_desc + 2)}|",
    ]
    for metric, fixed_val, structural_val, desc in rows:
        lines.append(
            f"| {metric:<{w_metric}} | {fixed_val:<{w_fixed}} | {structural_val:<{w_struct}} | {desc:<{w_desc}} |"
        )

    lines.extend(
        [
            "",
            "## Coverage",
            "",
            f"- Fixed files with coverage: {len(fixed_files)}",
            f"- Structural files with coverage: {len(structural_files)}",
            "",
            "## Notes",
            "",
            "- `fixed`: equal char windows with overlap.",
            "- `structural`: Python AST entities (class/function) + text blocks fallback.",
            "- Embeddings model: `nomic-embed-text` via local Ollama.",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def save_run_log(path: Path, log_lines: Sequence[str]) -> None:
    path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")


def bytesize(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.2f} MB"


def run_pipeline(cfg: RunConfig) -> None:
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    logs: List[str] = []
    logs.append(f"[{now_iso()}] Start indexing; root={cfg.root}")

    files = collect_files(cfg.root, cfg.include_exts, cfg.exclude_dirs)
    logs.append(f"[{now_iso()}] Discovered files: {len(files)}")
    print(f"Discovered files: {len(files)}")

    corpus: List[Tuple[Path, str, str]] = []
    skipped = 0
    for p in files:
        rel = str(p.relative_to(cfg.root)).replace("\\", "/")
        text = read_text(p, cfg.max_file_chars)
        if not text:
            skipped += 1
            continue
        corpus.append((p, rel, text))
    logs.append(f"[{now_iso()}] Readable text files: {len(corpus)}; skipped={skipped}")
    print(f"Readable files: {len(corpus)} (skipped: {skipped})")

    embedder = OllamaEmbedder(cfg.ollama_url, cfg.model, timeout_sec=cfg.timeout_sec)
    embedder.healthcheck()

    artifacts: Dict[str, Path] = {
        "chunks_fixed": cfg.out_dir / "chunks_fixed.jsonl",
        "chunks_structural": cfg.out_dir / "chunks_structural.jsonl",
        "embeddings_fixed": cfg.out_dir / "embeddings_fixed.jsonl",
        "embeddings_structural": cfg.out_dir / "embeddings_structural.jsonl",
        "report": cfg.out_dir / "report_chunking_comparison.md",
        "run_log": cfg.out_dir / "run_log.txt",
        "metrics": cfg.out_dir / "metrics_summary.json",
    }

    all_metrics: Dict[str, Dict] = {}
    coverage_all: Dict[str, Dict] = {}

    for strategy in ("fixed", "structural"):
        t0 = time.perf_counter()
        chunks: List[Dict] = []
        running_idx = 0
        for file_path, rel_path, text in corpus:
            built = build_chunks_for_file(
                path=file_path,
                rel_path=rel_path,
                text=text,
                strategy=strategy,
                cfg=cfg,
                global_index_start=running_idx,
            )
            chunks.extend(built)
            running_idx += len(built)
        elapsed_chunk = time.perf_counter() - t0
        print(f"[{strategy}] chunks: {len(chunks)}")
        logs.append(f"[{now_iso()}] {strategy}: chunking done; chunks={len(chunks)} time={elapsed_chunk:.3f}s")

        write_jsonl(artifacts[f"chunks_{strategy}"], chunks)

        t1 = time.perf_counter()
        embed_rows: List[Dict] = []
        error_count = 0
        for i in range(0, len(chunks), cfg.batch_size):
            batch = chunks[i:i + cfg.batch_size]
            texts = [str(item.get("content") or "") for item in batch]
            try:
                vectors = embedder.embed_batch(texts)
                if len(vectors) != len(batch):
                    raise RuntimeError("Embedding response length mismatch")
            except Exception as e:
                logs.append(f"[{now_iso()}] {strategy}: embedding batch failed at {i}; fallback to single mode; error={e}")
                vectors = []
                for local_idx, text in enumerate(texts):
                    try:
                        vector = embedder.embed_one_with_fallback(text, cfg.embedding_max_chars)
                        vectors.append(vector)
                    except Exception as item_error:
                        error_count += 1
                        logs.append(
                            f"[{now_iso()}] {strategy}: embedding item failed at {i + local_idx}; "
                            f"chunk_id={batch[local_idx].get('chunk_id')}; error={item_error}"
                        )
                        vectors.append([])

            for item, vector in zip(batch, vectors):
                if not vector:
                    continue
                embed_rows.append(
                    {
                        "chunk_id": item["chunk_id"],
                        "source": item["source"],
                        "title": item["title"],
                        "file": item["file"],
                        "section": item["section"],
                        "strategy": item["strategy"],
                        "content_hash": item["content_hash"],
                        "dim": len(vector),
                        "embedding": vector,
                    }
                )
        elapsed_embed = time.perf_counter() - t1
        write_jsonl(artifacts[f"embeddings_{strategy}"], embed_rows)
        logs.append(
            f"[{now_iso()}] {strategy}: embedding done; vectors={len(embed_rows)} "
            f"errors={error_count} time={elapsed_embed:.3f}s"
        )

        metrics = compute_chunk_metrics(chunks, len(corpus), elapsed_chunk, elapsed_embed, error_count)
        metrics["embeddings_total"] = len(embed_rows)
        metrics["artifact_chunks_size_bytes"] = bytesize(artifacts[f"chunks_{strategy}"])
        metrics["artifact_embeddings_size_bytes"] = bytesize(artifacts[f"embeddings_{strategy}"])
        all_metrics[strategy] = metrics
        coverage_all[strategy] = compute_coverage(chunks)

    write_report(
        artifacts["report"],
        all_metrics["fixed"],
        all_metrics["structural"],
        coverage_all["fixed"],
        coverage_all["structural"],
        len(corpus),
    )

    summary = {
        "generated_at": now_iso(),
        "root": str(cfg.root),
        "model": cfg.model,
        "ollama_url": cfg.ollama_url,
        "files_total": len(corpus),
        "metrics": all_metrics,
        "artifacts": {k: str(v) for k, v in artifacts.items()},
    }
    artifacts["metrics"].write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    save_run_log(artifacts["run_log"], logs)

    print("Artifacts:")
    for key, p in artifacts.items():
        if p.exists():
            print(f"- {key}: {p} ({format_bytes(bytesize(p))})")
        else:
            print(f"- {key}: {p} (missing)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build local RAG index with two chunking strategies.")
    parser.add_argument("--root", default=".", help="Project root for corpus scan.")
    parser.add_argument("--out-dir", default="RAG", help="Output directory for generated artifacts.")
    parser.add_argument("--model", default="nomic-embed-text", help="Ollama embeddings model.")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="Ollama base URL.")
    parser.add_argument("--fixed-chunk-size", type=int, default=1400, help="Fixed chunk size in chars.")
    parser.add_argument("--fixed-overlap", type=int, default=200, help="Overlap size in chars.")
    parser.add_argument("--min-chunk-chars", type=int, default=120, help="Skip very small chunks.")
    parser.add_argument("--batch-size", type=int, default=8, help="Embedding batch size.")
    parser.add_argument("--max-file-chars", type=int, default=300000, help="Read limit per file.")
    parser.add_argument("--embedding-max-chars", type=int, default=5000, help="Initial max chars for fallback single embedding.")
    parser.add_argument("--timeout-sec", type=int, default=90, help="HTTP timeout for Ollama.")
    parser.add_argument(
        "--include-exts",
        default=",".join(sorted(DEFAULT_INCLUDE_EXTS)),
        help="Comma-separated file extensions to include.",
    )
    parser.add_argument(
        "--exclude-dirs",
        default=",".join(sorted(DEFAULT_EXCLUDE_DIRS)),
        help="Comma-separated directory names to exclude.",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> RunConfig:
    include_exts = [item.strip().lower() for item in str(args.include_exts).split(",") if item.strip()]
    exclude_dirs = [item.strip() for item in str(args.exclude_dirs).split(",") if item.strip()]
    return RunConfig(
        root=Path(args.root).resolve(),
        out_dir=Path(args.out_dir).resolve(),
        model=str(args.model).strip(),
        ollama_url=str(args.ollama_url).strip(),
        fixed_chunk_size=max(100, int(args.fixed_chunk_size)),
        fixed_overlap=max(0, int(args.fixed_overlap)),
        min_chunk_chars=max(1, int(args.min_chunk_chars)),
        batch_size=max(1, int(args.batch_size)),
        include_exts=include_exts,
        exclude_dirs=exclude_dirs,
        max_file_chars=max(1000, int(args.max_file_chars)),
        embedding_max_chars=max(400, int(args.embedding_max_chars)),
        timeout_sec=max(5, int(args.timeout_sec)),
    )


if __name__ == "__main__":
    cfg = build_config(parse_args())
    run_pipeline(cfg)












# Local RAG Pipeline

В проекте реализован локальный RAG с двухэтапным retrieval:

1. first-stage semantic search по cosine similarity
2. second-stage rerank/filter (переключаемые режимы)

Дополнительно доступен query rewrite перед retrieval.

## Индексация

Скрипт строит локальный индекс кода проекта с двумя стратегиями chunking и эмбеддингами через Ollama.

```powershell
.\.venv\Scripts\python RAG\build_index.py
```

## Предусловия

1. Запущен Ollama (`http://127.0.0.1:11434`).
2. Загружена embedding-модель:

```powershell
ollama pull nomic-embed-text
```

## Артефакты индекса

- `RAG/chunks_fixed.jsonl`
- `RAG/chunks_structural.jsonl`
- `RAG/embeddings_fixed.jsonl`
- `RAG/embeddings_structural.jsonl`
- `RAG/report_chunking_comparison.md`
- `RAG/metrics_summary.json`
- `RAG/run_log.txt`

## Режимы second-stage

В UI доступен переключатель `Rerank`:

- `none`: baseline, только first-stage top-K (без дополнительной фильтрации)
- `threshold`: фильтр по `similarity_threshold`
- `heuristic`: комбинированный скор из cosine + lexical overlap
- `llm`: переранжирование кандидатов через LLM

## Query Rewrite

Тумблер `Rewrite` включает переписывание пользовательского запроса для retrieval.

- `off`: используется исходный запрос
- `on`: используется rewritten query

Если LLM для rewrite/rerank недоступна, retrieval не падает: применяется fallback на baseline-кандидаты.

## Параметры в UI

Во вкладке чата (блок параметров):

- `RAG` (on/off)
- `RAG база` (`fixed` / `structural`)
- `Rewrite` (on/off)
- `Rerank` (`none` / `threshold` / `heuristic` / `llm`)
- `TopK before`
- `Threshold`
- `TopK after`

## Параметры в .env

- `RAG_TOP_K_BEFORE_DEFAULT=10`
- `RAG_SIMILARITY_THRESHOLD_DEFAULT=0.5`
- `RAG_TOP_K_AFTER_DEFAULT=5`
- `RAG_REWRITE_DEFAULT=false`
- `RAG_RERANK_MODE_DEFAULT=none`
- `RAG_HEURISTIC_W_SEMANTIC=0.8`
- `RAG_HEURISTIC_W_LEXICAL=0.2`
- `RAG_LLM_MODEL=gpt-4o-mini`
- `RAG_LLM_TIMEOUT_SEC=40`
- `RAG_LLM_RERANK_MAX_CHUNKS=8`
- `RAG_LLM_RERANK_MAX_CHARS=1200`
- `RAG_LLM_API_KEY_ENV=PROXYAPI_KEY`

Приоритет: значения из UI выше значений из `.env`.

## Метрики и диагностика

В `message_stats` и строке метрик UI выводятся:

- активность RAG и стратегия базы
- флаги rewrite (enabled/applied)
- режим rerank
- `top_k_before`, `top_k_after`, `similarity_threshold`
- число кандидатов до/после second-stage
- ошибки rewrite/rerank (если были)

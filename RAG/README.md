# Local RAG Index Build

Скрипт строит локальный индекс кода проекта с двумя стратегиями chunking и эмбеддингами через Ollama.

## Запуск

```powershell
.\.venv\Scripts\python RAG\build_index.py
```

## Предусловия

1. Запущен Ollama (`http://127.0.0.1:11434`).
2. Загружена модель:

```powershell
ollama pull nomic-embed-text
```

## Артефакты

- `RAG/chunks_fixed.jsonl`
- `RAG/chunks_structural.jsonl`
- `RAG/embeddings_fixed.jsonl`
- `RAG/embeddings_structural.jsonl`
- `RAG/report_chunking_comparison.md`
- `RAG/metrics_summary.json`
- `RAG/run_log.txt`


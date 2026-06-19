# Pine Script v6 RAG Dataset Builder

This repository contains a local pipeline for building retrieval chunks from the canonical TradingView Pine Script v6 documentation:

- Pine Script User Manual: <https://www.tradingview.com/pine-script-docs/>
- Pine Script v6 Reference Manual: <https://www.tradingview.com/pine-script-reference/v6/>

The builder writes one JSONL record per chunk and keeps intermediate artifacts on disk so large crawls can be inspected or resumed manually.

## Install

```bash
python -m pip install -r requirements.txt
```

## Build

```bash
python scripts/build_pine_v6_rag.py --outdir output
```

For a small smoke test, cap the crawl and remove the delay:

```bash
python scripts/build_pine_v6_rag.py --outdir /tmp/pine-smoke --max-pages 2 --delay 0
```

## Outputs

The script creates:

- `raw_pages/*.html` with fetched source pages.
- `normalized_pages/*.json` with parsed page metadata, headings, code blocks, and text.
- `chunks/*.jsonl` with per-page retrieval chunks.
- `pine_v6_rag_dataset.jsonl` with the combined chunk dataset.
- `pine_v6_rag_manifest.csv` with a tabular manifest.
- `failed_fetches.json` with URLs skipped because of HTTP or network errors.
- `build_summary.json` with page, chunk, and failed-fetch counts.

Each chunk includes `doc_id`, `source`, `url`, `title`, `section_path`, `version`, `content_type`, `chunk_index`, `text`, `code_blocks`, `headings`, `tags`, `char_count`, and `embedding_text`.

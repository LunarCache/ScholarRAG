# Local evaluation datasets

This directory stores local JSON/JSONL evaluation datasets, generated candidates, parser annotations and linking reports. Generated files are intentionally ignored by Git because they may contain paper-derived text, provider trace IDs or reviewer data.

Supported minimal JSONL shape:

```json
{"question":"What is the main contribution?","document_ids":["doc_xxx"],"reference":"Optional grounded answer","tags":["method"]}
```

Run an evaluation with:

```bash
uv run scholarrag evaluate evals/your-dataset.jsonl
```

Export active contexts for the external EasyDataset workbench with:

```bash
uv run scholarrag export-easydataset-contexts evals/easydataset-contexts.jsonl
```

Synthetic or imported candidates must not be treated as reviewed or Gold without the human gates documented in [`../docs/HUMAN_ACCEPTANCE.md`](../docs/HUMAN_ACCEPTANCE.md).

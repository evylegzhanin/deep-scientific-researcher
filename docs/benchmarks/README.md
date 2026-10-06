# Доказательства замеров

Основные числа и ограничения — в [load report](../load-report.md). Здесь сохранены только подтверждения результата 04.10.2026; JSON не редактировались для сдачи.

| Что проверить | Файлы |
|---|---|
| RPS, latency, очередь, inference | [c1](2026-10-04/revision-2/research-load-baseline.json), [c2](2026-10-04/revision-2/research-load-c2.json), [inference](2026-10-04/revision-2/inference-context.json) |
| Quality и ACL | [quality 11/11](2026-10-04/revision-2/quality-controlled-corpus.json), [corpus scope](2026-10-04/revision-2/corpus-scope.json), [security 11/11](2026-10-04/revision-2/security-checks.json), [graph 4/4](2026-10-04/revision-1/graph-checks.json) |
| Наблюдаемость и ресурсы | [Jaeger trace](2026-10-04/revision-2/jaeger-controlled-corpus.json), [sampled resource peaks](2026-10-04/revision-2/resource-summary.json) |
| Offline и версии | [startup](2026-10-04/revision-2/offline-startup.json), [egress](2026-10-04/revision-2/runtime-isolation-after-reboot.json), [Docling](2026-10-04/revision-2/docling-offline-first-use.json), [packages](2026-10-04/revision-1/backend-versions.json), [runtime](2026-10-04/revision-2/runtime-provenance.json), [snapshot](2026-10-04/revision-2/runtime-status.json) |
| Reranker и consumer profile | [retrieval validation](2026-10-04/revision-1/retrieval-validation.json), [AWQ metadata](2026-10-04/revision-1/awq-checkpoint.json) |

Нагрузка выполнена на корпусе, включавшем разрешённые security-fixtures; quality 11/11 — отдельный фиксированный scope. GraphRAG ACL проверен в revision-1, повторная нагрузка/security — в revision-2. Параметры AWQ — только metadata, запуск не проверен.

SHA-256 в runtime provenance идентифицируют код измеренного контейнера. Локальный `worker.py` отличается дополнительным traceback в обработчике ошибок; [точная разница](2026-10-04/revision-2/local-source-difference.json). Остальные backend-файлы совпадают с измеренным runtime. JSON фиксируют прежние image IDs, а не Docker tag `submission` и не новый Git commit.

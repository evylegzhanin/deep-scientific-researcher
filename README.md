# Deep Scientific Researcher

MVP закрытой платформы анализа технических документов: PDF/сканы → OCR и чанки → GraphRAG → проверенный отчёт с цитатами. Профиль сдачи использует локальные модели и `APP_MODE=airgap`.

## Материалы для проверки

| Материал | Где смотреть |
|---|---|
| Код и запуск стека | [backend/](backend/), [infra/](infra/), [инструкция запуска](docs/operations.md) |
| ADD: C4, Deployment, Data Flow, Sequence, ER и ADR | [архитектура](docs/architecture.md), [модель данных](docs/sql-data-model.md), [решения](docs/decisions.md), [PDF](docs/ADD.pdf) |
| Демо-видео | [Смотреть на Яндекс Диске](https://disk.yandex.com/i/atwTK3628L3VOw) |
| RPS и latency | [краткий нагрузочный отчёт](docs/load-report.md) |

## Как работает MVP

FastAPI ставит задание в PostgreSQL; worker выполняет LangGraph: план → retrieval → анализ → проверка → повтор или завершение. Состояние сохраняется в checkpoint. Retrieval объединяет FTS, Qdrant и граф технических идентификаторов; Qwen3 Reranker оценивает обученные yes/no logits.

Права домена и ACL чанка проверяются до поиска, при обходе графа и перед выдачей/экспортом. Guardrails маскируют email/телефоны и удаляют неподтверждённые утверждения. SSE показывает этапы; отчёт доступен в Markdown и PDF.

| Что проверить в коде | Основные файлы |
|---|---|
| Stateful agent | [research.py](backend/research.py), [jobs.py](backend/jobs.py) |
| GraphRAG и rerank | [knowledge_graph.py](backend/knowledge_graph.py), [retrieval.py](backend/retrieval.py), [reranking.py](backend/reranking.py) |
| Доступ и guardrails | [access.py](backend/access.py), [guardrails.py](backend/guardrails.py), [report_review.py](backend/report_review.py) |

## Чтение документов

Встроенный читатель PDF/TXT доступен из библиотеки и цитат отчёта; поддерживает страницы и поиск по тексту. Для демонстрационного корпуса сохранены ссылки и контрольные суммы четырёх руководств ОВЕН; PDF загружаются отдельно и не входят в Git. [Источники, импорт и примеры вопросов](docs/document-reader.md).

## Проверенные результаты

На A100 80 GB, Qwen3.8-27B BF16, один worker: **0.010 RPS / 0.60 отчёта в минуту**, p95 **201 с** при concurrency 2 (N=4). Качество на фиксированном корпусе **11/11**, security **11/11**, GraphRAG ACL **4/4**. Проверены offline startup и блокировка внешней сети в API/worker/model. Это короткие синтетические проверки, не production SLA; подробности и raw JSON — в нагрузочном отчёте.

Ограничения: граф извлекает технические идентификаторы, выигрыш качества на размеченном корпусе не измерен; consumer AWQ-профиль задан конфигурацией, но не запускался. Хост benchmark имеет интернет; изолированы runtime-контейнеры.

Для регрессий: `poetry run pytest -q`. Интеграционные тесты требуют отдельную PostgreSQL-БД; порядок — в инструкции запуска. Без отдельной тестовой БД интеграционные проверки пропускаются; число passed не означает проверку удалённого GPU-стенда.

Сборка ADD: `python3 scripts/build_docs.py` (Pandoc, Tectonic, Chrome и frontend-зависимости). В репозитории оставлены исходники, инструкции, тесты и доказательства результатов для проверки решения.

## Структура репозитория

- `backend/`, `frontend/` — сервис и интерфейс; lock-файлы фиксируют зависимости.
- `infra/` — Compose, reverse proxy, наблюдаемость и закрытый контур.
- `scripts/`, `tests/` — подготовка корпуса, воспроизводимые проверки и регрессии.
- `docs/` — архитектура, эксплуатация и отобранные результаты benchmark.
- `demo/documents/` — только источники и manifest четырёх оригинальных руководств ОВЕН.

Начните с [инструкции запуска](docs/operations.md). Сервисы разворачиваются на Linux/GPU. Локальные `.env`, документы, модели, скачанные PDF и runtime-результаты не входят в поставку. Исторические замеры не описывают автоматически новый стенд.

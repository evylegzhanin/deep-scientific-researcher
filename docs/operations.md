# Запуск и проверка

Профиль для сдачи и воспроизведения измеренного стенда — [infra/compose.benchmark.yaml](../infra/compose.benchmark.yaml): API, worker, frontend, PostgreSQL, Qdrant, vLLM, Jaeger. Стенд Linux + Docker Compose + NVIDIA Container Toolkit; A100 80 GB, 16 vCPU, около 64 GB RAM. Все сервисы имеют `unless-stopped`.

## 1. Подготовка вне закрытого контура

Заранее установить зависимости, собрать образы и доставить модели. Приватные `.env`, сертификаты, веса и документы в Git не входят.

```bash
poetry install --only main
poetry run python -m scripts.stage_benchmark_models \
  --root "$HOME/research-models"
docker build -t research-backend:submission -f backend/Dockerfile .
docker build -t research-frontend:submission frontend
```

Доставить pinned vLLM image и образы БД/Jaeger из Compose через `docker save`/`docker load`. Веса `generation`, `embedding`, `reranker`, `docling` должны лежать в `~/research-models`; сверить [manifest](../scripts/model_manifest.py). Скрипт staging загружает embedding и reranker; generation и Docling доставить отдельно до изоляции.

## 2. Конфигурация на сервере

```bash
python3 -m scripts.init_benchmark_host
```

Скрипт создаёт `.env.benchmark` с правами 0600 и localhost TLS-сертификат; существующие секреты не заменяет. В этом файле выставить:

```dotenv
BENCHMARK_BACKEND_IMAGE=research-backend:submission
BENCHMARK_NETWORK_INTERNAL=true
GRAPH_RETRIEVAL_ENABLED=true
OPENBAO_ENABLED=false
```

`OPENBAO_ENABLED=false` относится только к изолированному учебному benchmark. Для целевого секретного хранилища использовать [OpenBao](../infra/openbao.hcl), TLS, read-only token file и профиль `security-prod` в [основном Compose](../infra/compose.yaml). Dev root token не подходит рабочему контуру. Основной Compose также содержит Prometheus/Grafana; [Langfuse](../infra/compose.langfuse.yaml) подключается отдельно.

## 3. Запуск

```bash
docker compose --env-file .env.benchmark \
  -f infra/compose.benchmark.yaml up -d
```

Offline-флаги, пути локальных моделей, API key и TLS заданы Compose. Первый запуск модели занимает несколько минут. `/health/ready` проверяет PostgreSQL, не весь pipeline. Перед пользовательским запросом дождаться успешной генерации модели.

В internal-сети опубликованные loopback-порты Docker на измеренном хосте не обеспечили доступ из host namespace. Benchmark-клиенты запускаются с `--network container:research-benchmark-frontend-1`, URL `https://localhost:443`. Для браузерного demo нужен отдельный разрешённый ingress/туннель; работающий host port не заявляется. Не открывать model/DB в интернет.

## 4. Приёмка

| Проверка | Воспроизводимый инструмент |
|---|---|
| Корпус: TXT, PDF, скан, таблица | [prepare_benchmark_corpus.py](../scripts/prepare_benchmark_corpus.py) |
| Фиксированный corpus scope | [isolate_benchmark_corpus.py](../scripts/isolate_benchmark_corpus.py) |
| Качество и цитаты / ACL / injection | [benchmark_quality.py](../scripts/benchmark_quality.py), [benchmark_security.py](../scripts/benchmark_security.py) |
| GraphRAG и запрещённый мост | [check_graph_retrieval.py](../scripts/check_graph_retrieval.py) |
| Startup и egress | [check_offline_startup.py](../scripts/check_offline_startup.py), [check_runtime_isolation.py](../scripts/check_runtime_isolation.py) |
| RPS/latency и inference | [benchmark_research.py](../scripts/benchmark_research.py), [benchmark_inference.py](../scripts/benchmark_inference.py) |

Параметры каждого инструмента — `python -m scripts.<имя> --help`. Startup-check требует остановленный model и запускает сервисы; его не применять во время исследований. Для проверки качества использовать synthetic accounts/корпус, не секретные рабочие документы. Raw результаты и статусы — в [нагрузочном отчёте](load-report.md).

Локальные тесты: `poetry run pytest -q`. Интеграции запускать только после создания отдельной тестовой БД и экспорта `TEST_DATABASE_URL`:

```bash
INTEGRATION_TESTS=1 DATABASE_URL="$TEST_DATABASE_URL" \
  poetry run pytest -q
```

Не указывать production-базу: тесты создают/удаляют свои fixtures. Последний полный прогон на изолированной БД — 86 passed. Для восстановления нужны согласованные backup PostgreSQL и оригиналов; Qdrant/граф можно переиндексировать. Целевые RPO/RTO не измерены.

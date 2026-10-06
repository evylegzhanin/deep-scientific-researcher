# Архитектура MVP

Система преобразует технические PDF, сканы и таблицы в отчёт на русском с проверяемыми цитатами. Профиль сдачи — локальные модели, `APP_MODE=airgap`, `GRAPH_RETRIEVAL_ENABLED=true`. Измерения и границы проверенного поведения — в [нагрузочном отчёте](load-report.md); выбор технологий — в [ADR](decisions.md).

<a id="c4-context"></a>
## C1. Контекст

```mermaid
flowchart TB
  U["Аналитик"] -->|"Вопросы, документы, отчёты"| P["Deep Scientific Researcher"]
  A["Администратор"] -->|"Пользователи, домены, права"| P
  D["Корпоративные документы"] -->|"Загрузка / импорт"| P
  E["ERP / CRM / DMS<br/>целевые интеграции"] -.->|"Файлы / будущие адаптеры"| P
```

**C1. Граница системы.** В MVP документы загружаются через API/интерфейс; прямые ERP/CRM-коннекторы не реализованы. Интернет-сбор исходного connected-профиля не используется при сдаче.

<a id="c4-containers"></a>
## C2. Контейнеры и разделение плоскостей

```mermaid
flowchart TB
  U["React SPA"] -->|"HTTPS / SSE"| N["Nginx: TLS и proxy"]
  subgraph CP["Control Plane"]
    A["FastAPI: auth, документы, исследования"]
    W["Worker / LangGraph: агентный цикл"]
  end
  subgraph DP["Data Plane"]
    P[("PostgreSQL: очередь, ACL, граф, checkpoint")]
    Q[("Qdrant: векторы чанков")]
    F[("Файловый том: оригиналы")]
    M["vLLM: локальная LLM/VLM"]
  end
  N --> A
  A -->|"Задание"| P
  W -->|"Захват / состояние"| P
  A --> F
  W --> F
  W --> Q
  A --> Q
  W --> M
  A -->|"Анализ изображений"| M
  W -->|"OTel spans"| J["Jaeger"]
```

**C2. Процессы и хранилища.** API и worker общаются через SQL-очередь. Embeddings и rerank работают локально внутри приложения. В измеренном стенде использован Jaeger. Prometheus, Grafana и Langfuse доступны дополнительными профилями [infra/](../infra/).

<a id="c4-worker"></a>
## C3. Компоненты агента

```mermaid
flowchart TB
  J["worker + jobs<br/>Lease / retry"] --> E["ResearchEngine<br/>Planner + agent loop"]
  E --> R["Retriever<br/>FTS / vector / graph / rerank"]
  E --> T["Tools: ingestion / sources<br/>фиксированные функции"]
  E --> M["ModelGateway<br/>локальные модельные вызовы"]
  E --> G["access + guardrails + report_review"]
  E --> S["Memory: ResearchState / SQL checkpoint"]
  R --> G
  J --> S
```

**C3. Ответственность компонентов.** Модель предлагает план и текст; код выполняет поиск, проверяет права и сохраняет результат. Memory — состояние run и PostgreSQL checkpoint, а не отдельный memory-сервис. В airgap внешний сбор пропускается. [Реализация](../backend/research.py).

<a id="c4-code"></a>
## C4. Код ядра агента

```mermaid
classDiagram
  class ResearchEngine {
    +plan(state)
    +retrieve(state)
    +analyze(state)
    +review(state)
    +route(state)
    +run(research_id, claim)
  }
  class ResearchState {
    str question
    int iteration
    list evidence
    bool needs_more_evidence
  }
  class Retriever {
    +search(query, user)
    +rank_evidence(query, candidates)
  }
  class ModelGateway
  ResearchEngine --> ResearchState : StateGraph
  ResearchEngine --> Retriever : разрешённые evidence
  ResearchEngine --> ModelGateway : план и текст
```

**C4. Существующие классы и методы.** Поля/методы показаны выборочно; полный контракт — в [research.py](../backend/research.py), [retrieval.py](../backend/retrieval.py) и [vision.py](../backend/vision.py).

<a id="research-flow"></a>
## B1. Stateful workflow

```mermaid
flowchart TB
  P["plan"] -->|"Уточнение"| U["END: ждём пользователя"]
  P -->|"План готов"| C["collect: локальный корпус"]
  C --> R["retrieve"] --> A["analyze"] --> V["review_report"]
  V -->|"Нужны данные, есть бюджет и прогресс"| C
  V -->|"OK / лимит / нет новых данных"| F["finalize + END"]
```

**B1. Цикл LangGraph.** `max_research_steps` ограничивает поиск; checkpoint привязан к `run_id`. Worker использует lease и heartbeat; retry продолжает тот же run. Отчёт и completed фиксируются атомарно. SSE передаёт статусы/этапы, а не токены LLM.

<a id="retrieval"></a>
## R1. GraphRAG и контроль контекста

```mermaid
flowchart TB
  A["Домен + ACL: разрешённые chunk IDs"] --> F["PostgreSQL FTS"]
  A --> V["Qdrant с фильтром ID"]
  A --> G["Identifier → seed chunks<br/>→ соседние identifiers → chunks"]
  F --> R["RRF: объединение рангов"]
  V --> R
  G --> R
  R --> C["Повторный ACL → rerank → top evidence"]
```

**R1. Поиск не расширяет права.** Онтология: Document → Chunk → Identifier; связи хранятся в PostgreSQL. Закрытый чанк не может быть мостом графа. Qwen3 Reranker использует yes/no logits и порог 0.1; произвольная семантическая онтология не строится. [Граф](../backend/knowledge_graph.py), [reranker](../backend/reranking.py), [ER](sql-data-model.md).

<a id="trust-data"></a>
## F1. Data Flow и граница доверия

```mermaid
flowchart TB
  D["Недоверенный PDF / скан / таблица"] --> I["API: auth, допустимый формат"]
  I --> P["Docling / Tesseract"]
  P --> S["Версия + SHA-256 + чанки + ACL"]
  S --> G["Граф идентификаторов / Qdrant"]
  G --> R["Разрешённые evidence → локальная LLM"]
  R --> V["PII / цитаты / смысловая проверка"]
  V --> O["Повторный ACL → Markdown / PDF"]
```

**F1. Документ — данные, а не инструкция агенту.** Оригинал и происхождение цитат сохраняются. Проверка удаляет неподтверждённые утверждения; недостаток данных приводит к уточнению/отказу. PII-фильтр покрывает email/телефоны, не полноценную DLP. Геометрию чертежей нужно сверять вручную.

<a id="research-sequence"></a>
## S1. Обработка запроса

```mermaid
sequenceDiagram
  actor U as Пользователь
  participant A as API / Guardrails
  participant D as PostgreSQL
  participant W as Worker / LangGraph
  participant R as Retriever / Rerank
  participant M as vLLM
  U->>A: Вопрос
  A->>D: Проверка сессии, run и job
  A-->>U: ID + queued SSE
  W->>D: claim_job / lease / checkpoint
  W->>M: План
  loop Ограниченный agent loop
    W->>R: Поиск с user ACL
    R-->>W: Разрешённые evidence
    W->>M: Анализ + проверка утверждений
    M-->>W: Текст / пробелы в доказательствах
    W->>D: Checkpoint + события
  end
  W->>D: Финальный ACL + отчёт + completed
  U->>A: Читать / экспортировать
  A->>D: Повторная проверка доступа
  A-->>U: Markdown / PDF либо отказ
```

**S1. Авторизация остаётся в коде.** Отзыв доступа закрывает ранее выданные ссылки, источники и экспорты. SSE читается из PostgreSQL; выполнение инструментов не передаётся модели.

<a id="deployment"></a>
## D1. Физическое размещение

```mermaid
flowchart TB
  U["Оператор / SSH"] --> H
  subgraph H["Измеренный хост: A100 80 GB / 16 vCPU / 62 GB RAM"]
    subgraph N["Docker internal network; IPv6 off"]
      F["Nginx / frontend: TLS"] --> A["API"]
      A --> P[("PostgreSQL / Qdrant")]
      W["Worker: CPU embeddings / rerank"] --> P
      W --> M["vLLM: GPU BF16; context 8192"]
    end
    S[".env 0600 + staged models / cache"] -.-> A
    S -.-> W
    S -.-> M
  end
  G["Целевая DMZ: TLS gateway / API balancing"] -.-> F
  B["OpenBao / Vault: security-prod профиль"] -.-> A
  B -.-> W
```

**D1. Измеренное размещение и целевые расширения.** Семь сервисов работают на одном хосте; `unless-stopped` обеспечивает повторный запуск после reboot. API/worker/model не имеют внешнего выхода; сам хост подключён. DMZ, балансировка нескольких API и OpenBao с TLS — целевой профиль, не заявленный результат benchmark. В нём приложение и GPU могут быть разнесены; правила firewall разрешают только внутренние зависимости. [Compose стенда](../infra/compose.benchmark.yaml), [секреты/профили](operations.md).

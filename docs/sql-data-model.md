# Модель данных

Два ER-вида раскрывают Data Plane из [C2](architecture.md#c4-containers). Показаны ключевые поля и связи; полный DDL, FK, индексы и миграции — [backend/schema.sql](../backend/schema.sql). Связи на обзорной схеме логические; она не заменяет DDL.

<a id="er-documents"></a>
## E1. Документы, права и GraphRAG

```mermaid
erDiagram
  USERS { uuid id PK }
  DOMAINS { uuid id PK }
  DOMAIN_MEMBERS { uuid user_id PK
    uuid domain_id PK
    text role }
  DOCUMENTS { uuid id PK
    uuid domain_id }
  DOCUMENT_VERSIONS { uuid id PK
    uuid document_id
    text sha256 }
  CHUNKS { uuid id PK
    uuid version_id
    text body }
  CHUNK_ACL { uuid chunk_id PK
    text principal_type PK
    uuid principal_id PK }
  KNOWLEDGE_ENTITIES { bigint id PK
    text identifier }
  CHUNK_ENTITY_MENTIONS { uuid chunk_id PK
    bigint entity_id PK }
  USERS ||--o{ DOMAIN_MEMBERS : членство
  DOMAINS ||--o{ DOMAIN_MEMBERS : роли
  DOMAINS ||--o{ DOCUMENTS : доступ
  DOCUMENTS ||--o{ DOCUMENT_VERSIONS : версии
  DOCUMENT_VERSIONS ||--o{ CHUNKS : фрагменты
  CHUNKS ||--o{ CHUNK_ACL : сужает
  CHUNKS ||--o{ CHUNK_ENTITY_MENTIONS : упоминания
  KNOWLEDGE_ENTITIES ||--o{ CHUNK_ENTITY_MENTIONS : сущности
```

**E1. Происхождение и доступ.** Домен даёт базовое право, ACL чанка может только сузить его. `principal_id` полиморфный user/group; таблицы групп здесь опущены. Вектор Qdrant имеет UUID чанка, оригинал находится в файловом томе. Граф восстанавливается из разрешённых упоминаний; сущность сама по себе не открывает доступ к документу.

<a id="er-research"></a>
## E2. Сессии, очередь и результаты

```mermaid
erDiagram
  direction LR
  USERS { uuid id PK }
  SESSIONS { uuid id PK
    uuid user_id }
  RESEARCHES { uuid id PK
    uuid owner_id
    text status }
  RESEARCH_RUNS { uuid id PK
    uuid research_id }
  RESEARCH_JOBS { uuid research_id PK
    text status }
  RESEARCH_EVENTS { bigint id PK
    uuid research_id }
  MESSAGES { uuid id PK
    uuid research_id }
  EVIDENCE { uuid id PK
    uuid research_id
    uuid chunk_id }
  REPORTS { uuid id PK
    uuid research_id
    text body }
  AUDIT_LOG { bigint id PK }
  USERS ||--o{ SESSIONS : сессии
  USERS ||--o{ RESEARCHES : владелец
  USERS o|--o{ AUDIT_LOG : actor
  RESEARCHES ||--o{ RESEARCH_RUNS : запуски
  RESEARCHES ||--o| RESEARCH_JOBS : задание
  RESEARCHES ||--o{ RESEARCH_EVENTS : SSE
  RESEARCHES ||--o{ MESSAGES : история
  RESEARCHES ||--o{ EVIDENCE : цитаты
  RESEARCHES ||--o| REPORTS : отчёт
```

**E2. Состояние и проверяемый ответ.** `evidence.chunk_id` ссылается на CHUNKS из E1; доступ проверяется заново при чтении отчёта. Сессии содержат hashes токенов, аудит — метаданные без текстов документов/секретов. Таблицы checkpoint создаёт PostgreSQL checkpointer LangGraph. PDF генерируется из Markdown и отдельно не хранится.

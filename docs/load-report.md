# Нагрузочный отчёт — 04.10.2026

**Стенд:** A100 80 GB PCIe, 16 vCPU Xeon Ice Lake, 62 GB RAM. **Профиль:** один worker, GraphRAG, Qwen3.8-27B BF16, vLLM 0.30.0, context 8192, prefix caching. Вход — synthetic документы о ДД-42; нагрузочный пользователь A имел доступ и к security-fixtures.

## Полный pipeline

| Concurrency | N / завершения | Успешный RPS | Отчётов/мин | p50 / p95, с |
|---|---:|---:|---:|---:|
| 1 | 2 / 2 | 0.009854 | 0.59 | 100.43 / 102.52 |
| 2 | 4 / 4 | 0.009996 | 0.60 | 198.96 / 201.17 |

**Практический результат:** около **0.010 RPS**, или **0.60 отчёта в минуту**. Ошибок/таймаутов в шести запросах не наблюдалось. Один worker обслуживает очередь последовательно: на concurrency 2 queue p95 99.93 с, execution p95 99.83 с. Первый SSE p95 80 мс — событие queued, не первый токен.

N=2/4 и отсутствие длительного soak не устанавливают capacity/SLA; p95 при таком N близок максимуму. Наличие отчёта не означает автоматически прохождение проверки качества. [Raw baseline](benchmarks/2026-10-04/revision-2/research-load-baseline.json), [raw c2](benchmarks/2026-10-04/revision-2/research-load-c2.json).

## Inference отдельно от исследований

2104–2105 входных токенов, 128 выходных, N=16 на ступень, уникальный cache salt.

| Concurrency | Выходных токенов/с | TTFT p95, с | Latency p95, с |
|---|---:|---:|---:|
| 1 | 24.62 | 0.605 | 5.20 |
| 4 | 73.09 | 2.350 | 8.15 |
| 16 | 139.95 | 9.357 | 14.63 |

[Raw inference](benchmarks/2026-10-04/revision-2/inference-context.json). Эти токены/с не являются RPS полного GraphRAG pipeline.

## Качество, ресурсы и offline

- **Quality 11/11** на отдельном корпусе: 7 документов / 9 чанков; основной ответ за 140.386 с. **Security 11/11**, **GraphRAG ACL 4/4** — узкие synthetic-проверки.
- За load-интервал: GPU SM peak 100%, host RAM 21.27 GiB, worker memory usage 11.72 GiB. Это sampled peaks, не непрерывный максимум.
- API/worker/model: все 9 egress probes заблокированы. После reboot model startup и генерация прошли; readiness около 202 с, с подготовленными весами и compiler cache. Сам host имеет интернет.
- Consumer AWQ, пустой cache, длительная нагрузка и выигрыш GraphRAG на размеченном корпусе не измерены. Исторический baseline со случайной головой reranker не используется для текущего результата.

[Quality](benchmarks/2026-10-04/revision-2/quality-controlled-corpus.json), [security](benchmarks/2026-10-04/revision-2/security-checks.json), [graph](benchmarks/2026-10-04/revision-1/graph-checks.json), [resources](benchmarks/2026-10-04/revision-2/resource-summary.json), [isolation](benchmarks/2026-10-04/revision-2/runtime-isolation-after-reboot.json), [startup](benchmarks/2026-10-04/revision-2/offline-startup.json), [runtime/source hashes](benchmarks/2026-10-04/revision-2/runtime-provenance.json). В [evidence index](benchmarks/README.md) перечислены остальные подтверждения и границы воспроизводимости.

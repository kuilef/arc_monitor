# Фактическая проверка Arc Monitor v0.1

Дата: 2026-09-22T11:23:47.528530+00:00. Среда: Linux, Python 3.13.5.

## Что выполнено

| Проверка | Результат |
|---|---|
| Полный pytest | **80 passed**, 0 failed |
| Line coverage пакета | **89.21%**, 1199 / 1344 statements |
| `python -m compileall -q src scripts` | Exit 0 |
| `node --check src/arc_monitor/web/app.js` | Exit 0 |
| SQLite schema, precision, transactions, duplicates/conflicts, coverage gaps | Проверены тестами |
| Демо | 7 токенов, 590 сделок, 1123 нормализованных события; повторный seed идемпотентен |
| Исследование | Реально созданы CSV и JSON отчёты на синтетическом наборе |
| HTTP API | HTML, static JS, JSON endpoints, фильтры, validation, CSP/Host/read-only поведение |
| Chromium UI | 7 строк, выбор токена, поиск, stage filter, графики, ширина 390 px; 0 JS errors |
| Внешние ресурсы UI | 0 внешних запросов в browser harness |
| Wheel | Собран, установлен в отдельный project environment; CLI/API/HTML/JS проверены из wheel |

Line coverage — метрика выполнения строк, не доказательство корректности стратегии.
Тесты API-источников используют синтетические ответы и MockTransport: это не live-результаты.
Browser-проверка и smoke запуска CLI выполнялись отдельно и не входят целиком в процент pytest coverage.

## Существенные ограничения проверки

**GitHub:** create_file отклонён с 403 `Resource not accessible by integration`.
Удалённый коммит/PR не создан; никакое прохождение CI на GitHub не заявлено.

**Bitquery:** API-ключ не предоставлялся. Документация просмотрена; normalizers, HTTP-ошибки,
дробление окон и idempotency проверены локально. Авторизованная валидация GraphQL schema,
доступность исторического диапазона и выгрузка всех launches 16–22 сентября **не выполнены**.

**RPC/Gecko:** проверены синтетические ответы, идентификаторы и ошибки. Никакой успешный
live RPC/Gecko backfill не заявлен. В контейнере DNS не позволял установить дополнительные
пакеты; это также исключает уверенное утверждение о работоспособности внешней сети из этой среды.

**Браузер:** прямую навигацию Chromium на localhost блокировала политика среды.
Проверен реальный HTTP-сервер отдельно, а визуальный browser-test выполнялся на offline
странице с мостом к фактическим ответам этого сервера. Скриншоты получены из настоящего UI,
не нарисованы. Это не полная проверка браузерной сети и CSP на компьютере пользователя.

**Wheel:** код импортирован из установленного wheel, не из src. Runtime-зависимости
повторно использованы из подготовленной среды через .pth; полностью чистая установка
зависимостей из сети не проверена.

**Не запускались:** Windows/PowerShell, Docker, сервер PostgreSQL, GitHub Actions, Ruff lint,
полный нагрузочный тест, реальная торговля, обучение/hold-out прогнозной модели.
Конфигурационные файлы для части этих сценариев существуют, что не равно успешному запуску.

## Доказательства в архиве

- `artifacts/final-tests.txt`, `junit.xml`, `coverage.json` — итоговая тестовая сессия.
- `artifacts/build.txt`, `wheel-install.txt`, `wheel-smoke.txt` — сборка и установка пакета.
- `artifacts/browser-check.json`, `browser-run.txt`, `browser-server.log` — API/UI проверки.
- `artifacts/dashboard.png`, `dashboard-mobile.png` — реальные скриншоты синтетического демо.
- `artifacts/demo-research/` — созданные CSV/JSON результаты, явно помеченные demo.
- `artifacts/*red.txt` — исходные падения проверок до реализации/исправлений, не текущий статус.

## Как повторить

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest --cov=arc_monitor --cov-report=term-missing
.\.venv\Scripts\python.exe -m arc_monitor --db data/demo.db --mode demo demo --serve
```

Все результаты демонстрационные. Наличие высоких score в демо не подтверждает ни
распознавание реального wash, ни вероятность x5/x10, ни возможность исполнения сделок.

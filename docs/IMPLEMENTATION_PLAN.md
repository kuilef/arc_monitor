# План реализации Arc Monitor

**Цель:** воспроизводимый исследовательский MVP: события → проверяемое хранилище →
признаки/исходы → локальная панель. Сначала инженерная проверка данных; не выдавать
проект за подтверждённую прибыльную стратегию.

**Архитектура:** один Python-пакет, SQLAlchemy/SQLite по умолчанию, отдельные адаптеры
источников, FastAPI и обычный HTML/JS без CDN. PostgreSQL — предусмотренный, но пока
не проверенный в этой среде путь. Исходные границы: [DESIGN.md](DESIGN.md).

## 1. Схема событий и хранилище — выполнено

- [x] `models.py`: нормализованные Launch, Trade, Liquidity, Holders, Funding, Flow, Risk;
      адреса, UTC, Decimal, запрет нечисловых/экстремальных денежных значений.
- [x] `storage.py`: транзакции, идемпотентность, конфликт одинакового ID с разным payload,
      отдельные dataset modes, raw-ответы, интервалы покрытия и checkpoints.
- [x] Проверить дубли, частично ошибочный пакет, разрывы истории, точность 18/6 decimals:
      `python -m pytest tests/test_storage.py -q`.

## 2. Источники — адаптеры выполнены; live-валидация остаётся

- [x] `providers/registry.py`, `bitquery.py`: известные контракты и raw topics;
      корректная ориентация USDC-пары, v4 Pool.Id, отдельные provider identities.
- [x] `collection.py`: backfill, адаптивное деление временного окна, hard request budget,
      остановка при насыщенной секунде, запрет будущего покрытия, безопасный resume.
- [x] `providers/rpc.py`: chain ID, read-only whitelist, RPC response identity,
      uint256-проверки, одинаковый block tag для двух представлений USDC, block/log hash.
- [x] `providers/gecko.py`: явный текущий срез известного пула, проверка токена/пула,
      метка USD-оценки с предположением о привязке USDC.
- [x] Проверить адаптеры на синтетических документированных ответах, 429/401/ошибках,
      happy-path backfill и повторах: `python -m pytest tests/test_providers.py -q`.
- [ ] На ключе пользователя проверить query schema, реальные сделки/логи и глубину истории;
      сопоставить независимую выборку по tx/pool. Это не выполнено без API-ключа.

## 3. Аналитика — базовый двигатель выполнен

- [x] `wash.py`: ограниченный набор round-trip/same-transaction эвристик, без вывода
      «net flow = 0 означает wash».
- [x] `metrics.py`: новые покупатели и группы, funding coverage, ликвидность и freshness,
      концентрация с явно размеченными исключениями, creator history, отдельные score.
- [x] `research.py`: признаки 5/15/30/60m; survival и future price labels 1/6/24/72h;
      цензурирование, reference перед cutoff, purged temporal split helper, CSV/JSON.
- [x] Проверить отсутствие look-ahead, влияние общего funder, неизвестные риски,
      отсутствующую историю и рыночные versus исполнимые returns:
      `python -m pytest tests/test_metrics.py -q`.
- [ ] Проверить статистическую/прогнозную ценность на реальном отложенном периоде.

## 4. Запускаемый продукт — выполнен MVP

- [x] `demo.py`: семь воспроизводимых синтетических сценариев; режимы demo/live разделены.
- [x] `cli.py`: init/demo/serve/import/export/backfill/poll/research/alerts/query/RPC/liquidity/doctor.
- [x] `app.py`, `web/`: read-only API, таблица, фильтры, сортировка, token inspector,
      графики, мобильная компоновка, ограничения покрытия, без внешних ресурсов.
- [x] `alerting.py`: локальные кандидаты, версия score и time-bucket дедупликация;
      никакой торговли или автоматической отправки сообщения.
- [x] Добавить инструкции, Windows-скрипты, Docker-конфигурацию и CI.
- [x] Проверить реальные HTTP endpoints и offline Chromium UI с ответами настоящего API:
      `python scripts/browser_smoke.py`.
- [ ] Выполнить нативную проверку PowerShell/Windows, Docker/PostgreSQL и GitHub Actions.

## 5. Проверка и доставка

- [x] Полный pytest, line coverage, Python compileall, JavaScript syntax check.
- [x] Собрать wheel и проверить импорт/CLI/API/static assets именно из установленного wheel.
- [x] Демо → CSV/JSON → HTTP server → поиск/детали/мобильный вид; зафиксировать доказательства.
- [ ] Ruff lint: отсутствует в среде, установка заблокирована DNS; прохождение не заявлено.
- [x] Подготовить исходники и переносимую Git-историю без ключей и рабочих баз.
- [x] Зафиксировать объём поставки и ограничения в SCOPE.md и VERIFICATION.md.
- [ ] Публикация через интеграцию GitHub: запись отклонена 403; локальные файлы не равны
      удалённому коммиту. Для загрузки с компьютера пользователя подготовлен publish.py.

## Дальнейшие инженерные этапы

Сначала авторизованная контрольная выборка; затем holders/funding/contract evidence и
полнота источников; далее реальные когорты и hold-out; только после этого — отправка
уведомлений, более быстрый streaming и модель, учитывающая исполнимость сделок.
Полная матрица 24 пунктов исходного задания: [SCOPE.md](SCOPE.md).

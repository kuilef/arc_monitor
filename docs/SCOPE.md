# Покрытие исходного задания

| Пункт задания | Состояние | Граница реализации |
|---|---|---|
| 1. Четыре уровня наблюдения | Частично | Token, creator, scoped ecosystem; launchpad-фильтр, без полной cohort-панели |
| 2. Bitquery | Реализован клиент, не проверен с ключом | HTTP trades/events, не все API и не обещание текущего тарифа |
| 3. Независимый RPC | Частично | chain, uint calls, registry logs; не полноценный независимый swap indexer/Arcscan REST |
| 4. Launchpad discovery | Частично | 8 factory/strategy entries и indexed token; social/fees/старые версии не полностью |
| 5. Generic discovery | Частично | Первая подходящая сделка; не contract creation → ERC20 → Initialize |
| 6. Token metrics | Частично | Основные торговые/ценовые метрики, неизвестные поля null; supply только из входного среза |
| 7. Holders | Расчёт и импорт | Автосбор исторических полных срезов не реализован |
| 8. Creator history | Частично | Другие наблюдаемые launches, зрелые/цензурированные survival 24h; не полный creator P&L |
| 9. Wash detector | Частично | Round trips/same transaction, concentration; не все перечисленные признаки |
| 10. Wallet clusters | Частично | First-observed-funder proxy и hub exclusion из входных событий |
| 11. Buyer velocity | Реализовано | Новые wallets 5m, предыдущие 5m; Runner использует группы-proxy |
| 12. Retention | Частично | Возврат первых покупателей через 30m; не все когорты и горизонты |
| 13. Survival / runner labels | Реализовано формально | 1/6/24/72h и будущие цены; правила не откалиброваны на рынке |
| 14. Liquidity retention | Реализован расчёт | Текущие и прошлые срезы, freshness, peak observed, не доказанный lifetime peak |
| 15. Chain/CCTP regime | Частично | Scoped trades + нормализованные Flow; автоматическая CCTP-реконсиляция отсутствует |
| 16. USDC decimals | Реализовано | 18/6 conversions, одна величина вместо суммирования representations |
| 17. Внешние cross-check | Частично | Текущий известный Gecko-пул; DEXScreener отсутствует |
| 18–19. Архитектура/таблицы | Реализована основа | SQLite default, PostgreSQL SQLAlchemy path; без migrations/Parquet |
| 20. Stages / экономия API | Частично | Классификация и hard request budget; нет адаптивных подписок по каждому токену |
| 21. Два score | Реализовано | Раздельные некалиброванные эвристики; null Risk при неполных основных свидетельствах |
| 22. Alerts | Частично | Локальный журнал с time-bucket dedup; не Telegram и не торговые сигналы |
| 23. Backtest | Исследовательский механизм | CSV/JSON, temporal features/labels, split helper; реальный датасет и hold-out не проверены |
| 24. Три стадии проекта | Первая инженерная поставка | Нет утверждения, что рыночная гипотеза уже проверена |

## Следующий порядок работ

1. Проверить небольшую авторизованную Bitquery-выборку; сверить raw topics/tx/pool,
   полноту окна и исполненные цены с независимым источником.
2. Уточнить доступную глубину истории и стоимость. Затем собрать доступные интервалы,
   сохраняя scope/version/coverage; не называть ограниченную выборку «всем рынком».
3. Добавить адаптеры полных holders и funding/contract evidence, CCTP reconciliation;
   для каждого источника тесты provenance, timestamp, staleness и look-ahead.
4. На реальных данных сформировать раздельные временные когорты и отложенный период.
   Проверить survival и цену после cutoff, а затем исполнимость с комиссиями и slippage.
5. Только после оценки precision/coverage и частоты ложных срабатываний включать
   наружные уведомления; WebSocket и интеллектуальный watch-budget — отдельная оптимизация.

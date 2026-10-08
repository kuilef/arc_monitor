# Arc/CCTP: ограниченная проверка набора тестовых данных

**Результат 8 октября 2026:** полезный небольшой корпус получен; отдельный проект
пока не обоснован. Нетривиальный воспроизводимый контрпример — потеря одного из
двух CCTP source-сообщений при ключе `(source domain, zero nonce)`. Он синтетический:
ABI-согласованные receipts принимаются существующим декодером, но их хеши и
адреса вымышлены. Реальная ошибка внешнего сервиса этим не доказана.

Дальнейшая работа имеет смысл только как небольшой набор conformance-векторов
в существующем репозитории. Для выделения отдельного набора нужны хотя бы один
публичный live CCTP receipt и проверка конкретной выгрузки на этих векторах.
Повторно строить общий USDC-инспектор оснований нет.

## Что можно воспроизвести

| Вектор | Происхождение | Проверяемый результат |
|---|---|---|
| Два emitter-а и gas | Два публичных mainnet receipts из одного блока | 16.859700 USDC имеют два Transfer-log-а с 18/6 decimals; наивная сумма даёт 33.719400. Gas учитывается отдельно и теряет точность при усечении до 6 decimals |
| Два исходящих CCTP сообщения | Синтетические ABI-векторы из существующих test builders | Оба source nonce равны нулю. Наивное перезаписывание по `(26, nonce)` оставляет 2 USDC из 4; ключи source tx + MessageSent logIndex сохраняют обе записи |
| Входящий CCTP fee | Синтетический ABI-вектор | Gross 1,000,000 = recipient net 990,000 + executed fee 10,000 units6; два разных mint-log-а |
| Одинаковые token transfer legs | Синтетический вектор | Два одинаковых legs с разными logIndex не позволяют выбрать один: `ambiguous`, исходный reconciler возвращает `unknown` |

Наивные проекции в таблице определены самим экспериментом. Это не реальные
выгрузки Bitquery, arctools или Arc USDC Inspector. В production-коде ничего
не исправлялось: corpus использует уже существующие `units`, receipt/CCTP и
trade reconciliation правила. `validate.py` обращается к внутренним функциям
из базы `a60a30a378bb692943876ac0ac3b1592e02ca656`; это исследовательский адаптер.

`fixtures/public-mainnet.json` содержит нормализованные JSON-RPC ответы без
изменения полей receipts. У block-response оставлены number/hash/timestamp и
список tx. `fixtures/synthetic-protocol.json` содержит отдельные, явно помеченные
синтетические примеры. `report.json` повторно вычисляется из обоих файлов.
Хеши SHA-256 относятся к JSON с отсортированными ключами и без пробелов;
переводы строк Windows/Linux не меняют идентичность данных.

## Публичное evidence и границы запроса

Блок **24,947,854**, hash
`0xb51a0ecf1cd3f0f2bb9afcc576cd5599fa6c2013dba14259d17d84a3f74bd853`,
время блока **2026-10-08T19:07:43Z**. Официальный анонимный RPC ответил chain ID
`0x13b2` (**5042**, mainnet).

* [Receipt с двумя USDC emitter-ами](https://explorer.arc.io/tx/0xbf3b9a3aa2e9a0c740725b6d6d42471a35565976d97394f234185b8f906d4477):
  system logIndex **0**, ERC-20 logIndex **1**, одинаковые sender/receiver.
  Amounts **16859700000000000000 units18** и **16859700 units6**.
  `gasUsed × effectiveGasPrice = 2143920107517588 units18`;
  усечение gas до units6 теряет **920107517588 units18**.
* [Успешный receipt без Transfer logs](https://explorer.arc.io/tx/0x6cf680262a4d9cd13a969c64c80802a7435baa5560daf17d34c2d80014fd61cb):
  gas остаётся отдельным фактом receipt-а. По нему не выводится полный wallet delta.

Всего выполнено **8** бесплатных RPC запросов: chain ID, три исторических receipts,
один latest block, один `eth_getLogs` по **256** блокам с фильтром CCTP
MessageTransmitterV2 + MessageSent/MessageReceived, два свежих receipts.
Полный скан и мониторинг не запускались; после этого сеть для corpus не используется.
Endpoint, границы, методы, tx, время получения и ответы сохранены в provenance.

Три хеша из [исторической сверки](../../docs/LIVE_INTEGRATION.md) вернули `result=null`:
`0xe7fe6ac3b80b454b8d3264e63d858ec4902b9869c29a3ae081e8288fd6ffbebd`,
`0x2647e938fd5c3b8295f4452179b0f135fb895b9c937dd56206411f0ced69d7ff`,
`0xf33d6f2cfe43c6b4de6eb0f34661cb0602a861b58eabd5bbc069b27fd9f2e3d5`.
Причина недоступности не установлена; историческое округление не перепроверено.
Новых Bitquery-запросов и raw-ответов провайдера нет. CCTP log discovery вернул
пустой список: `live_cctp_status=unknown`, доставка на другой стороне неизвестна.
Пустой короткий интервал не доказывает отсутствие CCTP в сети.

## Точный scope существующих инструментов

* [`arctools`, c4d79e7](https://github.com/ilkermanap/arctools/tree/c4d79e7d646cf4c94ea66452231ae66d11809f67):
  просмотрены README и [`arc-index/src/core.ts`](https://github.com/ilkermanap/arctools/blob/c4d79e7d646cf4c94ea66452231ae66d11809f67/packages/arc-index/src/core.ts).
  Pure `deriveMovements` уже хранит BigInt amounts, использует native emitter как
  источник движений, сопоставляет ERC-20 зеркала и сообщает anomalies/dust.
  README также описывает balance replay и gas residual, Ponder/Subsquid adapters.
  Поэтому dual-emitter, integer arithmetic и gas сами по себе не отличают этот
  прототип. Объект сравнения здесь — USDC indexing API, а наш узкий дополнительный
  артефакт — фиксированные CCTP identity/fee/ambiguous vectors с JSON-отчётом.
* [`Arc USDC Inspector`, 7ba0288](https://github.com/oxfrigg/arc-usdc-inspector/tree/7ba02885556a3ba508d023b6c11ecb8f9c23c863):
  просмотрены README и [`src/arc_inspector.py`](https://github.com/oxfrigg/arc-usdc-inspector/blob/7ba02885556a3ba508d023b6c11ecb8f9c23c863/src/arc_inspector.py).
  Код читает latest native/ERC-20 balances для адреса на testnet **5042002**,
  сравнивает представления после integer conversion и печатает результат.
  Этот путь не обрабатывает receipts, message identities или CCTP gross/net/fee.

Это сравнение конкретных прочитанных путей кода, а не заявление о мировой новизне
или об отсутствии похожих тестов в других проектах. Сам Arc Monitor уже проверяет
эти CCTP правила в unit tests. Вклад прототипа — небольшие сериализованные входы,
воспроизводимый отчёт, provenance и явные границы evidence.

## Повторение без сети

Из корня репозитория после установки обычных dev dependencies:

```powershell
python research/arc_correctness/validate.py --check
python -m pytest tests/test_correctness_corpus.py -q
python -m pytest --cov=arc_monitor --cov-report=term-missing
```

Запуск без `--check` перезаписывает только исследовательский `report.json`.
Ни `.env`, ни live SQLite, ни wallet keys не читаются; SQLite используется только
в памяти. Несовпадение chain/block/tx/log identity, повторный logIndex и
неоднозначные mirror candidates дают `unknown`/`ambiguous`.

Проверены fresh baseline **153 passed**, затем **163 passed** в полной проверке
(**10 новых тестов**, покрытие пакета **84%**, одно предупреждение зависимости).
Ruff новых файлов, compileall, deterministic replay, wheel build и проверка diff
на credential-паттерны прошли. Статус свежего CI перечислен в PR. Платные API, подписи,
транзакции и отправка сообщений третьим лицам не выполнялись. Лицензия проекта
не менялась; отдельные условия распространения нового набора не определены.

## Источники правил

* [Arc USDC system events](https://docs.arc.io/arc/references/usdc-system-events):
  emitter/decimals, зеркало одной операции, gas из receipt отдельно.
* [Connect to Arc](https://docs.arc.io/arc/references/connect-to-arc) и
  [contract addresses](https://docs.arc.io/arc/references/contract-addresses):
  mainnet 5042, testnet 5042002, CCTP mainnet domain 26.
* [Circle MessageV2, a92a2b4](https://github.com/circlefin/evm-cctp-contracts/blob/a92a2b4e7e6ef99bf0b05dca71780f5ec190e729/src/messages/v2/MessageV2.sol):
  `_formatMessageForRelay` записывает `EMPTY_NONCE` и empty executed finality.
* [Circle BurnMessageV2](https://github.com/circlefin/evm-cctp-contracts/blob/a92a2b4e7e6ef99bf0b05dca71780f5ec190e729/src/messages/v2/BurnMessageV2.sol) и
  [BaseTokenMessenger](https://github.com/circlefin/evm-cctp-contracts/blob/a92a2b4e7e6ef99bf0b05dca71780f5ec190e729/src/v2/BaseTokenMessenger.sol):
  отдельные amount/maxFee/feeExecuted и mint recipient/fee.

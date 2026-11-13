# ARM2-WIN-B1-UAT-001 — Issue #50

## Назначение и точная база

Дочерняя ветка `codex/arm2-win-b1-uat-001` от B1
`aaba19e5c363a8fee22eb6c49060d3452117bcd9`; PR base — `codex/arm2-uat-ui-b1`.
База PR #49 проверена: `651098136b38a17cc56a4629d040b439d79f6431`.
Оба родительских PR открыты, Draft, без merge. CI не опрашивался.

Это обновление существующего Windows UAT, не новая установка. Изменяется только
deploy-обвязка; B1 business API, SAVE, HIST, VER, ACCESS и миграции не изменяются.
Точная allowlist дополняется только GET четырех `/search`. Остальные методы и
интеграции закрыты на web proxy и ASGI; существующие CRUD/history сохраняются.

## План до установки

1. `upgrade.py preflight` только читает установленный экземпляр. Проверяются:
   неизменный исходный HEAD `6118252e8a43e8bf534012ba91284c20427ea5e0`, branch,
   tracked diff, исходный native wrapper против #42 (CRLF нормализуется), metadata,
   health/instance ID, PID/create_time/command и дерево Windows venv redirector,
   loopback-порты 18082/18083/55442, отдельный PGDATA, схема 0061, запас >10 GiB.
   Таблицы целиком сверяются по количествам и хешам строк без вывода записей.
   Фиксируется ARM1: tracked файлы, launcher/config, PostgreSQL config и listeners.
2. После PASS и локальных тестов `prepare` создаёт только внутри owner-only
   `C:\ARM2-UAT\ARM2-WIN-RUN-001\private\b1-upgrade-<package SHA prefix>`:
   исходный source с Git identity и dist, instance/secrets/process metadata,
   staged source, custom-format `pg_dump`. Секреты не печатаются и не публикуются.
3. Дамп реально восстанавливается в отдельный scratch cluster внутри этой же
   backup-папки на 127.0.0.1:55448, проверяется его собственный PGDATA и хеши
   всех таблиц. Scratch PostgreSQL останавливается через точный `pg_ctl -D`.
   Существующий UAT PGDATA не инициализируется, не переносится и не копируется.
4. После повторного preflight, неизменности данных/ARM1/секретов и staged hashes
   `install` останавливает только проверенные UAT PID, сохраняет исходный source
   целиком отдельным rename, помещает staged source по прежнему пути и атомарно
   заменяет instance/release metadata. DB/user IDs/секреты/порты остаются прежними.
5. Запуск, health точных source/package SHA и instance ID, полное сравнение таблиц
   до smoke. Затем Windows Edge, реальное сохранение/повторное чтение/история,
   stop/start/reopen, контроль ARM1. Только после этого `INSTALLED+SMOKE PASS`.

Любое несовпадение до установки — NO-GO. Снимки, дамп и проверочный кластер
сохраняются; программа их автоматически не удаляет. Запуск INIT/migrate в B1
controller исключён. Нет изменений PATH/firewall/registry/services и серверного deploy.

## Identity и возврат

`source_sha`/`approved_head` обозначают разрешённый B1 HEAD. `package_head` —
точный commit этого upgrade, включающий wrapper. `origin_head` связывает metadata
с исходной установкой. Прежний instance ID сохраняется. Release manifest содержит
хеши кода и frontend; policy проверяет обязательный набор файлов, отсутствие
выхода из source и сами хеши. Проверки DSN/loopback/PGDATA/0061 не ослабляются.

При ошибке применения `install` автоматически выполняет scoped stop, возвращает
исходный source и instance metadata, запускает прежний controller и проверяет
его health. Для ошибки последующего smoke:

```powershell
# Запускать из проверенной рабочей копии; использовать Python с psutil + psycopg.
python -B -X utf8 deploy/arm2-windows-native/upgrade.py rollback
```

Rollback выбирает SHA фактически установленного release manifest, не текущий
HEAD документации. Код откатывается без отката данных: миграций нет, все B1
записи совместимы с прежней схемой. В случае повреждения самой БД — STOP:
сохранить текущее состояние, проверить exact PGDATA/instance/dump hash и отдельно
согласовать восстановление проверенного дампа исключительно UAT. Автоматического
DROP/очистки/перезаписи БД нет. ARM1 и общий PostgreSQL в rollback не участвуют.

## Фактические результаты 13.11.2026: FAILED+ROLLBACK

**B1 сейчас НЕ установлен.** Исходный UAT снова работает на
`http://127.0.0.1:18082/arm2`; API `127.0.0.1:18083`, PostgreSQL
`127.0.0.1:55442`, база `arm2_win_run_001`, PGDATA
`C:\ARM2-UAT\ARM2-WIN-RUN-001\db-data`, схема `0061`.

| Гейт / проверка | Факт |
| --- | --- |
| PR #49 и #42, точные SHA/статусы | PASS, без изменений; Draft/open/unmerged |
| Read-only Windows preflight | PASS, ~192 ГБ свободно, собственные loopback PID/PGDATA |
| ARM1 baseline | PASS: 575 файлов; порты 5174/8014/55432, PID и creation time |
| Source/config copies | PASS, сохранены внутри private UAT; без потери Git identity |
| DB backup + реальное восстановление | PASS: все 81 public tables совпали по количествам и хешам строк |
| Установка подготовленного пакета | PASS до smoke: все 81 таблицы полностью совпали, secrets сохранены |
| Windows Edge boundary smoke | FAIL: HTTP-тело в ответе HEAD 404 вызвало ошибку HTTP parser |
| B1 authenticated GET/search, >100, TYPE/owner/plate, CP/ORG в установленном UAT | NOT RUN: smoke остановлен до login |
| Create/save, представители/подписант/legacy ФИО/user_id, 409/history в установленном UAT | NOT RUN: до создания данных проверка не дошла |
| B1 restart/reopen | NOT RUN, B1 откачен |
| Scoped rollback + старый restart/health | PASS, прежний source и instance ID восстановлены |
| Полная сверка UAT после rollback | PASS: те же 81 таблицы, те же secrets |
| ARM1 после rollback | PASS: все 575 файлов, PID/creation time/порты совпали |
| Серверная «Метка» | Не контактировали; серверную целостность удалённо не измеряли |

Подробные обезличенные результаты: [result.json](evidence/arm2-win-b1-uat/result.json).
Скриншоты фактически восстановленного UAT:
[форма входа](evidence/arm2-win-b1-uat/restored-login.png),
[health](evidence/arm2-win-b1-uat/restored-health.png).
Это evidence прежней восстановленной версии, не успешной приёмки B1.

Устанавливался package HEAD `e72d7e43cb6762929c2534d4fa826c0cb99778d0`,
business/frontend source B1 `aaba19e5c363a8fee22eb6c49060d3452117bcd9`.
После rollback фактический checkout снова
`6118252e8a43e8bf534012ba91284c20427ea5e0`, business source
`1f8c06d1f9269146ce7c07508b02dd09c89ff763`.
Instance ID до/после: `c796842a201fb47d3916f4ce8a673fb9`.

Проверенная backup-папка:
`C:\ARM2-UAT\ARM2-WIN-RUN-001\private\b1-upgrade-e72d7e43cb67`.
SHA256 `uat.dump`:
`b209abc13aba16b38b66d6625e4c4eff97614f1974e2f7615704c38bb92d621d`.
В ней сохранены `source-original`, исходные metadata, `backup-verified.json`,
`rollback.json`, остановленный scratch `drill-data` и исходный дамп. При возврате
восстановлен original source; DB restore не понадобился и не выполнялся над UAT.
Неудачная первоначальная подготовка с inherited PIPE сохранена отдельно в
`private\b1-upgrade-2a8f3b9b1fc5`; это НЕ проверенная копия для восстановления.
Её scratch-кластер также остановлен после проверки точного назначения.

### Исправления после остановки — только в ветке

HEAD 404 теперь не отправляет тело. Добавлен тест реальных HTTP-байтов для всех
четырёх search; он подтверждает пустое тело HEAD и статус 404. Неуспешный smoke
не выдаётся за ошибку только тестового скрипта: исправлен именно web wrapper.
Установка повторно не запускалась в соответствии с предписанием STOP Issue #50.

Локальная проверка итогового исправления: **15 native boundary/safety tests PASS**,
включая HEAD wire test, manifest tampering/path escape, чужой PGDATA, PID reuse,
старые CRUD/history и отказ установки при изменившихся данных. Регрессия новой
ветки: **67 ARM2 backend PASS**, 1097 других backend deselected, **386 frontend
PASS / 61 file**, main/citizens build PASS, Ruff и diff-check PASS. Backend
выполнялся только в disposable `arm2_b1_test` на 55447 с DSN/PGDATA/socket guard;
не в установленных базах. Реальные 401/permission 403 проверены backend tests;
полный Windows login/save/reopen smoke остаётся обязательным непрошедшим гейтом.
Есть существующее Starlette/httpx deprecation warning. GitHub CI не опрашивался.

### Что изменено в PR

- `uat_policy.py`: четыре точных GET search; прежние разрешения сохранены.
- `policy.py`: B1 source/origin/package identity, обязательный manifest и хеши,
  дополнительный запрет карты Yandex; прежние DSN/PGDATA/loopback проверки.
- `run.py`: чистое наследование окружения, проверка SHA/PGDATA health, запрет
  INIT/migrate из CLI обновлённого controller.
- `web.py`: B1 test banner, fail-closed обработка недопустимых методов, корректный HEAD.
- `upgrade.py`: read-only preflight, backup/restore drill, staged install, scoped rollback;
  вывод subprocess в закрытый файл и таймаут вместо наследуемых PIPE.
- `test_upgrade.py`: адресные тесты безопасности и HEAD-протокола.
- `ops/arm2_win_b1_smoke.cjs`: сценарий Windows Edge для следующей разрешённой проверки;
  только новые помеченные синтетические записи, без редактирования прежних карточек.
- Этот отчёт и обезличенные evidence. Business API/frontend B1 не переписывались.

### Ограничение / следующий гейт

Нужна отдельная оценка координатором исправленного wrapper и решение о повторной
попытке Windows upgrade. Текущий результат — **FAILED+ROLLBACK**, не завершённая
установка и не операторская приёмка. DOC-036, ARM1, каталог установок и сервер
не изменялись; merge/deploy не выполнялись. ARM1 GUI/содержимое работающей ARM1 БД
не открывались: вывод о сохранности ограничен перечисленными файлами/процессами
и отсутствием обращений нашего upgrade к ARM1 PostgreSQL.

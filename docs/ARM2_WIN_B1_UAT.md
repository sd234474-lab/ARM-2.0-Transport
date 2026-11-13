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

## Фактические результаты

Будут дополнены результатами гейтов, Windows smoke и ограничениями. B1 не считается
операторски принятым без решения владельца; DOC-036 не изменяется.

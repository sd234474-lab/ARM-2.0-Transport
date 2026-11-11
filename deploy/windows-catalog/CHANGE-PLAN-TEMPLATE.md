# Поштучный change-plan — ни один пункт не выполнен

Полный заполненный план хранится локально рядом с каталогом: `C:\ARM\00_START\PHYSICAL-CHANGE-PLAN.md`. Здесь — безопасный шаблон, без частных путей/данных/credential values. Для каждого физического DB-файла нужен **отдельный** пункт, даже если несколько файлов находятся в одной папке.

## ID пункта: `<stable-id>`

- Сущность и подтверждённое назначение: `<platform/configuration/infobase/registry/release/backup>`.
- Исходный физический путь: `<подтверждённый локально>`.
- Предлагаемый путь: `<новый, свободный, без junction; не создавать до решения>`.
- Кто использует исходный путь: `<launcher + shortcut + working directory + registry links + services/tasks, если найдены>`.
- Что именно разрешается: `<только copy/новый runtime/новая registration; перенос/удаление отдельно>`.
- Preconditions: `<актуальный consistency-aware backup, manifest/checksums и проверенный restore в отдельное место>`.
- Пошаговая операция: `<одно действие над одной сущностью, без wildcard>`.
- Проверка: `<совместимая platform/config/schema, login/open/save/reopen/history на отдельной копии, старый экземпляр доступен>`.
- Возврат: `<прежние paths/registry/ярлыки сохранены; новый экземпляр не заменяет старый до приёмки>`.
- Решение владельца: **НЕ СОГЛАСОВАНО**.

## Обязательные отдельные группы решений

1. Старый app/runtime — сначала новая копия с manifest зависимостей, а не перемещение venv.
2. Каждая старая SQLite DB отдельно. Нулевой файл — не DB, `.partial` — не успешный backup.
3. Последний установленный TEST release и его overlay отдельно от старого source checkout.
4. SQLite launcher registry отдельно от предметных DB: он содержит users/access/credential hashes; секретную копию не публиковать.
5. Каждая зарегистрированная TEST DB отдельно; новая registry registration только после restore/readiness-проверки конкретной копии.
6. Локальный contractor/PG — оставить на месте; logical dump/restore/порт/служба требуют отдельного решения. Не переносить live PGDATA.
7. Сторонний AutotuningWorkplace — оставить на месте, это отдельная установленная программа; миграция по правилам производителя.
8. INPUT packages/исторические BACKUP/ROLLBACK — сначала manifests/provenance/restore, не массовое переименование и не auto-cleanup по checksum.

Никакая строка этого документа не разрешает запуск/остановку программ, миграции, удаление оригинала или редактирование старого shortcut. Windows registry/firewall/startup/scheduled tasks не меняются в рамках каталогизации. При неоднозначности — «НЕ УСТАНОВЛЕНО», не guess/default.

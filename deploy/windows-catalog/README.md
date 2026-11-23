# ARM-WIN-STRUCT-001 — каталог Windows без переносов

Эти инструменты добавляют **только навигацию и учёт**, не запускают установленные АРМ, не меняют DB/registry/launchers/ярлыки, не устанавливают ARM2 и не требуют Docker. Работают на Windows 10 LTSC build 17763 с Python 3.13 и Windows PowerShell 5.1. Новое пользовательское представление — локальный `C:\ARM\00_START\index.html` и новый ярлык `ARM - Catalog` на рабочем столе. Существующие ярлыки не заменяются. Собранные локальные JSON/evidence и приватные пути **не коммитить**.

Владелец уточнил: **рабочих баз нет, все — прототипы; ценен последний установленный прототип**. Это приоритет сохранения, а не разрешение удалять остальные. Каталог различает последнюю установленную линию TEST-launcher и отдельный более поздний contractor, не выбирает базу автоматически и не подменяет неизвестную связь догадкой.

## Модель учёта

| Сущность | Что означает | Чем НЕ является |
|---|---|---|
| Платформа | Исполняемый код/релиз, путь, commit/file-version, runtime и локальные overlay fingerprints | Не выбранная DB и не пароль подключения |
| Конфигурация | Правила/схема/совместимость; в найденных прототипах преимущественно встроена в Python и SQL/Alembic migrations | Не выдуманный отдельный конфигурационный файл и не runtime .env |
| Информационная база | Физическая SQLite/PG DB, схема, происхождение, подтверждённое назначение | Не launcher registry, не PROJ technical SQLite, не `.partial` |
| Экземпляр | Платформа + конфигурация + выбранная или выбираемая база + launcher + URL/port + факт проверки | Не обещание запуска/приёмки по одному наличию файлов |

Стабильные ID связывают таблицы. В статическом HTML есть поиск по названию/версии/пути, копирование путей и разделы. В папках `01_PLATFORMS`–`05_RELEASES_BACKUPS` — **только ссылки на каталоги**, не команды запуска и не DB-файлы. Все URL показаны текстом. Старый launcher с ручным выбором базы остаётся старым launcher: каталог не создаёт новый default и не обходит его auth/access. Установочный staging-script с fetch/pip/migrations нельзя изображать кнопкой «открыть».

## Поток подготовки и реализации

1. Проверить approved feature base из Issue #39: `9e5dec4564a5626c0d9340a0493c85685b33a3c2`, новая ветка `codex/arm-win-struct-001`, PR base `codex/arm2-win-uat-001`. Не изменять рабочие исходники/HEAD найденных установок. Новые инструменты — только эта папка.
2. `collect.py --root <каталог> [--root ...] --source <source-root> --output <НОВЫЙ локальный facts.json>`: рекурсивные size/count/mtime до осмысленной глубины, SQL schema metadata и fingerprints защищаемых SQLite/launcher файлов. Junction/reparse points не обходятся. Access errors фиксируются, размеры предков считаются нижней оценкой. `.git`, venv/cache учитываются в размере, но не превращаются в предметные ИБ. Source fingerprint — Python/SQL без dependencies/tests; это доказательство различия кода, не полное сравнение всех дистрибутивов.
3. SQLite открываются `mode=ro&immutable=1` и `query_only`; connection явно закрывается. Читаются только migration metadata и ограниченные колонки `launcher_databases`. Не читаются account/credential/subject rows. При WAL/journal schema не признаётся свежей и не запрашивается; нулевой/partial файл не открывается как валидная DB. Snapshot не заменяет проверку восстановления backup.
4. PostgreSQL можно обследовать только при доступном уже работающем сервисе через безопасные SELECT и согласованный read-only доступ. Нет listening сервиса — **НЕ УСТАНОВЛЕНО**, не запускать/мигрировать ради инвентаризации, не извлекать пароль. Размер PGDATA не доказывает DB schema.
5. `snapshot.ps1 -Root <фактические корни> -Output <НОВЫЙ локальный JSON>`: read-only CIM/process/ports/services/tasks/shortcuts/startup metadata. Не выводит command arguments, .env или credentials. `NOT ESTABLISHED`/Errors — неизвестно, не «ресурса нет». Запросы Windows могут требовать разрешённого административного read-only доступа. Скрипт не меняет глобальную ExecutionPolicy/реестр/службы.
6. Составить **локальный** registry.json с моделью из `render.py`: таблицы platforms/configurations/infobases/instances/storage/changes и навигационные targets. Это ручная проверяемая классификация по файлам, manifests и словам владельца, а не классификация по названию папки. Не включать command/password/arguments. Для неизвестного — «НЕ УСТАНОВЛЕНО». Использовать фактически подтверждённые пути; пример из тестов не является установкой.
7. `render.py --registry <local-registry.json> --facts <local-before.json> --output <НОВЫЙ stage>` создаёт только новый staging-каталог с offline HTML/JS/CSS, локальным JSON и evidence. Не перезаписывает каталог. Проверить HTML/поиск/пути, реальные папки-targets, отсутствие секретов и команд, проверить сохранённые до операции fingerprints. Существующие shortcuts нужно включить в protection baseline.
8. В рамках явного разрешения владельца на **добавление навигации** исполнить `install.ps1 -Stage <проверенный stage>`. Он откажется, если `00_START` или `ARM - Catalog.lnk` существуют, если ancestry — reparse point, targets отсутствуют/не каталоги, evidence не совпадает или staged payload содержит executable/DB/старые `.lnk`. Создаёт только новый `00_START`, 14 (по локальному registry) folder shortcuts и один новый desktop shortcut. Ничего не открывает. При частичной ошибке не удаляет/не восстанавливает ничего автоматически; сохранить локальную диагностику и остановиться.
9. `verify.py --before <local-before.json> --output <НОВЫЙ after.json>` повторно сверяет SHA256+size+mtime старых файлов, не чинит изменения. Считать изменившийся внешний файл изменившимся — не приписывать себе абсолютную неизменность. Отдельно проверить цели новых `.lnk` через COM без их запуска. Новые evidence можно ДОБАВИТЬ в `00_START\90_EVIDENCE`; старые файлы не перезаписывать.

`install.ps1` никогда не переносит старые каталоги, не копирует реальную DB/secret config, не переписывает старый registry или shortcut и не создаёт startup/service/task. Если политика исполнения запрещает локальные scripts, использовать только согласованный способ запуска; автоматической смены системной политики в коде нет. Физические изменения приложения/DB вне добавления навигации требуют отдельного решения.

## Целевая логическая схема, не выполненные переименования

```text
C:\ARM\
  00_START\                 новый обзор и локальный реестр
    01_PLATFORMS\           folder-links → существующие платформы
    02_CONFIGURATIONS\      folder-links → существующие migrations/config sources
    03_INFOBASES\           folder-links → папки данных (не команды открыть DB)
    04_INSTANCES\           folder-links → старые launchers
    05_RELEASES_BACKUPS\    folder-links → сохранённые INPUT/BACKUP/ROLLBACK
    90_EVIDENCE\            локальные snapshots, без публикации в Git
  PLATFORMS\                будущее физическое размещение — только по решению
  CONFIGURATIONS\           будущие manifests совместимости, не секреты
  INFOBASES\TEST\           будущее, а не автоматически перенесённые DB
  RELEASES\ BACKUPS\        будущее; текущие BACKUP/ROLLBACK/TEST\INPUT остаются
  LOGS\ EXPORTS\ ARCHIVE\   классификация/retention после отдельного решения
  LEGACY\                   возможная карта путей, не скрытое удаление
```

Физически в этой задаче создаётся **только `00_START`**, его собственные категории и новый desktop link; корневые PLATFORMS/INFOBASES/BACKUPS и пр. не нужны для навигации и не создаются ради пустых папок. Наличие существующих `ARCHIVE/EXPORT` без файлов не доказывает, что все старые файлы туда надо переместить. Между source, установленным release, backup и rollback нельзя ставить знак равенства по именам/датам.

## Поштучный change-plan

Локальный registry/HTML содержит отдельные строки **from → proposed to → кто использует → preconditions → verify → rollback**: старый app; каждая группа старых DB; TEST release/runtime; launcher registry; каждая из двух TEST DB отдельно; INPUT installers; BACKUP/ROLLBACK. Contractor/Autotuning/incomplete artifacts предложено оставлять на месте. Ни один перенос не выполнен.

Общие preconditions для любого будущего пункта:

- SQLite live: согласованная SQLite backup API/controlled quiescence; учесть WAL/SHM и attachment/registry links. Не считать копирование одного `.db` при открытой WAL пригодным backup. Проверить restore **в отдельное место**, schema/платформу, login/open/save/reopen; только затем поштучно менять links с отдельным разрешением.
- PostgreSQL: штатный logical dump/restore с версиями/extensions/roles и проверкой новой отдельной DB. PGDATA не переносить/не переименовывать при работающем сервисе. Изменения служб/портов отдельно согласовывать.
- Python venv: перемещением каталога совместимость не гарантируется; новый runtime строится отдельно с manifest зависимостей и тестом. Старый runtime не трогать до приёмки.
- Registry/ярлыки/планировщик: сначала записать исходные targets и аргументы локально без секретов, проверить registry DB links и users/access на изолированной копии; создать новую ссылку, не молча перепривязать старую. Backup реестра может содержать credentials — не Git.
- Retention: checksum совпавших DB означает только побайтное совпадение снимка. Разные исторические роли сохраняются; удаление не разрешено. Нулевые/partial файлы не удалять автоматически.

## Проверки

```powershell
python -X utf8 deploy/windows-catalog/test_catalog.py -v
ruff check --config backend/pyproject.toml deploy/windows-catalog
```

Тесты проверяют ro SQLite без subject/credential rows и без изменения file/sidecars, отказ на WAL/partial, типы связей/неизвестные ID, запрет executable fields/remote navigation, HTML escaping и отсутствие executable href, отказ перезаписи, detection внешних изменений, PowerShell AST без запуска программ. Дополнительно локальный headless browser проверяет только новый HTML/поиск/отсутствие HTTP requests; не является запуском/приёмкой АРМ.

Публичный PR содержит только воспроизводимые инструменты/шаблоны и этот протокол. Полный реестр/матрица/фактические пути/контрольные снимки находятся на Windows. Нет merge/deploy, ожидания CI, миграций или установки ARM2.

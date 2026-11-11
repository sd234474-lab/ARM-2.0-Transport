# ARM2-WIN-RUN-001: проверенный нативный Windows UAT

На Windows 10 Enterprise LTSC build 17763 выполнен один рабочий нативный запуск.
После успешного браузерного сценария поиск других способов прекращён.

## Пользование установленным экземпляром

Открыть **http://127.0.0.1:18082/arm2**. На момент передачи экземпляр запущен.
Это отдельная тестовая база, не производственная установка.

Команды PowerShell работают из любого текущего каталога:

```powershell
& 'C:\ARM2-UAT\ARM2-WIN-RUN-001\START_ARM2.cmd'
& 'C:\ARM2-UAT\ARM2-WIN-RUN-001\HEALTH_ARM2.cmd'
& 'C:\ARM2-UAT\ARM2-WIN-RUN-001\STOP_ARM2.cmd'
```

Повторный START работающего экземпляра отклоняется. После STOP данные сохраняются;
START поднимает тот же кластер. INIT повторно выполнять нельзя.
Автозапуск и служба Windows не устанавливаются: после перезагрузки нужен START.

Новый логин — `uat-admin`. Случайный пароль доступен только локально владельцу:
`C:\ARM2-UAT\ARM2-WIN-RUN-001\private\secrets.json`, поле `admin_password`.
Не отправлять этот файл в переписку или GitHub. ACL папки private ограничен владельцем.
Логи: `logs\api.log`, `logs\web.log`, `logs\postgres.log` внутри нового корня.

## Что запущено

| Часть | Размещение / адрес |
|---|---|
| Корень | `C:\ARM2-UAT\ARM2-WIN-RUN-001` |
| Исходник приложения | `source`, exact approved HEAD `6118252e8a43e8bf534012ba91284c20427ea5e0` |
| Business source | `1f8c06d1f9269146ce7c07508b02dd09c89ff763`; backend/frontend без изменений |
| Python | системный 3.13.16 read-only, собственные `python` и `tools\bootstrap` venv |
| PostgreSQL / PostGIS | собственный `runtime\pgsql`, версии 17.11 / 3.6.2 |
| Информационная база | `arm2_win_run_001`, `db-data`, `127.0.0.1:55442`, Alembic `0061` |
| FastAPI/Uvicorn | `127.0.0.1:18083` |
| Собранный frontend / local proxy | `127.0.0.1:18082`, `source\frontend\dist` |
| Инструменты | системный Node 26.13.0 read-only; отдельный Playwright 1.64.1, системный Edge read-only |

Зависимости приложения установлены через `uv sync --frozen --no-dev --no-install-project`
из существующего lockfile. Сборка выполнена `npm ci` → `npm run build` в новом source.
Системные PATH, firewall, registry, службы и установленные компоненты не менялись.
Рабочая серверная «Метка» не использовалась.

Официальные самостоятельные бинарные архивы:

| Архив | SHA256 фактически загруженного файла |
|---|---|
| [PostgreSQL 17.11 Windows x64](https://get.enterprisedb.com/postgresql/postgresql-17.11-1-windows-x64-binaries.zip) | `6eabdf00d2893713b75db4336a23c3fdf505f056e217ec6e2e95d901750cfea3` |
| [PostGIS pg17 3.6.2 x64](https://download.osgeo.org/postgis/windows/pg17/postgis-bundle-pg17-3.6.2x64.zip) | `7ba180ee2a352987b9a2f194673652c59483b55852295ccf401dceccd8765425` |

Это контрольные суммы полученных архивов, не заявление о независимой подписи поставщика.
Архивы и подробный manifest сохранены локально в `downloads`.

## Граница изоляции

`native_app.py` оборачивает неизменённое приложение. Используется точный deploy-only
allowlist из #38: штатные auth/roles, ARM2 CRUD/history и необходимый bootstrap UI.
Остальные API получают 404. Отдельные worker/publisher/trainer/polling процессы не
запускаются; в общем API нет startup/lifespan фоновых задач. Интеграционные токены
пусты, webhook-роуты недоступны. Организации как многобазовая платформа отключены.
Справочник собственных организаций ARM2 при этом доступен штатным способом.

Перед импортом приложения проверяются literal loopback DSN, конкретная новая БД,
порт, test-настройки и локальная идентичность. Python socket guard запрещает внешний
DNS/исходящие соединения и обращения к старым портам. Для libpq дополнительно
зафиксирован локальный DSN; guard Python не является системным firewall для C DLL.
API/proxy обслуживают только проверенный локальный контур. Health проверяет реальную
БД, data_directory, PostGIS и 0061; случайный instance_id защищает от подмены ответом
другого сервера. Публичный доступ не настраивается.

STOP сверяет PID, время создания и команду собственного процесса. Дочерний Python
проверяется по native entrypoint; собственный системный conhost завершается естественно.
При неизвестном дочернем процессе остановка отклоняется. PostgreSQL останавливается
только собственным pg_ctl после проверки postmaster PID, executable и data directory.
Нет остановки по имени, занятому порту или массового kill.

## Фактическая проверка 2026-11-11

- Headless Edge: `/arm2` → штатный вход новым пользователем → создать TYPE → сохранить
  (201) → список → повторно открыть карточку → история → детали события. PASS.
- `TYPE-000002`, версия 1, событие CREATED №2: имя, версия и история независимо прочитаны
  из нового PostgreSQL. PASS.
- Для CP и ORG созданы только новые тестовые карточки/представители. Несуществующий
  и чужой signer отклонены API: четыре ответа 422; версия и история не изменились.
- Четыре прямых SQL UPDATE с такими ссылками отклонены FK (SQLSTATE 23503), каждая
  транзакция откачена; исходные signer/версии сохранены. PASS.
- Полный STOP освободил все три новых порта. START поднял ту же БД; та же запись и
  детали истории снова прочитаны Edge и SQL. PASS.
- 7 адресных unittest для DSN/integration guards, фактического socket guard/socketpair,
  PID reuse/чужой команды, другого PGDATA, scoped conhost и ZIP path traversal. PASS.
- Ruff с конфигурацией backend, `git diff --check`, frontend build. PASS.
- Read-only сравнение 1807 прежних файлов по SHA256/размеру/mtime: изменений нет.
  Дополнительно 203 Python/SQL файла прежних прототипов совпали с предыдущим manifest.
  Прежние порты 8080/8081/5174/8014/55432 не слушали до запуска, после STOP и после START.

Граница утверждения о сохранности — перечисленные локальные manifests и наблюдения
процессов/портов, не полный побайтовый снимок Windows. ARM1 специально не запускался;
работоспособность его GUI заново не заявляется. Старая contractor DB не открывалась:
её остановленный PGDATA проверен как файлы, runtime не исполнялся.

Локальные доказательства: `evidence\browser.json`, `database.json`,
`restart-browser.json`, `protected-before.json`, `protected-after.json`.
Они содержат локальные сведения и в Git не включаются.

Отдельный человекочитаемый `INSTALLATIONS-EXPORT-ARM2-WIN-RUN-001.txt` создан рядом
с существующим каталогом. Старые registry/index/ярлыки оставлены как были. Экспорт
сохраняет ранее учтённые установки и добавляет фактическую нативную ARM2; прежняя
запись о неустановленном Docker-пакете является исторической. Реальные пути старых
установок находятся только в этом локальном экспорте.

## Скрипты и воспроизведение

Работающий экземпляр уже подготовлен: следующий раздел предназначен для проверки
процедуры на новом свободном корне, а не повторной инициализации существующей БД.
Скрипты намеренно привязаны к согласованному корню и отказываются его переинициализировать.
До подготовки проверяется отсутствие reparse points у корня и его предков.

1. Убедиться, что согласованный новый корень и три порта свободны. Создать только этот
   корень и подкаталог `downloads`. Использовать проверенный системный Python read-only.
2. `prepare.py downloads --catalog-path <existing-catalog> --protected-pgdata <existing-PGDATA>`:
   пути берутся из локальной инвентаризации; чтение для исходного защитного manifest,
   затем загрузка и распаковка собственных официальных бинарников. Старый runtime не нужен.
3. `prepare.py source`: новый clone approved ветки, проверка exact HEAD, копирование
   только native deploy scripts. Несовпадение HEAD — STOP.
4. `prepare.py python`, затем `prepare.py frontend`: свои venv/кеши/пакеты/сборка.
5. Только собственным `tools\bootstrap\Scripts\python.exe` выполнить установленный
   `source\deploy\arm2-windows-native\run.py init`, затем `start`.
   Init создаёт случайные секреты, новый кластер/БД, применяет 0061 и использует штатную
   команду create-admin. Чужая/уже существующая база не мигрируется.
6. Системным Node выполнить установленный `browser-check.cjs`; собственным bootstrap
   Python выполнить `run.py verify`. Браузерный тест создаёт новые тестовые данные.
   Для повторного чтения без создания данных: `browser-check.cjs --read-existing`.
7. Проверить STOP/START, повторное чтение и `audit.py --catalog-path <existing-catalog>`.
   Ключ `--export` создаёт новый файл рядом с каталогом и откажется перезаписывать его.

Локальная проверка скриптов из checkout:

```powershell
& 'C:\ARM2-UAT\ARM2-WIN-RUN-001\tools\bootstrap\Scripts\python.exe' -B -m unittest discover -s deploy/arm2-windows-native -p test_native.py -v
& 'C:\ARM2-UAT\ARM2-WIN-RUN-001\tools\bootstrap\Scripts\ruff.exe' check --config backend/pyproject.toml deploy/arm2-windows-native
git diff --check
```

Блокеров работающего native UAT нет. Это ограниченная проверка запуска, а не полная
операторская приёмка. CI не опрашивался и не ожидался; независимую проверку выполняет
координатор. Merge и production deploy не выполнялись.

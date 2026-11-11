# ARM2-WIN-UAT-R2-001 — пакет подготовки, без установки

**На осмотренном ПК установка NO-GO.** Windows 10 LTSC build 17763, Docker не обнаружен; актуальный restore ARM1 и рабочий baseline не подтверждены. См. [фактический осмотр и план защиты](WINDOWS-INVENTORY.md). Скрипты подготовки/сборки/запуска/создания администратора ниже в рамках Issue #37 **не выполнялись**. Они предназначены для следующего явно разрешённого этапа на поддерживаемом Windows, после независимой проверки пакета.

Приложение заморожено на `1f8c06d1f9269146ce7c07508b02dd09c89ff763`, вся stacked chain PR #34/#36; миграции до `0061`. Метка `ARM2-WIN-UAT-R2-001` — тестовая маркировка, не production semver. HEAD самого пакета берётся из итогового Draft PR/отчёта; его deploy-файлы добавлены поверх этого source SHA. Файлы `backend/`, `frontend/` и общие механизмы не меняются этим пакетом.

## Что входит и что изолировано

- [compose.yaml](compose.yaml): только `uat-db`, `uat-migrate`, `uat-api`, `uat-web`. Отдельные PostGIS DB/два named volumes и internal network. Нет host mounts, production env, внешних volumes/networks, docker.sock; DB/API без опубликованных портов. Web только `127.0.0.1:18082`. `restart: no`: ничего не добавляется в Windows startup/services/tasks.
- [api.Dockerfile](api.Dockerfile), [web.Dockerfile](web.Dockerfile): полный backend/auth/ARM2/миграции и frontend с существующими uv/npm lockfiles. OCR weights не скачиваются; worker/publisher/trainer/receiver не входят в service graph. Базовые image tags версионированы, но не закреплены digest: после будущей сборки нужно зафиксировать image IDs/digests в evidence; бинарная идентичность повторной сборки сейчас не заявлена.
- [uat_policy.py](uat_policy.py), [uat_entrypoint.py](uat_entrypoint.py): отдельная deploy-оболочка над неизменённым `app.api:app`. Только health, login/me/logout, meta, users GET/POST/PATCH, существующие ARM2 CRUD/scoped-history и необходимый shell-bootstrap `GET /api/platform/status` (только флаг enabled=false). Это сужение поверхности UAT, не новый auth/RBAC. Webhook, фото/обработка, integration API, внешние sync, tenant/platform operations и user DELETE отклоняются. Исходный `FastAPI` не имеет lifespan/startup scheduler; импорт receiver использует helpers, polling loop защищён `__main__`. Runtime internal network, пустые integration tokens, запрет организаций/обучения и CSP только self дают дополнительные независимые ограничения. Статический тест не заменяет будущую проверку runtime.
- [uat_control.py](uat_control.py): upgrade строго `0061` только после проверки UAT host/db/user/env; организации/tenant scanning отключены. Создание `uat-admin` — существующий `app.cli create-admin`, интерактивный getpass, без пароля в коде/env/командной строке. Других admin-команд нет.
- [uat.ps1](uat.ps1): явный namespace/context/file/env, owner gate, проверка source tree/владения ресурсами/портом; никаких общих stop/prune/down/reset. [inventory.ps1](inventory.ps1) — отдельно read-only, без вывода секретов. Sample [env](uat.env.example) и [gate](gate.example.json) не дают GO.
- [smoke.py](smoke.py): **будущий mutating** API smoke внутри UAT, после отдельного разрешения; создаёт только fictitious UAT records, проверяет сохранение через повторные GET и history. Не является GUI-приёмкой, не запускался в этом поручении.

Ничего не подключает рабочую «Метку» или АРМ 1.0. Не копировать их данные, конфиги, ключи или учетные записи. Поддержка Wialon/анкеты/документов/планирования не заявлена. Вкладки с отсутствующим API сохраняют честный статус «не реализовано». Общий shell «Метки» остаётся видимым; его не-ARM2 функции в этом пакете недоступны по API. Начальный URL — **`/arm2`**, не страница обзора `/`.

## Требования и разрешения следующего этапа

[Официальные требования Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/), проверены 11.11.2026: Windows x64, Windows 10 22H2 build 19045 или Windows 11 23H2 build 22631+, поддерживаемый производителем lifecycle, WSL 2.1.5+, SLAT/виртуализация, минимум 8 GB RAM. Рекомендуется поддерживаемый Windows 11. Включение WSL/виртуализации, установка Docker, изменения ОС/групп и возможный reboot — **отдельное решение владельца/IT**, не команда этого пакета. Docker Desktop лицензия/права доступа к локальному Linux engine также проверяются IT. Пакет не .exe.

Ориентировочно для полной frozen Python/ML dependency сборки: 4 CPU, 16 GB RAM, 30–40 GB свободного диска, несколько GB network downloads. Это рекомендации, а не измеренные требования; проверять свободное место/лимиты Docker перед build. Интернет нужен для git/images/uv/npm при сборке, не для рабочих API после запуска. Нужны Git, Windows PowerShell 5.1+, Docker Desktop + Linux containers, Compose v2 с `config --format json`, `up --wait`/`--wait-timeout`. Поддержку этих флагов проверить `docker compose version`/`docker compose up --help` read-only; не подменять команды корневым Compose.

Скрипт использует только локальный context `desktop-linux`/named pipe, отвергает наследуемые DOCKER_HOST/DOCKER_CONTEXT/COMPOSE_FILE/COMPOSE_PROJECT_NAME/COMPOSE_PROFILES. Не переключает context и не стартует Desktop/WSL. Ошибка/неизвестный ресурс/занятый порт/несовпадение labels = STOP; нельзя «освобождать порт» остановкой ARM1 или другого сервиса.

## Воспроизводимая последовательность — только ПОСЛЕ нового разрешения

Ни одна команда этого раздела не выполнялась Codex в рамках подготовки. До clone подтвердить целевой Windows, защищаемые ARM1 каталоги/конфиги/БД/launchers, backup+restore evidence, baseline и owner decision по [checklist](WINDOWS-INVENTORY.md). Не обновлять текущую LTSC автоматически. Не размещать clone внутри существующего АРМ, backup или junction. Требуется отдельный root; скрипт отвергнет текущий checkout разработки.

```powershell
$repo = Join-Path ($env:SystemDrive + '\') 'ARM2-UAT\ARM2-WIN-UAT-R2-001'
if (Test-Path -LiteralPath $repo) { throw 'STOP: target already exists; inspect it' }
# Сначала осмотреть каждый существующий parent на ReparsePoint; не продолжать при junction.
git clone --single-branch --branch codex/arm2-win-uat-001 https://github.com/sd234474-lab/ARM-2.0-Transport.git $repo
$packageHead = Read-Host 'Paste exact package HEAD from independently reviewed PR'
git -C $repo checkout --detach $packageHead
git -C $repo rev-parse HEAD
$p = Join-Path $repo 'deploy\arm2-windows-uat'
```

Проверить код/HEAD по независимому отчёту. Скопировать sample gate в `.local\uat-gate.json` **этого нового clone**, заполнить все evidence-поля и ProtectedRoots фактическими путями установок, данных, конфигов и backup. Все исходные флаги false; установить true можно только по состоявшейся проверке, а не для обхода STOP. Ни script, ни галочка не проверяют restore/GUI baseline вместо человека. Не размещать там секреты. При Restricted ExecutionPolicy остановиться и получить согласованный IT способ запуска/подпись; пакет не меняет глобальную политику.

```powershell
$gate = Join-Path $repo '.local\uat-gate.json'
# Только после заполнения gate и отдельного решения владельца:
& "$p\uat.ps1" -Action Prepare -GateFile $gate -OwnerApproved
& "$p\uat.ps1" -Action Validate -GateFile $gate -OwnerApproved
& "$p\uat.ps1" -Action Build -GateFile $gate -OwnerApproved
& "$p\uat.ps1" -Action Start -GateFile $gate -OwnerApproved
& "$p\uat.ps1" -Action Health
& "$p\uat.ps1" -Action CreateAdmin -GateFile $gate -OwnerApproved
```

Prepare генерирует новые 32-byte random hex DB password/inert webhook secret в защищённый ACL `.local\arm2-uat-r2-001\uat.env`; не выводит их и не перезаписывает существующий env. Это не рабочие токены; не использовать sample placeholders. CreateAdmin запрашивает новый пароль ≥12 символов и подтверждение в TTY, не раскрывает пароль. При повторе create-admin уже существующая учетная запись не сбрасывается. Следующие учётки создаются администратором через существующий экран `/users`: fictitious A, B, OPERATOR с ARM2 VIEW+EDIT и VIEW-only с ARM2 VIEW. Удалить default non-ARM2 permissions при создании OPERATOR; не выдавать exchange/learning. Пароли назначить локально, в git/отчёт не вносить. Назначать representative.user_id может ADMIN_OPERATOR; наличие ARM2 EDIT у OPERATOR само по себе не открывает `/users`.

Start явно перечисляет четыре сервиса, без build/переноса данных. Compose ждёт DB, успешный exit миграции и API/web health. UAT readiness отвечает 503 до схемы `0061` и проверяет `current_database()`. Health должен показать package, полный source SHA, database `arm2_uat_r2_001`, migration `0061`. В браузере открыть `http://127.0.0.1:18082/arm2`, проверить TEST ONLY banner и `/uat-instance.json`; static marker **не заменяет** проверку живого `/api/health`. Если что-то не совпало, **не вводить пароль**. Никаких HTTPS/domain/webhook registrations.

```powershell
# Опциональный API smoke: ПОСЛЕ отдельного разрешения создавать fictitious UAT records.
& "$p\uat.ps1" -Action Smoke -GateFile $gate -OwnerApproved
```

Smoke спрашивает UAT-admin password в памяти, проверяет identity до login, создаёт уникальные TYPE/CP/ORG/REP/VEH и две неактивные fictitious учётки со случайными невыводимыми паролями. Проверяет user A→B→null/reopen/history, no-op/409/422, owner CP→ORG→null и неизменность ролей/прав. Оставляет данные для осмотра; GUI, negative direct SQL и восстановление ARM1 этим smoke не принимаются. Итог `API_SMOKE_PASS` допустим только после реального выполнения.

## Останов, повторный запуск, rollback тестового экземпляра

```powershell
& "$p\uat.ps1" -Action Stop -OwnerApproved
# После повторной проверки gate/baseline: Start с теми же env/volume; Prepare повторно не нужен.
& "$p\uat.ps1" -Action Start -GateFile $gate -OwnerApproved
# Только после завершения UAT и явного решения удалить ТЕСТОВЫЕ данные:
& "$p\uat.ps1" -Action Cleanup -OwnerApproved
```

Stop проверяет exact имена/labels/mounts и останавливает только UAT web/API/migrate/DB. Cleanup дополнительно требует точную фразу `DELETE arm2-uat-r2-001 TEST DATA`, удаляет только четыре UAT-контейнера, два названных UAT-volume и названную сеть; проверяет чужие attachments. Ошибка проверки = никаких попыток очистить «похожие» объекты. Host-файлы, images, Docker data-root и ARM1 не удаляются. Очистка необратимо удаляет тестовые данные; если нужен UAT backup, это отдельное согласование. Env остаётся локально защищённым, не публиковать его. После stop/restart/cleanup повторить ARM1 baseline; его restore/останов не выполняется скриптом.

## Диагностика без смешения проектов

Проверять только `arm2-uat-r2-001-*`: `docker --context desktop-linux ps -a --filter label=com.docker.compose.project=arm2-uat-r2-001`. Для exit миграции: `docker --context desktop-linux inspect arm2-uat-r2-001-migrate --format '{{json .State}}'`. При необходимости — logs только конкретного UAT container; логи/DB error могут содержать тестовые данные, хранить локально и маскировать перед передачей. **Не** печатать `compose config`, `docker inspect .Config.Env` или содержимое env — там пароль.

Нет Docker/context/Compose flags, resource collision, занятый порт, неверный source/schema, failed migration, неготовый health, непройденный ARM1 baseline/restore → STOP/NO-GO. Не использовать root compose, `subscribe`, `check-max`, worker или изменённый production env для «починки». При миграционном AUD-02 orphan/mismatch не исправлять/удалять данные автоматически: только локальная диагностика UAT и отдельное решение. Ошибка build означает пакет ещё не прошёл runtime gate, а не повод ставить его поверх ARM1.

## Короткая программа ручного UAT

Во всех строках использовать только fictitious UAT data. После каждого сохранения — закрыть карточку/вернуться из списка, проверить server fields и scoped history по кнопке. Успешный HTTP/клик без сохранённой записи не считается результатом. Фиксировать PASS/FAIL, object ID, before/after, expected/actual; секреты не фиксировать.

| Начало → действия | Конечный результат и проверка |
|---|---|
| Подтверждённый ARM1 baseline → login UAT-admin на 18082/arm2 | Живой marker/source/DB/schema совпадают; ARM1 одновременно работает на своём URL/DB. Не-ARM2 integrations недоступны. |
| Новый TYPE → имя, сохранить с неполными требованиями → попытаться выбрать в VEH | TYPE REVIEW, неполные признаки видны, новое назначение запрещено. Заполнить СТС=true, тарировка=false, Wialon=true → CONFIRMED; reopen сохраняет false отдельно от null, history показывает изменение. |
| Новые CP и ORG → одно имя → затем full_name/legal_address | REVIEW → CONFIRMED без требования банка/ИНН/REP; разные ID/карточки. Банковские номера с ведущими нулями сохраняются строками после reopen; правка одного поля не очищает другие. |
| Свой CPREP/ORGREP → создать/открыть → выбрать основной подписант родителя | Несколько представителей остаются отдельными; signer только своей карточки; reopen/history подтверждают. Чужая/несуществующая ссылка через технический negative тест отклоняется; состояние записи не меняется. DB-level negative tests уже были в AUD-FIX, не исполнять произвольный SQL на ARM1. |
| ADMIN_OPERATOR → REP user A → save/reopen → B → save/reopen → «Не связан» | Реально сохранённые integer A/B/null, версии +1, history before→after; users role/permissions не меняются. Save без изменений — no-op, без новой версии/события. |
| Отдельный OPERATOR VIEW+EDIT → открыть REP; затем VIEW-only | Без права user-directory новое назначение блокировано с объяснением, текущая связь сохраняется при правке других полей. VIEW-only не сохраняет. После изменения прав администратором перелогиниться и проверить реальный отказ API. |
| CONFIRMED TYPE/CP/ORG → VEH create → owner CP → ORG → none | TYPE и прочие VEH поля остаются, прежнее owner-поле обнулено; нет статуса REVIEW/CONFIRMED у VEH; reopen и history подтверждают каждую смену. |
| Две независимые браузерные сессии открыли одну карточку → первая сохранила → вторая сохраняет старую версию | 409, черновик сохранён, нет ложного success. «Сравнить» не стирает draft; отмена reload не стирает; подтверждение принимает новый GET/version, следующая осознанная правка успешно сохраняется. Повторить REP с user_id. |
| Карточка открыта → нажать «История» → событие/detail → страницы | До нажатия history-запроса нет; после — история только этого объекта, before→after, author/time/source/version, реальные следующие страницы. Не изображать глобальный журнал/фильтры. |
| Start → одновременный ARM1 baseline → Stop → baseline → повтор Start → baseline → Cleanup → baseline | ARM1 доступен без простоя, его конфиги/данные не затронуты UAT. Любой сбой запрещает принять установку безопасной. |

Известные границы: AUD-04 global history, AUD-05 полный VEH.LIST, AUD-06 universal READ/API/UI, AUD-07 readiness не приняты; Wialon/анкеты/документы/планирование отсутствуют. Префиксы CPREP-/ORGREP- не менялись. Это ограниченный функциональный срез, не production release и не приёмка всех процессов.

## Что проверено при подготовке

Локальные policy/service graph/guard/PowerShell-parser тесты: `python -X utf8 deploy/arm2-windows-uat/test_package.py -v` (нужны существующие backend dependencies, включая SQLAlchemy/PyYAML). Ruff: `ruff check --config backend/pyproject.toml deploy/arm2-windows-uat`. Они не запускают серверы/контейнеры/ARM1.

Docker Compose interpolation/schema, image builds, migration-in-container, health/login/API smoke и Windows GUI **НЕ выполнялись**: доступного Docker engine нет, установка/запуск UAT этим поручением запрещены. Результаты AUD-FIX на source SHA — отдельные предыдущие доказательства, а не PASS контейнеров этого пакета. Перед установкой нужны отдельный Linux/container smoke на этом пакете и независимая проверка, затем owner decision и ручная Windows UAT с защитой ARM1. GitHub Actions не опрашивается.

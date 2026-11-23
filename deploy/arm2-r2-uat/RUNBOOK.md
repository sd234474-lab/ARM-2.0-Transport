# Ubuntu R2 — PREPARED, не INSTALLED

Issue #75. Комплект основан строго на `3bd71a13bc92e5a048002fda0cfd23b8d5b1c720` (PR #74).
Команды ниже **не выполнялись на VM**. Их применение требует отдельного эксплуатационного разрешения.
Residual: **VM operational preflight pending**. Полный CI проверяется независимо.

## Идентичность и запреты

| | Новый R2 | Сохранённый B1 — не адресовать командами |
|---|---|---|
| Каталог | `/home/arm2admin/arm2-uat/r2-release` | `/home/arm2admin/arm2-uat/app` |
| Project | `arm2-ubuntu-uat-r2` | `arm2-ubuntu-uat` |
| DB / ревизия | `arm2_r2_uat` / `0070` | `maxphotos` / `0061` |
| DB volume | `arm2-ubuntu-uat-r2_r2_postgres_data` | `arm2-ubuntu-uat_postgres_data` |
| Runtime volume | `arm2-ubuntu-uat-r2_r2_photo_data` → `/data` | `arm2-ubuntu-uat_photo_data` |
| FILE volume | `arm2-ubuntu-uat-r2_r2_file_data` → `/files` | не использовать |
| Networks | `arm2-ubuntu-uat-r2_r2_internal` (internal), `arm2-ubuntu-uat-r2_r2_ingress` | не подключаться |
| HTTP | `127.0.0.1:R2_HTTP_PORT`, порт выбирается проверкой | `127.0.0.1:8080` |

Только `r2-db`, `r2-api`, `r2-web` долговременные. API UID/GID 10001; DB/API без опубликованных портов и без egress. `r2-api` имеет DNS alias `api` **только внутри своей internal сети**, поэтому существующий nginx проксирует `/api/health` в R2, а не в B1. Compose автономный (JSON-нотация YAML 1.2), без базового `compose.yaml`/B1 override.

**STOP** при существующих R2 ресурсах на первой установке (включая прежний R2 из другого каталога), занятом/неизвестном порте, shared volume/network, неизвестной ревизии, недостатке места/RAM, неверном key/env. Ничего не удалять, не переименовывать и не чинить по догадке. Не выполнять `down -v`, prune, reset execute или destructive downgrade. Даже остановка B1 не является rollback: B1 остаётся как был.

## 1. PREFLIGHT и конфигурация — только после нового разрешения

Оператор размещает **одобренную новую SHA этого комплекта**, не просто старую base SHA, в точном новом каталоге. Если каталог/ресурсы уже существуют — STOP и отдельное решение владельца. Ни копирования БД/users/файлов B1, ни автоматического выбора main. На Windows/NAT ничего не настраивать этим комплектом. Нужен Docker Compose v2 с поддержкой `--wait`/`--format json`; отсутствие поддержки — STOP, не устанавливать/обновлять Docker без отдельного допуска.

В новой Bash-сессии Ubuntu (Docker без sudo, Python 3):

```bash
set -euo pipefail
cd /home/arm2admin/arm2-uat/r2-release
umask 077
ENV_FILE="$HOME/.config/arm2-r2-uat.env"
# Создать приватный файл из env.example только если его ещё нет; существующий не перезаписывать.
test ! -e "$ENV_FILE"
mkdir -p "$HOME/.config"
cp deploy/arm2-r2-uat/env.example "$ENV_FILE"
chmod 600 "$ENV_FILE"
```

Локально сгенерировать **два независимых** `secrets.token_hex(32)` и записать непосредственно в файл приватным редактором/локальным скриптом, без вывода в терминал/логи, истории shell и Git. `R2_SOURCE_SHA` = `git rev-parse HEAD` проверенного одобренного checkout. Выбрать кандидат `R2_HTTP_PORT` (не 8080), `APP_ORIGIN=http://127.0.0.1:<тот же порт>`; preflight проверит Docker bindings и временным bind проверит отсутствие host listener. Проверка порта не резервирует его: если он занят между preflight и start, Docker должен отказать; STOP без изменения чужого listener. Указанный в тестах 18093 **не назначается как свободный**. Key/password нельзя брать из B1. `.env` в корне не создавать; env-файл не source/export. Все токены должны оставаться пустыми.

```bash
# Все дальнейшие Compose-команды строго через эту функцию и один файл.
dc() { env -i PATH="$PATH" HOME="$HOME" docker compose \
  --env-file "$ENV_FILE" -p arm2-ubuntu-uat-r2 -f compose.arm2-r2-uat.yaml "$@"; }
python3 deploy/arm2-r2-uat/preflight.py --env-file "$ENV_FILE" --phase fresh
dc config --quiet
```

GO означает проверенную **подготовку**, не установку: конкретный каталог, чистый git SHA, env 0600, локальный Docker socket, минимум 2 GiB **доступной** RAM, 15 GiB свободного места на checkout и Docker filesystem. Это минимальный порог: сборка Torch/OCR может потребовать больше; при нехватке STOP, не менять VM/диск самостоятельно. Не печатать `dc config` без `--quiet`: он содержит секреты. Не публиковать `docker inspect` выбранного API целиком.

## 2. BUILD → CREATE FRESH DB → MIGRATE → ADMIN

```bash
dc build r2-api r2-web
# Повторная проверка после сборки: до создания БД/томов всё ещё нужен fresh GO.
python3 deploy/arm2-r2-uat/preflight.py --env-file "$ENV_FILE" --phase fresh
dc up -d --wait r2-db
# UID-подготовка ТОЛЬКО новых двух R2 volumes из этого Compose.
# --rm: не оставляет четвёртого stopped compose-контейнера.
dc run --rm --no-deps --user 0:0 --entrypoint python r2-api -c \
  "import os; [os.chown(p,10001,10001) for p in ('/files','/data')]; [os.chmod(p,0o700) for p in ('/files','/data')]"
# Свежая БД должна иметь ноль пользовательских таблиц: любое другое значение → STOP.
test "$(dc exec -T r2-db psql -XAt -U arm2r2 -d arm2_r2_uat -v ON_ERROR_STOP=1 -c \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' AND table_name<>'spatial_ref_sys'")" = 0
dc run --rm --no-deps r2-api python -m app.cli migrate
test "$(dc exec -T r2-db psql -XAt -U arm2r2 -d arm2_r2_uat -v ON_ERROR_STOP=1 -c \
  'SELECT version_num FROM alembic_version')" = 0070
dc run --rm --no-deps r2-api python -m app.cli create-admin --login r2-operator --name 'R2 UAT operator'
```

Команда create-admin интерактивно дважды запрашивает новый пароль ≥12 символов; не передавать пароль аргументом/через чат. Создаёт ADMIN_OPERATOR штатным CLI, без B1 users. `migrate` использует head данного закреплённого образа; проверка 0070 обязательна. **При любой ошибке миграции STOP**, API/web не поднимать, дальнейшие команды не выполнять. 0070 требует пустой CP: никаких переносов старых карточек и никаких автоматических очисток. Начальный PostGIS создаёт `spatial_ref_sys`, это служебная таблица, не бизнес-наполнение.

Если установка прервана после создания ресурсов, `fresh` закономерно выдаст NO-GO. Не обходить проверку повторным up/migrate/chown. Предъявить оператору конкретный остаток и отдельный план продолжения. Runbook предназначен для первой чистой установки; `ready` — для уже завершённой.

## 3. START → HEALTH / LOGIN / REOPEN

```bash
dc up -d --wait r2-api r2-web
python3 deploy/arm2-r2-uat/preflight.py --env-file "$ENV_FILE" --phase ready
dc ps
PORT=$(sed -n 's/^R2_HTTP_PORT=//p' "$ENV_FILE")
curl --fail --silent --show-error "http://127.0.0.1:$PORT/api/health"
# Проверка той же цепочки nginx → alias api → R2 DB; db/api host ports отсутствуют.
```

Через отдельно разрешённый SSH tunnel открыть `http://127.0.0.1:<локальный tunnel port>/arm2`. Для cookie/origin использовать именно согласованный test-only origin/порт: если tunnel меняет origin, STOP и согласовать эксплуатационную конфигурацию, а не ослаблять CSRF. NAT/Windows forwarding этим заданием не разрешены. Войти `r2-operator`, создать/открыть CP/ORG/VEH, проверить 5 вкладок, историю и V4 XLSX. COOKIE_SECURE=false допустим только для выбранного loopback HTTP UAT; это не production-настройка.

**Ограничение файлового провайдера:** существующий B3/B5 использует `YandexDisk()`, не локальный `/files` provider. Mount `/files` сам по себе не переключает backend. В комплекте нет рабочего токена, provider override или monkeypatch. Internal DB/API network блокирует внешний egress. Поэтому DOC upload/download и V4 ZIP с вложениями не объявлять прошедшими UAT: для них требуется **отдельно разрешённый изолированный тестовый Yandex Disk provider**, его private root, отдельные credentials, backup/version retention и отдельно рассмотренная egress-конфигурация. Рабочий token подключать запрещено. Пустой токен/отказ FILE — ожидаемый STOP для этого участка, не повод включать production-интеграцию. `/data` содержит B5 временные файлы; `/files` сохраняется в backup для целостности выделенного контура, но не заменяет backup внешних оригиналов.

## 4. BACKUP R2 DB + FILE/runtime (согласованный offline cut)

Сначала `ready` GO. На время backup остановить **только R2 HTTP**, БД оставить включённой; другие операторы не должны писать в R2 SQL напрямую. Штатных workers в этом Compose нет. Backup вне checkout/volumes, права 0700/0600; защищает независимая копия на другом носителе после проверки SHA (не VM snapshot). Не отправлять backup в Git/PR.

```bash
python3 deploy/arm2-r2-uat/preflight.py --env-file "$ENV_FILE" --phase ready
BACKUP="$HOME/arm2-r2-backups/$(date -u +%Y%m%dT%H%M%SZ)"
test ! -e "$BACKUP"
mkdir -p "$BACKUP"; chmod 700 "$BACKUP"
dc stop r2-web r2-api
dc exec -T r2-db pg_dump -U arm2r2 -d arm2_r2_uat -Fc > "$BACKUP/database.dump"
dc exec -T r2-db psql -XAt -U arm2r2 -d arm2_r2_uat -v ON_ERROR_STOP=1 -c \
  "SELECT version_num FROM alembic_version; SELECT count(*) FROM users; SELECT count(*) FROM arm2_counterparties; SELECT count(*) FROM arm2_documents; SELECT count(*) FROM arm2_history_events;" > "$BACKUP/sql-state.txt"
API_IMAGE="arm2-r2-api:$(git rev-parse HEAD)"
docker run --rm --network none --user 0:0 \
  --mount type=volume,src=arm2-ubuntu-uat-r2_r2_file_data,dst=/files,readonly \
  --mount type=volume,src=arm2-ubuntu-uat-r2_r2_photo_data,dst=/data,readonly \
  --entrypoint python "$API_IMAGE" -c \
  "import sys,tarfile; t=tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz'); t.add('/files',arcname='files'); t.add('/data',arcname='data'); t.close()" > "$BACKUP/volumes.tar.gz"
(cd "$BACKUP" && sha256sum database.dump volumes.tar.gz sql-state.txt > SHA256SUMS && sha256sum -c SHA256SUMS)
dc start --wait r2-api r2-web
python3 deploy/arm2-r2-uat/preflight.py --env-file "$ENV_FILE" --phase ready
```

Если backup оборвался — STOP, каталог пометить неполным и не использовать; R2 HTTP может оставаться offline. После адресной проверки оператор решает возобновление. Не писать backup в тот же Docker volume. Приватный env/signing key хранить отдельно в разрешённом защищённом backup конфигурации; не печатать и не включать в публичный отчёт. Внешние FILE-оригиналы требуют согласованного независимого provider backup; без него этот backup полон только для отключённого локального контура.

## 5. RESTORE smoke — исключительно scratch, не поверх R2/B1

После backup проверить SHA и восстановить в **новые явно scratch** ресурсы без сети/портов. Никакой миграции, API или пользователя B1. Это временная проверка дампа и побайтового FILE/runtime restore, а не операторская сдача B3. Проверить дополнительное свободное место минимум размера backup+восстановленных данных перед запуском.

```bash
(cd "$BACKUP" && sha256sum -c SHA256SUMS)
SCRATCH="arm2-r2-restore-$(date -u +%Y%m%dT%H%M%SZ)"
for suffix in pg files data; do
  ! docker volume inspect "$SCRATCH-$suffix" >/dev/null 2>&1
  docker volume create "$SCRATCH-$suffix" >/dev/null
done
docker run -d --rm --name "$SCRATCH-db" --network none \
  -e POSTGRES_DB=arm2_r2_uat -e POSTGRES_USER=arm2r2 -e POSTGRES_HOST_AUTH_METHOD=trust \
  --mount type=volume,src="$SCRATCH-pg",dst=/var/lib/postgresql/data postgis/postgis:16-3.5
# Trust только в network=none scratch, никогда не для R2/B1.
for attempt in $(seq 1 60); do
  if docker exec "$SCRATCH-db" pg_isready -U arm2r2 -d arm2_r2_uat >/dev/null; then break; fi
  sleep 1
done
docker exec "$SCRATCH-db" pg_isready -U arm2r2 -d arm2_r2_uat
docker exec -i "$SCRATCH-db" pg_restore --exit-on-error --no-owner -U arm2r2 -d arm2_r2_uat < "$BACKUP/database.dump"
docker exec "$SCRATCH-db" psql -XAt -U arm2r2 -d arm2_r2_uat -v ON_ERROR_STOP=1 -c \
  "SELECT version_num FROM alembic_version; SELECT count(*) FROM users; SELECT count(*) FROM arm2_counterparties; SELECT count(*) FROM arm2_documents; SELECT count(*) FROM arm2_history_events;" > "$BACKUP/restored-sql-state.txt"
cmp "$BACKUP/sql-state.txt" "$BACKUP/restored-sql-state.txt"
docker run --rm -i --network none --user 0:0 \
  --mount type=volume,src="$SCRATCH-files",dst=/files \
  --mount type=volume,src="$SCRATCH-data",dst=/data \
  --entrypoint python "$API_IMAGE" -c '
import hashlib,io,os,sys,tarfile
t=tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()),mode="r:gz")
members=t.getmembers()
assert all(m.name.split("/")[0] in {"files","data"} and ".." not in m.name.split("/") and (m.isdir() or m.isfile()) for m in members)
t.extractall("/",members=members,filter="data")
for m in members:
    if m.isfile():
        path="/"+m.name
        assert hashlib.file_digest(t.extractfile(m),"sha256").digest()==hashlib.file_digest(open(path,"rb"),"sha256").digest()
for root in ("/files","/data"):
    for folder,dirs,files in os.walk(root):
        for path in [folder,*[os.path.join(folder,f) for f in files]]:
            os.chown(path,10001,10001)
print("scratch FILE/runtime byte verification PASS")' < "$BACKUP/volumes.tar.gz"
```

Если restore/сравнение не прошло — backup **не считается восстановимым**, STOP, сохранить диагностику приватно. Существующие R2/B1 volumes не являются destination ни одной restore-команды. После успешной проверки scratch можно удалить только явными именами:

```bash
[[ "$SCRATCH" =~ ^arm2-r2-restore-[0-9]{8}T[0-9]{6}Z$ ]]
docker stop "$SCRATCH-db"  # --rm удалит только этот transient container
docker volume rm "$SCRATCH-pg" "$SCRATCH-files" "$SCRATCH-data"
```

## 6. RESET DRY-RUN → STOP/START → повторное открытие

**В этом задании команда не запускалась даже в dry-run на VM.** После отдельного допуска и `ready` GO:

```bash
python3 backend/tools/arm2_uat_reset.py --project arm2-ubuntu-uat-r2
# Без --execute! Проверить DB/FILE identities и counts; данные остаются прежними.
dc stop r2-web r2-api
dc stop r2-db
dc start --wait r2-db
dc start --wait r2-api r2-web
python3 deploy/arm2-r2-uat/preflight.py --env-file "$ENV_FILE" --phase ready
curl --fail --silent --show-error "http://127.0.0.1:$PORT/api/health"
```

Повторно войти тем же R2 пользователем, открыть созданные CP/ORG/VEH, версии/историю и серверный B5 draft. Сравнить до/после: reset plan и stop/start ничего не удаляют. Не обходить отказ reset guard и не менять предметный reset-код. FILE-проверка ограничена отдельно допущенным провайдером.

Rollback первой установки: `dc stop r2-web r2-api r2-db`; сохранить новые R2 volumes/логи для разбора. **B1 не останавливать, не переключать и не мигрировать**, его прежняя доступность сохраняется. Никаких установленных результатов или Ubuntu UAT PASS в отчёте этого PR нет.

## Локальные адресные проверки комплекта

```sh
python -m unittest discover -s deploy/arm2-r2-uat -p 'test_*.py' -v
ruff check deploy/arm2-r2-uat
```

Результат автора: **10 unit-тестов PASS**, включая отрицательные подслучаи; **Ruff PASS**; **8 Bash-блоков RUNBOOK прошли `bash -n`**, без выполнения команд; `git diff --check` PASS.

Проверка реальным `docker compose ... config --quiet` и disposable Docker smoke выполняются только при наличии локального изолированного Docker. На компьютере автора Docker CLI не обнаружен: **NOT RUN**, не PASS. Unit-тесты используют синтетическую env и mock Docker state, не подключаются к VM/БД и не запускают reset. Состав и секреты проверяются отдельно; это PREPARED-комплект, не эксплуатационный допуск.

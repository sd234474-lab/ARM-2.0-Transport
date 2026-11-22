# Issue #87 — D: платформенное имя файла тарировки

База: Draft PR [#86](https://github.com/sd234474-lab/ARM-2.0-Transport/pull/86),
`db66dfd78150ff7b77c10672b405c36ef14a9f6e`, поверх #84.
Remote/base/head повторно сверены; #86 не изменялся. Ветка D:
`codex/arm2-doc-name-d-issue87`. Основание — актуальная редакция
[Issue #87](https://github.com/sd234474-lab/ARM-2.0-Transport/issues/87),
поздние уточнения DOC-036 от 22.11.2026.

## Карта требований и фактический call graph

| DOC-036 / требование | Общий механизм и реальные потребители | Параметры | Доказательство |
|---|---|---|---|
| DOC.FILE.NAME-01, R01/R02 — настройка | `DocumentKinds.tsx` → schemas → `document_kind_store.Adapter.normalize` → `document_file_storage.validate_naming_template`; ORM и 0073 дублируют защиту на уровне SQL | Закрытый `VEH_CALIBRATION`, только «Тарировка», только `['VEH']`; прежние два пресета сохранены | UI настройка/повторное открытие; API create/update несовместимых CP/ORG/VEH; migration test |
| VEH.F02/F03/F08, NAME.R01/R02 — источники | `document_file_storage.safe_name`: закрытый каталог зарегистрированных полей; общий `vehicle_models.calibration_matches_document`, также используемый существующим approval eligibility | `VEH.plate_number`, положительный конечный `calibration_m3`, подтверждённый для этого DOCUMENT, текущей revision и номера; никогда B5 proposal или `VEH.name` | До подтверждения; 25 → 27.5; устаревший proof; нуль/отрицательное/NaN; новая revision; свободное имя машины |
| DOC.FILE.NAME / B3 | `read_file` и `bundles` → `document_file_store.export_name` → `safe_name`; B3 prepare → тот же `safe_name` с планируемой revision | FILE-ID, фактическое расширение, текущие реквизиты; `download_name` отделён от immutable `stored_name` | API preview/download и SHA, metadata; browser smoke |
| VIEW / все потребители | DOCUMENT/Calibration → общая preview page → `previewBytes` → текущее безопасное имя из Content-Disposition → прежний общий viewer | Серверное имя File/Blob, без клиентской маски; неизменные authenticated fetch, byte bounds, MIME/security | FilePreviewPage, fileNamingTransport, Documents tests; PDF canvas в отдельной вкладке |
| B5 / DOC.MULTIFILE.R01–R03 | `questionnaire_store.export` → `export_name` → `safe_name`; новые принятые B5 FILE → `safe_name` с непотверждённым объёмом | Каждый FILE отдельно; защищённые manifest parent/DOCUMENT/revision/FILE/SHA + фактически выданное имя | Многофайловый ZIP, обратный импорт no-op, отсутствие новой revision/дублей; прежние B5 tests |
| Процесс Issue #85 | Выпуск нового пакета: `approval_store.issue_letter` → `export_name` → `approval_letter.render_package`; одна библиотека имени | Имена справок из текущего подтверждения; письмо/контракты/решения прежние | Новый ZIP содержит 25м3; после 27м3 ранее выпущенный ZIP и request неизменны |
| DOC.FILE.STORE.R01, ID/VER/HIST | Исходные записи FILE и физические storage paths не переписываются; naming не вызывает SAVE | `stored_name`, `original_upload_name`, `storage_ref`, revision/FILE-ID, size/SHA | Полный SQL snapshot FILE до/после подтверждения/смены имени; восстановление SQL + FILE/runtime |
| ACCESS/VALREL | Существующие document_access / permission checks и DOC-KIND SAVE; до provider/download | Отзыв прав не обходится новым именем; новая применимость проверяется на create/update | Отказ 403; 422 для несовместимого шаблона; общая регрессия |

Единственная библиотека форматирования — `document_file_storage.safe_name`.
`export_name` только загружает актуальный DOC-KIND/родителя и передаёт параметры.
В B5, viewer и Calibration нет второго форматтера имени.

## Поведение и исторические границы

Пример выдачи: `Тарировка Е679РН186 25м3-FILE-000123.pdf`.
Дробный объём использует прежнюю безопасную нормализацию разделителей:
`27.5` → `27_5м3`. Даты не добавляются. Для действительно отсутствующего
параметра всех пресетов используется общий `НЕТ ДАННЫХ`.

Положительный объём доступен только при совпадении сохранённого proof с
этим DOCUMENT/его текущей revision/госномером. Для новой staging/revision
или исторической revision, которую текущий proof не подтверждает, выводится
`НЕТ ДАННЫХ`; файл не маркируется чужим актуальным объёмом. Это не меняет
операцию подтверждения и решения заказчика Issue #85.

При смене объёма/имени родителя меняется только следующая выдача имени.
Ранее созданный B5 ZIP и уже выпущенный пакет согласования остаются
историческими байтами и именами. Их скачивание не пересобирает архив.
Письма и ответы процесса сохраняют своё зафиксированное FILE-имя.
Не принятый ещё исходный кандидат анкеты сохраняет имя поступившего вложения:
у него ещё нет подтверждённого DOCUMENT/DOC-KIND/FILE-контекста.

Обратная привязка B5 продолжает использовать подписанный исходный manifest,
а не вновь вычисленное имя. Сравнение подписанных канонических строк переведено
в UTF-8 bytes: `hmac.compare_digest(str, str)` отклонял кириллицу. Ключ,
HMAC, состав manifest, проверка родителя/FILE/SHA не изменены и не ослаблены.

## Миграция и сохранность

**0073 после 0072 необходима**: существующий SQL CHECK разрешал только два
пресета. Новая миграция расширяет CHECK одним зарегистрированным вариантом
и добавляет проверку его применимости. Новых колонок/таблиц нет, DML нет.
0061–0072 не изменялись. Настройки существующих видов не переписываются:
новый вариант явно выбирается в DOC-KIND.

Downgrade 0073 → 0072 допустим на чистом/неиспользованном расширении и
fail-closed, если новый пресет есть в настройке либо истории. Ничего не удаляется
для искусственного прохождения downgrade. Миграционные тесты прежнего стека
сохраняют проверки данных, обновлено только ожидание единственного head 0073.
Прежний offline reset не расширялся.

## Авторские проверки

- Новые backend D tests: **26 PASS** (25 + адресная проверка явной B5-привязки
  к VEH без строки техники), включая реальный pg_dump/pg_restore
  с matching-major PostgreSQL clients, FILE/runtime и продолжение сохранения ответа.
  После restore сравниваются DOC-KIND, VEH proof, FILE и HISTORY.
- Frontend: **501 PASS**, обе production builds PASS; отдельный повторный прогон
  49 тестов изменённых UI/transport потребителей PASS.
- Ruff backend и `git diff --check`: PASS.
- Прежние lifecycle tests CP/ORG/VEH дополнены разделением immutable history
  и нового вычисляемого `download_name`. Сравниваются все прежние поля редакции;
  дополнительно проверяются текущее имя из metadata/Content-Disposition и байты
  каждого скачивания после RETYPE/MOVE. Тот же контракт проверяют I05 и legacy
  retype. Оба затронутых модуля целиком: **17 PASS**.
- Локальный Edge/Playwright smoke: PASS. Синтетическая загрузка PDF → новая revision
  с `НЕТ ДАННЫХ` → подтверждение оператором 25 м³ → общий предпросмотр в отдельной
  вкладке с PDF canvas → скачивание `Тарировка Е679РН186 25м3-FILE-000004.pdf`.
  Исходное имя, FILE-ID, SHA и размер неизменны; JS errors: 0.
- Общая регрессия ARM2 и полный GitHub CI фиксируются в Draft PR после завершения.

Все локальные данные синтетические, отдельные одноразовые PostgreSQL DB и
файловый провайдер; реальные анкеты, секреты и `.local` не входят в коммит.

**CONFIRMED FIX:** платформенный preset, общие текущие download/export имена,
UI/preview transport и Unicode signed roundtrip.
**NO CHANGE:** предметный процесс #85, два номера контрактов, права,
исторические FILE/ZIP, физические оригиналы, старая «Метка», B1 и VM.
**REQUIRES DECISION:** нет.

Нет merge/deploy и обращений к Ubuntu R2. Передаётся стек #84 → #86 → D
для отдельного разрешения на установку и независимую операторскую приёмку.
**UAT NOT CLAIMED WITHOUT OWNER.**

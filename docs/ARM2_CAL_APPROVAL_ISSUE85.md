# Issue #85 — тарировка и согласование с заказчиком

Это авторский отчёт реализации, не независимая операторская приёмка.
Ubuntu deploy, merge и очистка стенда не выполнялись и этим заданием не разрешены.

## База и границы

- Рабочая ветка: `codex/arm2-cal-approval-issue85`.
- Проверенная база PR #84: `43d5d6d053a8ed759dbf9c19c632ddb91206f33d`.
- Повторная проверка remote перед сдачей: PR #84 остаётся Draft с тем же HEAD.
- Полный CI именно базы: [run 45014823449](https://github.com/sd234474-lab/ARM-2.0-Transport/actions/runs/45014823449), success.
- G0 Ubuntu был только read-only: R2 project `arm2-ubuntu-uat-r2`, каталог
  `/home/arm2admin/arm2-uat/r2-release`, revision `0071`, HEAD базы, порт `18093`.
- Новая миграция: `0072 → 0071`; существующие `0061–0071` не менялись.
- Изменены только ARM2-модули, их миграция, тесты и этот отчёт. ARM1, хостовая
  «Метка», B1, пользовательские репозитории, DOC и REG не редактировались.

## Карта требований и общих механизмов

Рабочий контракт: [Issue #85](https://github.com/sd234474-lab/ARM-2.0-Transport/issues/85),
[DOC-035](https://docs.google.com/document/d/1KvIIBHI2miQe2mWqonZAF5-t5KtKfHx4dwHVo2NilfE/edit),
[DOC-036](https://docs.google.com/spreadsheets/d/1jxj_ATB7pfgdbukaQ5mH2CdsPPisDAxU5ggitO2pPjI/edit),
[DOC-037, «17 Тарировка и согласование»](https://docs.google.com/spreadsheets/d/1R8HtDimUW6pER9kjLxyhAXDljMlaJrwUivBJqOVyEZo/edit#gid=37017).

| Требование | Реализация | Доказательство |
|---|---|---|
| VEH.F08 / CAL-001: положительный nullable объём | `Vehicle.calibration_m3`, read-only response; обычный command whitelist его не содержит | Подтверждение 25; запрет PUT-инъекции; B4/B5 сохраняют все proof-поля |
| VEH.F09 / CAL-002: системная дата | `calibration_confirmed_at`, UTC timestamp, автор `calibration_confirmed_by` | После повторного открытия; no-op не меняет дату/автора/версию |
| DISC-TM-05 / CAL-003/004: явная сверка | На конкретном B3-документе VEH: открыть оригинал, ввести число, подтвердить сверку и команду | Backend + UI тесты; проверяются VEH/DOCUMENT версии, revision и госномер |
| Актуальный источник / CAL-008 | Один явный указатель DOCUMENT/revision/plate в proof, без `find(first)` | 679HOWO/Е679РН186, 25 м³, два документа: письмо использует подтверждённый источник |
| Недоступный оригинал | B3 `download` проверяет реальные байты, размер и SHA; readiness использует `verify_remote` | Недоступный источник блокирует письмо; отказ без частичного SAVE |
| VEH.R02 / CAL-008 | Одна `approval_store.readiness` для карточки, списка и выпуска; TYPE.requires_calibration | `work_readiness_complete=false`; полная готовность СТС/Wialon не заявляется |
| DISC-TM-08 / CAL-005/012/014 | Два обязательных номера в шапке одного PDF; одна APPROVAL и per-VEH APPROVAL-ITEM | Незаполненный номер отклоняется до публикации; реальный PDF в браузере |
| Нет зависимости от Contract | Номера — строки исторического снимка | Нет Contract-модели, FK или `contract_ids` |
| Решение по характеристикам | Снимок госномера + подтверждённого объёма; источник ответа, автор и время | Ответ V1 APPROVED / V2 REJECTED; изменение объёма/номера не наследует решение |
| Редакции PDF | Новая редакция не подтверждает число автоматически и не стирает customer evidence | Новый PDF той же пары сохраняет APPROVED, но требует явного подтверждения актуальной редакции для нового письма |
| Общие ID/SAVE/VER/HIST/VALREL | Registry/adapters для APPROVAL и APPROVAL-ITEM; общее `create_object/update_object` | Одна внешняя SQL-транзакция; fault injection HISTORY откатывает заявку, решения и FILE |
| Серверные поля в общем SAVE | Отдельный явный `server_fields/server_values` контракт, не обычные входные поля | Общее ядро по-прежнему проверяет principal, context, версии, связи и историю |
| Общий FILE/STORAGE | Те же `DocumentFile`, глобальные FILE-ID, private runtime, SHA, provider, safe_name и protected_response | Оригиналы открываются/скачиваются после reload и SQL+FILE/runtime restore |
| ZIP только исходящий | `generated_bundle=true` допустим только для серверного process FILE; обычный документный маршрут не изменяет content policy | Вложенный ZIP и неподдерживаемый ответ отклоняются; прежние B3-потребители регрессированы |
| Никакого нового именователя / D #83 | Существующий preset DOCUMENT_NAME и `safe_name`; FILE-ID в имени | DOC-KIND `file_naming_fields`/новые шаблоны не вводились |
| Общий preview | Узкая allowlist новых protected endpoints; прежние FilePreviewPage/FilePreview | Браузерный smoke: отдельная вкладка, реальный PDF canvas и PNG ответа, без iframe и второго renderer |
| Ограничение HTTP | Общий `bounded_route`: сырой ответ максимум 2 МиБ; PDF/JPG/PNG | Адресный 413; нет base64/multipart overhead и новых nginx-исключений; B5 32m сохранён |
| Общие квоты runtime | `staging_quota` для ручного B3 и процесса; 64 кандидата, прежний общий byte budget | Сохраняется 413; истёкшие временные копии удаляются только из private runtime, ledger/remote originals не удаляются |
| Защита форм и ошибки | `useDraftGuard`, ErrorMessage, общий backend translator; scoped memory cache нового процесса | Повтор с тем же ключом; dirty guard; новый login/workspace не наследует частный parked draft |
| Идемпотентность и сбои | Durable private attempt + общая advisory lease; provider вне бизнес-транзакции; повторная авторизация перед публикацией | Потеря ответа после commit; отказ provider; повтор без дублей; отзыв прав во время upload |
| Безопасный downgrade | 0072 отказывается удалять confirmation/approval/retry evidence | Адресный DB-тест; старые guard-assertions сохранены |

Физическое имя даты выбрано `calibration_confirmed_at`: это момент операторского
подтверждения, не введённая вручную дата справки. Исторический объём сохраняется
при смене госномера, но proof становится неактуальным и в письмо не проходит.

## Операторский маршрут

1. VEH → Документы → конкретная карточка типа «Тарировка». Открыть актуальный
   оригинал общим просмотрщиком, сверить госномер и число, явно подтвердить объём.
2. Техника → Согласование с заказчиком → Новое письмо. Ввести оба номера,
   выбрать допущенные сервером машины и выпустить один PDF/ZIP.
3. Повторно открыть заявку. Приложить PDF/JPG/PNG до 2 МиБ; «Проверить оригинал
   ответа» открывает его общим preview в отдельной вкладке.
4. Явно выбрать решения по нужным строкам; остальные остаются без изменений.
   Сохранить ответ и решения одним набором. История доступна для заявки и решений.
5. Новая редакция справки сама не меняет решение заказчика. Для новой пары
   госномер/объём требуется новое согласование с отдельным доказательством.

Исторический снимок выпущенного письма неизменяем; зарегистрированные ответы
не удаляются. Повтор той же команды возвращает сохранённую квитанцию. Повтор
тех же байтов и решений в той же заявке — no-op без новой редакции и upload.
Неподтверждённые uploads живут в private runtime до двух часов; после истечения
нужна новая явная команда. Техническая provenance сохраняется для разбора сбоя.

## Проверки и доказательства

- Новый backend-набор: синтетический полный цикл, подтверждение/no-op/409/403,
  два источника, смена PDF и характеристик, B4/B5, идемпотентность, storage/HIST
  fault injection, отзыв прав, миграция и downgrade guard.
- Отдельный `pg_dump/pg_restore` тест на новых одноразовых локальных PostgreSQL:
  SQL + копии тестового FILE/runtime → новая БД/каталоги → повторное открытие
  письма и временного ответа → успешное сохранение решения и скачивание.
  Никаких реальных документов, облачных учётных данных или VM-операций.
- Полный frontend: **492 PASS**; TypeScript/Vite production builds PASS;
  Ruff всего backend PASS.
- Авторский browser smoke: реальная локальная API/DB/FakeDisk + Edge/Playwright,
  две машины, оба номера, выпуск/скачивание ZIP, PDF в общей новой вкладке,
  PNG-кандидат, APPROVED/REJECTED, reload, история, read-only объём. JS errors: 0.
  Синтетические снимки: `evidence/issue85/letter.png`, `request.png`, `browser.json`.
- Итог полной backend/CI регрессии фиксируется в описании Draft PR на точной SHA.

Старый offline reset намеренно остаётся fail-closed на `0072`: его разрешённые
ревизии и список из 23 таблиц не расширены на новые доказательства согласования.
Регрессия подтверждает отказ на `0072`, затем на одноразовой БД проверяет прежний
разрешённый контракт `0071` и возвращает схему к `0072` перед дальнейшими API-шагами.
Это не разрешение на reset текущего или будущего стенда.

**BASE/HEAD VERIFIED; PLATFORM REUSE VERIFIED; TWO CONTRACT NUMBERS HEADER VERIFIED;
ONE REQUEST / PER-VEH DECISION VERIFIED; NO CONTRACT FK VERIFIED; B1/PROD UNTOUCHED;
UAT NOT CLAIMED WITHOUT OWNER.**

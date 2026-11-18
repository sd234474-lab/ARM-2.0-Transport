# B5: завершение coding и авторская сквозная проверка

Дата: 18.11.2026. Ветка `codex/arm2-b5-questionnaire`, Draft PR #71.
Исходный HEAD: `9af93a08a25fdd46ce3f74623a9bb2b9d13db5d9`.
Интеграционная база: `ddadebd7be7f5044628fd6b2ee656ca69d823db7`.
Миграционная линия остаётся `0069 → 0068`; миграции не менялись.

**Результат: AUTHOR-RUN PASS.** Кодирование и авторская сквозная сдача B5 завершены.
Это не независимая приёмка, не Ubuntu UAT и не допуск к эксплуатации.
Новых предметных требований, полей, файлового хранилища или SAVE нет.
Продуктовые backend/frontend-модули сохранены без изменений относительно исходного HEAD.

## Что завершено

- Добавлен `backend/tools/arm2_questionnaire_browser.mjs`: три исполняемых режима
  `review`, `accept`, `reopen`. Вход, загрузка, решения, конкурирующий B2, просмотр,
  сохранение, очистка, экспорт, повторный импорт и повторное открытие выполняются
  реальными DOM-действиями Playwright. В скрипте нет API-клиента, подмен ответов,
  внедрения вызовов приложения или прямых HTTP-запросов вместо действий оператора.
  Ответы настоящих UI-запросов читаются для проверок.
- Доведен существующий `arm2_questionnaire_qa.py`: снимки дополнены обеими таблицами
  представителей; отдельно проверены неизменность доступа пользователей, единственность
  разрешённого B2-изменения до SAVE, итоговые VERSION и состав ZIP текущих оригиналов.
  Добавлена сверка порта подключения с `postmaster.pid` собственного QA-кластера.
- Playwright **1.65.1** закреплён отдельными package.json/package-lock.json в
  `backend/tools/qa-browser`. Он не входит в продуктовый runtime.
- Доказанных дефектов продуктового B5, препятствующих этому маршруту, не обнаружено.
  Исправления потребовались QA-оркестратору и браузерному сценарию.

## Фактически пройденный маршрут

1. `review`: нативный вход; ZIP с V2; существующий CP; отдельный новый REP с тем же
   именем, что у старого; существующая, новая, архивная и ошибочная VEH; выбор четырёх
   вложений; PDF-кандидат и действующий PDF открыты в интерфейсе. SAVE без отдельного
   B2-confirmation отклонён с 409. Другой оператор получил 403 на чужой черновик и
   через интерфейс B2 изменил владельца VEH на ORG. Старая сверка отклонена с 409;
   старые одобрения сброшены, решения выбраны заново и открыты после перезагрузки страницы.
   **Успешного B5 SAVE на этой стадии нет.** SQL-сравнение допускает ровно одну
   конкурирующую B2-операцию: только владелец/версия одной VEH и одно событие HIST.
2. Собственная БД выгружена через `pg_dump`, восстановлена в **другую новую** БД;
   скопированы provider и private staging; собственный PostgreSQL реально остановлен
   и запущен. Снимки DB, FILE и staging совпали. Снимки включают CPREP/ORGREP.
3. `accept`: новый backend/frontend/браузер и нативный вход; тот же UUID черновика,
   revision, fingerprint и решения. PDF/PNG/JPEG открыты из восстановленного staging.
   Одна успешная финальная команда записала CP, REP, три VEH и четыре DOCUMENT вместе.
   Receipt содержит **16 событий** с одним correlation ID; финальные версии сверены с БД.
   Принятый комплект очищен через UI. Из карточки CP выгружен ZIP **ARM_IMPORT_V3**:
   пять текущих оригиналов; заменённый исторический PDF в ZIP отсутствует. Проверены
   раздельные ФИО CP в выгрузке. Обратный импорт дал no-op без дублей/новых событий;
   завершённый повторный пакет также очищен через UI.
4. Принятое состояние ещё раз выгружено и восстановлено в третью новую БД с другой
   копией FILE/staging; PostgreSQL ещё раз реально перезапущен. Снимки снова совпали.
5. `reopen`: новый backend/frontend/браузер; активных задач **0**; тот же receipt;
   CP/банковские строки, два самостоятельных контакта и один основной подписант,
   существующая/новая/бывшая архивная VEH открыты в UI. Просмотр события истории
   подтвердил correlation ID. Открыты текущая редакция 2 и прежняя 1; исторический
   PDF скачан кнопкой интерфейса и проверен по SHA-256. Дополнительная проверка
   оркестратора скачала **все шесть** текущих/исторических FILE и сравнила исходные байты.

Неправильная строка не создала VEH; вложенный ZIP не стал FILE/DOCUMENT.
Из шести FILE один является заменённым историческим оригиналом, один переносится
между редакциями без дублирования, четыре приняты этой командой B5.
Связи представителей с пользователями и фактические права пользователей не изменились.

## Воспроизведение только в собственной disposable-среде

Нужны Node.js, backend-dev зависимости из существующего uv.lock, локальный
PostgreSQL/PostGIS с правом создавать тестовые БД и собственный PGDATA **этой копии**
репозитория в `.local/pg-data`. Не запускать одновременно с тестами на этом кластере:
сквозной сценарий дважды перезапускает его. Исходная БД из TEST_DATABASE_URL не изменяется;
создаются три новых `arm2_int_b5_<uuid>_*_test`, существующие БД не перезаписываются и не удаляются.

```bash
uv sync --project backend --frozen
npm ci --prefix frontend
npm ci --prefix backend/tools/qa-browser
npm exec --prefix backend/tools/qa-browser -- playwright install chromium
```

Переменные задаются вне Git:

| Переменная | Назначение |
| --- | --- |
| `TEST_DATABASE_URL` | Только localhost/127.0.0.1, собственный QA-аккаунт и имя с `_test`; порт обязан совпасть с PGDATA |
| `ARM2_QA_PG_BIN` | Каталог `pg_dump`, `pg_restore`, `pg_ctl` |
| `PYTHONPATH` | `backend` при запуске из корня |
| `ARM2_QA_BROWSER_EXECUTABLE` | Необязательный путь к установленному Edge/Chromium; иначе используется браузер Playwright |
| `ARM2_QA_PLAYWRIGHT` | Необязательно; по умолчанию закреплённый пакет в `backend/tools/qa-browser/node_modules/playwright` |

Команда **N**, из корня:

```bash
uv run --project backend python backend/tools/arm2_questionnaire_qa.py --pg-data .local/pg-data
```

Пароль синтетических операторов и signing key генерируются в памяти и передаются
дочерним процессам, не включаются в отчёт. Настоящий облачный provider не используется:
подключён существующий file-backed QA adapter. Дампы, исходный синтетический ZIP,
runtime, provider, логи и временные выгрузки остаются в игнорируемой `.local`.
PASS означает успешные действия и сравнения, а не только запуск процессов.

Фактическая авторская среда: Windows, собственный PostgreSQL 17, headless Microsoft
Edge 157.0.4341.60, Playwright 1.65.1; точные Python/Node-версии находятся в JSON-доказательстве.
Это локальная AUTHOR-RUN, **не Windows/Ubuntu UAT** и не проверка рабочих сред.

## Адресные проверки

Команда **B** — **94 PASS**, включая 26 тестов Excel V2/V3:

```bash
uv run --project backend pytest \
  backend/tests/test_arm2_questionnaire_exchange.py \
  backend/tests/test_arm2_questionnaire_migration.py \
  backend/tests/test_arm2_questionnaire_parse.py \
  backend/tests/test_arm2_questionnaire_workflow.py \
  -q --tb=short
```

```text
uv run --project backend ruff check backend  → PASS
npm test --prefix frontend                  → 66 файлов, 443 PASS
npm run build --prefix frontend             → обе сборки PASS
node --check backend/tools/arm2_questionnaire_browser.mjs → PASS
git diff --check                            → PASS
```

В 94 backend-проверки входят 403/409/422, конкуренция операторов, отказы HIST/SQL/provider,
идемпотентность после потери ответа, миграция 0069 и сохранность ранее реализованного Excel-обмена.
Предыдущий CI исходного HEAD не подменяет CI нового коммита. Новый GitHub CI в сессии
не ожидается: после push его самостоятельно проверяет независимый контролёр.

## T01–T33: выполненные проверки

В таблице **B** обозначает исполненный адресный pytest выше. **N** — дополнение
реальным сквозным маршрутом; оно не означает, что все негативные варианты B заменены
браузерными проверками. Каждый pytest содержит свои проверки данных/FILE/HIST,
версий и повторного открытия; перечень HTTP 200 доказательством не считается.

| Требование | Фактически выполненный тест | Команды | Результат | Подтверждённое конечное состояние |
| --- | --- | --- | --- | --- |
| T01 | [`test_t01_xlsx_no_files_preview_is_private_and_single_cp_field_save`](../backend/tests/test_arm2_questionnaire_workflow.py#L121) | B | PASS | Предпросмотр XLSX приватен; выбранное поле CP, версия, HIST и receipt сохранены; повторное открытие совпадает. |
| T02 | [`test_t02_n_p_bind_to_declared_vehicle_not_physical_filename`](../backend/tests/test_arm2_questionnaire_workflow.py#L134) | B + N | PASS | N/P связываются с VEH своей строки, не с машиной из имени файла; принятые документы имеют нужного родителя. |
| T03 | [`test_t03_unchanged_fields_require_no_acceptance_or_new_history`](../backend/tests/test_arm2_questionnaire_workflow.py#L150) | B + N | PASS | Совпавшие поля не требуют приёмки; версия и HIST не меняются; receipt no-op. |
| T04 | [`test_t04_lost_response_uuid_fingerprint_replays_receipt_without_duplicates`](../backend/tests/test_arm2_questionnaire_workflow.py#L158) | B | PASS | Повтор UUID+fingerprint после потери ответа возвращает тот же receipt без новых объектов/FILE/HIST. |
| T05 | [`test_t05_only_selected_field_changes_other_values_and_history_preserved`](../backend/tests/test_arm2_questionnaire_workflow.py#L168) | B + N | PASS | Изменено только выбранное поле; остальные реквизиты и прежняя история сохранены. |
| T06 | [`test_t06_empty_inn_allowed_duplicate_inn_is_not_a_new_cp`](../backend/tests/test_arm2_questionnaire_workflow.py#L181) | B | PASS | Пустой ИНН допустим; существующий заполненный ИНН не создаёт второй CP. |
| T07 | [`test_t07_legacy_map_formula_hints_no_arbitrary_details_or_zero_guessing`](../backend/tests/test_arm2_questionnaire_workflow.py#L194) | B + N | PASS | V2-паспорт, кеши и нули сохранены; B7 и произвольные details-метки не импортируются. |
| T08 | [`test_t08_identical_representative_names_do_not_merge_or_assign_users`](../backend/tests/test_arm2_questionnaire_workflow.py#L209) | B + N | PASS | Одинаковые имена не сливают REP; IDs различны; user_id и права не назначаются. |
| T09 | [`test_t09_valid_invalid_duplicate_plate_rows_are_explicit`](../backend/tests/test_arm2_questionnaire_workflow.py#L223) | B + N | PASS | Корректная техника сопоставлена по нормализованному номеру; невалидная/повторная строка не создаёт VEH-ID. |
| T10 | [`test_t10_unknown_type_explicit_without_type_and_confirmed_category`](../backend/tests/test_arm2_questionnaire_workflow.py#L233) | B | PASS | Неизвестный TYPE требует выбора CONFIRMED либо явного решения без TYPE; категория номера проверяется. |
| T11 | [`test_t11_archive_reactivation_is_an_explicit_field_not_readiness`](../backend/tests/test_arm2_questionnaire_workflow.py#L249) | B + N | PASS | Архивная VEH возвращена только по принятому предложению; ID сохранён, готовность не назначена. |
| T12 | [`test_t12_owner_reassignment_requires_b2_confirmation`](../backend/tests/test_arm2_questionnaire_workflow.py#L264) | B + N | PASS | Смена владельца требует B2-confirmation; отказ не меняет выбранный набор; после подтверждения сохраняется тот же VEH. |
| T13 | [`test_t13_withdrawal_only_detaches_current_declarant_never_archives`](../backend/tests/test_arm2_questionnaire_workflow.py#L278) | B | PASS | ВЫБЫЛО снимает связь только действующего отправителя; чужой владелец и архивность не изменяются. |
| T14 | [`test_t14_missing_ambiguous_file_does_not_guess_parent_or_block_other_fields`](../backend/tests/test_arm2_questionnaire_workflow.py#L292) | B | PASS | Отсутствующее/неоднозначное вложение не получает угаданного родителя; независимое поле можно принять. |
| T15 | [`test_t15_company_card_and_unbound_file_require_selected_parents`](../backend/tests/test_arm2_questionnaire_workflow.py#L303) | B + N | PASS | Карточка предприятия привязана к CP; непривязанный файл требует допустимого родителя и вида. |
| T16 | [`test_t16_signed_document_parent_identity_and_sha_return_is_noop`](../backend/tests/test_arm2_questionnaire_workflow.py#L328) | B + N | PASS | Проверены защищённые parent/DOCUMENT/FILE и SHA; идентичный возврат не создаёт редакцию. |
| T17 | [`test_t17_changed_file_new_revision_preserves_other_files_and_old_bytes`](../backend/tests/test_arm2_questionnaire_workflow.py#L339) | B + N | PASS | Новая редакция сохраняет комплект, переносимый FILE и прежние байты; исторический оригинал доступен. |
| T18 | [`test_t18_missing_attachment_never_removes_accepted_original`](../backend/tests/test_arm2_questionnaire_workflow.py#L365) | B | PASS | Отсутствие вложения не удаляет принятый FILE, DOCUMENT или его связь. |
| T19 | [`test_t19_volume_is_review_only_not_a_vehicle_write`](../backend/tests/test_arm2_questionnaire_workflow.py#L374) | B + N | PASS | Предложенный объём остаётся сведением для сверки; прямой записи результата тарировки нет. |
| T20 | [`test_t20_file_acceptance_does_not_invent_or_clear_vehicle_approval`](../backend/tests/test_arm2_questionnaire_workflow.py#L383) | B | PASS | Приёмка файла не имитирует и не сбрасывает согласование характеристик. |
| T21 | [`test_t21_three_valid_and_one_unselected_error_are_one_atomic_set`](../backend/tests/test_arm2_questionnaire_workflow.py#L393) | B + N | PASS | Три корректные выбранные строки сохранены одним атомарным набором; четвёртая ошибочная не создана. |
| T22 | [`test_t22_rejected_proposal_does_not_change_object`](../backend/tests/test_arm2_questionnaire_workflow.py#L402) | B | PASS | Отклонённое поле не изменяет карточку, VERSION или HIST. |
| T23 | [`test_t23_cancel_deletes_private_payload_and_temp_files_not_domain_objects`](../backend/tests/test_arm2_questionnaire_workflow.py#L410) | B | PASS | Отмена очищает частный пакет и временные байты; действующие данные/принятые оригиналы не меняются. |
| T24 | [`test_t24_selected_failure_rolls_back_whole_set_and_retries_private_upload`](../backend/tests/test_arm2_questionnaire_workflow.py#L424)<br>[`test_t24_stale_version_and_revocation_do_not_apply_any_selected_change`](../backend/tests/test_arm2_questionnaire_workflow.py#L448) | B + N | PASS | HIST/SQL/provider failure откатывают весь набор; повтор работает; устаревшая версия/отозванные права дают отказ без частичной записи. |
| T25 | [`test_t25_server_draft_reopens_in_new_authenticated_session`](../backend/tests/test_arm2_questionnaire_workflow.py#L467) | B + N | PASS | Серверные решения открыты в новой сессии; native QA дополнительно восстановил DB+FILE+staging после настоящего restart. |
| T26 | [`test_t26_export_contains_all_vehicle_ids_without_other_cp_requisites`](../backend/tests/test_arm2_questionnaire_workflow.py#L480) | B | PASS | Каталог включает полный разрешённый парк, архивные/без владельца; чужие реквизиты CP не раскрыты. |
| T27 | [`test_t27_missing_current_file_requires_explicit_incomplete_export_reason`](../backend/tests/test_arm2_questionnaire_workflow.py#L497) | B | PASS | Недоступный актуальный файл требует явной причины неполного ZIP; автор/пропуски записаны. |
| T28 | [`test_t28_three_dates_old_questionnaire_is_not_rejected`](../backend/tests/test_arm2_questionnaire_workflow.py#L513) | B + N | PASS | Различаются дата заполнения, загрузки и приёмки; возраст анкеты не блокирует полезное изменение. |
| T29 | [`test_t29_historic_review_type_link_is_preserved_and_not_newly_assigned`](../backend/tests/test_arm2_questionnaire_workflow.py#L522) | B | PASS | Историческая REVIEW-связь сохранена; новое назначение требует CONFIRMED и совместимой категории. |
| T30 | [`test_t30_other_author_has_no_draft_file_or_receipt_access`](../backend/tests/test_arm2_questionnaire_workflow.py#L536) | B + N | PASS | Чужой автор не получает черновик, staged FILE или receipt; native rival также получил 403. |
| T31 | [`test_t31_zip_transport_is_never_a_document_or_file`](../backend/tests/test_arm2_questionnaire_workflow.py#L553) | B + N | PASS | Транспортный ZIP не стал DOCUMENT/FILE; при завершении payload и временные байты очищены. |
| T32 | [`test_t32_nested_zip_is_unsupported_attachment_not_extracted`](../backend/tests/test_arm2_questionnaire_workflow.py#L562) | B + N | PASS | Вложенный ZIP показан как неподдерживаемый; не распакован и не опубликован как FILE/DOCUMENT. |
| T33 | [`test_t33_verified_pdf_jpg_jpeg_png_preview_and_accept`](../backend/tests/test_arm2_questionnaire_workflow.py#L574) | B + N | PASS | Проверены PDF/JPG/JPEG/PNG: фактический тип, просмотр и приёмка; native PDF/PNG/JPEG открыты после восстановления staging. |

## Доказательства и границы сдачи

[Обезличенный результат](evidence/arm2-b5-author-run/result.json) содержит receipt,
выбранные решения, результаты фаз, хеши dump/snapshot/FILE и исходного кода сценария.
В той же папке сохранены только успешные синтетические скриншоты: конкурирующий B2,
решения, receipt, восстановленные PDF/PNG/JPEG, no-op, представитель и история B3.
Пароли, cookies, signing key, дампы БД, исходные анкеты и оригиналы документов не опубликованы.

Технических блокеров авторской сдачи не осталось. Draft PR #71 остаётся unmerged;
merge/rebase/deploy не выполнялись, DOC/REG, ARM1, установленная «Метка», UAT/PROD
и рабочие базы не затрагивались. Независимая проверка и решение о допуске на
изолированную Ubuntu UAT выполняются после этой авторской сдачи.

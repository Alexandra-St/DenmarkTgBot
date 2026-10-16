# Проверка классификации на реальных публикациях

**Дата выборки:** 16.10.2026. Выполнены только GET-запросы к официальным источникам; новые запросы к лентам после снятия выборки не выполнялись. Проверены два настроенных ATOM feed-а Høringsportalen и две конкретные записи ODA по ID. Сырые ответы не сохранялись.

## Результат

Снятые данные дали **3 DIRECT, 10 POTENTIAL, 14 IRRELEVANT** (27 публикаций). Первичную классификацию снимка выполнили на ruleset `1.0`; после этого ruleset повышен до `1.1`. Проверенные изменения не меняют эти 27 категорий: в выборке нет конфликта прямого совпадения только в документе, а неполнота ODA влияет на блокировку уведомлений, не на категорию. Сырые ответы не сохранялись, поэтому повторный прогон полного снимка после изменения правил невозможен. Это тематическая сортировка, не вывод о принятии закона, его вступлении в силу или влиянии на конкретного человека.

### Høringsportalen

Каждый из двух feed-ов вернул HTTP 200, содержал 25 записей, все 25 были разобраны, временные метки присутствовали. После объединения получены 25 уникальных публикаций; 25 повторов между двумя выборками объединены по ID. ATOM не объявляет полноту извлечённого `content_text`, поэтому для всех этих записей полнота исходного материала **не установлена**.

| ID | Краткое название | Результат / совпавшие правила |
| --- | --- | --- |
| 71507 | Høring … Ukrainesærloven | DIRECT — `direct.ukraine_special_law`, `direct.displaced_from_ukraine` |
| 71259 | Forslag til ændring af Ukraine særloven | DIRECT — `direct.ukraine_special_law` |
| 71099 | Forslag til lov om ændring af Ukrainesærloven | DIRECT — `direct.ukraine_special_law` |
| 71024 | Udlændingeloven, hjemrejse og udvisning | POTENTIAL — `potential.residence_immigration_law` |
| 70923 | Udlændingeloven; ordning for certificerede virksomheder | POTENTIAL — `potential.residence_immigration_law` |
| 71061 | Udlændingeloven; særlige rejsedokumenter | POTENTIAL — `potential.residence_immigration_law` |
| 70965 | Udlændingeloven, integrationsloven og indfødsret | POTENTIAL — `potential.residence_immigration_law`, `potential.integration_targeted` |
| 70916 | Udlændingeloven og EU’s migrations- og asylpagt | POTENTIAL — `potential.residence_immigration_law` |
| 70331 | Hjemrejse, udlændinge og øvrige love | POTENTIAL — `potential.residence_immigration_law`, `potential.housing_displaced_population`; evidence in title/content |
| 70332 | Udlændingeloven, CPR og øvrige love | POTENTIAL — `potential.residence_immigration_law` |
| 70355 | Udlændingeloven, hjemrejse og EU’s migrations- og asylpagt | POTENTIAL — `potential.residence_immigration_law` |
| 69593 | Udlændinge-, integrations- og danskuddannelseslove | POTENTIAL — `potential.residence_immigration_law`, `potential.integration_targeted`, `potential.family_reunification` |
| 69564 | Udlændingeloven og udvisning | POTENTIAL — `potential.residence_immigration_law` |
| 71554 | Religious preachers; Danish civics course | IRRELEVANT — no configured rule matched |
| 71473 | Criminal sentence at monitored accommodation facility | IRRELEVANT — no configured rule matched |
| 71472 | Electronic monitoring | IRRELEVANT — no configured rule matched |
| 71401 | Repatriation support | IRRELEVANT — no configured rule matched |
| 71402 | Repatriation supplement for MENAPT origins | IRRELEVANT — no configured rule matched |
| 71400 | Repatriation support for departure to Afghanistan/Syria | IRRELEVANT — no configured rule matched |
| 70764 | Amendment to immigration regulation | IRRELEVANT — no configured rule matched |
| 70657 | Danish tests for adult foreigners | IRRELEVANT — no configured rule matched |
| 70371 | Amendment to immigration regulation | IRRELEVANT — no configured rule matched |
| 70191 | Foreigners’ access to the Faroe Islands | IRRELEVANT — no configured rule matched |
| 70190 | Greenland aviation carriers and border control | IRRELEVANT — no configured rule matched |
| 70125 | EU residence order | IRRELEVANT — no configured rule matched |

### Folketinget ODA

Запрошены только два известных `Sag` ID и связанные документы, с ограничением `$top=20` для связей. Эти примеры намеренно считаются неполными: полная гидратация этапов, статусов и всех связей не выполнялась. Оба классифицировались IRRELEVANT по доступным заголовкам; у результата `notification_blocked: true`, `notify_eligible: false`.

| Sag ID | Заголовок дела | Наблюдавшиеся документы | Результат |
| --- | --- | --- | --- |
| 106028 | Forslag til lov om ændring af lov om leje (digital kommunikation om særligt bebyrdende meddelelser) | 5 связанных записей; заголовки о представлении министра, консультационном ответе, запросе LLO и парламентских вопросах | IRRELEVANT; нет совпавших правил; неполная выборка |
| 71032 | Forslag til lov om ændring af elforsyningsloven og lov om fremme af vedvarende energi (kapacitet i elnettet) | 20 записей в пределах `$top=20`; заголовки о сети, центрах обработки данных, вопросах министру и временной шкале | IRRELEVANT; нет совпавших правил; ограниченная выборка |

## Что показала проверка и изменения

- Найденная проблема: прямое Ukraine-специфичное упоминание только в заголовке связанного документа могло сочетаться с явно посторонним заголовком дела и теряться как обычное подавленное совпадение. Теперь такой конфликт даёт `POTENTIAL` для ручной проверки, сохраняет положительные и отрицательные rule ID и структурированную диагностику `conflicting_context`. Прямое совпадение в основном заголовке по-прежнему имеет приоритет.
- Ограниченная ODA выборка не должна становиться основанием для уведомления. При `complete: false` классификация остаётся видимой, добавляется `incomplete_source_data`, а уведомление блокируется и `notify_eligible` становится `false`. Отсутствующее поле `complete` не считается доказательством неполноты.
- Широкие упоминания `udlændingeloven` дали 10 POTENTIAL на этом малом срезе. Это консервативное правило с риском лишней ручной проверки; выборка не даёт обоснования для сужения без пропуска потенциально затрагивающих положений.
- Ложноотрицательные кандидаты для последующего экспертного пересмотра: 70764 и 70371 (название говорит об иммиграционном регулировании, но не совпало с текущими выражениями о проживании); 71473 (объект размещения/контроля, но правило не подтвердилось по извлечённому тексту); 70191 (специфичный для Фарер вопрос). Это наблюдения для будущей проверки, а не основание автоматически менять категории.
- У ATOM краткий HTML-content и нет стандартного поля полноты/версии полной карточки; у двух ODA записей намеренно не загружены все связанные данные. Поэтому аудит не подтверждает полноту корпуса или окончательную правовую релевантность.

## Воспроизводимость и ограничения

Результаты относятся к записанным ответам и текущим правилам, а не к текущему состоянию источников. Выборка — 25 записей из двух ограниченных 25-элементных ATOM feed-ов плюс два точечных ODA кейса, не случайная и не статистически представительная. Feed-ы могут пересекаться и не содержат пагинации. Сведения о полном тексте консультаций и всех связях ODA не извлекались. Реальные ID и краткие выдержки приведены для аудита; примеры unit-тестов остаются синтетическими.

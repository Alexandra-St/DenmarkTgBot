# Исследование официальных источников

**Дата проверки:** 16.10.2026. Проверены реальные HTTP-ответы официальных сайтов; запросы только на чтение.

## 1. Folketinget Open Data (ODA)

Доступен: [ODA API](https://oda.ft.dk/api/), [официальная документация](https://www.ft.dk/da/dokumenter/aabne_data), [OData metadata](https://oda.ft.dk/api/$metadata). Все проверенные запросы вернули HTTP 200. Документация указывает OData 3.0. Корень `/api/` возвращает JSON со списком наборов данных; среди них подтверждены `Sag`, `Dokument`, `Sagstrin`, `Sagsstatus`, `Afstemning`, а также связующие наборы.

### Структура и связи

Ответы коллекций имеют `odata.metadata` и массив `value`; записи имеют целочисленный `id` и поля на датском. Поля ниже сверены с живым `$metadata` и ответами:

- `Sag` — дело: `id`, `typeid`, `kategoriid`, `statusid`, `titel`, `nummer`, `periodeid`, `opdateringsdato`; связана с `Sagsstatus`, `Sagstrin`, `SagDokument`, `SagAktør` и др. `statusid` — текущий статус дела через `Sagsstatus`.
- `Sagstrin` — этап: `id`, `sagid`, `titel`, `dato`, `typeid`, `statusid`, `opdateringsdato`; навигация к `Sag`, `Sagstrinsstatus`, `Sagstrinstype`, `Afstemning` и документам этапа.
- `Dokument` — документ: `id`, `titel`, `dato`, `modtagelsesdato`, `frigivelsesdato`, `opdateringsdato`, тип/категория/статус. Связь с делом проходит через `SagDokument` (`sagid`, `dokumentid`), а не прямое поле `sagid` в `Dokument`.
- `Afstemning` — голосование: `id`, `nummer`, `vedtaget`, `konklusion`, `kommentar`, `mødeid`, `sagstrinid`, `opdateringsdato`; связано с `Sagstrin` и `Møde`. У проверенной записи `sagstrinid` был заполнен, но в другой реальной записи был `null`, поэтому связь не следует считать обязательной.
- `Sagsstatus` и `Sagstrinsstatus` — справочники статусов. У самого `Sag` хранится `statusid`; у этапа — отдельный `statusid`.

Пример живого ответа `Sag` (сокращён):

```json
{"odata.metadata":"https://oda.ft.dk/api/$metadata#Sag","value":[
  {"id":106028,"titel":"Forslag til lov om ændring af lov om leje...",
   "nummer":"L 34","statusid":25,"opdateringsdato":"2026-10-15T21:55:42.37"}
]}
```

### Запросы и отслеживание изменений

Проверены `$filter`, `$orderby`, `$top`, `$skip`, `$inlinecount=allpages`, `$select` и `$expand`. Например, фильтр по `opdateringsdato` вернул 147 совпадений для проверенного временного окна, а `$skip=1&$top=1` вернул следующую страницу:

```text
https://oda.ft.dk/api/Sag?$filter=opdateringsdato%20ge%20datetime%272026-10-14T00:00:00%27&$orderby=opdateringsdato%20desc&$top=100
```

Официальная документация прямо говорит, что **каждый элемент** имеет `opdateringsdato` с датой и временем, и рекомендует использовать её для поиска новых и изменённых записей с сохранением времени последнего опроса. Поэтому новые и изменённые записи можно обнаруживать повторным фильтрованным чтением. Следует опрашивать отдельно `Sag`, `Sagstrin`, `Dokument`, `SagDokument` и `Afstemning`: обновление связанного набора не следует выводить из изменения родительской записи. Состояние дела определять по `Sag.statusid` и раскрытому `Sagsstatus`; этапы и голосования читать по собственным ID и времени обновления. Рекомендуется сохранять водяной знак отдельно для каждого набора, брать небольшой перекрывающий временной интервал и устранять повторы по `id` + `opdateringsdato`.

Ссылки на PDF доступны в данных документов согласно документации Folketinget. Официальные открытые данные охватывают парламентские дела и документы с октября 2013 года; календарь и новости сайта не входят. Данные публикуются «как есть», без гарантии постоянной доступности; ошибочно опубликованные записи могут удаляться, о чём указывается в таблице `slettet`.

## 2. Høringsportalen

Доступны [список feed-ов](https://hoeringsportalen.dk/Syndication), [общая ATOM-лента](https://hoeringsportalen.dk/Syndication/HearingsFeed) и целевые ленты:

- По ведомству Udlændinge- og Integrationsministeriet: `https://hoeringsportalen.dk/Syndication/HearingsByAuthorityFeed?authorityId=1745`
- По категории «Udlændinge, flygtninge og integration»: `https://hoeringsportalen.dk/Syndication/HearingsByFormAreaFeed?formAreaId=37`

Официальная страница говорит, что для каждой категории или ведомства доступны 25 последних **обновлённых** консультаций. Проверенные ответы имеют HTTP 200, `Content-Type: application/atom+xml`. Пример реального фрагмента, сокращён:

```xml
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Høringsportalen - Alle høringer</title>
  <updated>2026-10-16T03:28:31Z</updated>
  <entry>
    <id>https://hoeringsportalen.dk/Hearing/Details/71507</id>
    <title>Høring over udkast til lov om ændring ... Ukrainesærloven</title>
    <updated>2026-09-17T14:27:44Z</updated>
    <author><name>Udlændinge- og Integrationsministeriet</name></author>
    <link rel="alternate" href="https://hoeringsportalen.dk/Hearing/Details/71507"/>
    <content type="html">... Type: Lovforslag ... Høringsfrist ... Status ...</content>
  </entry>
</feed>
```

`entry/id` — URI страницы конкретной консультации; `updated` у ленты и записей — UTC-время. В наблюдавшемся ATOM нет `published`; сведения о ведомстве, типе, сроке и статусе частично находятся в HTML `content`. Запись ведёт на карточку консультации, а не на оригиналы документов. На странице карточки для проверенного украинского законопроекта были отдельные ссылки на PDF проекта, сопроводительного письма и списка адресатов.

Сверять записи следует по `entry/id` и `entry/updated`, а при изменении повторно читать карточку. Две целевые ленты полезны для тематической выборки, но также ограничены 25 записями каждая. Официальное описание RSS (портал указывает ATOM для Syndication) сообщает об обновлениях при создании консультации, изменении срока и истечении срока; оно не устанавливает SLA доставки для ATOM.

## Частота опроса, пробелы и открытые вопросы

- **Три раза в день** (примерно каждые 8 часов) обычно подходит для пакетного обзора, но это не гарантия полноты или своевременности. В ATOM-ленте нет пагинации, только 25 последних обновлений: если за интервал лента вытеснит более 25 новых/изменённых записей, пропущенные записи могут исчезнуть из окна. Для снижения риска целевые ленты стоит проверять каждые 1–2 часа, общий feed — несколько раз в день; сохранять обработанные ID и время обновления. Официального SLA обновления не найдено.
- В ODA трёх опросов в день достаточно для задержки до 8 часов при корректной пагинации и сохранённом `opdateringsdato`; документация не обещает задержку публикации данных. Для более оперативных уведомлений целесообразен более частый опрос.
- Høringsportalen обязателен для ряда материалов только при внешнем консультировании; часть материалов публикуется добровольно. Ответы на консультации могут появиться только после срока консультации, а некоторые ответы и заключения могут отсутствовать. Поэтому портал не является полным реестром всех принятых норм или административных изменений.
- В ODA не входят календарь/новости Folketinget; охват ограничен парламентскими данными. Проверки не установили, гарантирует ли изменение каждого вложенного документа обновление родительского `Sag`; поэтому отслеживать нужно сущности и таблицы-связки отдельно.
- Не документированы лимиты частоты запросов, SLA и гарантия неизменности URI/ID ATOM. ODA предупреждает, что доступ и условия использования могут меняться; требуется указывать Folketinget как источник.

### Источники

- [Folketinget: Åbne data](https://www.ft.dk/da/dokumenter/aabne_data)
- [ODA service root](https://oda.ft.dk/api/) и [OData metadata](https://oda.ft.dk/api/$metadata)
- [Høringsportalen: feeds](https://hoeringsportalen.dk/Syndication) и [общее описание портала](https://hoeringsportalen.dk/about)
- [Карточка консультации по Ukrainesærloven, ID 71507](https://hoeringsportalen.dk/Hearing/Details/71507)

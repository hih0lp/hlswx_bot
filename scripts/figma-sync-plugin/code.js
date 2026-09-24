// Переносит в макет правки, сделанные в боте.
//
// Зачем плагин, а не HTTP: REST API Figma умеет файл только читать — эндпоинта,
// меняющего содержимое ноды, у него нет. Менять файл может лишь Plugin API, то
// есть код, исполняемый внутри Figma. Этот файл им и является. MCP делает ровно
// то же самое, но через посредника с лимитом 20 вызовов в месяц на тарифе
// Starter; локальный плагин лимитов не знает.
//
// Проходы, и все они идемпотентны — повторный запуск ничего не портит и
// отчитывается «уже так»:
//   EDITS    — подписи, изменённые в боте (см. docs/figma_fixes_pending.md);
//   DROPS    — удаление лишних колонок в списках (Telegram ID в «Все записи»);
//   TRIMS    — приведение числа строк на странице к тому, что влезает в бота;
//   REGROUPS — перекладка кнопок в один ряд;
//   INSERTS  — недостающие строки и кнопки, клоном соседней ноды;
//   SPLITS   — разводит пару кнопок из одного ряда по отдельным рядам;
//   PAIRS    — обратное: возвращает пару кнопок в один ряд;
//   ROWS     — раскладывает кнопки контейнера по заданной сетке (2+2+1 и
//              подобные), когда трёх проходов выше не хватает;
//   WIDTHS   — выравнивает размер кнопки по соседям, ничего не перенося;
//   ADOPTS   — возвращает в кадр ноду, случайно вытащенную из него на холст;
//   REORDERS — переставляет строку внутри кадра;
//   COLUMNS  — раскладывает колонку кадров на холсте с ровным зазором: после
//              INSERTS кадр становится выше и наезжает на соседний снизу;
//   шрифт    — возвращает «Google Sans» нодам, которым при правке достался
//              запасной «Google Sans Flex» (только если шрифт установлен);
//   PROBES   — разведка: печатает структуру кадра, ничего не меняя. Нужна там,
//              где правка на раскладке, а строения кадра в локальном дампе нет.
//
// Плюс кнопка «Скачать дамп макета» в отчёте: выгружает страницу Flows в тот
// же JSON, по которому сверяются бот и макет. Без неё дамп устаревал после
// каждого прогона, а снять его было нечем — REST-API требует токена, MCP
// упирается в лимит. Подробности в README.

const PAGE_ID = "97:2"; // страница 🎯 Flows

// --- подписи ---------------------------------------------------------------
// `from` нужен, чтобы найти ноду по тексту, если её id в макете сменился.
// Пустой `id` — «ищи только по тексту» (кадров 368:* нет в локальном дампе).
const EDITS = [
  // Правка 16.09.2026: подпись не влезала в кнопку в половину ряда.
  { id: "144:269", from: "💰 Пополнить баланс", to: "💰 Пополнить", where: "Профиль 144:262" },
  { id: "146:231", from: "💰 Пополнить баланс", to: "💰 Пополнить", where: "Профиль 146:218" },
  { id: "155:545", from: "💰 Пополнить баланс", to: "💰 Пополнить", where: "Профиль 155:532" },
  // Правка 16.09.2026: раздел назван так же, как его собственный экран 368:406.
  { id: "367:405", from: "🏙 Города и группы", to: "🏙 Города и чаты", where: "Админка 337:208 — пункт меню" },
  { id: "368:442", from: "← Города и группы", to: "← Города и чаты", where: "Города 368:429 — «Назад»" },
  { id: "368:678", from: "← Города и группы", to: "← Города и чаты", where: "Уведомление 368:664 — «Назад»" },
  // Правка 17.09.2026: подписи не влезали в кнопку в половину ряда.
  { id: "344:1344", from: "➕ Подключить бота", to: "➕ Подключить", where: "Партнёры 344:1333" },
  { id: "344:1346", from: "🔎 Найти партнёра", to: "🔎 Найти", where: "Партнёры 344:1333" },
  { id: "", from: "➕ Добавить город", to: "➕ Добавить", where: "Города и чаты 368:406, Города 368:429" },
  { id: "", from: "✅ Добавить город", to: "✅ Добавить", where: "Новый город 368:644 — подтверждение" },
  { id: "", from: "🏙 Открыть город", to: "🏙 Открыть", where: "Уведомление 368:664" },
  // Опечатка в макете: у соседних записей очереди «1 чат».
  { id: "341:1137", from: "💬 1 час", to: "💬 1 чат", where: "Очередь 341:1112" },
  // Та же опечатка осталась ещё в трёх кадрах списков публикаций. Без id:
  // ищем по тексту и правим все совпадения разом.
  { id: "", from: "💬 1 час", to: "💬 1 чат", where: "Списки публикаций 340:751, 340:892, 340:1024" },

  // --- правки 18.09.2026, сверка docs/figma_diff_2026-09-18.md ---

  // Справочник тарифов закреплён в ТЗ этапа 2 (п. 6.6) — там полные названия,
  // и бот берёт их оттуда. Сокращения в макете правим под ТЗ.
  { id: "", from: "💎 Драг. металлы", to: "💎 Драгоценные металлы", where: "Тарифы 375:454, 375:510, 375:618, 375:674" },
  { id: "", from: "🔗 Реферальки", to: "🔗 Рефералки", where: "Тарифы 375:454, 375:510, 375:618, 375:674" },

  // На соседнем кадре того же экрана (349:522) плюс уже нарисован эмодзи.
  { id: "", from: "+ Добавить", to: "➕ Добавить", where: "Белый список 338:453" },

  // Разряды в суммах: в боте неразрывный пробел, в макете осталась запятая
  // из старого скриншота. Каждая подпись правится отдельно — общего шаблона
  // у плагина нет, а текст ищется целиком.
  { id: "", from: "#65 — 1,200 ₽", to: "#65 — 1 200 ₽", where: "Ожидают оплаты 340:385" },
  { id: "", from: "#60 — 10,500 ₽", to: "#60 — 10 500 ₽", where: "Ожидают оплаты 340:385" },
  { id: "", from: "#59 — 9,000 ₽", to: "#59 — 9 000 ₽", where: "Ожидают оплаты 340:385" },
  { id: "", from: "#57 — 7,000 ₽", to: "#57 — 7 000 ₽", where: "Ожидают оплаты 340:385" },
  { id: "", from: "#41 — 3,000 ₽", to: "#41 — 3 000 ₽", where: "Ожидают оплаты 340:385" },
  { id: "", from: "#36 — 2,500 ₽", to: "#36 — 2 500 ₽", where: "Ожидают оплаты 340:385" },
  { id: "", from: "#35 — 3,500 ₽", to: "#35 — 3 500 ₽", where: "Ожидают оплаты 340:385" },

  // Пропущенное двоеточие: на всех остальных карточках «Статус: …».
  { id: "", from: "🟢 Статус Подключён", to: "🟢 Статус: Подключён", where: "Партнёр 344:1431" },
  { id: "", from: "🕐 Последняя активность Сегодня, 14:32", to: "🕐 Последняя активность: Сегодня, 14:32", where: "Партнёр 344:1431" },
  // Строчная буква в начале экрана.
  { id: "", from: "✅ бот найден", to: "✅ Бот найден", where: "Уведомление 344:1601" },

  // Строка записи очереди не влезает в кнопку: «⏱ Следующее сообщение: через
  // 2 мин» — 35 знакомест при 28 доступных. В списке очереди «следующее» и так
  // подразумевается. Правится во всех кадрах сразу (341:1112 и 340:751).
  { id: "", from: "⏱ Следующее сообщение: через 2 мин", to: "⏱ Через 2 мин", where: "Очередь 341:1112, В очереди 340:751" },
  { id: "", from: "⏱ Следующее сообщение: через 4 мин", to: "⏱ Через 4 мин", where: "Очередь 341:1112, В очереди 340:751" },
  { id: "", from: "⏱ Следующее сообщение: через 6 мин", to: "⏱ Через 6 мин", where: "Очередь 341:1112, В очереди 340:751" },
  { id: "", from: "⏱ Следующее сообщение: через 8 мин", to: "⏱ Через 8 мин", where: "Очередь 341:1112, В очереди 340:751" },

  // Категории на экране выбора: три из пяти нарисованных нет в справочнике
  // тарифов. `in` обязателен — «Аренда» и «Другое» встречаются и на экранах
  // тарифов, правка по тексту зацепила бы их.
  { id: "", in: "349:381", from: "Работа", to: "Шабашки", where: "Категория 349:381" },
  { id: "", in: "349:381", from: "Услуги", to: "Вакансии", where: "Категория 349:381" },
  { id: "", in: "349:381", from: "Продажа", to: "Купля / продажа", where: "Категория 349:381" },

  // На кадре 61 запись и 8 на странице — это 8 страниц, как в 338:725.
  // `in` обязателен: подпись «1 / 6» стоит ещё в четырёх кадрах (списки
  // публикаций, пользователи, выбор города), и правка по тексту задела бы их.
  { id: "", in: "349:580", from: "1 / 6", to: "1 / 8", where: "Все записи 349:580 — пагинация" },
];

// --- удаление колонки с Telegram ID ----------------------------------------
// Заказчик 17.09.2026: «Цифры 19#### не нужны, их из макета убрать». В боте
// подпись записи — «#N @username» (app/handlers/admin/whitelist.py), id там
// никогда не было. Строка макета собрана как
// [«#N», точка, «1904406102», точка, «@username»], поэтому вместе с числом
// убираем одну точку-разделитель — иначе останется «#1 · · @username».
const DROPS = [
  { id: "338:725", name: "Все записи", where: "Белый список 338:725 — Telegram ID в строках" },
];

// --- число строк на странице ------------------------------------------------
// В макете 10 записей, в боте 8: экран уходит подписью к баннеру, а она
// ограничена 1024 символами (app/handlers/admin/common.py, LIST_PER_PAGE).
// Подпись пагинации пересчитывается по тому же числу записей, что в шапке.
const TRIMS = [
  { id: "338:725", name: "Все записи", keep: 8, where: "Белый список 338:725 — 10 → 8 записей" },
];

// --- сетка кнопок -----------------------------------------------------------
// Контейнер рядов и подписи кнопок задаются явно: плагин собирает кнопки по
// подписям в нужном порядке, переносит в первый ряд и убирает опустевшие.
const REGROUPS = [
  // Пусто. 17.09.2026 здесь стояла перекладка «1…5 мин» в один ряд на кадре
  // 341:1248: правило заказчика «не влезает в кнопку — правим макет» мы тогда
  // применили наоборот, к кнопкам, которые прекрасно помещались. 20.09.2026
  // заказчик сверился со своим оригиналом (wJbvRHNGmCH6Owl4OjJ7Ud) и отменил
  // правку: там кнопки стоят 2×2+1, и бот теперь следует оригиналу. Возврат
  // сетки в копии делают проходы SPLITS и PAIRS ниже.
];

// --- добавление строк и кнопок ----------------------------------------------
// Экраны, где бот показывает больше, чем нарисовано. Новую ноду не создаём с
// нуля, а клонируем соседнюю: у клона сразу правильный шрифт, кегль, цвет и
// поведение в авто-лейауте, и не повторяется история с подменой Google Sans.
//
//   like   — текст ноды-образца, с которой снимаем клон;
//   texts  — что добавить, по порядку;
//   before — перед какой подписью вставить (по умолчанию — следом за образцом);
//   kind   — "line" для строки текста, "button" для кнопки (клонируем не саму
//            подпись, а кадр кнопки вокруг неё).
const INSERTS = [
  {
    id: "337:208",
    name: "Админка",
    kind: "line",
    like: "📋 Публикации сегодня: 28",
    texts: ["⏱ В очереди: 4"],
    where: "Админка 337:208 — строка «В очереди» есть только на 337:233",
  },
  {
    id: "338:453",
    name: "Белый список",
    kind: "line",
    like: "✅ Активных записей: 58",
    texts: ["🚫 Истекших: 3"],
    where: "Белый список 338:453 — строка «Истекших» есть в 338:725",
  },
  {
    id: "144:262",
    name: "Профиль",
    kind: "button",
    like: "📄 Правила",
    before: "📄 Правила",
    texts: ["🔗 Интеграция бота"],
    where: "Профиль 144:262 — кнопка есть на 155:532",
  },
  {
    id: "146:218",
    name: "Профиль",
    kind: "button",
    like: "📄 Правила",
    before: "📄 Правила",
    texts: ["🔗 Интеграция бота"],
    where: "Профиль 146:218 — кнопка есть на 155:532",
  },
  // Раздел «Обучение модели» по решению заказчика от 17.09.2026 управляет
  // обучением целиком, а в кадре остались два счётчика и две кнопки.
  {
    id: "349:354",
    name: "Сообщения",
    kind: "line",
    like: "⏳ Без вердикта: 23",
    texts: [
      "📂 В модели: 1 284 примеров",
      "➕ Добавлено вручную: 42",
      "Новые примеры попадают в модель только после переобучения.",
    ],
    where: "Обучение модели 349:354 — счётчики выборки",
  },
  {
    id: "349:354",
    name: "Сообщения",
    kind: "button",
    like: "📚 Обученные примеры",
    texts: [
      "➕ Добавить пример",
      "📎 Загрузить файл",
      "📋 Логи классификаций",
      "🧠 Переобучить модель",
    ],
    where: "Обучение модели 349:354 — кнопки обучения",
  },
  // Категории: в кадре пять, в боте десять плюс «Запрещённый контент».
  // Три существующие переименовываются (EDITS выше), остальные добавляются.
  {
    id: "349:381",
    name: "Категория",
    kind: "button",
    like: "Другое",
    texts: [
      "Драгоценные металлы",
      "ИП / ООО",
      "Рефералки",
      "Верификация",
      "Работа онлайн",
      "⛔ Запрещённый контент",
    ],
    where: "Категория 349:381 — недостающие категории",
  },
  // Найдено сверкой рядов кнопок 20.09.2026: у бота эти кнопки есть по делу,
  // а в кадрах их не нарисовали.
  {
    id: "349:337",
    name: "Сообщения",
    kind: "button",
    like: "👥 Нескольким пользователям",
    texts: ["📣 Всем пользователям"],
    where: "Сообщения 349:337 — третий адресат рассылки",
  },
  {
    // Образцом и опорой берём «← В админку»: «📝 Заявки» встаёт над ней
    // собственным рядом во всю ширину — так этот экран собирает бот. Кнопки
    // «➕ Подключить» и «🔎 Найти» в образец не годятся: они половинные.
    id: "344:1333",
    name: "Партнеры",
    kind: "button",
    like: "← В админку",
    before: "← В админку",
    texts: ["📝 Заявки"],
    where: "Партнеры 344:1333 — раздел заявок на подключение",
  },
];

// Текст клона переписываем целиком: общего префикса с образцом у него нет,
// а стиль у свежего клона одинаковый по всей строке, так что терять нечего.
async function setAllText(node, text) {
  const swaps = await prepareFonts(node);
  if (node.characters !== text) node.characters = text;
  return swaps;
}

function textNodeByChars(scope, chars) {
  return scope.findOne((n) => n.type === "TEXT" && n.characters.trim() === chars);
}

// Кнопка — кадр, внутри которого ровно одна подпись. У строки текста
// образцом служит сама текстовая нода.
function templateFor(scope, kind, like) {
  const text = textNodeByChars(scope, like);
  if (!text) return null;
  if (kind !== "button") return text;
  const box = text.parent;
  if (!box || !("children" in box) || box.children.length !== 1) return null;
  return box;
}

async function insertClones(frame, target) {
  const template = templateFor(frame, target.kind, target.like);
  if (!template) throw new Error("нода-образец «" + target.like + "» не найдена");
  const parent = template.parent;

  // Опорная нода сдвигается на только что вставленную, иначе несколько подписей
  // подряд лягут задом наперёд: каждая вставлялась бы сразу за образцом.
  let anchor = target.before ? templateFor(frame, target.kind, target.before) : template;
  if (!anchor) throw new Error("опорная нода «" + target.before + "» не найдена");

  let added = 0;
  for (const text of target.texts) {
    // Идемпотентность: если такая подпись в кадре уже есть, второй раз не
    // добавляем — плагин можно гонять сколько угодно.
    if (textNodeByChars(frame, text)) continue;

    const clone = template.clone();
    const at = parent.children.indexOf(anchor);
    parent.insertChild(at < 0 ? parent.children.length : at + (target.before ? 0 : 1), clone);

    const label = target.kind === "button" ? clone.children[0] : clone;
    await setAllText(label, text);
    if (!target.before) anchor = clone;
    added++;
  }
  return added;
}

// --- возврат родного шрифта --------------------------------------------------
// Если «Google Sans» в системе нет, правка текста тянет за собой подмену на
// «Google Sans Flex» (см. resolveFont). Рендерится он почти неотличимо, но
// файл при каждом прогоне расслаивается всё сильнее. Этот проход возвращает
// родное семейство — но только если оно доступно: без установленного шрифта
// он честно ничего не делает и говорит об этом.
const RESTORE_FROM = "Google Sans Flex";
const RESTORE_TO = "Google Sans";

async function restoreFonts(page) {
  const victims = page
    .findAllWithCriteria({ types: ["TEXT"] })
    .filter((n) => n.fontName !== figma.mixed && n.fontName.family === RESTORE_FROM);
  if (!victims.length) return { restored: 0, available: true };

  // Проверяем доступность один раз, на стиле первой найденной ноды.
  try {
    await figma.loadFontAsync({ family: RESTORE_TO, style: victims[0].fontName.style });
  } catch (err) {
    return { restored: 0, available: false, count: victims.length };
  }

  let restored = 0;
  for (const node of victims) {
    const style = node.fontName.style;
    try {
      await figma.loadFontAsync({ family: RESTORE_FROM, style: style });
      await figma.loadFontAsync({ family: RESTORE_TO, style: style });
      node.fontName = { family: RESTORE_TO, style: style };
      restored++;
    } catch (err) {
      // начертания может не быть — оставляем ноду как есть
    }
  }
  return { restored: restored, available: true, count: victims.length };
}

// --- разведение пар кнопок ---------------------------------------------------
// Обратная операция к REGROUPS. В макете пара кнопок стоит в одном ряду, но в
// половину ряда Telegram вмещает 16 знакомест, а в этих подписях 17–21 — хвост
// обрежется. Та же история, что была с «Добавить город»: правило заказчика —
// «не влезает в кнопку, значит правим макет».
//
// Кнопка уже готовый скруглённый кадр, поэтому «ряд в полную ширину» получается
// сам собой: достаточно поднять её на уровень выше, рядом с «← Назад».
// Четыре разведения от 18.09.2026 отсюда убраны намеренно: контейнер-ряд
// после разведения удаляется, поиск по id его уже не найдёт, а имена
// «Frame 1» и «Frame 2» носят десятки контейнеров. Именно на этом прогон
// 18.09 разнёс кнопки в чужих кадрах «Ошибка» и «Ошибка 2». Поиск по
// неоднозначному имени с тех пор запрещён — повторный прогон не сломает файл,
// а честно напишет в отчёт, что контейнера нет. **Записи ниже после удачного
// прогона тоже уберите.**
//
// Три записи заведены 20.09.2026 по итогам первой сверки рядов кнопок
// (`scripts/figma_buttons.py`). Во всех трёх пара не помещается в ряд: в
// половину ряда влезает 16 знакомест, а тут 21, 17 и 18.
//
// **`name` здесь не указан намеренно, и добавлять его нельзя.** Запасной
// поиск по имени для этого прохода — ловушка: контейнер он же и удаляет, а
// имена не уникальны. «Frame 45» носят два контейнера — `340:342` из «Оплат»
// и `344:1342` из «Партнёров», где пара кнопок стоять как раз должна. После
// удаления первого поиск по имени нашёл бы единственный оставшийся, счёл его
// однозначным и разнёс бы чужую пару — ровно авария 18.09.2026, только
// изнутри правила, которое от неё защищает. Без `name` `findFrame` честно
// вернёт null, и повторный прогон запишет в отчёт «контейнер не найден».
const SPLITS = [
  // Три записи от 20.09.2026 (`340:346`, `340:342`, `349:331`) отработали и
  // убраны: контейнеров больше нет, и повторный прогон только писал бы в
  // отчёт «не найден».
  {
    // Белый список нарисован дважды: `338:453` из меню панели и `349:522` из
    // «Управления». Пару развели только на первом — вторая всплыла, когда бот
    // 20.09.2026 начал различать эти два пути (кадр 349:522, `adm:manage:wl`).
    id: "349:563",
    where: "Белый список 349:522 — «🔎 Найти пользователя» (21) не влезает в пару",
  },
];

// Ширина кнопки задаётся не числом, а режимом: в ряду из двух она занимает
// половину, в собственном ряду — всю строку. При переносе режим не меняется
// сам, и кнопка остаётся половинной — именно так «Настройки очереди» и
// «Шабашки» оказались вдвое уже соседей. Поэтому режим выставляем явно, по
// соседу из нового контейнера.
// Заодно переносим вертикальные поля: кнопка в ряду из двух нарисована
// компактнее (высота 48), а в собственном ряду — свободнее (71). Без этого
// перенесённая кнопка встаёт правильной ширины, но заметно ниже соседей.
function matchBox(button, siblings) {
  const model = siblings.find((n) => n !== button && isRound(n));
  if (!model) return;
  try {
    button.layoutSizingHorizontal = model.layoutSizingHorizontal;
    button.paddingTop = model.paddingTop;
    button.paddingBottom = model.paddingBottom;
    button.paddingLeft = model.paddingLeft;
    button.paddingRight = model.paddingRight;
  } catch (err) {
    // у кадра без авто-лейаута этих свойств нет — оставляем как есть
  }
}

function splitRow(container) {
  if (container.layoutMode !== "HORIZONTAL") return 0;
  const buttons = container.children.filter(isRound);
  if (buttons.length < 2) return 0;

  const parent = container.parent;
  const models = parent.children.slice();
  let at = parent.children.indexOf(container);
  for (const button of buttons) {
    parent.insertChild(at, button);
    matchBox(button, models);
    at++;
  }
  if (container.children.length === 0) container.remove();
  return buttons.length;
}

// --- возврат пары кнопок в один ряд ------------------------------------------
// Уборка за собственной ошибкой: 18.09.2026 проход `SPLITS` не нашёл свой
// контейнер по id (его удалил предыдущий прогон), свалился на поиск по имени
// «Frame 1» и разнёс кнопки в чужих кадрах «Ошибка» и «Ошибка 2». Там они
// стояли парой и должны стоять парой: обе подписи короткие и помещаются.
//
// Ряд-контейнер удалён вместе с содержимым, поэтому берём образцом уцелевший
// такой же ряд из «Админки» — у него та же ширина колонок и те же поля.
const PAIRS = [
  {
    id: "125:409",
    name: "Ошибка",
    template: "337:220",
    labels: ["✏️ Изменить текст", "🏠 Главное меню"],
    where: "Ошибка 125:409 — вернуть кнопки в один ряд",
  },
  {
    id: "125:418",
    name: "Ошибка 2",
    template: "337:220",
    labels: ["✏️ Изменить текст", "🏠 Главное меню"],
    where: "Ошибка 2 125:418 — вернуть кнопки в один ряд",
  },
];

async function pairButtons(frame, target) {
  const buttons = target.labels.map((label) => buttonByLabel(frame, label));
  if (buttons.some((b) => !b)) throw new Error("кнопки не нашлись");

  // Уже в одном горизонтальном ряду — работы нет.
  const parent = buttons[0].parent;
  if (parent.layoutMode === "HORIZONTAL" && buttons.every((b) => b.parent === parent)) {
    return 0;
  }

  const template = await figma.getNodeByIdAsync(target.template);
  if (!template || template.layoutMode !== "HORIZONTAL") {
    throw new Error("образец ряда " + target.template + " не найден");
  }

  const row = template.clone();
  const models = row.children.slice();
  parent.insertChild(parent.children.indexOf(buttons[0]), row);
  for (const button of buttons) {
    row.appendChild(button);
    matchBox(button, models);
  }
  for (const stale of models) stale.remove();
  return buttons.length;
}

// --- раскладка кнопок по сетке -----------------------------------------------
// `REGROUPS` и `PAIRS` умеют только «всё в один ряд» и «эти две вместе».
// Сетку 2+2+1 ими не собрать: `pairButtons` видит «3 мин» и «4 мин» в одном
// горизонтальном контейнере с остальными и отчитывается «уже в одном ряду», а
// `splitRow` разводит ряд целиком и обратно уже не соберёт. Поэтому отдельный
// проход, который задаёт раскладку целиком и потому идемпотентен: он сверяет
// текущие ряды с нужными и, если совпало, не трогает ничего.
//
//   id       — контейнер-колонка, внутри которого лежат ряды кнопок;
//   template — образец горизонтального ряда: с него клонируются недостающие.
//              Берите ряд из этого же кадра — у него уже верные зазор, поля и
//              режим ширины, и подгонять `matchBox`-ом ничего не придётся;
//   grid     — подписи кнопок по рядам, сверху вниз.
//
// Ширину кнопок не трогаем намеренно: у всех стоит режим FILL, и она считается
// от контейнера сама — в ряду из двух выходит по половине, в собственном ряду
// во всю строку. Это ровно то, что нарисовано в оригинале.
const ROWS = [
  {
    id: "341:1272",
    // Запасной поиск по имени тут не сработает — «Frame 3» носят 32
    // контейнера страницы, и `findFrame` честно откажется править вслепую.
    // Имя оставлено ради внятной ошибки в отчёте, а не ради поиска.
    name: "Frame 3",
    template: "341:1273",
    grid: [["1 мин", "2 мин"], ["3 мин", "4 мин"], ["5 мин"]],
    where: "Интервал 341:1248 — сетка минут 2+2+1",
  },
];

function labelOf(button) {
  const text = button.findOne((n) => n.type === "TEXT");
  return text ? text.characters.trim() : "";
}

// Как кнопки колонки разложены сейчас: ряд из нескольких — горизонтальный
// контейнер, ряд из одной — сама кнопка прямым ребёнком колонки.
function rowGroups(column) {
  const out = [];
  for (const child of column.children) {
    if (isRound(child)) {
      out.push([child]);
      continue;
    }
    if (child.layoutMode === "HORIZONTAL") {
      const inner = child.children.filter(isRound);
      if (inner.length) out.push(inner);
    }
  }
  return out;
}

async function layoutRows(column, target) {
  const buttons = new Map();
  for (const row of target.grid) {
    for (const label of row) {
      const button = buttonByLabel(column, label);
      if (!button) throw new Error("кнопка «" + label + "» не найдена");
      buttons.set(label, button);
    }
  }

  const now = rowGroups(column).map((row) => row.map(labelOf));
  if (JSON.stringify(now) === JSON.stringify(target.grid)) return 0;

  const template = await figma.getNodeByIdAsync(target.template);
  if (!template || template.layoutMode !== "HORIZONTAL") {
    throw new Error("образец ряда " + target.template + " не найден");
  }

  // Раскладку ставим с того места, где кнопки лежат сейчас: в колонке может
  // быть и что-то кроме них.
  let at = column.children.findIndex((n) => isRound(n) || n.layoutMode === "HORIZONTAL");
  if (at < 0) at = column.children.length;

  const made = [];
  for (const labels of target.grid) {
    if (labels.length === 1) {
      column.insertChild(at, buttons.get(labels[0]));
    } else {
      // Клон несёт с собой копии чужих кнопок — их убираем, как в `pairButtons`.
      const row = template.clone();
      const stale = row.children.slice();
      column.insertChild(at, row);
      for (const label of labels) row.appendChild(buttons.get(label));
      for (const node of stale) node.remove();
      made.push(row);
    }
    at++;
  }

  // Ряды прошлой раскладки опустели — иначе остались бы полоски в высоту
  // отступа. Ряд-образец среди них: клонировать его мы уже закончили.
  for (const child of column.children.slice()) {
    if (made.indexOf(child) >= 0) continue;
    if (child.layoutMode === "HORIZONTAL" && !child.children.some(isRound)) child.remove();
  }
  return buttons.size;
}

// --- выравнивание ширины кнопок ----------------------------------------------
// Кнопки, которые прошлый прогон поднял из ряда наверх, остались половинными:
// режим ширины и поля при переносе не менялись. Разводить их уже не нужно —
// нужно только выровнять размер по соседу, стоящему в собственном ряду.
const WIDTHS = [
  {
    id: "341:1112", name: "Очередь",
    labels: ["⚙️ Настройки очереди"],
    where: "Очередь 341:1112 — ширина «Настройки очереди»",
  },
  {
    id: "349:564", name: "Найти пользователя",
    labels: ["🔎 Найти пользователя"],
    where: "Найти пользователя 349:564 — ширина «Найти пользователя»",
  },
  {
    id: "349:381", name: "Категория",
    labels: ["Шабашки", "Купля / продажа"],
    where: "Категория 349:381 — ширина двух кнопок",
  },
];

function normalizeWidths(frame, target) {
  let fixed = 0;
  for (const label of target.labels) {
    const button = buttonByLabel(frame, label);
    if (!button) throw new Error("кнопка «" + label + "» не найдена");
    const models = button.parent.children.filter((n) => n !== button && isRound(n));
    if (!models.length) throw new Error("не с чем сравнить размер «" + label + "»");
    // Сверяем и высоту: у кнопки из ряда меньше вертикальные поля, и она
    // остаётся ниже соседей, даже когда ширину уже поправили руками.
    const before = Math.round(button.width) + "x" + Math.round(button.height);
    matchBox(button, models);
    if (Math.round(button.width) + "x" + Math.round(button.height) !== before) fixed++;
  }
  return fixed;
}

// --- возврат ноды, вытащенной из кадра ---------------------------------------
// Кнопку легко утащить из кадра на холст мышью — она остаётся отдельным
// кадром рядом, а на экране её больше нет. Так 18.09.2026 «← Назад» выпала из
// «Категории» и легла на страницу самостоятельным кадром с именем «1».
const ADOPTS = [
  {
    id: "349:392",
    into: "349:381",
    label: "← Назад",
    where: "Категория 349:381 — вернуть кнопку «Назад» в кадр",
  },
];

async function adoptNode(target) {
  const frame = await figma.getNodeByIdAsync(target.into);
  if (!frame || !("children" in frame)) throw new Error("кадр " + target.into + " не найден");

  // Уже внутри — работы нет.
  if (buttonByLabel(frame, target.label) ) return 0;

  const node = await figma.getNodeByIdAsync(target.id);
  if (!node) throw new Error("нода " + target.id + " не найдена");

  frame.appendChild(node);
  try {
    node.layoutPositioning = "AUTO";   // вернуть в поток авто-лейаута
  } catch (err) {
    // кадр без авто-лейаута — позиция и так своя
  }
  matchBox(node, frame.children.filter((n) => n !== node));
  return 1;
}

// --- перестановка строки -----------------------------------------------------
// Там, где макет спорит сам с собой: на 338:661 статус записи стоит последним,
// а на 349:1219 — сразу под именем. Бот следует второму, приводим первый.
const REORDERS = [
  {
    id: "338:661",
    name: "Пользователь",
    move: "🟢 Статус: Активен",
    after: "ID: 1904406102",
    where: "Пользователь 338:661 — статус под именем, как в 349:1219",
  },
];

// Опорная строка обычно лежит глубже переставляемой: «ID» стоит внутри
// горизонтального ряда с именем, а статус — прямо в пузыре сообщения.
// Поднимаемся от опорной ноды до того предка, который лежит с переставляемой
// в одном контейнере, — за ним и встаём.
function siblingLevel(node, other) {
  let cur = other;
  while (cur && cur.parent && cur.parent !== node.parent) cur = cur.parent;
  return cur && cur.parent === node.parent ? cur : null;
}

function reorderLine(frame, target) {
  const node = textNodeByChars(frame, target.move);
  if (!node) throw new Error("строка «" + target.move + "» не найдена");
  const anchor = textNodeByChars(frame, target.after);
  if (!anchor) throw new Error("опорная строка «" + target.after + "» не найдена");

  const sibling = siblingLevel(node, anchor);
  if (!sibling) {
    throw new Error("строки лежат в разных ветках — переставьте вручную");
  }
  const parent = node.parent;
  const want = parent.children.indexOf(sibling) + 1;
  if (parent.children.indexOf(node) === want) return 0;
  parent.insertChild(want, node);
  return 1;
}

// --- колонка кадров на холсте ------------------------------------------------
// Кадры на странице расставлены колонками с постоянным зазором. Когда проход
// INSERTS дорисовывает строки и кнопки, кадр становится выше и наезжает на
// соседний снизу: так «Обучение модели» съело экран 349:366, а «Категория» —
// 349:404, и заказчик увидел кашу из двух экранов.
//
// Раскладываем колонку заново по фактическим высотам — уже после того, как все
// остальные проходы отработали. Позиция задаётся абсолютно, поэтому повторный
// запуск даёт тот же результат.
const COLUMNS = [
  {
    ids: ["349:354", "349:366", "349:381", "349:404"],
    gap: 200,
    where: "Обучение модели → Категория → Категория сохранена — колонка на холсте",
  },
];

async function relayoutColumn(target) {
  const nodes = [];
  for (const id of target.ids) {
    const node = await figma.getNodeByIdAsync(id);
    if (!node) throw new Error("кадр " + id + " не найден");
    nodes.push(node);
  }

  let moved = 0;
  let y = nodes[0].y + nodes[0].height + target.gap;
  for (const node of nodes.slice(1)) {
    if (Math.round(node.y) !== Math.round(y)) {
      node.y = y;
      moved++;
    }
    y = node.y + node.height + target.gap;
  }
  return moved;
}

// --- разведка ---------------------------------------------------------------
// Ничего не меняет, печатает строение кадра. Нужна там, где правка на
// раскладке, а кадра в локальном дампе либо нет, либо он выгружен без
// layout-свойств. Список пустой — значит все известные вопросы закрыты.
const PROBES = [];

// Если родного шрифта в системе нет, берём ближайшего родственника.
const FALLBACK_FAMILIES = ["Google Sans", "Google Sans Flex", "Roboto", "Inter"];

const fontCache = new Map();

async function resolveFont(font) {
  const key = font.family + " " + font.style;
  if (fontCache.has(key)) return fontCache.get(key);

  const candidates = [font];
  for (const family of FALLBACK_FAMILIES) {
    if (family !== font.family) {
      candidates.push({ family: family, style: font.style });
      candidates.push({ family: family, style: "Regular" });
    }
  }

  for (const candidate of candidates) {
    try {
      await figma.loadFontAsync(candidate);
      fontCache.set(key, candidate);
      return candidate;
    } catch (err) {
      // шрифта нет — пробуем следующий
    }
  }
  throw new Error("не удалось загрузить ни один шрифт для " + font.family + " / " + font.style);
}

// Текст правится посегментно: у ноды может быть несколько шрифтов (эмодзи
// обычно отдельным сегментом), и Figma не даст менять символы, пока каждый из
// них не загружен.
async function prepareFonts(node) {
  const swaps = [];
  for (const segment of node.getStyledTextSegments(["fontName"])) {
    const original = segment.fontName;
    const use = await resolveFont(original);
    if (use.family !== original.family || use.style !== original.style) {
      node.setRangeFontName(segment.start, segment.end, use);
      swaps.push(original.family + "/" + original.style + " → " + use.family + "/" + use.style);
    }
  }
  return swaps;
}

// Меняем только расходящийся хвост: общий префикс остаётся вместе со своим
// стилем, поэтому эмодзи и кегль не сбрасываются.
function replaceTail(node, to) {
  const from = node.characters;
  if (from === to) return { status: "уже так", text: from };

  let i = 0;
  while (i < from.length && i < to.length && from[i] === to[i]) i++;
  if (i === 0) {
    // Общего префикса нет — различается сам первый символ, как в
    // «+ Добавить» → «➕ Добавить». Сохранять нечего, переписываем строку
    // целиком: шрифты к этому моменту уже загружены `prepareFonts`.
    node.characters = to;
    return { status: "изменено", was: from, text: node.characters };
  }

  node.deleteCharacters(i, from.length);
  const tail = to.slice(i);
  if (tail) node.insertCharacters(i, tail, "BEFORE");
  return { status: "изменено", was: from, text: node.characters };
}

async function findNode(page, edit) {
  // id проверяем текстом: он мог смениться и достаться чужой ноде, а править
  // не тот текст хуже, чем не найти ноду вовсе. Пустой id — «ищи по тексту».
  const byId = edit.id ? await figma.getNodeByIdAsync(edit.id) : null;
  if (byId && byId.type === "TEXT" && (byId.characters === edit.from || byId.characters === edit.to)) {
    return { nodes: [byId], foundBy: "id" };
  }

  // Поиск по тексту идёт по всей странице — кроме случаев, когда у правки
  // задан `in`. Подписи вроде «1 / 6» встречаются на десятке экранов, и без
  // ограничения кадром правка разъехалась бы по всему макету.
  let scope = page;
  if (edit.in) {
    scope = await figma.getNodeByIdAsync(edit.in);
    if (!scope || !("findAllWithCriteria" in scope)) {
      return { nodes: [], foundBy: "кадр " + edit.in + " не найден" };
    }
  }

  // id в макете мог смениться — ищем по тексту. Совпадений может быть
  // несколько: одна и та же подпись стоит на нескольких экранах (кнопка
  // «Добавить город» есть и в разделе, и в списке городов) — правим все.
  // Уже применённый текст тоже считаем совпадением, иначе повторный запуск
  // отчитывался бы «нода не найдена» вместо «уже так».
  const matches = scope
    .findAllWithCriteria({ types: ["TEXT"] })
    .filter((n) => n.characters === edit.from || n.characters === edit.to);
  if (matches.length) {
    return { nodes: matches, foundBy: "тексту" + (matches.length > 1 ? " ×" + matches.length : "") };
  }
  return { nodes: [], foundBy: null };
}

// --------------------------------------------------------------- строки списка

// Кадр ищем по id, а если его нет (в клоне id мог смениться) — по имени.
//
// Имя берём **только когда оно единственное на странице**. Раньше брался
// первый попавшийся, и это вышло боком: контейнер `341:1119` был удалён
// предыдущим прогоном, поиск свалился на имя «Frame 1» — а так называются
// десятки контейнеров, — и проход разнёс кнопки в чужих кадрах «Ошибка» и
// «Ошибка 2». Лучше честно не найти, чем молча починить не то.
async function findFrame(page, target) {
  const byId = target.id ? await figma.getNodeByIdAsync(target.id) : null;
  if (byId && "children" in byId) return byId;
  if (!target.name) return null;

  const named = page.findAll((n) => n.name === target.name && "children" in n);
  if (named.length === 1) return named[0];
  if (named.length > 1) {
    throw new Error(
      "имя «" + target.name + "» носят " + named.length +
      " контейнеров — уточните id, вслепую не правлю",
    );
  }
  return null;
}

// Обёртка для проходов: сама пишет в отчёт, почему кадра нет. Без неё ошибка
// неоднозначного имени вылетала бы из цикла и обрывала весь прогон.
async function resolveFrame(page, target, fail, what) {
  try {
    const node = await findFrame(page, target);
    if (!node) {
      fail(target.where, (what || "кадр") + " не найден");
      return null;
    }
    return node;
  } catch (err) {
    fail(target.where, String(err.message || err));
    return null;
  }
}

// Строка списка записей: внутри есть «#N» и «@username».
function isRecordRow(node) {
  if (!("children" in node)) return false;
  const texts = node.children.filter((c) => c.type === "TEXT").map((c) => c.characters.trim());
  return texts.some((t) => /^#\d+$/.test(t)) && texts.some((t) => t.startsWith("@"));
}

function recordRows(frame) {
  return frame.findAll(isRecordRow);
}

// Telegram ID — это отдельная TEXT-нода из одних цифр.
function isIdText(node) {
  return node.type === "TEXT" && /^\d{6,}$/.test(node.characters.trim());
}

function dropIdColumn(frame) {
  let removed = 0;
  for (const row of recordRows(frame)) {
    for (const child of row.children.filter(isIdText)) {
      child.remove();
      removed++;
    }
    // Разделителей должно остаться ровно столько, сколько промежутков между
    // оставшимися подписями, то есть один. Лишние точки убираем с конца —
    // порядок детей при этом не трогаем.
    const dots = row.children.filter((c) => c.type === "ELLIPSE");
    for (const dot of dots.slice(1)) {
      dot.remove();
      removed++;
    }
  }
  return removed;
}

// Пагинация вида «1 / 7»: знаменатель пересчитываем по числу записей из шапки
// («Всего: 61») и новому размеру страницы — иначе подпись разойдётся с макетом.
function retitlePager(frame, perPage) {
  const header = frame.findOne(
    (n) => n.type === "TEXT" && /всего/i.test(n.characters),
  );
  if (!header) return null;
  const parsed = /(\d+)/.exec(header.characters);
  if (!parsed) return null;

  const pages = Math.max(1, Math.ceil(Number(parsed[1]) / perPage));
  const pager = frame.findOne(
    (n) => n.type === "TEXT" && /^\d+\s*\/\s*\d+$/.test(n.characters.trim()),
  );
  if (!pager) return null;

  const want = pager.characters.replace(/(\d+)\s*(\/\s*)(\d+)/, "$1 $2" + pages).replace(/\s+/g, " ");
  return { node: pager, want: want };
}

function trimRows(frame, keep) {
  const rows = recordRows(frame);
  const extra = rows.length > keep ? rows.slice(keep) : [];
  for (const row of extra) row.remove();

  // Запись нарисована **внутри** кнопки, поэтому, убрав её, мы оставляем
  // пустую оболочку — в макете это полоска-кнопка без подписи. Так на
  // `338:725` после прогона 17.09.2026 и повисли два пустых кадра: сверка
  // сеток 20.09 приняла их за два лишних ряда кнопок. Чистим всегда, а не
  // только когда что-то обрезали, — иначе старый мусор останется навсегда.
  const shells = frame.findAll(
    (n) => isRound(n) && "children" in n && n.children.length === 0,
  );
  for (const shell of shells) shell.remove();

  return { rows: extra.length, shells: shells.length };
}

// ----------------------------------------------------------------- сетка кнопок

// Кнопка отличается от служебного контейнера скруглением: у кнопок и пузыря
// сообщения `cornerRadius` = 40, у рядов-обёрток его нет.
function isRound(node) {
  return typeof node.cornerRadius === "number" && node.cornerRadius >= 20;
}

// Кнопка — это кадр, внутри которого ровно одна подпись из списка.
function buttonByLabel(container, label) {
  return container.findOne(
    (n) =>
      "children" in n &&
      n.children.length === 1 &&
      n.children[0].type === "TEXT" &&
      n.children[0].characters.trim() === label,
  );
}

function regroupIntoOneRow(container, labels) {
  const buttons = labels.map((label) => buttonByLabel(container, label));
  const missing = labels.filter((_, i) => !buttons[i]);
  if (missing.length) throw new Error("не нашлись кнопки: " + missing.join(", "));

  // Ряд-приёмник — первый горизонтальный контейнер.
  const row = container.children.find((n) => n.layoutMode === "HORIZONTAL");
  if (!row) throw new Error("горизонтального ряда в контейнере нет");

  let touched = 0;

  const ordered =
    row.children.length === buttons.length &&
    row.children.every((child, i) => child === buttons[i]);
  if (!ordered) {
    // appendChild переносит ноду, а не копирует, поэтому порядок задаётся самим
    // порядком вызовов — кнопки встанут как в `labels`.
    for (const button of buttons) row.appendChild(button);
    touched += buttons.length;
  }

  // Размеры правим **всегда**, а не только когда что-то переносили. Кнопка,
  // перенесённая прошлым запуском, осталась во всю ширину и выпирала из ряда
  // (так «5 мин» на кадре 341:1248 вылезла на 180px за правый край), а проверка
  // порядка коротко замыкалась на «уже так» и до размеров не доходила.
  const model = row.children.find(isRound);
  if (model) {
    for (const button of row.children) {
      if (button === model || !isRound(button)) continue;
      const before = Math.round(button.width);
      matchBox(button, [model]);
      if (Math.round(button.width) !== before) touched++;
    }
  }

  // Ряды, из которых всё забрали, остаются пустыми кадрами и держат отступ.
  for (const child of container.children.slice()) {
    if (child !== row && "children" in child && child.children.length === 0) {
      child.remove();
      touched++;
    }
  }
  return touched;
}

// ------------------------------------------------------------------- разведка

function describe(node, depth, indent) {
  const bits = [indent + node.id + " · " + node.type + " · «" + node.name + "»"];
  if (node.type === "TEXT") {
    bits.push(" — " + JSON.stringify(node.characters.slice(0, 40)));
  }
  if ("layoutMode" in node && node.layoutMode !== "NONE") {
    bits.push(
      " [" + node.layoutMode.toLowerCase() +
      ", gap " + node.itemSpacing +
      (node.layoutWrap ? ", " + node.layoutWrap.toLowerCase() : "") + "]",
    );
  }
  if ("layoutPositioning" in node && node.layoutPositioning === "ABSOLUTE") {
    bits.push(" [absolute]");
  }
  const lines = [bits.join("")];
  if (depth > 0 && "children" in node) {
    for (const child of node.children) {
      lines.push(...describe(child, depth - 1, indent + "  "));
    }
  }
  return lines;
}

// ---------------------------------------------------------------------- запуск

async function run() {
  const page = await figma.getNodeByIdAsync(PAGE_ID);
  if (!page) throw new Error("страница " + PAGE_ID + " не найдена — это точно нужный файл?");
  await page.loadAsync();
  await figma.setCurrentPageAsync(page);

  const rows = [];
  let changed = 0;
  let already = 0;
  let failed = 0;

  const fail = (where, text) => {
    rows.push({ state: "fail", where: where, text: text });
    failed++;
  };
  const ok = (where, text, note) => {
    rows.push({ state: "ok", where: where, text: text, note: note || "" });
    changed++;
  };
  const skip = (where, text) => {
    rows.push({ state: "skip", where: where, text: text });
    already++;
  };

  // 1. подписи
  for (const edit of EDITS) {
    const found = await findNode(page, edit);
    if (!found.nodes.length) {
      fail(edit.where, "нода не найдена" + (found.foundBy ? " — " + found.foundBy : ""));
      continue;
    }
    try {
      for (const node of found.nodes) {
        const swaps = await prepareFonts(node);
        const result = replaceTail(node, edit.to);
        const note = swaps.length ? "шрифт подменён: " + swaps.join(", ") : "";
        if (result.status === "изменено") ok(edit.where, result.was + " → " + result.text, note);
        else skip(edit.where, result.text);
      }
    } catch (err) {
      fail(edit.where, String(err.message || err));
    }
  }

  // 2. колонка с Telegram ID
  for (const target of DROPS) {
    const frame = await resolveFrame(page, target, fail, "кадр");
    if (!frame) continue;
    try {
      const removed = dropIdColumn(frame);
      if (removed) ok(target.where, "убрано нод: " + removed);
      else skip(target.where, "идентификаторов в строках нет");
    } catch (err) {
      fail(target.where, String(err.message || err));
    }
  }

  // 3. число записей на странице
  for (const target of TRIMS) {
    const frame = await resolveFrame(page, target, fail, "кадр");
    if (!frame) continue;
    try {
      const trimmed = trimRows(frame, target.keep);
      const notes = [];
      if (trimmed.rows) notes.push("убрано строк: " + trimmed.rows + ", осталось " + target.keep);
      if (trimmed.shells) notes.push("убрано пустых кнопок: " + trimmed.shells);
      if (notes.length) ok(target.where, notes.join("; "));
      else skip(target.where, "строк уже " + target.keep + " или меньше, пустых кнопок нет");

      const pager = retitlePager(frame, target.keep);
      if (!pager) {
        skip(target.where + " — пагинация", "подпись не найдена, пропускаю");
      } else {
        await prepareFonts(pager.node);
        const result = replaceTail(pager.node, pager.want);
        if (result.status === "изменено") {
          ok(target.where + " — пагинация", result.was + " → " + result.text);
        } else {
          skip(target.where + " — пагинация", result.text);
        }
      }
    } catch (err) {
      fail(target.where, String(err.message || err));
    }
  }

  // 4. сетка кнопок
  for (const target of REGROUPS) {
    const container = await resolveFrame(page, target, fail, "контейнер рядов");
    if (!container) continue;
    try {
      const touched = regroupIntoOneRow(container, target.labels);
      if (touched) ok(target.where, "кнопок в ряду: " + target.labels.length);
      else skip(target.where, "кнопки одним рядом и одного размера");
    } catch (err) {
      fail(target.where, String(err.message || err));
    }
  }

  // 5. недостающие строки и кнопки
  for (const target of INSERTS) {
    const frame = await resolveFrame(page, target, fail, "кадр");
    if (!frame) continue;
    try {
      const added = await insertClones(frame, target);
      if (added) ok(target.where, "добавлено нод: " + added + " (" + target.texts.join(", ") + ")");
      else skip(target.where, "уже есть");
    } catch (err) {
      fail(target.where, String(err.message || err));
    }
  }

  // 6. разведение пар кнопок по отдельным рядам
  for (const target of SPLITS) {
    const container = await resolveFrame(page, target, fail, "ряд");
    if (!container) continue;
    try {
      const moved = splitRow(container);
      if (moved) ok(target.where, "разведено кнопок: " + moved);
      else skip(target.where, "кнопки уже по отдельным рядам");
    } catch (err) {
      fail(target.where, String(err.message || err));
    }
  }

  // 7. возврат пар кнопок в один ряд и выравнивание ширины
  for (const target of PAIRS) {
    const frame = await resolveFrame(page, target, fail, "кадр");
    if (!frame) continue;
    try {
      const paired = await pairButtons(frame, target);
      if (paired) ok(target.where, "кнопок в ряду: " + paired);
      else skip(target.where, "кнопки уже в одном ряду");
    } catch (err) {
      fail(target.where, String(err.message || err));
    }
  }

  for (const target of ROWS) {
    const column = await resolveFrame(page, target, fail, "контейнер рядов");
    if (!column) continue;
    try {
      const placed = await layoutRows(column, target);
      if (placed) ok(target.where, "разложено кнопок: " + placed);
      else skip(target.where, "кнопки уже стоят этой сеткой");
    } catch (err) {
      fail(target.where, String(err.message || err));
    }
  }

  for (const target of WIDTHS) {
    const frame = await resolveFrame(page, target, fail, "кадр");
    if (!frame) continue;
    try {
      const fixed = normalizeWidths(frame, target);
      if (fixed) ok(target.where, "выровнено кнопок: " + fixed);
      else skip(target.where, "ширина уже как у соседей");
    } catch (err) {
      fail(target.where, String(err.message || err));
    }
  }

  for (const target of ADOPTS) {
    try {
      const adopted = await adoptNode(target);
      if (adopted) ok(target.where, "кнопка «" + target.label + "» возвращена в кадр");
      else skip(target.where, "кнопка уже в кадре");
    } catch (err) {
      fail(target.where, String(err.message || err));
    }
  }

  // 8. перестановка строк
  for (const target of REORDERS) {
    const frame = await resolveFrame(page, target, fail, "кадр");
    if (!frame) continue;
    try {
      const moved = reorderLine(frame, target);
      if (moved) ok(target.where, "строка «" + target.move + "» переставлена");
      else skip(target.where, "строка уже на месте");
    } catch (err) {
      fail(target.where, String(err.message || err));
    }
  }

  // 9. раскладка колонки — после всех правок, по фактическим высотам
  for (const target of COLUMNS) {
    try {
      const moved = await relayoutColumn(target);
      if (moved) ok(target.where, "кадров переставлено: " + moved);
      else skip(target.where, "кадры уже стоят ровно");
    } catch (err) {
      fail(target.where, String(err.message || err));
    }
  }

  // 10. родной шрифт вместо подменённого
  try {
    const fonts = await restoreFonts(page);
    if (fonts.restored) {
      ok("Шрифт", "возвращено нод на «" + RESTORE_TO + "»: " + fonts.restored);
    } else if (!fonts.available) {
      skip(
        "Шрифт",
        "нод с подменённым шрифтом: " + fonts.count + ", но «" + RESTORE_TO +
        "» в системе нет — установите его и запустите плагин ещё раз",
      );
    } else {
      skip("Шрифт", "подменённых нод нет");
    }
  } catch (err) {
    fail("Шрифт", String(err.message || err));
  }

  // 11. разведка — ничего не меняет
  for (const target of PROBES) {
    const frame = await resolveFrame(page, target, fail, "кадр");
    if (!frame) continue;
    rows.push({
      state: "probe",
      where: target.where,
      text: describe(frame, 3, "").join("\n"),
    });
  }

  return {
    rows: rows, changed: changed, already: already, failed: failed,
    dump: serializePage(page),
  };
}

// --- выгрузка дампа ----------------------------------------------------------
// `docs/design/figma.json` — то, по чему сверяются бот и макет (`figma_read.py`,
// `figma_screens.py`, `figma_buttons.py` и два теста). Снимался он REST-API, а
// для этого нужен personal access token, которого у нас нет; Figma MCP оригинал
// не читает вовсе, да и лимит там 20 вызовов в месяц. Выходило, что после
// каждого прогона плагина дамп устаревал и сверка врала — так 20.09.2026 кадр
// `341:1248` ещё показывал расхождение, хотя в файле его уже не было.
//
// Плагин работает внутри Figma и видит документ целиком, поэтому дамп он может
// снять сам: ни токена, ни лимита, ни сети (в манифесте networkAccess: none).
// Поля — ровно те, что читают наши скрипты; лишнее не тащим, файл и так на
// несколько мегабайт.
function serializeNode(node) {
  const out = { id: node.id, type: node.type, name: node.name };
  if (node.type === "TEXT") out.characters = node.characters;
  if (typeof node.cornerRadius === "number") out.cornerRadius = node.cornerRadius;
  if (node.layoutMode && node.layoutMode !== "NONE") {
    out.layoutMode = node.layoutMode;
    out.itemSpacing = node.itemSpacing;
  }
  if ("layoutSizingHorizontal" in node) out.layoutSizingHorizontal = node.layoutSizingHorizontal;
  // Баннер отличается от кнопки заливкой картинкой — по ней считается, на каких
  // кадрах баннер вообще нарисован (docs/figma_diff_2026-09-20.md).
  if (node.fills && node.fills !== figma.mixed && node.fills.length) {
    out.fills = node.fills.map((f) => ({ type: f.type, visible: f.visible !== false }));
  }
  const box = node.absoluteBoundingBox;
  if (box) out.absoluteBoundingBox = { x: box.x, y: box.y, width: box.width, height: box.height };
  if ("children" in node) out.children = node.children.map(serializeNode);
  return out;
}

function serializePage(page) {
  return {
    name: figma.root.name,
    // В Plugin API времени последней правки нет — ставим момент выгрузки и
    // честно помечаем, чем снято: иначе не отличить от выгрузки REST-API.
    lastModified: new Date().toISOString(),
    exportedBy: "figma-sync-plugin",
    document: { id: "0:0", type: "DOCUMENT", name: "Document", children: [serializeNode(page)] },
  };
}

figma.showUI(__html__, { width: 640, height: 560 });

run().then(
  (report) => figma.ui.postMessage({ type: "report", report: report }),
  (err) => figma.ui.postMessage({ type: "fatal", message: String((err && err.message) || err) })
);

figma.ui.onmessage = (msg) => {
  if (msg === "close") figma.closePlugin();
};

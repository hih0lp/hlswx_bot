from aiogram.fsm.state import State, StatesGroup


class AdminFlow(StatesGroup):
    whitelist_username = State()
    whitelist_found = State()
    whitelist_cities = State()
    # Кадр 338:537 — развилка «Все чаты / Выбрать чаты» перед мультивыбором.
    whitelist_chats_mode = State()
    whitelist_chats = State()
    whitelist_comment = State()
    wlbl_marketplace_id = State()
    wlbl_find = State()
    premium_banner_photo = State()
    broadcast_text = State()
    broadcast_confirm = State()
    tariff_price = State()
    # Пополнение обучающей выборки из панели: один пример текстом либо файл
    # с пачкой примеров (.xlsx / .csv / .json / .zip / картинка с OCR).
    ml_sample_text = State()
    ml_sample_file = State()
    # Добавление города и чата — по два шага, как в кадрах макета 368:637 и
    # 368:550: сначала название, потом подтверждение (город) либо username/ID
    # группы (чат).
    city_add = State()
    city_add_confirm = State()
    chat_add = State()
    chat_add_target = State()
    access_user = State()
    message_recipient = State()
    message_text = State()
    message_confirm = State()
    many_recipient = State()
    many_text = State()
    many_confirm = State()
    whitelist_term = State()
    whitelist_term_date = State()
    whitelist_find = State()
    # Кадр 349:954 — поиск в разделе «Пользователи». Своё состояние, а не
    # `whitelist_find`: там ищут запись белого списка, здесь — пользователя.
    users_find = State()
    # Своё состояние, а не PublicationEditFlow: тот перехватывает
    # publication_actions_router, включённый раньше админского, и там правка
    # разрешена только автору объявления (ТЗ 6.9).
    publication_text = State()

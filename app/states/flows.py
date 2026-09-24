from aiogram.fsm.state import State, StatesGroup


class SubscriptionFlow(StatesGroup):
    waiting_text = State()
    selecting_volume = State()
    selecting_city = State()
    selecting_chats = State()


class PackageFlow(StatesGroup):
    selecting_city = State()
    selecting_tariff = State()
    waiting_post_payment_text = State()


class PublishFlow(StatesGroup):
    choosing_city = State()
    waiting_text = State()
    waiting_contact = State()


class PublicationEditFlow(StatesGroup):
    waiting_text = State()


class TopUpFlow(StatesGroup):
    waiting_amount = State()
    waiting_method = State()


class WhitelabelApplyFlow(StatesGroup):
    brand_title = State()
    bot_username = State()
    comment = State()

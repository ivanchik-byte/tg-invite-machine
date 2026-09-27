from aiogram.fsm.state import State, StatesGroup

class AccountState(StatesGroup):
    waiting_for_file = State()
    waiting_for_password = State()

class ProxyState(StatesGroup):
    waiting_for_input = State()

class ParserState(StatesGroup):
    waiting_for_chat = State()
    waiting_for_days = State()

class InviterState(StatesGroup):
    waiting_for_target_group = State()
    waiting_for_migrate_confirm = State()
    waiting_for_invite_limit = State()
    waiting_for_custom_delay = State()

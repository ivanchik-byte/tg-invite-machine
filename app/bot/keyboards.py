from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

def main_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Аккаунты", callback_data="nav_accounts"),
        InlineKeyboardButton(text="Прокси", callback_data="nav_proxies")
    )
    builder.row(
        InlineKeyboardButton(text="Сбор аудитории", callback_data="nav_parser"),
        InlineKeyboardButton(text="Инвайтер", callback_data="nav_inviter")
    )
    builder.row(
        InlineKeyboardButton(text="Общая статистика", callback_data="nav_stats")
    )
    return builder.as_markup()

def accounts_menu_keyboard(has_accounts: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Загрузить архив TData", callback_data="acc_upload_tdata"),
        InlineKeyboardButton(text="Загрузить .session", callback_data="acc_upload_session")
    )
    if has_accounts:
        builder.row(
            InlineKeyboardButton(text="Проверить валидность", callback_data="acc_check_all"),
            InlineKeyboardButton(text="Проверить @SpamBot", callback_data="acc_check_spambot")
        )
        builder.row(
            InlineKeyboardButton(text="Выгрузить Excel (.xlsx)", callback_data="acc_export_excel"),
            InlineKeyboardButton(text="Очистить забаненные", callback_data="acc_purge_banned")
        )
        builder.row(
            InlineKeyboardButton(text="Список аккаунтов", callback_data="acc_list_0")
        )
    builder.row(
        InlineKeyboardButton(text="Назад в меню", callback_data="nav_main")
    )
    return builder.as_markup()

def proxies_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Добавить прокси списком", callback_data="proxy_add"),
        InlineKeyboardButton(text="Проверить соединение", callback_data="proxy_check")
    )
    builder.row(
        InlineKeyboardButton(text="Назад в меню", callback_data="nav_main")
    )
    return builder.as_markup()

def parser_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Собрать всех участников", callback_data="parse_all"),
        InlineKeyboardButton(text="Собрать активных по дням", callback_data="parse_active")
    )
    builder.row(
        InlineKeyboardButton(text="Выгрузить базу в файл", callback_data="parse_export")
    )
    builder.row(
        InlineKeyboardButton(text="Назад в меню", callback_data="nav_main")
    )
    return builder.as_markup()

def inviter_menu_keyboard(task_running: bool = False, is_paused: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if not task_running:
        builder.row(
            InlineKeyboardButton(text="Запустить инвайт", callback_data="invite_start"),
            InlineKeyboardButton(text="Профиль скорости", callback_data="invite_speed")
        )
    elif is_paused:
        builder.row(
            InlineKeyboardButton(text="Возобновить", callback_data="invite_resume"),
            InlineKeyboardButton(text="Остановить", callback_data="invite_stop")
        )
    else:
        builder.row(
            InlineKeyboardButton(text="Пауза", callback_data="invite_pause"),
            InlineKeyboardButton(text="Остановить", callback_data="invite_stop")
        )
    builder.row(
        InlineKeyboardButton(text="Назад в меню", callback_data="nav_main")
    )
    return builder.as_markup()

def speed_profile_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Осторожный (50-110с)", callback_data="set_speed_cautious"),
        InlineKeyboardButton(text="Обычный (35-75с)", callback_data="set_speed_normal"),
        InlineKeyboardButton(text="Быстрый (17-37с)", callback_data="set_speed_fast")
    )
    builder.row(
        InlineKeyboardButton(text="Назад", callback_data="nav_inviter")
    )
    return builder.as_markup()

def back_keyboard(callback_target: str = "nav_main") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Назад", callback_data=callback_target)
    )
    return builder.as_markup()

def migrate_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Да, мигрировать", callback_data="migrate_confirm"))
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="migrate_cancel"))
    return builder.as_markup()

def accounts_pagination_keyboard(offset: int, limit: int, total: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    nav_buttons = []
    if offset > 0:
        prev_offset = max(0, offset - limit)
        nav_buttons.append(InlineKeyboardButton(text="Назад", callback_data=f"acc_list_{prev_offset}"))
    if offset + limit < total:
        next_offset = offset + limit
        nav_buttons.append(InlineKeyboardButton(text="Вперед", callback_data=f"acc_list_{next_offset}"))
    if nav_buttons:
        builder.row(*nav_buttons)
    builder.row(InlineKeyboardButton(text="К списку меню", callback_data="nav_accounts"))
    return builder.as_markup()

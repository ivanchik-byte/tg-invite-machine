from typing import Optional
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
        InlineKeyboardButton(text="Телеметрия", callback_data="nav_stats"),
        InlineKeyboardButton(text="Обновить статус", callback_data="nav_main_refresh")
    )
    return builder.as_markup()

def accounts_menu_keyboard(has_accounts: bool = True, cooldown_count: int = 0) -> InlineKeyboardMarkup:
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
        if cooldown_count > 0:
            builder.row(
                InlineKeyboardButton(text=f"Сбросить отлежку ({cooldown_count})", callback_data="acc_reset_cooldowns")
            )
        builder.row(
            InlineKeyboardButton(text="Выгрузить Excel (.xlsx)", callback_data="acc_export_excel"),
            InlineKeyboardButton(text="Очистить забаненные", callback_data="acc_purge_banned")
        )
        builder.row(
            InlineKeyboardButton(text="Список аккаунтов", callback_data="acc_list_0"),
            InlineKeyboardButton(text="Удалить все", callback_data="acc_purge_all_confirm")
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
        InlineKeyboardButton(text="Привязать к аккаунтам", callback_data="proxy_auto_bind"),
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
        InlineKeyboardButton(text="Выгрузить базу в файл", callback_data="parse_export"),
        InlineKeyboardButton(text="Очистить базу", callback_data="parse_clear")
    )
    builder.row(
        InlineKeyboardButton(text="Назад в меню", callback_data="nav_main")
    )
    return builder.as_markup()

def inviter_menu_keyboard(
    task_running: bool = False,
    is_paused: bool = False,
    daily_limit: Optional[int] = None,
    privacy_blacklist: bool = True,
    paused_task_id: Optional[int] = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if not task_running:
        if paused_task_id is not None:
            builder.row(
                InlineKeyboardButton(text=f"Возобновить задачу #{paused_task_id}", callback_data=f"invite_resume_paused_{paused_task_id}")
            )
        builder.row(
            InlineKeyboardButton(text="Запустить инвайт", callback_data="invite_start"),
            InlineKeyboardButton(text="Профиль скорости", callback_data="invite_speed")
        )
        limit_text = f"Дневной лимит ({daily_limit})" if daily_limit else "Дневной лимит"
        builder.row(
            InlineKeyboardButton(text=limit_text, callback_data="invite_daily_limit"),
            InlineKeyboardButton(text="Сброс лимитов", callback_data="invite_reset_limits")
        )
        bl_text = "Блэклист приватности: [ВКЛ]" if privacy_blacklist else "Блэклист приватности: [ВЫКЛ]"
        builder.row(
            InlineKeyboardButton(text=bl_text, callback_data="invite_toggle_blacklist")
        )
        builder.row(
            InlineKeyboardButton(text="История инвайтов", callback_data="invite_history")
        )

    elif is_paused:
        builder.row(
            InlineKeyboardButton(text="Возобновить", callback_data="invite_resume"),
            InlineKeyboardButton(text="Остановить", callback_data="invite_stop")
        )
        builder.row(
            InlineKeyboardButton(text="История инвайтов", callback_data="invite_history")
        )
    else:
        builder.row(
            InlineKeyboardButton(text="Пауза", callback_data="invite_pause"),
            InlineKeyboardButton(text="Остановить", callback_data="invite_stop")
        )
        builder.row(
            InlineKeyboardButton(text="История инвайтов", callback_data="invite_history")
        )
    builder.row(
        InlineKeyboardButton(text="Назад в меню", callback_data="nav_main")
    )
    return builder.as_markup()

def daily_limit_keyboard(current_limit: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    presets = [10, 20, 30, 50, 100]
    row1 = [InlineKeyboardButton(text=f"{'• ' if current_limit == v else ''}{v}", callback_data=f"set_daily_{v}") for v in presets[:3]]
    row2 = [InlineKeyboardButton(text=f"{'• ' if current_limit == v else ''}{v}", callback_data=f"set_daily_{v}") for v in presets[3:]]
    builder.row(*row1)
    builder.row(*row2)
    custom_label = f"• Свой лимит: {current_limit}" if current_limit not in presets else "Свой суточный лимит"
    builder.row(InlineKeyboardButton(text=custom_label, callback_data="set_daily_custom"))
    builder.row(InlineKeyboardButton(text="Назад в инвайтер", callback_data="nav_inviter"))
    return builder.as_markup()

def speed_profile_keyboard(current_profile: str = "normal") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    c_mark = "• " if current_profile == "cautious" else ""
    n_mark = "• " if current_profile == "normal" else ""
    f_mark = "• " if current_profile == "fast" else ""
    builder.row(
        InlineKeyboardButton(text=f"{c_mark}Осторожный (50-110с)", callback_data="set_speed_cautious"),
        InlineKeyboardButton(text=f"{n_mark}Обычный (35-75с)", callback_data="set_speed_normal"),
        InlineKeyboardButton(text=f"{f_mark}Быстрый (17-37с)", callback_data="set_speed_fast")
    )
    custom_mark = "• " if current_profile.startswith("custom:") else ""
    clean_range = current_profile.replace("custom:", "").replace(":", "-")
    custom_text = (
        f"{custom_mark}Свой интервал: {clean_range}с"
        if current_profile.startswith("custom:")
        else "Свой интервал"
    )
    builder.row(
        InlineKeyboardButton(text=custom_text, callback_data="set_speed_custom")
    )
    builder.row(
        InlineKeyboardButton(text="Назад", callback_data="nav_inviter")
    )
    return builder.as_markup()

def inviter_config_keyboard(selected_limit: Optional[int], current_profile: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    chips = [5, 10, 20, 50]
    limit_buttons = []
    for val in chips:
        mark = "• " if selected_limit == val else ""
        limit_buttons.append(
            InlineKeyboardButton(text=f"{mark}{val}", callback_data=f"cfg_limit_{val}")
        )
    all_mark = "• " if selected_limit is None else ""
    limit_buttons.append(
        InlineKeyboardButton(text=f"{all_mark}Все", callback_data="cfg_limit_all")
    )
    builder.row(*limit_buttons)

    custom_limit_label = (
        f"• Свой лимит: {selected_limit}"
        if (selected_limit is not None and selected_limit not in chips)
        else "Свой лимит"
    )
    builder.row(InlineKeyboardButton(text=custom_limit_label, callback_data="cfg_limit_custom"))

    c_mark = "• " if current_profile == "cautious" else ""
    n_mark = "• " if current_profile == "normal" else ""
    f_mark = "• " if current_profile == "fast" else ""
    builder.row(
        InlineKeyboardButton(text=f"{c_mark}Осторожный (50-110с)", callback_data="cfg_speed_cautious"),
        InlineKeyboardButton(text=f"{n_mark}Обычный (35-75с)", callback_data="cfg_speed_normal"),
        InlineKeyboardButton(text=f"{f_mark}Быстрый (17-37с)", callback_data="cfg_speed_fast"),
    )

    custom_speed_mark = "• " if current_profile.startswith("custom:") else ""
    clean_range = current_profile.replace("custom:", "").replace(":", "-")
    custom_speed_text = (
        f"{custom_speed_mark}Свой интервал: {clean_range}с"
        if current_profile.startswith("custom:")
        else "Свой интервал"
    )
    builder.row(InlineKeyboardButton(text=custom_speed_text, callback_data="cfg_speed_custom"))

    builder.row(InlineKeyboardButton(text="Запустить инвайтинг", callback_data="cfg_launch"))
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="cfg_cancel"))

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

def accounts_pagination_keyboard(offset: int, limit: int, total: int, account_items: list | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if account_items:
        for acc_id, name in account_items:
            builder.row(
                InlineKeyboardButton(text=f"Удалить #{acc_id} {name}", callback_data=f"acc_del_{acc_id}_{offset}")
            )
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

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

def proxies_menu_keyboard(has_proxies: bool = False, dead_count: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Добавить прокси списком", callback_data="proxy_add"),
        InlineKeyboardButton(text="Проверить соединение", callback_data="proxy_check")
    )
    if has_proxies:
        builder.row(
            InlineKeyboardButton(text="Список прокси", callback_data="proxy_list_0"),
            InlineKeyboardButton(text="Автопривязка", callback_data="proxy_auto_bind"),
        )
        purge_buttons = []
        if dead_count > 0:
            purge_buttons.append(
                InlineKeyboardButton(text=f"Очистить нерабочие ({dead_count})", callback_data="proxy_purge_dead_confirm")
            )
        purge_buttons.append(
            InlineKeyboardButton(text="Удалить все", callback_data="proxy_purge_all_confirm")
        )
        builder.row(*purge_buttons)
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
    recent_only: bool = False,
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
        recent_text = "Только недавно в сети: [ВКЛ]" if recent_only else "Только недавно в сети: [ВЫКЛ]"
        builder.row(
            InlineKeyboardButton(text=recent_text, callback_data="invite_toggle_recent")
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

def speed_rows(builder: InlineKeyboardBuilder, current_profile: str, prefix: str) -> None:
    mark = lambda name: "• " if current_profile == name else ""
    builder.row(
        InlineKeyboardButton(text=f"{mark('cautious')}Осторожный (50-110с)", callback_data=f"{prefix}_cautious"),
        InlineKeyboardButton(text=f"{mark('normal')}Обычный (35-75с)", callback_data=f"{prefix}_normal"),
        InlineKeyboardButton(text=f"{mark('fast')}Быстрый (17-37с)", callback_data=f"{prefix}_fast")
    )
    custom_mark = "• " if current_profile.startswith("custom:") else ""
    clean_range = current_profile.replace("custom:", "").replace(":", "-")
    custom_text = (
        f"{custom_mark}Свой интервал: {clean_range}с"
        if current_profile.startswith("custom:")
        else "Свой интервал"
    )
    builder.row(InlineKeyboardButton(text=custom_text, callback_data=f"{prefix}_custom"))

def speed_profile_keyboard(current_profile: str = "normal") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    speed_rows(builder, current_profile, "set_speed")
    builder.row(
        InlineKeyboardButton(text="Назад", callback_data="nav_inviter")
    )
    return builder.as_markup()

def inviter_config_keyboard(selected_limit: Optional[int], current_profile: str, recent_only: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    carousel_mark = "• " if selected_limit is None else ""
    target_mark = "• " if selected_limit is not None else ""
    builder.row(
        InlineKeyboardButton(text=f"{carousel_mark}Карусель (Safe)", callback_data="cfg_mode_carousel"),
        InlineKeyboardButton(text=f"{target_mark}Целевой план (Target)", callback_data="cfg_mode_target"),
    )
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

    speed_rows(builder, current_profile, "cfg_speed")

    builder.row(InlineKeyboardButton(text="Запустить инвайтинг", callback_data="cfg_launch"))
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="cfg_cancel"))

    return builder.as_markup()

def back_keyboard(callback_target: str = "nav_main") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Назад", callback_data=callback_target)
    )
    return builder.as_markup()

def page_nav(builder: InlineKeyboardBuilder, offset: int, limit: int, total: int, prefix: str) -> None:
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"{prefix}_{max(0, offset - limit)}"))
    if offset + limit < total:
        nav.append(InlineKeyboardButton(text="Вперед", callback_data=f"{prefix}_{offset + limit}"))
    if nav:
        builder.row(*nav)

def migrate_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Да, мигрировать", callback_data="migrate_confirm"))
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="migrate_cancel"))
    return builder.as_markup()

def accounts_grid_keyboard(entries: list, offset: int, limit: int, total: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    row = []
    for acc_id, tag, short in entries:
        row.append(InlineKeyboardButton(text=f"{tag} {short}", callback_data=f"acc_view_{acc_id}_{offset}"))
        if len(row) == 2:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)
    page_nav(builder, offset, limit, total, "acc_list")
    builder.row(InlineKeyboardButton(text="К списку меню", callback_data="nav_accounts"))
    return builder.as_markup()


def accounts_pagination_keyboard(offset: int, limit: int, total: int, account_items: list | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if account_items:
        for acc_id, name in account_items:
            builder.row(
                InlineKeyboardButton(text=f"#{acc_id} {name}", callback_data=f"acc_view_{acc_id}_{offset}")
            )
    page_nav(builder, offset, limit, total, "acc_list")
    builder.row(InlineKeyboardButton(text="К списку меню", callback_data="nav_accounts"))
    return builder.as_markup()

def account_view_keyboard(acc_id: int, offset: int, has_proxy: bool = False, excluded: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    excl_text = "Убрать из задачи [SKIP]" if excluded else "В задаче [ВКЛ]"
    builder.row(
        InlineKeyboardButton(text=excl_text, callback_data=f"acc_excl_{acc_id}_{offset}")
    )
    builder.row(
        InlineKeyboardButton(text="Выбрать/сменить прокси", callback_data=f"acc_proxy_pick_{acc_id}_{offset}")
    )
    if has_proxy:
        builder.row(
            InlineKeyboardButton(text="Отвязать прокси", callback_data=f"acc_proxy_detach_{acc_id}_{offset}")
        )
    builder.row(
        InlineKeyboardButton(text="Удалить аккаунт", callback_data=f"acc_del_{acc_id}_{offset}")
    )
    builder.row(
        InlineKeyboardButton(text="Назад к списку", callback_data=f"acc_list_{offset}")
    )
    return builder.as_markup()

def account_proxy_pick_keyboard(acc_id: int, offset: int, proxies: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for p_id, label in proxies:
        builder.row(
            InlineKeyboardButton(text=label, callback_data=f"acc_proxy_apply_{acc_id}_{p_id}_{offset}")
        )
    builder.row(
        InlineKeyboardButton(text="Назад к аккаунту", callback_data=f"acc_view_{acc_id}_{offset}")
    )
    return builder.as_markup()

def proxy_pagination_keyboard(offset: int, limit: int, total: int, proxy_items: list | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if proxy_items:
        for p_id, label in proxy_items:
            builder.row(
                InlineKeyboardButton(text=label, callback_data=f"proxy_view_{p_id}_{offset}")
            )
    page_nav(builder, offset, limit, total, "proxy_list")
    builder.row(
        InlineKeyboardButton(text="Проверить все", callback_data="proxy_check"),
        InlineKeyboardButton(text="К меню прокси", callback_data="nav_proxies")
    )
    return builder.as_markup()

def proxy_view_keyboard(proxy_id: int, offset: int, has_accounts: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Привязать к аккаунту", callback_data=f"proxy_bind_pick_{proxy_id}_{offset}")
    )
    if has_accounts:
        builder.row(
            InlineKeyboardButton(text="Отвязать все аккаунты", callback_data=f"proxy_unbind_{proxy_id}_{offset}")
        )
    builder.row(
        InlineKeyboardButton(text="Проверить этот прокси", callback_data=f"proxy_probe_{proxy_id}_{offset}"),
        InlineKeyboardButton(text="Удалить прокси", callback_data=f"proxy_del_{proxy_id}_{offset}")
    )
    builder.row(
        InlineKeyboardButton(text="Назад к списку", callback_data=f"proxy_list_{offset}")
    )
    return builder.as_markup()

def proxy_bind_account_select_keyboard(proxy_id: int, offset: int, accounts: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for acc_id, label in accounts:
        builder.row(
            InlineKeyboardButton(text=label, callback_data=f"proxy_bind_set_{proxy_id}_{acc_id}_{offset}")
        )
    builder.row(
        InlineKeyboardButton(text="Назад к прокси", callback_data=f"proxy_view_{proxy_id}_{offset}")
    )
    return builder.as_markup()

def proxy_purge_dead_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Да, удалить нерабочие", callback_data="proxy_purge_dead_exec"),
        InlineKeyboardButton(text="Отмена", callback_data="nav_proxies")
    )
    return builder.as_markup()

def proxy_purge_all_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Да, удалить ВСЕ прокси", callback_data="proxy_purge_all_exec"),
        InlineKeyboardButton(text="Отмена", callback_data="nav_proxies")
    )
    return builder.as_markup()


# Архитектура системы TG-Invite-Machine

Документ описывает техническое устройство, внутренние модули, алгоритмы обработки данных, схемы конвейеров инвайтинга и структуру базы данных TG-Invite-Machine версии v0.1.0-stable.

---

## 1. Обзор архитектуры

TG-Invite-Machine спроектирован по принципу разделения плоскости управления (Control Plane) и плоскости исполнения (Data Plane).

Плоскость управления реализована на фреймворке aiogram 3 и предоставляет интерактивный интерфейс администратора внутри Telegram. Плоскость исполнения состоит из пула асинхронных воркеров Telethon, изолированных через индивидуальные SOCKS5/HTTP прокси.

```
                  +----------------------------------------------+
                  |            Администратор Telegram            |
                  +----------------------+-----------------------+
                                         |
                                         | Telegram Bot API (HTTPS)
                                         v
                  +----------------------------------------------+
                  |         Control Plane (aiogram 3.x)          |
                  |                                              |
                  |  - AdminOnlyMiddleware (RBAC проверка ID)    |
                  |  - FSM контекст (сбор, лимиты, интервалы)    |
                  |  - Inline UI & Панель предстартовой настройки|
                  +----------------------+-----------------------+
                                         |
                                         v
                  +----------------------------------------------+
                  |           Сервисный слой и оркестратор       |
                  |                                              |
                  |  - InviterOrchestrator & TaskManager         |
                  |  - CollectorService (активные / все)         |
                  |  - AccountService & ProxyService             |
                  |  - SpamBotService & ExportService            |
                  +-----------+----------------------+-----------+
                              |                      |
                              v                      v
+-------------------------------+                  +-------------------------------+
|      Data & Storage Layer     |                  |   Data Plane (Telethon Pool)  |
|                               |                  |                               |
| - SQLite WAL / PostgreSQL     |                  | - Worker 1 -> SOCKS5 Прокси A |
| - SQLAlchemy 2.0 Async        |                  | - Worker 2 -> SOCKS5 Прокси B |
| - Fernet Cryptography Vault   |                  | - Worker N -> SOCKS5 Прокси N |
| - Strict Zero-Leak Policy     |                  | - MTProto Client Factory      |
+-------------------------------+                  +---------------+---------------+
                                                                   |
                                                                   | MTProto (TCP / SOCKS5)
                                                                   v
                                                   +-------------------------------+
                                                   |       Telegram Cloud API      |
                                                   +-------------------------------+
```

---

## 2. Модульная структура репозитория

```
tg-invite-machine/
├── app/
│   ├── bot/
│   │   ├── handlers/
│   │   │   ├── accounts.py       # Загрузка сессий, TData, ввод 2FA, аудит
│   │   │   ├── common.py         # Главное меню и системная статистика
│   │   │   ├── inviter.py        # Настройка лимитов, интервалов, запуск и контроль задач
│   │   │   ├── parser.py         # Настройка и запуск сбора участников
│   │   │   └── proxies.py        # Добавление и валидация SOCKS5/HTTP прокси
│   │   ├── middlewares/
│   │   │   └── auth.py           # Проверка прав администратора бота
│   │   ├── keyboards.py          # Меню, пагинация и чипы конфигурации
│   │   ├── states.py             # FSM состояния (AccountState, InviterState и др.)
│   │   └── main.py               # Точка входа Telegram-бота
│   │
│   ├── core/
│   │   ├── config.py             # Валидация настроек Pydantic v2
│   │   ├── database.py           # Инициализация SQLAlchemy, PRAGMA WAL, сессии
│   │   ├── security.py           # Шифрование Fernet, безопасная распаковка архивов
│   │   └── utils.py              # Нормализация юзернеймов, безопасное редактирование UI
│   │
│   ├── models/
│   │   └── models.py             # Модели: Account, Proxy, TargetGroup, AudienceMember, InviteTask
│   │
│   ├── services/
│   │   ├── account_service.py    # Регистрация и пакетный импорт аккаунтов
│   │   ├── collector_service.py  # Сбор участников по сообщениям и по алфавиту
│   │   ├── export_service.py     # Генерация отчетов Excel (.xlsx) и списков (.txt)
│   │   ├── inviter_service.py    # Оркестратор инвайтинга, расчет задержек, Circuit Breaker
│   │   ├── proxy_service.py      # Парсинг строковых прокси и TCP-проверка
│   │   ├── spambot_service.py    # Автоматический опрос @SpamBot для пула аккаунтов
│   │   └── task_manager.py       # Менеджер активных фоновых задач и UI-троттлинг
│   │
│   └── telegram/
│       ├── client_factory.py     # Создание клиентов Telethon со строгой привязкой к прокси
│       └── converter.py          # Конвертация TData (opentele2) и файлов .session
│
├── data/                         # Директория для inviter.db и экспортированных файлов
├── docs/                         # Техническая документация
├── tests/                        # Набор из 50 тестов pytest
├── Dockerfile                    # Сборка контейнера приложения
├── docker-compose.yml            # Сервисная конфигурация Docker
├── requirements.txt              # Зависимости Python
└── .env.example                  # Шаблон конфигурации
```

---

## 3. Жизненный цикл сессии и безопасность учетных данных

Система поддерживает два способа добавления рабочих номеров в пул: одиночный импорт и пакетную загрузку.

### Конвертация TData через opentele2
При загрузке архива TData (`.zip`):
1. Метод `safe_extract_zip` распаковывает архив во временный каталог, проверяя канонические пути и защищая файловую систему от атак Zip-Slip и Zip-Bomb.
2. Библиотека `opentele2` инициализирует объект `TDesktop`.
3. Вызов `tdesk.ToTelethon(flag=CreateNewSession, password=..., proxy=...)` создает новую сессионную пару ключей с корректным DC без генерации ошибки `AUTH_KEY_DUPLICATED`.
4. Сессионная строка извлекается через `StringSession.save()` и сразу шифруется ключом Fernet.
5. Исходные временные файлы удаляются с диска.

### Двухэтапная аутентификация (2FA)
Если при конвертации TData или чтении файла `.session` библиотека Telegram фиксирует наличие облачного пароля:
1. Вызов возвращает маркер `tdata_password_required`.
2. Бот переходит в состояние `AccountState.waiting_for_password` и запрашивает пароль у администратора.
3. При получении пароля бот немедленно удаляет входящее сообщение (`await message.delete()`), чтобы исключить сохранение учетных данных в открытом виде в чате.
4. Пароль шифруется Fernet и сохраняется в поле `Account.two_fa_password`.

### Сетевая изоляция (Zero-Leak)
Вся сетевая активность рабочих аккаунтов строго привязана к их прокси:
- При включенной настройке `REQUIRE_STRICT_PROXIES=true` фабрика клиентов `client_factory.py` выбрасывает исключение `ProxySecurityError`, если прокси отсутствует или деактивирован.
- Аккаунт ни при каких условиях не выполняет сетевые вызовы с реального IP адреса сервера.

---

## 4. Конвейер инвайтинга и защита от банов

Оркестратор `InviterOrchestrator` управляет распределенным процессом добавления пользователей:

```mermaid
sequenceDiagram
    participant TM as Task Manager
    participant ORCH as Inviter Orchestrator
    participant DB as SQLite / PostgreSQL
    participant TG as Telegram MTProto

    TM->>ORCH: Запуск задачи (task_id, target, limit, profile)
    loop Цикл инвайтинга
        ORCH->>DB: Проверка достижения max_invites
        opt Лимит достигнут
            ORCH->>DB: Статус completed, фиксация finished_at
            ORCH->>TM: Уведомление о завершении
        end
        ORCH->>DB: Выбор доступного аккаунта (лимит < 20, не в отлежке)
        ORCH->>DB: Выбор пользователя из очереди (status = pending)
        ORCH->>TG: Pre-invite: UpdateStatus + чтение сообщений
        ORCH->>TG: InviteToChannelRequest(target, user)
        alt Успешно
            ORCH->>DB: member.status = invited, account.record_invite()
        else FloodWaitError
            ORCH->>DB: account.cooldown = now + wait_time, member.status = deferred
        else PeerFloodError
            ORCH->>DB: account.cooldown = now + 24h, member.status = deferred
        else UserPrivacyRestrictedError
            ORCH->>DB: member.status = restricted
        else AccountBannedError
            ORCH->>DB: account.status = banned, member.status = pending
        end
        ORCH->>ORCH: Пауза по тримодальной модели
    end
```

### Моделирование органических задержек
Функция `calculate_delay` вычисляет задержку между действиями по тримодальной схеме:
- 25% случаев: быстрый клик (диапазон `0.7 * min_delay` до `min_delay`).
- 60% случаев: средний темп (диапазон `min_delay` до `max_delay`).
- 15% случаев: длинная пауза (диапазон `max_delay` до `1.5 * max_delay`).

При кастомном профиле (`custom:min:max`) границы задаются администратором в секундах, сохраняя пропорции тримодального распределения.

### Circuit Breaker
Если в течение 30-минутного скользящего окна фиксируется серия флуд-ошибок, превышающая `CIRCUIT_BREAKER_FLOOD_THRESHOLD`, оркестратор переводит задачу в статус `paused`. Это исключает массовый выход пула сессий из строя при изменении антиспам-политики целевого чата.

---

## 5. Схема базы данных

```mermaid
erDiagram
    PROXIES ||--o{ ACCOUNTS : "назначается"
    ACCOUNTS ||--o{ AUDIENCE_MEMBERS : "приглашает"
    TARGET_GROUPS ||--o{ AUDIENCE_MEMBERS : "содержит"
    TARGET_GROUPS ||--o{ INVITE_TASKS : "цель задачи"

    PROXIES {
        int id PK
        string host
        int port
        string username
        string password "Зашифрован Fernet"
        string protocol "socks5 / http"
        bool is_active
        datetime last_checked_at
    }

    ACCOUNTS {
        int id PK
        string phone UK
        text session_encrypted "Зашифрован Fernet"
        text two_fa_password "Зашифрован Fernet"
        string status "active / cooldown / banned / spambot"
        bool is_active
        int proxy_id FK
        int daily_invites_count
        datetime last_invite_at
        datetime cooldown_until
        int flood_incidents
    }

    TARGET_GROUPS {
        int id PK
        string title
        string username
        bigint tg_id UK
        bigint access_hash
        string chat_type "channel / supergroup / basic_group"
        bool is_active
    }

    AUDIENCE_MEMBERS {
        int id PK
        bigint tg_id
        bigint access_hash
        string username
        string source_chat
        string status "pending / invited / restricted / deferred"
        string reason
        int invited_by_account_id FK
        int target_group_id FK
    }

    INVITE_TASKS {
        int id PK
        int target_group_id FK
        string speed_profile
        string status "pending / running / paused / completed / stopped"
        int max_invites
        int total_targets
        int successful_invites
        int restricted_count
        int flood_errors
        datetime finished_at
    }
```

### Конкурентность и настройки SQLite
Для предотвращения блокировок файла при совместной работе бота и воркеров применяются прагмы:
- `PRAGMA journal_mode=WAL;`
- `PRAGMA synchronous=NORMAL;`
- `connect_args={"timeout": 15}`

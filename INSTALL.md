# Руководство по установке и настройке TG-Invite-Machine

Документ содержит пошаговую инструкцию по установке, подготовке учетных данных Telegram, заполнению файла конфигурации `.env` и запуску сервиса.

---

## 1. Системные требования

* **Операционная система**: Linux (Ubuntu 22.04+, Debian 12+, Rocky Linux), macOS или Windows (WSL2 / PowerShell).
* **Оперативная память**: от 512 МБ RAM.
* **Дисковое пространство**: от 1 ГБ свободного места.
* **Среда выполнения**:
  * Docker 24.0+ и Docker Compose V2 (рекомендуемый способ).
  * Либо Python 3.12+ со стандартными библиотеками разработки.

---

## 2. Подготовка учетных данных

Перед началом настройки необходимо подготовить следующие данные:

1. **Telegram API ID и API Hash**:
   * Авторизуйтесь на официальном портале [my.telegram.org](https://my.telegram.org).
   * Перейдите в раздел **API development tools**.
   * Создайте приложение (название и короткое имя могут быть произвольными).
   * Сохраните полученные `App api_id` (число) и `App api_hash` (строка из 32 символов).

2. **Токен Telegram-бота управления**:
   * Откройте официального бота [@BotFather](https://t.me/BotFather) в Telegram.
   * Отправьте команду `/newbot`, укажите имя и юзернейм бота.
   * Скопируйте полученный токен (формат: `123456789:ABCDefghIJKlmnoPQRstuvWXYZ`).

3. **Ваш Telegram ID (для доступа администратора)**:
   * Узнайте свой числовой ID через любого инфо-бота (например, [@userinfobot](https://t.me/userinfobot)).
   * Доступ к боту управления ограничен: только указанный `ADMIN_ID` сможет взаимодействовать с панелью.

---

## 3. Генерация ключа шифрования

Сессионные токены аккаунтов и 2FA пароли шифруются алгоритмом Fernet. Сгенерируйте секретный ключ:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Пример полученной строки:
```
k_J7b6V9x1Z8qYw2mN5pL0tRt7uIoPaSdFgHjKlZxCv=
```

---

## 4. Настройка файла переменных окружения (.env)

Склонируйте репозиторий и создайте файл `.env` на основе шаблона:

```bash
git clone https://github.com/ivanchik-byte/tg-invite-machine.git
cd tg-invite-machine
cp .env.example .env
```

Откройте `.env` и внесите ваши данные:

```env
# Токен бота управления от @BotFather
BOT_TOKEN=123456789:ABCDefghIJKlmnoPQRstuvWXYZ

# Ваш числовой Telegram ID
ADMIN_ID=123456789

# Учетные данные приложения с my.telegram.org
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef

# Сгенерированный ключ Fernet
ENCRYPTION_KEY=k_J7b6V9x1Z8qYw2mN5pL0tRt7uIoPaSdFgHjKlZxCv=

# Путь к базе данных (SQLite WAL по умолчанию)
DATABASE_URL=sqlite+aiosqlite:///data/inviter.db

# Настройки безопасности и пауз
DEFAULT_SPEED_PROFILE=normal
MIN_DELAY_BETWEEN_INVITES=30
MAX_DELAY_BETWEEN_INVITES=60
MAX_INVITES_PER_SESSION_DAILY=20
CIRCUIT_BREAKER_FLOOD_THRESHOLD=3
REQUIRE_STRICT_PROXIES=true
```

---

## 5. Запуск через Docker Compose (Рекомендуемый)

Docker изолирует все системные зависимости и автоматически монтирует том с базой данных `data/`.

Запуск в фоновом режиме:
```bash
docker compose up -d --build
```

Просмотр журналов работы:
```bash
docker compose logs -f
```

Остановка сервиса:
```bash
docker compose down
```

---

## 6. Локальный запуск без Docker

Если запуск выполняется напрямую на хост-системе:

```bash
# 1. Создание виртуального окружения
python3 -m venv venv

# 2. Активация окружения
source venv/bin/activate  # на Linux / macOS
# .\venv\Scripts\activate # на Windows PowerShell

# 3. Установка зависимостей
pip install --upgrade pip
pip install -r requirements.txt

# 4. Запуск тестов для проверки готовности
pytest -W error

# 5. Запуск приложения
python -m app.main
```

---

## 7. Чеклист решения частых проблем

1. **Бот не отвечает на команду /start**:
   * Проверьте значение `ADMIN_ID` в файле `.env`. Если ID не совпадает с вашим фактическим Telegram ID, middleware заблокирует обработку без вывода ошибок в чат.

2. **Ошибка ProxySecurityError при загрузке аккаунтов**:
   * Включена политика `REQUIRE_STRICT_PROXIES=true`. Сначала добавьте хотя бы один активный рабочий SOCKS5/HTTP прокси в меню Прокси.

3. **Ошибки блокировки SQLite (database is locked)**:
   * Приложение автоматически включает `PRAGMA journal_mode=WAL;` и таймаут ожидания 15 секунд. Убедитесь, что каталог `data/` имеет права на запись для пользователя, от имени которого запущен контейнер.

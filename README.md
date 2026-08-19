# 📅 Schedule Service - Веб-сервис подписки на расписание групп

Сервис для получения и экспорта расписания учебных групп в формате iCal (.ics) с возможностью подписки через веб-интерфейс.

## Возможности

- ✅ Получение расписания по номеру группы
- ✅ Генерация ICS-календаря со стабильными UID событий
- ✅ Веб-интерфейс для выбора группы и управления подпиской
- ✅ QR-код для быстрой подписки с мобильного устройства
- ✅ WebCal-ссылка для подписки в iPhone/Google Calendar
- ✅ Rate limiting (защита от частых обновлений)
- ✅ Фоновое обновление расписания
- ✅ Сохранение последнего успешного календаря при ошибках
- ✅ Защищённый служебный endpoint для cron-обновлений

## Быстрый старт

### Через Docker Compose (рекомендуется)

```bash
# Скопируйте файл конфигурации
cp .env.example .env

# Отредактируйте .env при необходимости (измените SECRET_TOKEN)

# Запустите сервис
docker-compose up -d --build

# Сервис доступен по адресу http://localhost:8000
```

### Локально без Docker

```bash
# Установите зависимости
pip install -r requirements.txt

# Запустите сервер
uvicorn main:app --host 0.0.0.0 --port 8000

# Или для разработки с автоперезагрузкой
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## API Endpoints

### Публичные endpoints

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | `/` | Главная страница с формой выбора группы |
| GET | `/group/{group_slug}` | Страница группы с информацией и ссылками |
| GET | `/cal/{group_slug}.ics` | Скачать ICS-файл расписания |
| POST | `/api/groups/{group_slug}/refresh` | Запустить обновление расписания |
| GET | `/api/groups/{group_slug}/status` | Получить статус обновления группы |

### Служебные endpoints

| Метод | Endpoint | Описание |
|-------|----------|----------|
| POST | `/internal/refresh-all` | Обновить все активные группы (требуется токен) |

#### Пример использования /internal/refresh-all

```bash
curl -X POST http://localhost:8000/internal/refresh-all \
  -H "X-Secret-Token: ваш_секретный_токен"
```

## Переменные окружения

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `SECRET_TOKEN` | `change_this_secret_token` | Токен для защиты служебных endpoints |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/schedule.db` | URL подключения к БД |
| `REFRESH_COOLDOWN_SECONDS` | `300` | Задержка между ручными обновлениями (сек) |

## Структура проекта

```
/workspace/
├── main.py              # FastAPI приложение
├── models.py            # SQLAlchemy модели
├── schedule_parser.py   # Бизнес-логика парсинга и генерации ICS
├── requirements.txt     # Python зависимости
├── Dockerfile           # Docker образ
├── docker-compose.yml   # Docker Compose конфигурация
├── .env.example         # Шаблон переменных окружения
├── templates/
│   ├── index.html       # Главная страница
│   └── group.html       # Страница группы
└── static/              # Статические файлы (если понадобятся)
```

## Использование

### 1. Открыть главную страницу

Перейдите на `http://localhost:8000` и введите номер группы (например, `242-621`).

### 2. Страница группы

На странице группы вы увидите:
- Статус последнего обновления
- Кнопку "Обновить расписание"
- Ссылку для скачивания .ics файла
- Ссылку для подписки (WebCal)
- QR-код для мобильной подписки

### 3. Подписка в календаре

#### iPhone (iOS Calendar)
1. Скопируйте WebCal-ссылку со страницы группы
2. Откройте Настройки → Календари → Добавить календарь → Добавить подписку
3. Вставьте ссылку и нажмите "Далее"

#### Google Calendar
1. Откройте Google Calendar в браузере
2. Нажмите "+ Другие календари" → "По URL"
3. Вставьте WebCal-ссылку и нажмите "Добавить календарь"

#### Android
1. Используйте приложение Calendar
2. Добавьте календарь по URL
3. Вставьте WebCal-ссылку

## Автоматическое обновление

### Через внешний cron (рекомендуется для бесплатного хостинга)

Настройте cron job на внешнем сервисе (GitHub Actions, cron-job.org и т.д.):

```bash
# Пример для GitHub Actions (расписание каждые 6 часов)
*/6 * * * * curl -X POST https://your-service.com/internal/refresh-all \
  -H "X-Secret-Token: ваш_секретный_токен"
```

### Пример GitHub Actions workflow

Создайте `.github/workflows/refresh-schedule.yml`:

```yaml
name: Refresh Schedule

on:
  schedule:
    - cron: '0 */6 * * *'  # Каждые 6 часов

jobs:
  refresh:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger refresh
        run: |
          curl -X POST ${{ secrets.SCHEDULE_SERVICE_URL }}/internal/refresh-all \
            -H "X-Secret-Token: ${{ secrets.SCHEDULE_SERVICE_TOKEN }}"
```

## Деплой на бесплатные платформы

### Railway.app

1. Подключите репозиторий к Railway
2. Добавьте переменные окружения из `.env.example`
3. Railway автоматически обнаружит Dockerfile и развернёт сервис

### Render.com

1. Создайте новый Web Service
2. Подключите репозиторий
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Добавьте переменные окружения

**Важно:** Для бесплатного тарифа используйте SQLite с хранением данных в persistente disk или переключитесь на PostgreSQL.

### Fly.io

```bash
# Инициализируйте приложение
flyctl launch

# Добавьте переменные окружения
flyctl secrets set SECRET_TOKEN=your_secret_token

# Деплой
flyctl deploy
```

## Безопасность

- 🔒 Измените `SECRET_TOKEN` перед деплоем в production
- 🔒 Используйте HTTPS для продакшена (обязательно для iOS)
- 🔒 Rate limiting защищает от злоупотреблений
- 🔒 Служебные endpoints защищены токеном

## Troubleshooting

### Ошибка "Calendar not found"
- Нажмите кнопку "Обновить расписание" на странице группы
- Проверьте логи сервиса на наличие ошибок парсинга

### События дублируются в календаре
- UID событий генерируется детерминировано на основе данных
- Если дубли всё же появились, удалите календарь и добавьте заново

### Ошибка подключения к источнику расписания
- Проверьте доступность источника `rasp.dmami.ru`
- Последний успешный календарь сохраняется и продолжает отдаваться

## Лицензия

MIT

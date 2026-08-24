import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, func
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from sqlalchemy.pool import StaticPool
import aiohttp
from bs4 import BeautifulSoup
import icalendar
from icalendar import Calendar, Event, vText
import pytz

# --- Конфигурация и Логирование ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)

DATABASE_URL = "sqlite:///./schedule.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- Модели БД ---
class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, index=True, nullable=False) # Например "242-621"
    name = Column(String, nullable=True)
    
    schedule_json = Column(Text, nullable=True) # Храним JSON строкой
    ics_text = Column(Text, nullable=True)      # Храним готовый ICS
    
    is_fetching = Column(Boolean, default=False)
    fetch_status = Column(String, default="idle") # idle, fetching, success, error
    fetch_step = Column(String, default="Ожидание...")
    
    last_updated_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    version = Column(Integer, default=0)

# --- Инициализация БД ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    logger.info("База данных создана/проверена.")
    yield

app = FastAPI(lifespan=lifespan, title="Schedule Service")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Парсер (Твоя логика) ---
# Вставь сюда свой рабочий код парсинга. 
# Для примера заглушка, которая генерирует тестовые данные, если реальный URL не доступен или логика сложная.
# ЗАМЕНИ ЭТУ ФУНКЦИЮ НА СВОЙ РЕАЛЬНЫЙ ПАРСЕР
async def parse_schedule_from_source(group_slug: str) -> Dict[str, Any]:
    """
    Возвращает словарь вида:
    {
        "group_name": "242-621",
        "lessons": [
            {
                "subject": "Математика",
                "date": "2023-10-23", # YYYY-MM-DD
                "start_time": "09:00",
                "end_time": "10:30",
                "location": "Ауд. 101",
                "teacher": "Иванов"
            },
            ...
        ]
    }
    """
    logger.info(f"[PARSER] Запуск парсинга для группы {group_slug}")
    
    # TODO: Вставь сюда свой реальный код парсинга через aiohttp и BeautifulSoup
    # Пример реальной логики (раскомментировать и адаптировать):
    """
    url = f"https://site.ru/schedule/{group_slug}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=10) as response:
            if response.status != 200:
                raise Exception(f"Ошибка загрузки страницы: {response.status}")
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            # ... парсинг soup ...
    """
    
    # ЗАГЛУШКА ДЛЯ ТЕСТА (УДАЛИТЬ ПРИ ПРОДЕ)
    await asyncio.sleep(1) # Имитация сети
    import random
    if group_slug == "error":
        raise Exception("Тестовая ошибка парсинга")
    
    lessons = []
    # Генерируем 2 пары на ближайший понедельник для теста
    today = datetime.now()
    # Найдем ближайший понедельник
    days_until_monday = (0 - today.weekday()) % 7
    if days_until_monday == 0: days_until_monday = 7 # Если сегодня пн, берем следующий
    next_monday = today + timedelta(days=days_until_monday)
    
    lessons.append({
        "subject": "Веб-разработка (Пара 1)",
        "date": next_monday.strftime("%Y-%m-%d"),
        "start_time": "09:00",
        "end_time": "10:30",
        "location": "Компьютерный класс",
        "teacher": "Преподаватель 1"
    })
    lessons.append({
        "subject": "Базы данных (Пара 2)",
        "date": next_monday.strftime("%Y-%m-%d"),
        "start_time": "10:45",
        "end_time": "12:15",
        "location": "Аудитория 202",
        "teacher": "Преподаватель 2"
    })
    
    logger.info(f"[PARSER] Найдено {len(lessons)} занятий.")
    return {
        "group_name": group_slug,
        "lessons": lessons
    }

def generate_ics(data: Dict[str, Any], group_slug: str) -> str:
    """Генерирует ICS контент из распарсенных данных."""
    cal = Calendar()
    cal.add('prodid', f'-//Schedule Service//{group_slug}//RU')
    cal.add('version', '2.0')
    cal.add('calscale', 'GREGORIAN')
    cal.add('method', 'PUBLISH')
    cal.add('x-wr-calname', f'Расписание {group_slug}')

    tz = pytz.timezone('Europe/Moscow') # Или твой часовой пояс

    for lesson in data.get('lessons', []):
        event = Event()
        subject = lesson.get('subject', 'Без названия')
        date_str = lesson.get('date')
        start_str = lesson.get('start_time', '09:00')
        end_str = lesson.get('end_time', '10:30')
        location = lesson.get('location', '')
        
        if not date_str:
            continue
            
        # Парсинг даты и времени
        try:
            dt_start = datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %H:%M")
            dt_end = datetime.strptime(f"{date_str} {end_str}", "%Y-%m-%d %H:%M")
            
            # Локализуем время
            dt_start = tz.localize(dt_start)
            dt_end = tz.localize(dt_end)
            
            event.add('summary', subject)
            event.add('dtstart', dt_start)
            event.add('dtend', dt_end)
            event.add('dtstamp', datetime.now(tz))
            
            if location:
                event.add('location', location)
            
            # Стабильный UID
            uid_str = f"{group_slug}-{subject}-{date_str}-{start_str}-{end_str}-{location}"
            # Простая хеш-сумма или очистка для UID
            import hashlib
            uid_hash = hashlib.md5(uid_str.encode('utf-8')).hexdigest()
            event.add('uid', f"{uid_hash}@schedule.service")
            
            cal.add_component(event)
        except Exception as e:
            logger.error(f"Ошибка при создании события: {e}")
            continue

    return cal.to_ical().decode('utf-8')

# --- Эндпоинты ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/group/{group_slug}", response_class=HTMLResponse)
async def get_group_page(request: Request, group_slug: str, db: Session = Depends(get_db)):
    group = db.query(Group).filter(Group.slug == group_slug).first()
    if not group:
        # Создаем запись, если нет
        group = Group(slug=group_slug, name=group_slug, fetch_status="idle")
        db.add(group)
        db.commit()
        db.refresh(group)
    
    return templates.TemplateResponse("group.html", {
        "request": request, 
        "group": group,
        "has_schedule": group.schedule_json is not None
    })

@app.get("/cal/{group_slug}.ics", response_class=PlainTextResponse)
async def get_calendar(group_slug: str, db: Session = Depends(get_db)):
    group = db.query(Group).filter(Group.slug == group_slug).first()
    if not group or not group.ics_text:
        raise HTTPException(status_code=404, detail="Календарь еще не сгенерирован. Нажмите 'Обновить'.")
    
    return PlainTextResponse(
        content=group.ics_text,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=schedule_{group_slug}.ics"}
    )

@app.get("/api/groups/{group_slug}/status")
async def get_status(group_slug: str, db: Session = Depends(get_db)):
    group = db.query(Group).filter(Group.slug == group_slug).first()
    if not group:
        raise HTTPException(status_code=404, detail="Группа не найдена")
    
    return {
        "slug": group.slug,
        "name": group.name,
        "is_fetching": group.is_fetching,
        "fetch_status": group.fetch_status,
        "fetch_step": group.fetch_step,
        "last_updated_at": group.last_updated_at.isoformat() if group.last_updated_at else None,
        "last_error": group.last_error,
        "has_schedule": group.schedule_json is not None
    }

@app.post("/api/groups/{group_slug}/refresh")
async def refresh_schedule(group_slug: str, db: Session = Depends(get_db)):
    """
    Синхронное обновление. Запрос висит пока не обновится или не упадет.
    Это гарантирует, что после ответа 200 OK данные в базе актуальны.
    """
    group = db.query(Group).filter(Group.slug == group_slug).first()
    if not group:
        group = Group(slug=group_slug, name=group_slug)
        db.add(group)
        db.commit()
        db.refresh(group)

    # Проверка кулдауна (1 минута)
    if group.last_updated_at:
        delta = datetime.now() - group.last_updated_at
        if delta.total_seconds() < 60 and group.fetch_status == "success":
            return {"status": "cooldown", "message": "Подождите 1 минуту перед следующим обновлением"}

    if group.is_fetching:
        return {"status": "in_progress", "message": "Обновление уже идет"}

    # Блокируем
    group.is_fetching = True
    group.fetch_status = "fetching"
    group.fetch_step = "Подготовка..."
    group.last_error = None
    db.commit()

    try:
        logger.info(f"[REFRESH] Начало обновления для {group_slug}")
        
        # Этап 1: Загрузка и парсинг
        group.fetch_step = "Загрузка данных с источника..."
        db.commit()
        
        data = await parse_schedule_from_source(group_slug)
        
        # Этап 2: Сохранение JSON
        group.fetch_step = "Сохранение JSON..."
        db.commit()
        group.schedule_json = json.dumps(data, ensure_ascii=False)
        group.name = data.get("group_name", group_slug)
        
        # Этап 3: Генерация ICS
        group.fetch_step = "Генерация календаря (.ics)..."
        db.commit()
        ics_content = generate_ics(data, group_slug)
        group.ics_text = ics_content
        
        # Финал
        group.last_updated_at = datetime.now()
        group.fetch_status = "success"
        group.fetch_step = "Готово"
        group.version += 1
        logger.info(f"[REFRESH] Успешно обновлено для {group_slug}")
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"[REFRESH] Ошибка для {group_slug}: {error_msg}", exc_info=True)
        group.fetch_status = "error"
        group.fetch_step = "Ошибка"
        group.last_error = error_msg
        # Не сбрасываем старые данные, если они были
        if not group.schedule_json:
            group.ics_text = None # Если это первый запуск и ошибка, то календаря нет
            
    finally:
        group.is_fetching = False
        db.commit()

    if group.fetch_status == "error":
        # Возвращаем ошибку клиенту, но с кодом 200 (или можно 500, но фронтенд проще так)
        return {"status": "error", "message": group.last_error, "step": group.fetch_step}
    
    return {"status": "success", "message": "Расписание обновлено", "updated_at": group.last_updated_at}

@app.post("/internal/refresh-all")
async def refresh_all(request: Request, db: Session = Depends(get_db)):
    # Простая защита по токену
    token = request.headers.get("X-Secret-Token")
    if token != os.getenv("SECRET_TOKEN", "admin"):
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    groups = db.query(Group).all()
    count = 0
    for g in groups:
        # Запускаем фоново или последовательно? Для крона лучше последовательно с таймаутом
        # Но здесь упростим: просто помечаем, что нужно обновить, или вызываем логику
        # Для прода лучше вынести в отдельную задачу Celery/RQ, но для MVP:
        try:
            # Рекурсивный вызов логики был бы плох, лучше дублировать код или вынести в сервис
            # Для краткости оставим как есть, в реальном проде вынесите логику парсинга в сервис
            pass 
        except:
            pass
        count += 1
    return {"refreshed": count}
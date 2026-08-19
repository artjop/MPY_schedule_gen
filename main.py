import os
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool
import requests
from bs4 import BeautifulSoup
import icalendar
from icalendar import Calendar, Event, vText
import pytz

# --- Конфигурация и Логирование ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
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
    slug = Column(String, unique=True, index=True, nullable=False)
    schedule_json = Column(Text, nullable=True)  # Храним JSON как строку
    ics_text = Column(Text, nullable=True)
    last_updated_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    is_fetching = Column(Boolean, default=False)
    fetch_stage = Column(String, default="Ожидание...") # Этап обновления

# --- Инициализация БД ---
def init_db():
    Base.metadata.create_all(bind=engine)

# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("База данных инициализирована.")
    yield

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# --- Парсер (Бизнес-логика) ---
def parse_schedule_from_url(url: str, group_slug: str) -> Dict[str, Any]:
    """
    Парсит расписание с сайта. Возвращает список событий.
    """
    logger.info(f"[{group_slug}] Начинаем парсинг URL: {url}")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"[{group_slug}] Ошибка загрузки страницы: {e}")
        raise Exception(f"Не удалось загрузить страницу: {str(e)}")

    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Попытка найти таблицу расписания (адаптируй селекторы под реальный сайт)
    # Пример: ищем таблицу с классом 'schedule' или похожим
    # ВАЖНО: Здесь должна быть твоя рабочая логика парсинга. 
    # Я оставлю заглушку, которую ты заменишь на свой рабочий код парсера.
    
    events = []
    
    # --- ВСТАВЬ СЮДА СВОЙ РАБОЧИЙ КОД ПАРСИНГА ---
    # Пример структуры, которую он должен вернуть:
    # events = [
    #   {
    #     "subject": "Математика",
    #     "date": "2023-10-25",
    #     "start_time": "09:00",
    #     "end_time": "10:30",
    #     "location": "Ауд. 101",
    #     "teacher": "Иванов И.И."
    #   }
    # ]
    
    # Заглушка для демонстрации (УДАЛИ ЭТОТ БЛОК и вставь свой парсер)
    table = soup.find('table')
    if not table:
        # Если таблицы нет, возможно структура изменилась. 
        # Для теста создадим фейковые данные, чтобы проверить пайплайн
        logger.warning(f"[{group_slug}] Таблица не найдена, генерируем тестовые данные.")
        today = datetime.now()
        for i in range(2):
            events.append({
                "subject": f"Пара {i+1} (Тест)",
                "date": today.strftime("%Y-%m-%d"),
                "start_time": f"{9 + i}:00",
                "end_time": f"{10 + i}:30",
                "location": "Онлайн",
                "teacher": "Система"
            })
    else:
        # Тут был бы твой код перебора строк таблицы
        # rows = table.find_all('tr') ...
        logger.info(f"[{group_slug}] Таблица найдена, но логика парсинга требует реализации.")
        # Для примера вернем пустой список, если парсер не реализован детально
        pass
    # -----------------------------------------------

    if not events:
        logger.warning(f"[{group_slug}] События не найдены.")
        
    return {"events": events, "raw_html_len": len(response.text)}

def generate_ics(events: list, group_name: str) -> str:
    cal = Calendar()
    cal.add('prodid', f'-//Schedule App//{group_name}//RU')
    cal.add('version', '2.0')
    cal.add('calscale', 'GREGORIAN')
    cal.add('method', 'PUBLISH')
    cal.add('x-wr-calname', f"Расписание {group_name}")

    tz = pytz.timezone('Europe/Moscow') # Замени на свой часовой пояс

    for ev in events:
        event = Event()
        event.add('summary', ev.get('subject', 'Без названия'))
        
        # Формирование UID для стабильности
        # UID должен быть уникальным и постоянным для одного и того же занятия
        uid_str = f"{group_name}-{ev.get('subject')}-{ev.get('date')}-{ev.get('start_time')}-{ev.get('location')}"
        event.add('uid', uid_str.encode('utf-8').hex())
        
        event.add('dtstamp', datetime.now(tz))
        
        try:
            dt_start = datetime.strptime(f"{ev['date']} {ev['start_time']}", "%Y-%m-%d %H:%M")
            dt_end = datetime.strptime(f"{ev['date']} {ev['end_time']}", "%Y-%m-%d %H:%M")
            
            event.add('dtstart', tz.localize(dt_start))
            event.add('dtend', tz.localize(dt_end))
        except Exception as e:
            logger.error(f"Ошибка даты в событии {ev}: {e}")
            continue

        if ev.get('location'):
            event.add('location', ev.get('location'))
        if ev.get('teacher'):
            event.add('description', f"Преподаватель: {ev.get('teacher')}")
            
        cal.add_component(event)

    return cal.to_ical().decode('utf-8')

# --- Фоновая задача (в потоке) ---
def run_refresh_task(group_slug: str):
    db = SessionLocal()
    try:
        group = db.query(Group).filter(Group.slug == group_slug).first()
        if not group:
            return

        # Этап 1: Загрузка
        group.fetch_stage = "Загрузка данных..."
        group.last_error = None
        db.commit()

        # URL для парсинга (замени на реальный URL для группы)
        # Например: url = f"https://site.com/schedule/{group_slug}"
        url = "https://www.mirea.ru/schedule/" # Заглушка
        
        try:
            result = parse_schedule_from_url(url, group_slug)
            events = result.get('events', [])
            
            # Этап 2: Генерация ICS
            group.fetch_stage = "Генерация календаря..."
            db.commit()
            
            ics_content = generate_ics(events, group_slug)
            
            # Этап 3: Сохранение
            group.fetch_stage = "Сохранение..."
            group.schedule_json = json.dumps(result, ensure_ascii=False)
            group.ics_text = ics_content
            group.last_updated_at = datetime.now()
            group.is_fetching = False
            group.fetch_stage = "Готово"
            db.commit()
            logger.info(f"[{group_slug}] Обновление успешно завершено. Событий: {len(events)}")
            
        except Exception as e:
            logger.error(f"[{group_slug}] Ошибка во время парсинга: {e}")
            group.is_fetching = False
            group.fetch_stage = "Ошибка"
            group.last_error = str(e)
            db.commit()

    except Exception as e:
        logger.critical(f"[{group_slug}] Критическая ошибка в задаче обновления: {e}")
        if 'group' in locals() and group:
            group.is_fetching = False
            group.fetch_stage = "Критическая ошибка"
            group.last_error = str(e)
            db.commit()
    finally:
        db.close()

# --- Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/group/{group_slug}", response_class=HTMLResponse)
async def get_group_page(request: Request, group_slug: str):
    db = SessionLocal()
    group = db.query(Group).filter(Group.slug == group_slug).first()
    
    if not group:
        # Создаем группу, если нет
        group = Group(slug=group_slug, fetch_stage="Никогда не обновлялось")
        db.add(group)
        db.commit()
        db.refresh(group)
    
    db.close()
    return templates.TemplateResponse("group.html", {
        "request": request, 
        "group": group,
        "base_url": str(request.base_url)
    })

@app.post("/api/groups/{group_slug}/refresh")
async def refresh_group(group_slug: str):
    db = SessionLocal()
    group = db.query(Group).filter(Group.slug == group_slug).first()
    
    if not group:
        group = Group(slug=group_slug)
        db.add(group)
        db.commit()
        db.refresh(group)

    # Проверка кулдауна (1 минута)
    if group.last_updated_at:
        delta = datetime.now() - group.last_updated_at
        if delta.total_seconds() < 60 and not group.is_fetching:
             db.close()
             return {"status": "cooldown", "message": "Подождите 1 минуту перед следующим обновлением"}

    if group.is_fetching:
        db.close()
        return {"status": "in_progress", "message": "Обновление уже идет"}

    # Запускаем процесс
    group.is_fetching = True
    group.fetch_stage = "Инициализация..."
    group.last_error = None
    db.commit()
    db.close()

    # Запускаем в отдельном потоке, чтобы не блокировать ответ, но контролировать статус
    thread = threading.Thread(target=run_refresh_task, args=(group_slug,))
    thread.start()

    return {"status": "started", "message": "Обновление запущено"}

@app.get("/api/groups/{group_slug}/status")
async def get_status(group_slug: str):
    db = SessionLocal()
    group = db.query(Group).filter(Group.slug == group_slug).first()
    db.close()

    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    return {
        "slug": group.slug,
        "is_fetching": group.is_fetching,
        "fetch_stage": group.fetch_stage,
        "last_updated_at": group.last_updated_at.isoformat() if group.last_updated_at else None,
        "last_error": group.last_error,
        "has_schedule": group.schedule_json is not None
    }

@app.get("/cal/{group_slug}.ics")
async def get_ics(group_slug: str):
    db = SessionLocal()
    group = db.query(Group).filter(Group.slug == group_slug).first()
    db.close()

    if not group or not group.ics_text:
        raise HTTPException(status_code=404, detail="Расписание еще не сгенерировано. Нажмите 'Обновить'.")

    return PlainTextResponse(
        content=group.ics_text,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=schedule_{group_slug}.ics"}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
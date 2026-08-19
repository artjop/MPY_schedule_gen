import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager
import threading
import time

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from pydantic import BaseModel
import requests
from bs4 import BeautifulSoup
import icalendar
from icalendar import Calendar, Event, vText
import qrcode
import io
import base64

# --- Конфигурация ---
DATABASE_URL = "sqlite:///./schedule.db"
REFRESH_COOLDOWN_SECONDS = 60  # 1 минута
PARSING_TIMEOUT_SECONDS = 60   # Таймаут на парсинг

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Создание папки static
os.makedirs("static", exist_ok=True)

# --- База данных ---
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, index=True)
    name = Column(String, nullable=True)
    
    schedule_json = Column(JSON, default=list)
    ics_text = Column(Text, nullable=True)
    
    last_updated_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    
    # Статусы для фронтенда
    is_fetching = Column(Boolean, default=False)
    fetch_stage = Column(String, default="idle") # idle, fetching, parsing, generating, saving

Base.metadata.create_all(bind=engine)

# --- Модели Pydantic ---
class RefreshResponse(BaseModel):
    status: str
    message: str

class StatusResponse(BaseModel):
    status: str # ok, fetching, error, idle
    last_updated_at: Optional[datetime] = None
    last_error: Optional[str] = None
    fetch_stage: str = "idle"

# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Startup: Creating static directory and initializing DB...")
    os.makedirs("static", exist_ok=True)
    yield
    logger.info("Shutdown...")

app = FastAPI(lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# --- Зависимости ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Бизнес-логика (Парсинг) ---
# ВСТАВЬ СЮДА СВОЙ РАБОЧИЙ КОД ПАРСИНГА
# Я оставил заглушку, которая генерирует тестовые данные, если реальный парсер упадет
def parse_schedule_logic(group_slug: str) -> list:
    """
    Здесь должна быть твоя логика парсинга.
    Возвращает список событий.
    """
    logger.info(f"[{group_slug}] Запуск логики парсинга...")
    
    # ПРИМЕР ЗАГЛУШКИ (УДАЛИ ЭТОТ БЛОК И ВСТАВЬ СВОЙ КОД)
    # -------------------------------------------------------
    time.sleep(2) # Имитация задержки сети
    
    # Проверка на "сломанный" парсер для демонстрации ошибки
    if group_slug == "broken":
        raise Exception("Сайт расписания недоступен (демо ошибки)")

    events = []
    # Генерируем пару тестовых событий
    now = datetime.now()
    # Понедельник текущей недели
    monday = now - timedelta(days=now.weekday())
    
    # Событие 1: Пн 9:00
    start_1 = monday.replace(hour=9, minute=0, second=0, microsecond=0)
    end_1 = start_1 + timedelta(hours=2)
    
    # Событие 2: Ср 13:00
    wednesday = monday + timedelta(days=2)
    start_2 = wednesday.replace(hour=13, minute=0, second=0, microsecond=0)
    end_2 = start_2 + timedelta(hours=2)

    events.append({
        "subject": "Высшая математика",
        "location": "Ауд. 305",
        "start": start_1.isoformat(),
        "end": end_1.isoformat(),
        "description": f"Группа {group_slug}"
    })
    
    events.append({
        "subject": "Физика",
        "location": "Ауд. 202",
        "start": start_2.isoformat(),
        "end": end_2.isoformat(),
        "description": f"Группа {group_slug}"
    })
    # -------------------------------------------------------
    
    logger.info(f"[{group_slug}] Спарсено {len(events)} событий.")
    return events

def generate_ics_text(group_name: str, events: list) -> str:
    cal = Calendar()
    cal.add('prodid', f'-//Schedule Service//{group_name}//RU')
    cal.add('version', '2.0')
    cal.add('calscale', 'GREGORIAN')
    cal.add('method', 'PUBLISH')

    for event_data in events:
        event = Event()
        event.add('summary', event_data.get('subject', 'Без названия'))
        event.add('dtstart', datetime.fromisoformat(event_data['start']))
        event.add('dtend', datetime.fromisoformat(event_data['end']))
        event.add('dtstamp', datetime.utcnow())
        
        if event_data.get('location'):
            event.add('location', event_data['location'])
        if event_data.get('description'):
            event.add('description', event_data['description'])
            
        # Стабильный UID
        uid_str = f"{group_name}-{event_data['subject']}-{event_data['start']}-{event_data['end']}"
        event.add('uid', uid_str)
        
        cal.add_component(event)
    
    return cal.to_ical().decode('utf-8')

# --- Фоновая задача (в потоке) ---
def run_refresh_task(group_slug: str):
    db = SessionLocal()
    try:
        group = db.query(Group).filter(Group.slug == group_slug).first()
        if not group:
            logger.error(f"Группа {group_slug} не найдена в БД внутри задачи")
            return

        # Этап 1: Загрузка/Начало
        group.is_fetching = True
        group.fetch_stage = "fetching"
        group.last_error = None
        db.commit()
        logger.info(f"[{group_slug}] Старт обновления (этап: fetching)")

        try:
            # Этап 2: Парсинг
            group.fetch_stage = "parsing"
            db.commit()
            logger.info(f"[{group_slug}] Этап: parsing")
            
            # Запускаем парсинг с таймаутом через отдельный поток внутри потока (надежно)
            result_container = {'data': None, 'error': None}
            
            def target():
                try:
                    result_container['data'] = parse_schedule_logic(group_slug)
                except Exception as e:
                    result_container['error'] = str(e)

            t = threading.Thread(target=target)
            t.start()
            t.join(timeout=PARSING_TIMEOUT_SECONDS)
            
            if t.is_alive():
                raise TimeoutError(f"Парсинг превысил лимит {PARSING_TIMEOUT_SECONDS} сек")
            
            if result_container['error']:
                raise Exception(result_container['error'])

            events = result_container['data']
            
            # Этап 3: Генерация ICS
            group.fetch_stage = "generating"
            db.commit()
            logger.info(f"[{group_slug}] Этап: generating")
            
            ics_content = generate_ics_text(group.name or group_slug, events)
            
            # Этап 4: Сохранение
            group.fetch_stage = "saving"
            db.commit()
            logger.info(f"[{group_slug}] Этап: saving")

            group.schedule_json = events
            group.ics_text = ics_content
            group.last_updated_at = datetime.utcnow()
            group.is_fetching = False
            group.fetch_stage = "idle"
            db.commit()
            
            logger.info(f"[{group_slug}] Успешно обновлено!")

        except Exception as e:
            logger.error(f"[{group_slug}] Ошибка: {e}")
            group.is_fetching = False
            group.fetch_stage = "idle"
            group.last_error = f"Ошибка обновления: {str(e)}"
            db.commit()

    except Exception as e:
        logger.critical(f"[{group_slug}] Критическая ошибка в задаче: {e}")
    finally:
        db.close()

# --- Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/group/{group_slug}", response_class=HTMLResponse)
async def get_group_page(request: Request, group_slug: str, db: Session = Depends(get_db)):
    group = db.query(Group).filter(Group.slug == group_slug).first()
    
    if not group:
        # Создаем новую группу, если нет
        group = Group(slug=group_slug, name=group_slug)
        db.add(group)
        db.commit()
        db.refresh(group)
    
    # Генерация QR
    host = str(request.base_url).rstrip('/')
    ics_url = f"{host}/cal/{group_slug}.ics"
    webcal_url = ics_url.replace("http://", "webcal://").replace("https://", "webcal://")
    
    qr = qrcode.make(webcal_url)
    buffered = io.BytesIO()
    qr.save(buffered, format="PNG")
    qr_b64 = base64.b64encode(buffered.getvalue()).decode()

    return templates.TemplateResponse("group.html", {
        "request": request,
        "group": group,
        "qr_code": qr_b64,
        "ics_url": ics_url,
        "webcal_url": webcal_url
    })

@app.get("/cal/{group_slug}.ics", response_class=PlainTextResponse)
async def get_calendar(group_slug: str, db: Session = Depends(get_db)):
    group = db.query(Group).filter(Group.slug == group_slug).first()
    if not group:
        raise HTTPException(status_code=404, detail="Группа не найдена")
    
    if not group.ics_text:
        # Если календаря нет, можно вернуть пустой или ошибку
        # Для удобства вернем пустой валидный календарь
        cal = Calendar()
        cal.add('prodid', '-//Schedule Service//RU')
        cal.add('version', '2.0')
        return PlainTextResponse(cal.to_ical().decode('utf-8'), media_type="text/calendar; charset=utf-8")
    
    return PlainTextResponse(group.ics_text, media_type="text/calendar; charset=utf-8")

@app.post("/api/groups/{group_slug}/refresh", response_model=RefreshResponse)
async def refresh_group(group_slug: str, db: Session = Depends(get_db)):
    group = db.query(Group).filter(Group.slug == group_slug).first()
    
    if not group:
        group = Group(slug=group_slug, name=group_slug)
        db.add(group)
        db.commit()
        db.refresh(group)

    # Проверка кулдауна и статуса
    now = datetime.utcnow()
    if group.is_fetching:
        return RefreshResponse(status="in_progress", message="Обновление уже идет")
    
    if group.last_updated_at:
        delta = now - group.last_updated_at
        if delta.total_seconds() < REFRESH_COOLDOWN_SECONDS and not group.last_error:
             # Разрешаем повторное обновление если была ошибка
             pass # Можно раскомментировать для строгого кулдауна
             # return RefreshResponse(status="too_early", message=f"Подождите еще {int(REFRESH_COOLDOWN_SECONDS - delta.total_seconds())} сек")

    # Запускаем задачу в отдельном потоке, но НЕ ждем её
    # Важно: мы должны-commit-ить начальный статус ДО запуска потока, чтобы фронт увидел изменение
    group.is_fetching = True
    group.fetch_stage = "waiting" # Ждет старта потока
    group.last_error = None
    db.commit()
    
    # Запуск в фоне
    thread = threading.Thread(target=run_refresh_task, args=(group_slug,))
    thread.start()
    
    return RefreshResponse(status="accepted", message="Обновление запущено")

@app.get("/api/groups/{group_slug}/status", response_model=StatusResponse)
async def get_status(group_slug: str, db: Session = Depends(get_db)):
    group = db.query(Group).filter(Group.slug == group_slug).first()
    if not group:
        # Если группы нет вообще
        return StatusResponse(status="idle", fetch_stage="idle")
    
    status = "ok"
    if group.is_fetching:
        status = "fetching"
    elif group.last_error:
        status = "error"
    elif not group.last_updated_at:
        status = "idle"
        
    return StatusResponse(
        status=status,
        last_updated_at=group.last_updated_at,
        last_error=group.last_error,
        fetch_stage=group.fetch_stage or "idle"
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
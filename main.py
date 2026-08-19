import os
import json
import logging
import time
import threading
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import Response
import httpx
from icalendar import Calendar, Event, vText
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from contextlib import asynccontextmanager

# --- Конфигурация ---
DATABASE_URL = "sqlite:///./schedule.db"
TZ_MOSCOW = ZoneInfo("Europe/Moscow")
COOLDOWN_SECONDS = 60  # 1 минута
PARSING_TIMEOUT = 60   # 60 секунд на парсинг

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
    slug = Column(String, unique=True, index=True, nullable=False)
    schedule_json = Column(JSON, default=list)
    ics_text = Column(Text, nullable=True)
    last_updated_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    is_fetching = Column(Boolean, default=False)
    fetch_stage = Column(String, default="Ожидание...") # Новый этап

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Логика парсинга (Имитация/Заглушка для примера, замените на свой реальный парсер) ---
# ВСТАВЬТЕ СЮДА ВАШ РЕАЛЬНЫЙ КОД ПАРСИНГА
async def fetch_schedule_from_source(group_slug: str) -> list:
    """
    Здесь должен быть ваш реальный код парсинга.
    Для теста я верну фейковые данные, если не найду реального URL.
    ЗАМЕНИТЕ ЭТУ ФУНКЦИЮ НА ВАШУ ЛОГИКУ.
    """
    logger.info(f"Начинаем парсинг для {group_slug}...")
    
    # Пример реальной логики (раскомментируйте и адаптируйте под себя):
    # url = f"https://portal.tpu.ru/SHARED/r/RATF/academic/schedule/{group_slug}" 
    # async with httpx.AsyncClient() as client:
    #     resp = await client.get(url, timeout=PARSING_TIMEOUT)
    #     resp.raise_for_status()
    #     html = resp.text
    #     return parse_html(html) # Ваша функция парсинга
    
    # ИМИТАЦИЯ ДЛЯ ПРОВЕРКИ РАБОТОСПОСОБНОСТИ (УДАЛИТЬ ПРИ ПРОДЕ)
    await asyncio.sleep(2) # Имитация задержки сети
    import random
    if random.random() > 0.9:
        raise Exception("Случайная ошибка сети (для теста)")
        
    # Генерируем тестовые данные
    events = []
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]
    for day in days:
        count = random.randint(0, 4)
        for i in range(count):
            start_hour = 9 + i * 2
            events.append({
                "day": day,
                "time_start": f"{start_hour:02d}:00",
                "time_end": f"{start_hour+1:02d}:50",
                "subject": f"Предмет {day} {i+1}",
                "room": f"Ауд. {random.randint(100, 500)}",
                "type": "Лекция" if i == 0 else "Семинар"
            })
    return events

def generate_ics(events: list, group_slug: str) -> str:
    cal = Calendar()
    cal.add('prodid', f'-//Schedule Service//{group_slug}//RU')
    cal.add('version', '2.0')
    cal.add('calscale', 'GREGORIAN')
    cal.add('method', 'PUBLISH')
    cal.add('x-wr-calname', f'Расписание {group_slug}')

    # Простая логика разворачивания расписания на семестр (пример)
    # В реальном проекте тут должна быть ваша логика дат
    from datetime import date
    start_date = date.today()
    
    # Найдем ближайший понедельник как точку отсчета
    days_ahead = 0 - start_date.weekday()
    if days_ahead < 0: # Target day already happened this week
        days_ahead += 7
    next_monday = start_date + timedelta(days=days_ahead)

    day_map = {"Пн": 0, "Вт": 1, "Ср": 2, "Чт": 3, "Пт": 4, "Сб": 5, "Вс": 6}

    for event in events:
        day_name = event.get('day')
        if day_name not in day_map:
            continue
            
        weekday_offset = day_map[day_name]
        
        # Генерируем события на 4 недели вперед для примера
        for week in range(4):
            event_date = next_monday + timedelta(weeks=week, days=weekday_offset)
            
            t_start = event.get('time_start', '09:00')
            t_end = event.get('time_end', '10:30')
            
            dt_start = datetime.combine(event_date, datetime.strptime(t_start, "%H:%M").time())
            dt_end = datetime.combine(event_date, datetime.strptime(t_end, "%H:%M").time())
            
            # Стабильный UID
            uid_str = f"{group_slug}-{event.get('subject')}-{t_start}-{t_end}-{event.get('room')}"
            uid = hashlib.md5(uid_str.encode()).hexdigest() + "@schedule.local"

            ve = Event()
            ve.add('summary', f"{event.get('subject')} ({event.get('type')})")
            ve.add('dtstart', dt_start)
            ve.add('dtend', dt_end)
            ve.add('location', event.get('room', ''))
            ve.add('uid', uid)
            ve.add('dtstamp', datetime.now(TZ_MOSCOW))
            
            cal.add_component(ve)

    return cal.to_ical().decode('utf-8')

# --- Глобальное хранилище состояния задач (для синхронизации потоков) ---
# В продакшене с несколькими воркерами лучше использовать Redis
active_tasks: Dict[str, dict] = {} 

def run_refresh_task(db: Session, group_slug: str):
    """Функция, выполняемая в отдельном потоке"""
    stage = "Инициализация..."
    try:
        # 1. Обновляем статус в БД сразу
        db_group = db.query(Group).filter(Group.slug == group_slug).first()
        if not db_group:
            logger.error(f"Группа {group_slug} не найдена в БД внутри задачи")
            return

        db_group.is_fetching = True
        db_group.fetch_stage = "Загрузка данных..."
        db_group.last_error = None
        db.commit()

        # 2. Парсинг
        stage = "Парсинг данных..."
        db_group.fetch_stage = stage
        db.commit()
        
        # Вызываем функцию парсинга (может быть асинхронной, нужно обернуть)
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        events = loop.run_until_complete(fetch_schedule_from_source(group_slug))
        
        if not events:
            logger.warning(f"Расписание пустое для {group_slug}")
            # Не считаем это ошибкой, просто пустой календарь

        # 3. Генерация ICS
        stage = "Генерация ICS..."
        db_group.fetch_stage = stage
        db.commit()
        
        ics_content = generate_ics(events, group_slug)
        
        # 4. Сохранение
        stage = "Сохранение..."
        db_group.fetch_stage = stage
        db.commit()
        
        db_group.schedule_json = events
        db_group.ics_text = ics_content
        db_group.last_updated_at = datetime.now()
        db_group.is_fetching = False
        db_group.fetch_stage = "Готово"
        db.commit()
        
        logger.info(f"Обновление для {group_slug} завершено успешно.")

    except Exception as e:
        logger.error(f"Ошибка при обновлении {group_slug}: {e}", exc_info=True)
        # Откат флага при ошибке
        db_group = db.query(Group).filter(Group.slug == group_slug).first()
        if db_group:
            db_group.is_fetching = False
            db_group.fetch_stage = f"Ошибка: {str(e)}"
            db_group.last_error = str(e)
            db.commit()

# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Сервис запущен")
    yield
    # Shutdown
    logger.info("Сервис остановлен")

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# --- Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/group/{group_slug}", response_class=HTMLResponse)
async def group_page(request: Request, group_slug: str, db: Session = Depends(get_db)):
    group = db.query(Group).filter(Group.slug == group_slug).first()
    if not group:
        # Создаем заглушку, если группы нет
        group = Group(slug=group_slug, schedule_json=[], is_fetching=False, fetch_stage="Не создано")
        db.add(group)
        db.commit()
        db.refresh(group)
    
    return templates.TemplateResponse("group.html", {
        "request": request, 
        "group": group
    })

@app.get("/cal/{group_slug}.ics")
async def get_ics(group_slug: str, db: Session = Depends(get_db)):
    group = db.query(Group).filter(Group.slug == group_slug).first()
    if not group or not group.ics_text:
        raise HTTPException(status_code=404, detail="Календарь еще не сгенерирован. Нажмите 'Обновить'.")
    
    return Response(
        content=group.ics_text,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={group_slug}.ics"}
    )

@app.post("/api/groups/{group_slug}/refresh")
async def refresh_group(group_slug: str, db: Session = Depends(get_db)):
    group = db.query(Group).filter(Group.slug == group_slug).first()
    
    if not group:
        group = Group(slug=group_slug, schedule_json=[], is_fetching=False, fetch_stage="Ожидание...")
        db.add(group)
        db.commit()
        db.refresh(group)

    # Проверка кулдауна
    if group.last_updated_at:
        delta = datetime.now() - group.last_updated_at
        if delta.total_seconds() < COOLDOWN_SECONDS and not group.is_fetching:
             return {"status": "cooldown", "message": f"Подождите {COOLDOWN_SECONDS} сек"}

    # Проверка, идет ли уже обновление
    if group.is_fetching:
        return {"status": "in_progress", "message": "Обновление уже идет"}

    # Запускаем задачу в фоне (в потоке, чтобы не блокировать ответ API, но с контролем статуса)
    group.is_fetching = True
    group.fetch_stage = "Запуск..."
    group.last_error = None
    db.commit()
    
    # Небольшая задержка, чтобы коммит ушел в БД до старта потока
    time.sleep(0.1)
    
    thread = threading.Thread(target=run_refresh_task, args=(SessionLocal(), group_slug))
    thread.start()
    
    return {"status": "started", "message": "Обновление запущено"}

@app.get("/api/groups/{group_slug}/status")
async def get_status(group_slug: str, db: Session = Depends(get_db)):
    group = db.query(Group).filter(Group.slug == group_slug).first()
    if not group:
        raise HTTPException(status_code=404, detail="Группа не найдена")
    
    # Если флаг is_fetching висит слишком долго (защита от зависаний > 2 мин)
    if group.is_fetching and group.last_updated_at:
         # Это упрощенная проверка, в идеале хранить started_at
         pass 

    return {
        "slug": group.slug,
        "is_fetching": group.is_fetching,
        "fetch_stage": group.fetch_stage,
        "last_updated_at": group.last_updated_at.isoformat() if group.last_updated_at else None,
        "last_error": group.last_error,
        "has_calendar": bool(group.ics_text)
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
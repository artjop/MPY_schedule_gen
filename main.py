"""
Schedule Service — FastAPI приложение.

Бизнес-логика парсинга и генерации ICS живёт в schedule_parser.py
(проверенный рабочий парсер) и здесь НЕ переписывается: endpoint'ы просто
вызывают schedule_parser.fetch_and_convert_schedule().

Хранилище: SQLite, одна строка на группу (общая сущность):
  - schedule_json — актуальный JSON расписания группы (один на группу);
  - ics_text      — общий .ics-календарь группы (один на группу, подписка).
Пользователи не создают отдельных копий календаря — ссылка одна на группу.

Простота: FastAPI + SQLite + один процесс, без внешних сервисов —
бесплатно хостится на Render / Railway / любой VPS.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

import pytz

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from models import Base, Group
from schedule_parser import fetch_and_convert_schedule

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./schedule.db")
SECRET_TOKEN = os.getenv("SECRET_TOKEN", "change_this_secret_token")
REFRESH_COOLDOWN_SECONDS = int(os.getenv("REFRESH_COOLDOWN_SECONDS", "60"))
# Период автообновления всех групп (по умолчанию — раз в неделю)
REFRESH_INTERVAL_SECONDS = int(os.getenv("REFRESH_INTERVAL_SECONDS", str(7 * 24 * 3600)))

# Группы, которые сервис «сажает» при старте (чтобы календари существовали
# даже после того, как Render стёр базу при перезапуске)
POPULAR_GROUPS = ["242-621", "242-521", "242-421", "242-321"]

os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def seed_popular_groups() -> None:
    """Создаёт записи популярных групп, если их ещё нет."""
    db = SessionLocal()
    try:
        for slug in POPULAR_GROUPS:
            get_or_create_group(db, slug)
    finally:
        db.close()


def refresh_missing_schedules() -> None:
    """Обновляет группы, у которых ещё нет календаря (после старта/рестарта)."""
    db = SessionLocal()
    try:
        for group in db.query(Group).all():
            if group.ics_text is None:
                refresh_group(db, group)
    finally:
        db.close()


def refresh_all_schedules() -> None:
    """Обновляет расписание всех групп (еженедельный таск)."""
    db = SessionLocal()
    try:
        for group in db.query(Group).all():
            refresh_group(db, group)
    finally:
        db.close()


async def scheduler_loop() -> None:
    """Фоновая задача: посев групп + автообновление раз в REFRESH_INTERVAL_SECONDS."""
    try:
        await asyncio.to_thread(seed_popular_groups)
        await asyncio.to_thread(refresh_missing_schedules)
        logger.info("[SCHED] Стартовое заполнение групп выполнено.")
    except Exception as e:
        logger.error(f"[SCHED] Стартовое заполнение: {e}", exc_info=True)

    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(refresh_all_schedules)
            logger.info("[SCHED] Еженедельное обновление всех групп выполнено.")
        except Exception as e:
            logger.error(f"[SCHED] Еженедельное обновление: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    # После перезапуска процесса никакой фонобновления не идёт —
    # сбрасываем «зависшие» статусы fetching и таймер кулдауна.
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE groups SET fetch_status = "
            "CASE WHEN ics_text IS NOT NULL THEN 'ok' ELSE 'pending' END "
            "WHERE fetch_status = 'fetching'"
        ))
        conn.execute(text("UPDATE groups SET last_refresh_attempt = NULL"))
    logger.info("База данных готова.")
    # Фоновая задача автообновления (стартует, не блокируя приложение)
    scheduler_task = asyncio.create_task(scheduler_loop())
    yield
    scheduler_task.cancel()


app = FastAPI(lifespan=lifespan, title="Schedule Service")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# --- Часовой пояс ---
# Расписание — московское, поэтому время для отображения приводим к Europe/Moscow.
# В БД храним UTC (datetime.utcnow), а форматируем уже в Москве.
MOSCOW_TZ = pytz.timezone("Europe/Moscow")


def format_moscow_time(dt: Optional[datetime]) -> str:
    """Форматирует время из БД (UTC) в московское для показа на странице."""
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")


templates.env.filters["moscow_time"] = format_moscow_time


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_or_create_group(db: Session, group_slug: str) -> Group:
    """Одна запись на группу — общая сущность для всех пользователей."""
    group = db.query(Group).filter(Group.slug == group_slug).first()
    if not group:
        group = Group(slug=group_slug, fetch_status="pending")
        db.add(group)
        db.commit()
        db.refresh(group)
    return group


def refresh_group(db: Session, group: Group) -> dict:
    """
    Синхронно обновляет расписание группы через рабочий парсер.
    Возвращает словарь-статус для API.
    """
    # Кулдаун между ручными обновлениями (защита источника от частых запросов)
    if group.last_refresh_attempt and group.fetch_status == "ok":
        delta = datetime.utcnow() - group.last_refresh_attempt
        if delta < timedelta(seconds=REFRESH_COOLDOWN_SECONDS):
            return {
                "status": "cooldown",
                "message": f"Подождите ещё {max(1, int((REFRESH_COOLDOWN_SECONDS - delta.total_seconds())))} секунд",
            }

    group.fetch_status = "fetching"
    group.last_refresh_attempt = datetime.utcnow()
    db.commit()

    try:
        # Единственное место вызова бизнес-логики парсинга:
        schedule_data, ics_content = fetch_and_convert_schedule(group.slug)

        group.schedule_json = json.dumps(schedule_data, ensure_ascii=False)
        group.ics_text = ics_content
        group.title = (schedule_data.get("group") or {}).get("title") or group.slug
        group.last_fetched_at = datetime.utcnow()
        group.last_error = None
        group.fetch_status = "ok"
        group.version += 1
        db.commit()

        logger.info(f"[REFRESH] {group.slug}: ok ({len(ics_content)} bytes ICS)")
        return {
            "status": "success",
            "message": "Расписание обновлено",
            "updated_at": group.last_fetched_at.isoformat(),
        }
    except Exception as e:
        # Старые данные (если были) сохраняются — календарь продолжает отдаваться
        logger.error(f"[REFRESH] {group.slug}: {e}", exc_info=True)
        group.fetch_status = "error"
        group.last_error = str(e)
        db.commit()
        return {"status": "error", "message": str(e)}


# --- Страницы ---

DAY_ORDER = {'ПН': 1, 'ВТ': 2, 'СР': 3, 'ЧТ': 4, 'ПТ': 5, 'СБ': 6, 'ВС': 7}


def build_schedule_table(schedule_json_str: Optional[str]) -> list:
    """
    Превращает сохранённый JSON расписания в список строк для таблицы предпросмотра.
    Переиспользует рабочий парсер (schedule_parser.json_to_df) — логика не дублируется.
    """
    if not schedule_json_str:
        return []
    try:
        import schedule_parser

        data = json.loads(schedule_json_str)
        df = schedule_parser.json_to_df(data)
        if df.empty:
            return []

        rows = []
        seen = set()
        for _, r in df.iterrows():
            dts = r.get('dts')
            period = ' – '.join(dts) if isinstance(dts, list) and len(dts) == 2 else ''
            time_range = r.get('time_range')
            time_str = '–'.join(time_range) if isinstance(time_range, list) and len(time_range) == 2 else ''

            key = (
                r.get('day'), r.get('sbj'), r.get('teacher'), period, time_str,
                r.get('type'), r.get('location'),
            )
            if key in seen:
                continue
            seen.add(key)

            rows.append({
                'day': str(r.get('day') or ''),
                'time': time_str,
                'period': period,
                'subject': str(r.get('sbj') or ''),
                'type': str(r.get('type') or ''),
                'teacher': str(r.get('teacher') or '').strip(),
                'location': str(r.get('location') or ''),
            })

        rows.sort(key=lambda x: (DAY_ORDER.get(x['day'], 99), x['time'], x['subject']))
        return rows
    except Exception as e:
        logger.warning(f"[TABLE] Не удалось построить предпросмотр: {e}")
        return []


@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    return templates.TemplateResponse(
        "index.html", {"request": request, "title": "Расписание групп"}
    )


@app.get("/group/{group_slug}", response_class=HTMLResponse)
def group_page(request: Request, group_slug: str, db: Session = Depends(get_db)):
    group = get_or_create_group(db, group_slug)
    return templates.TemplateResponse("group.html", {
        "request": request,
        "group": group,
        "has_schedule": group.schedule_json is not None,
        "lessons": build_schedule_table(group.schedule_json),
    })


# --- Подписка на календарь ---

@app.get("/cal/{group_slug}.ics", response_class=PlainTextResponse)
def get_calendar(group_slug: str, db: Session = Depends(get_db)):
    """Один общий .ics на группу — все подписчики получают одну и ту же ссылку."""
    group = db.query(Group).filter(Group.slug == group_slug).first()
    if not group or not group.ics_text:
        raise HTTPException(
            status_code=404,
            detail="Календарь ещё не сгенерирован. Нажмите «Обновить расписание».",
        )
    return PlainTextResponse(
        content=group.ics_text,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="schedule_{group_slug}.ics"'},
    )


# --- API ---

@app.get("/api/groups/{group_slug}/status")
def group_status(group_slug: str, db: Session = Depends(get_db)):
    group = db.query(Group).filter(Group.slug == group_slug).first()
    if not group:
        raise HTTPException(status_code=404, detail="Группа не найдена")
    return group.to_dict()


@app.post("/api/groups/{group_slug}/refresh")
def refresh_schedule(group_slug: str, db: Session = Depends(get_db)):
    group = get_or_create_group(db, group_slug)
    return refresh_group(db, group)


@app.post("/internal/refresh-all")
def refresh_all(request: Request, db: Session = Depends(get_db)):
    """Обновление всех известных групп для внешнего cron (GitHub Actions и т.п.)."""
    token = request.headers.get("X-Secret-Token")
    if token != SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Unauthorized")

    groups = db.query(Group).all()
    results = []
    for group in groups:
        res = refresh_group(db, group)
        results.append({"slug": group.slug, **res})
    return {"refreshed": len(groups), "results": results}

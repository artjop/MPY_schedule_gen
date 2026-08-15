"""
Main FastAPI application for the schedule service.
"""

import os
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import qrcode
from io import BytesIO
import base64

from models import create_database_engine, Group
from schedule_parser import fetch_and_convert_schedule

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./schedule.db")
SECRET_TOKEN = os.getenv("SECRET_TOKEN", "change_this_secret_token")
REFRESH_COOLDOWN_SECONDS = int(os.getenv("REFRESH_COOLDOWN_SECONDS", "300"))  # 5 minutes default

# Initialize database
engine, async_session, init_db = create_database_engine(DATABASE_URL)

# Initialize FastAPI app
app = FastAPI(
    title="Schedule Service",
    description="Web service for group schedule subscriptions with ICS calendar export",
    version="1.0.0"
)

# Templates and static files
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
async def startup():
    """Initialize database on startup."""
    await init_db()


# Dependency to get DB session
async def get_db():
    async with async_session() as session:
        yield session


# Pydantic models for API
class RefreshStatus(BaseModel):
    status: str  # ok, in_progress, too_early, error
    message: Optional[str] = None
    last_updated_at: Optional[datetime] = None
    last_error: Optional[str] = None


class GroupStatus(BaseModel):
    slug: str
    title: str
    status: str  # ok, fetching, error, pending
    last_updated_at: Optional[datetime] = None
    last_error: Optional[str] = None
    has_schedule: bool = False


# Helper functions
async def get_or_create_group(session, group_slug: str) -> Group:
    """Get existing group or create new one."""
    from sqlalchemy import select
    
    result = await session.execute(select(Group).where(Group.slug == group_slug))
    group = result.scalar_one_or_none()
    
    if not group:
        group = Group(slug=group_slug, fetch_status="pending")
        session.add(group)
        await session.commit()
        await session.refresh(group)
    
    return group


async def refresh_group_schedule(session, group: Group) -> tuple[bool, Optional[str]]:
    """
    Refresh schedule for a group.
    
    Returns:
        Tuple of (success, error_message)
    """
    try:
        # Fetch and convert schedule
        schedule_data, ics_content = fetch_and_convert_schedule(group.slug)
        
        # Update group data
        group.schedule_json = json.dumps(schedule_data, ensure_ascii=False)
        group.ics_text = ics_content
        group.last_fetched_at = datetime.utcnow()
        group.fetch_status = "ok"
        group.last_error = None
        group.version += 1
        
        # Extract group title if available
        if schedule_data.get('group'):
            group.title = schedule_data['group'].get('title', group.slug)
        
        await session.commit()
        return True, None
        
    except Exception as e:
        error_msg = str(e)
        group.fetch_status = "error"
        group.last_error = error_msg
        group.last_fetched_at = datetime.utcnow()
        await session.commit()
        return False, error_msg


# Routes
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Home page with group selection form."""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "title": "Расписание групп"
    })


@app.get("/group/{group_slug}", response_class=HTMLResponse)
async def group_page(request: Request, group_slug: str):
    """Group page with schedule info and subscription links."""
    async with async_session() as session:
        group = await get_or_create_group(session, group_slug)
        
        # Generate QR code for webcal link
        base_url = str(request.base_url).rstrip('/')
        webcal_url = f"webcal://{request.url.hostname}{('/' + request.url.path.lstrip('/').rsplit('/', 1)[0]) if '/' in request.url.path else ''}/cal/{group_slug}.ics"
        webcal_url = f"webcal://{request.url.hostname}/cal/{group_slug}.ics"
        
        # Generate QR code
        qr = qrcode.make(webcal_url)
        qr_buffer = BytesIO()
        qr.save(qr_buffer, format='PNG')
        qr_base64 = base64.b64encode(qr_buffer.getvalue()).decode()
        
        return templates.TemplateResponse("group.html", {
            "request": request,
            "group": group,
            "group_slug": group_slug,
            "base_url": base_url,
            "qr_base64": qr_base64,
            "webcal_url": webcal_url
        })


@app.get("/cal/{group_slug}.ics")
async def get_calendar(group_slug: str):
    """Download ICS calendar file for a group."""
    async with async_session() as session:
        group = await get_or_create_group(session, group_slug)
        
        if not group.ics_text:
            # No calendar yet - trigger background refresh and return error
            raise HTTPException(
                status_code=404,
                detail="Calendar not found. Please refresh the schedule first."
            )
        
        return PlainTextResponse(
            content=group.ics_text,
            media_type="text/calendar; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="schedule_{group_slug}.ics"',
                "Cache-Control": "public, max-age=300"  # Cache for 5 minutes
            }
        )


@app.post("/api/groups/{group_slug}/refresh", response_model=RefreshStatus)
async def refresh_schedule(group_slug: str, background_tasks: BackgroundTasks):
    """
    Trigger schedule refresh for a group.
    
    Rules:
    - If refresh is already in progress, return "in_progress"
    - If last refresh was recently, return "too_early"
    - Otherwise, start background refresh
    """
    async with async_session() as session:
        group = await get_or_create_group(session, group_slug)
        
        now = datetime.utcnow()
        
        # Check if already fetching
        if group.fetch_status == "fetching":
            return RefreshStatus(
                status="in_progress",
                message="Refresh is already in progress",
                last_updated_at=group.last_fetched_at,
                last_error=group.last_error
            )
        
        # Check cooldown
        if group.last_refresh_attempt:
            time_since_last = (now - group.last_refresh_attempt).total_seconds()
            if time_since_last < REFRESH_COOLDOWN_SECONDS:
                remaining = int(REFRESH_COOLDOWN_SECONDS - time_since_last)
                return RefreshStatus(
                    status="too_early",
                    message=f"Please wait {remaining} seconds before refreshing again",
                    last_updated_at=group.last_fetched_at,
                    last_error=group.last_error
                )
        
        # Mark as fetching and start background task
        group.fetch_status = "fetching"
        group.last_refresh_attempt = now
        await session.commit()
        
        # Start background refresh
        background_tasks.add_task(refresh_group_schedule, session, group)
        
        return RefreshStatus(
            status="started",
            message="Refresh started",
            last_updated_at=group.last_fetched_at,
            last_error=group.last_error
        )


@app.get("/api/groups/{group_slug}/status", response_model=GroupStatus)
async def get_group_status(group_slug: str):
    """Get current status of a group's schedule."""
    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(Group).where(Group.slug == group_slug))
        group = result.scalar_one_or_none()
        
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
        
        return GroupStatus(
            slug=group.slug,
            title=group.title or group.slug,
            status=group.fetch_status,
            last_updated_at=group.last_fetched_at,
            last_error=group.last_error,
            has_schedule=group.ics_text is not None
        )


@app.post("/internal/refresh-all")
async def refresh_all_groups(request: Request, background_tasks: BackgroundTasks):
    """
    Internal endpoint to refresh all active groups.
    Protected by secret token header.
    
    Usage: POST /internal/refresh-all with header X-Secret-Token: <token>
    """
    # Check secret token
    token = request.headers.get("X-Secret-Token", "")
    if token != SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing secret token")
    
    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(Group))
        groups = result.scalars().all()
        
        refreshed_count = 0
        for group in groups:
            now = datetime.utcnow()
            
            # Skip if currently fetching
            if group.fetch_status == "fetching":
                continue
            
            # Skip if within cooldown (shorter cooldown for auto-refresh)
            auto_cooldown = 3600  # 1 hour for auto-refresh
            if group.last_refresh_attempt:
                time_since_last = (now - group.last_refresh_attempt).total_seconds()
                if time_since_last < auto_cooldown:
                    continue
            
            # Mark and start refresh
            group.fetch_status = "fetching"
            group.last_refresh_attempt = now
            refreshed_count += 1
            
            # Start background refresh
            background_tasks.add_task(refresh_group_schedule, session, group)
        
        return {
            "status": "ok",
            "message": f"Started refresh for {refreshed_count} groups",
            "groups_queued": refreshed_count
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

"""
Business logic module for parsing and converting timetable data.
Reuses the existing code from MPY_timetable.ipynb notebook.
"""

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
import pytz
from ical.calendar import Calendar
from ical.event import Event
from ical.calendar_stream import IcsCalendarStream
import requests


def get_schedule(group: str) -> Any:
    """
    Fetch schedule JSON from the source API.
    
    Args:
        group: Group name/slug (e.g., '242-621')
    
    Returns:
        JSON response as dict
    """
    url = f'https://rasp.dmami.ru/site/group?group={group}&session=0'
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0',
        'Accept': '*/*',
        'Accept-Language': 'ru,en-US;q=0.7,en;q=0.3',
        'X-Requested-With': 'XMLHttpRequest',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'Priority': 'u=0',
        'Referer': 'https://rasp.dmami.ru/'
    }

    response = requests.get(url, headers=headers, cookies={'session': '0'}, timeout=30)
    response.raise_for_status()
    return response.json()


def parse_date_range(date_range_str: str) -> List[str]:
    """
    Parse Russian date range string to ISO format dates.
    
    Args:
        date_range_str: Date range in format "07 Окт - 20 Окт"
    
    Returns:
        List of [start_date, end_date] in ISO format
    """
    months = {
        "Янв": 1, "Фев": 2, "Мар": 3, "Апр": 4,
        "Май": 5, "Июн": 6, "Июл": 7, "Авг": 8,
        "Сен": 9, "Окт": 10, "Ноя": 11, "Дек": 12
    }

    start_str, end_str = date_range_str.split(" - ")

    def parse_date(date_str: str) -> datetime:
        day, month_abbr = date_str.split()
        month = months[month_abbr]
        # Determine year - if month is Jan/Feb and current month is Nov/Dec, use next year
        now = datetime.now()
        year = now.year
        if month in (1, 2) and now.month in (11, 12):
            year += 1
        return datetime(year=year, month=month, day=int(day))

    start_date = parse_date(start_str)
    end_date = parse_date(end_str)
    return [start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')]


def json_to_df(data: Dict) -> pd.DataFrame:
    """
    Convert schedule JSON to pandas DataFrame.
    
    Args:
        data: Schedule JSON data
    
    Returns:
        DataFrame with parsed schedule
    """
    lessons_times = {
        1: '9:00-10:30',
        2: '10:40-12:10',
        3: '12:20-13:50',
        4: '14:30-16:00',
        5: '16:10-17:40',
        6: '18:20-19:40',
        7: '19:50-21:10',
    }

    combined_df = pd.DataFrame()
    days = [' ', 'ПН', 'ВТ', 'СР', 'ЧТ', 'ПТ', 'СБ', 'ВС']
    
    grid = data.get('grid', {})
    
    for week_num in range(1, 7):
        for day_num in range(1, 8):
            week_key = str(week_num)
            day_key = str(day_num)
            
            if week_key in grid and day_key in grid[week_key]:
                day_lessons = grid[week_key][day_key]
                if day_lessons:
                    # Создаем DataFrame из занятий дня
                    temp_df = pd.DataFrame(day_lessons)
                    if not temp_df.empty:
                        # Добавляем номер пары на основе позиции в списке
                        # API возвращает занятия в порядке их следования (1-я, 2-я и т.д.)
                        temp_df['lesson'] = list(range(1, len(temp_df) + 1))
                        temp_df['day'] = days[day_num]
                        combined_df = pd.concat([combined_df, temp_df], ignore_index=True)

    if combined_df.empty:
        return combined_df

    # Parse date ranges
    combined_df['dts'] = combined_df['dts'].apply(parse_date_range)

    # Extract time ranges based on lesson position
    def extract_times(lesson_number: int) -> List[str]:
        time_range = lessons_times.get(lesson_number, '9:00-10:30')
        start_time, end_time = time_range.split('-')
        return [start_time, end_time]

    # Add time_range column
    combined_df['time_range'] = combined_df['lesson'].apply(extract_times)
    
    return combined_df


def generate_uid(row: pd.Series, group_slug: str) -> str:
    """
    Generate stable UID for an event based on its properties.
    UID depends on: group + subject + date + time start + time end + location
    
    This ensures that identical events get the same UID across updates,
    preventing duplicate events in calendar clients.
    """
    import hashlib
    
    # Get all components for UID generation
    subject = str(row.get('sbj', ''))
    teacher = str(row.get('teacher', ''))
    date_start = row['dts'][0] if isinstance(row['dts'], list) else str(row.get('df', ''))
    date_end = row['dts'][1] if isinstance(row['dts'], list) else str(row.get('dt', ''))
    time_start = row['time_range'][0] if isinstance(row.get('time_range'), list) else '09:00'
    time_end = row['time_range'][1] if isinstance(row.get('time_range'), list) else '10:30'
    location = str(row.get('location', ''))
    lesson_day = str(row.get('day', ''))
    
    # Create a deterministic string for hashing
    uid_string = f"{group_slug}|{subject}|{date_start}|{time_start}|{time_end}|{location}|{lesson_day}"
    
    # Generate hash
    hash_obj = hashlib.md5(uid_string.encode('utf-8'))
    return hash_obj.hexdigest()


def ical_gen(df: pd.DataFrame, group_slug: str, group_title: str = "") -> str:
    """
    Generate ICS calendar content from DataFrame.
    
    Args:
        df: DataFrame with parsed schedule
        group_slug: Group identifier for UID generation
        group_title: Human-readable group title
    
    Returns:
        ICS file content as string
    """
    calendar = Calendar()
    days_mapping = {
        'ПН': 0, 'ВТ': 1, 'СР': 2, 'ЧТ': 3, 'ПТ': 4, 'СБ': 5, 'ВС': 6
    }
    timezone = pytz.timezone("Europe/Moscow")
    
    if df.empty:
        # Return empty calendar with proper header
        return IcsCalendarStream.calendar_to_ics(calendar)
    
    for index, row in df.iterrows():
        try:
            start_date = datetime.strptime(row['dts'][0], '%Y-%m-%d')
            end_date = datetime.strptime(row['dts'][1], '%Y-%m-%d')
            day_of_week = days_mapping.get(row['day'], 0)

            current_date = start_date
            while current_date <= end_date:
                if current_date.weekday() == day_of_week:
                    start_time = datetime.strptime(row['time_range'][0], '%H:%M').time()
                    end_time = datetime.strptime(row['time_range'][1], '%H:%M').time()

                    start_datetime = datetime.combine(current_date, start_time)
                    end_datetime = datetime.combine(current_date, end_time)

                    start_datetime = timezone.localize(start_datetime)
                    end_datetime = timezone.localize(end_datetime)
                    
                    # Build summary: Subject • Location • Teacher
                    summary_parts = []
                    if row.get('sbj'):
                        summary_parts.append(str(row['sbj']))
                    if row.get('location'):
                        summary_parts.append(str(row['location']))
                    if row.get('teacher'):
                        summary_parts.append(str(row['teacher']))
                    
                    summary = ' • '.join(summary_parts) if summary_parts else 'Занятие'
                    
                    # Generate stable UID
                    uid = generate_uid(row, group_slug)
                    
                    calendar.events.append(
                        Event(
                            summary=summary,
                            dtstart=start_datetime,
                            dtend=end_datetime,
                            location=str(row.get('location', '')),
                            uid=uid
                        )
                    )
                current_date += timedelta(days=1)
        except Exception as e:
            # Skip problematic rows but continue processing
            print(f"Error processing row {index}: {e}")
            continue

    return IcsCalendarStream.calendar_to_ics(calendar)


def fetch_and_convert_schedule(group_slug: str) -> tuple[Dict, str]:
    """
    Main function to fetch schedule and convert to ICS.
    
    Args:
        group_slug: Group identifier
    
    Returns:
        Tuple of (schedule_json, ics_content)
    
    Raises:
        Exception: If fetching or conversion fails
    """
    # Fetch schedule
    schedule_data = get_schedule(group_slug)
    
    if schedule_data.get('status') != 'ok':
        raise ValueError(f"API returned status: {schedule_data.get('status')}")
    
    # Get group info
    group_info = schedule_data.get('group', {})
    group_title = group_info.get('title', group_slug)
    
    # Convert to DataFrame
    df = json_to_df(schedule_data)
    
    # Generate ICS
    ics_content = ical_gen(df, group_slug, group_title)
    
    return schedule_data, ics_content

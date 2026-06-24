#!/usr/bin/env python3
"""
Weather Bot — Discord Bot 版
朝6:00 / 夜21:00 に天気予報を投稿、セレクトメニューで地域切替・1週間予報・お出かけ先対応
地震情報は5分ごとに監視、タイムカード機能付き
"""

import urllib.request
import urllib.parse
import json
import os
import asyncio
import logging
from calendar import monthrange
from datetime import datetime, timezone, timedelta
from pathlib import Path

import discord
from discord.ext import tasks, commands
from weather_codes import WEATHER_CODES

# ── ログ設定 ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("weather-bot")

# ── 設定 ──────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))
TC_CHANNEL_ID = int(os.environ.get("TC_CHANNEL_ID", "0"))
TIMECARD_HOURLY_WAGE = int(os.environ.get("TIMECARD_HOURLY_WAGE", "1000"))

DATA_DIR = Path(__file__).resolve().parent / "data"
STATE_FILE = DATA_DIR / "state.json"
LOCATIONS_FILE = DATA_DIR / "locations.json"
TIMECARD_FILE = DATA_DIR / "timecard.json"

LOCATIONS = {
    "matsumoto": {"name": "松本市",   "lat": 36.230, "lon": 137.972, "city_code": "20210"},
    "azumino":   {"name": "安曇野市", "lat": 36.320, "lon": 137.900, "city_code": "20215"},
}

JMA_WARNING_URL = "https://www.jma.go.jp/bosai/warning/data/warning/200000.json"
JMA_QUAKE_URL = "https://www.jma.go.jp/bosai/quake/data/list.json"

WARNING_CODES = {
    "00": "なし",
    "02": "注意報",
    "03": "警報",
    "14": "特別警報",
}

JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"

QUAKE_INTERVAL = 300
QUAKE_POSTED_MAX = 200

# ─────────────────────────────────────────────────────
# data/ 初期化
# ─────────────────────────────────────────────────────
def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text("{}", encoding="utf-8")
    if not LOCATIONS_FILE.exists():
        LOCATIONS_FILE.write_text("{}", encoding="utf-8")
    if not TIMECARD_FILE.exists():
        TIMECARD_FILE.write_text("{}", encoding="utf-8")


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────────────────────────────────────────
# ネットワーク
# ─────────────────────────────────────────────────────
def fetch(url: str, timeout: int = 15, retries: int = 3) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "weather-news-bot/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt < retries - 1:
                import time
                time.sleep(1)
                logger.warning("fetch リトライ %d/%d (%s): %s", attempt + 1, retries, url, e)
            else:
                raise RuntimeError(f"fetch 失敗 (retries={retries}): {url}") from e
    return ""


# ─────────────────────────────────────────────────────
# 天気
# ─────────────────────────────────────────────────────
def get_weather(lat: float, lon: float, days: int = 2) -> dict:
    params = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode,windspeed_10m_max,uv_index_max",
        "hourly": "temperature_2m,weathercode,precipitation_probability,windspeed_10m",
        "timezone": "Asia/Tokyo", "forecast_days": days,
    })
    data = json.loads(fetch(f"https://api.open-meteo.com/v1/forecast?{params}"))
    d = data["daily"]

    def day_info(idx):
        code = d["weathercode"][idx]
        icon, desc = WEATHER_CODES.get(code, ("❓", "不明"))
        return {
            "icon": icon, "desc": desc,
            "temp_max": d["temperature_2m_max"][idx],
            "temp_min": d["temperature_2m_min"][idx],
            "precip": d["precipitation_sum"][idx],
            "wind": d["windspeed_10m_max"][idx],
            "uv": d["uv_index_max"][idx],
        }

    hourly = data["hourly"]
    today_hours = []
    tomorrow_hours = []
    today_date = d["time"][0]
    tomorrow_date = d["time"][1] if len(d["time"]) > 1 else None
    for i, t in enumerate(hourly["time"]):
        h = int(t.split("T")[1].split(":")[0])
        if h in (6, 9, 12, 15, 18, 21):
            entry = {
                "hour": h,
                "temp": hourly["temperature_2m"][i],
                "icon": WEATHER_CODES.get(hourly["weathercode"][i], ("❓", "不明"))[0],
                "precip": hourly["precipitation_probability"][i],
                "wind": hourly["windspeed_10m"][i],
            }
            if t.startswith(today_date):
                today_hours.append(entry)
            elif tomorrow_date and t.startswith(tomorrow_date):
                tomorrow_hours.append(entry)

    return {"today": day_info(0), "tomorrow": day_info(1), "hours": today_hours, "hours_tomorrow": tomorrow_hours}


def get_pollen_info(city_code: str) -> str:
    """ウェザーニュース花粉APIから今日の花粉飛散数を取得"""
    from datetime import datetime as _dt
    today = _dt.now(JST)
    start = today.strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    url = f"https://wxtech.weathernews.com/opendata/v1/pollen?citycode={city_code}&start={start}&end={end}"
    try:
        raw = fetch(url, timeout=10)
        lines = [l for l in raw.strip().split("\n") if l and not l.startswith("citycode")]
        if not lines:
            return "🌼 花粉: データなし"
        # 最新の有効なデータ（-9999以外）を探す
        latest = None
        for line in reversed(lines):
            parts = line.split(",")
            if len(parts) >= 3:
                val = int(parts[2])
                if val >= 0:
                    latest = val
                    break
        if latest is None:
            return "🌼 花粉: 飛散少なし"
        level = "少ない" if latest < 10 else "やや多い" if latest < 50 else "多い" if latest < 100 else "非常に多い"
        return f"🌼 花粉: {level}（飛散数: {latest}個/cm²）"
    except Exception:
        return "🌼 花粉情報取得失敗"


def get_jma_warnings() -> list[str]:
    try:
        data = json.loads(fetch(JMA_WARNING_URL))
        warnings = set()
        for at in data.get("areaTypes", []):
            for area in at.get("areas", []):
                for w in area.get("warnings", []):
                    if w.get("status") == "発表":
                        w_code = w.get("code", "00")
                        w_name = WARNING_CODES.get(w_code, f"不明({w_code})")
                        warnings.add(w_name)
        return list(warnings)
    except Exception:
        return []


def weather_comment(w: dict) -> str:
    comments = []
    if w["precip"] > 20:
        comments.append("☔ 雨が降りそうです。傘必須です。")
    elif w["precip"] > 5:
        comments.append("🌂 降水あり。念のため傘があると安心。")
    if w["temp_max"] > 30:
        comments.append("🥵 暑いです。熱中症に注意。")
    elif w["temp_max"] < 5:
        comments.append("🥶 寒いです。防寒必須。")
    if w["wind"] > 20:
        comments.append("💨 風が強めです。")
    if w["uv"] > 7:
        comments.append("🧴 UVが強いです。日焼け止め必須。")
    if not comments:
        comments.append("😊 過ごしやすい一日になりそうです。")
    return " ".join(comments)


def uv_label(uv: float | None) -> str:
    if uv is None:
        return "?"
    if uv <= 2:
        return "弱い"
    if uv <= 5:
        return "中程度"
    if uv <= 7:
        return "強い"
    if uv <= 10:
        return "非常に強い"
    return "極端に強い"


def temp_graph(hours_data: list[dict]) -> str:
    lines = []
    for h in hours_data:
        lines.append(
            f"🕐{h['hour']:2d}時  "
            f"{h['icon']} {h['temp']:5.1f}℃  "
            f"降水{h['precip']:3d}%  "
            f"風{h['wind']:.1f}km/h"
        )
    return "\n".join(lines)


def build_embed(location_key: str, mode: str = "today_tomorrow") -> discord.Embed:
    loc = LOCATIONS[location_key]
    now = datetime.now(JST)
    fetch_time = now.strftime("%H:%M")

    if mode == "week":
        params = urllib.parse.urlencode({
            "latitude": loc["lat"], "longitude": loc["lon"],
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode,windspeed_10m_max,uv_index_max",
            "timezone": "Asia/Tokyo", "forecast_days": 7,
        })
        data = json.loads(fetch(f"https://api.open-meteo.com/v1/forecast?{params}"))
        d = data["daily"]
        embed = discord.Embed(
            title=f"🌤️ {loc['name']} 1週間予報",
            description=f"📅 {now.month}月{now.day}日({WEEKDAYS[now.weekday()]}) から",
            color=0x4A90D9,
        )
        for i in range(min(7, len(d["time"]))):
            dt = datetime.strptime(d["time"][i], "%Y-%m-%d")
            code = d["weathercode"][i]
            icon, desc = WEATHER_CODES.get(code, ("❓", "不明"))
            uv_val = d["uv_index_max"][i]
            uv_str = uv_label(uv_val)
            day_label = f"{dt.month}/{dt.day}({WEEKDAYS[dt.weekday()]})"
            embed.add_field(
                name=f"{day_label} {icon} {desc}",
                value=(
                    f"🌡️ {d['temperature_2m_max'][i]:.0f}/{d['temperature_2m_min'][i]:.0f}℃  "
                    f"🌧️ {d['precipitation_sum'][i]:.0f}mm  "
                    f"💨 {d['windspeed_10m_max'][i]:.0f}km/h  "
                    f"☀️ UV:{uv_str}"
                ),
                inline=True,
            )
        embed.set_footer(text=f"取得時間 {fetch_time} | 毎朝6:00更新 | Open-Meteo")
        return embed

    # today_tomorrow モード
    w = get_weather(loc["lat"], loc["lon"])
    date_str = f"{now.month}月{now.day}日({WEEKDAYS[now.weekday()]})"
    embed = discord.Embed(
        title=f"🌤️ {loc['name']}の天気",
        description=f"📅 {date_str}",
        color=0x4A90D9,
    )

    today = w["today"]
    embed.add_field(
        name="📅 今日",
        value=(
            f"{today['icon']} {today['desc']}\n"
            f"🌡️ 最高 **{today['temp_max']}**℃ / 最低 **{today['temp_min']}**℃\n"
            f"🌧️ 降水量 {today['precip']}mm ／ 風 {today['wind']}km/h ／ UV {today['uv']}\n"
            f"💡 {weather_comment(today)}"
        ),
        inline=False,
    )

    # 今日の時間帯予報
    if w["hours"]:
        graph = temp_graph(w["hours"])
        embed.add_field(name="⏰ 今日の時間帯予報", value=f"```{graph}```", inline=False)

    tm = w["tomorrow"]
    embed.add_field(
        name="📅 明日",
        value=(
            f"{tm['icon']} {tm['desc']}\n"
            f"🌡️ 最高 **{tm['temp_max']}**℃ / 最低 **{tm['temp_min']}**℃\n"
            f"🌧️ 降水量 {tm['precip']}mm ／ 風 {tm['wind']}km/h ／ UV {tm['uv']}\n"
            f"💡 {weather_comment(tm)}"
        ),
        inline=False,
    )

    # 明日の時間帯予報
    if w["hours_tomorrow"]:
        graph = temp_graph(w["hours_tomorrow"])
        embed.add_field(name="⏰ 明日の時間帯予報", value=f"```{graph}```", inline=False)


    pollen = get_pollen_info(loc["city_code"])
    embed.add_field(name="花粉情報", value=pollen, inline=False)

    embed.set_footer(text=f"取得時間 {fetch_time} | 毎朝6:00更新 | Open-Meteo / 気象庁")
    return embed


# ─────────────────────────────────────────────────────
# 地震
# ─────────────────────────────────────────────────────
def get_jma_quakes() -> list[dict]:
    try:
        raw = fetch(JMA_QUAKE_URL)
        data = json.loads(raw)
        results = []
        for item in data:
            dt_str = item.get("at", "")
            name = item.get("anm", "")
            mag = str(item.get("mag", ""))
            cod = item.get("cod", "")
            depth = ""
            if cod:
                parts = cod.replace("/", "").split("-")
                if len(parts) >= 2:
                    try:
                        depth_val = int(parts[-1]) // 1000
                        depth = f"{depth_val}km"
                    except ValueError:
                        pass
            max_int = str(item.get("maxi", ""))
            results.append({
                "at": dt_str, "name": name, "mag": mag,
                "depth": depth, "maxInt": max_int,
            })
        return results
    except Exception as e:
        logger.error("地震情報取得失敗: %s", e)
        return []


# ─────────────────────────────────────────────────────
# タイムカード
# ─────────────────────────────────────────────────────
def load_timecard() -> dict:
    if not TIMECARD_FILE.exists():
        TIMECARD_FILE.write_text("{}", encoding="utf-8")
    try:
        return json.loads(TIMECARD_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_timecard(data: dict) -> None:
    TIMECARD_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def calc_work_hours(in_time: str, out_time: str) -> str:
    try:
        ih, im = map(int, in_time.split(":"))
        oh, om = map(int, out_time.split(":"))
        diff = (oh * 60 + om) - (ih * 60 + im)
        if diff < 0:
            diff += 24 * 60
        return f"{diff // 60}時間{diff % 60}分"
    except Exception:
        return "-"


def calc_work_minutes(in_time: str, out_time: str) -> int:
    """勤務時間を分単位で返す"""
    try:
        ih, im = map(int, in_time.split(":"))
        oh, om = map(int, out_time.split(":"))
        diff = (oh * 60 + om) - (ih * 60 + im)
        if diff < 0:
            diff += 24 * 60
        return diff
    except Exception:
        return 0


def build_timecard_embed(today: str) -> discord.Embed:
    tc = load_timecard()
    entry = tc.get(today, {})
    d = datetime.strptime(today, "%Y-%m-%d")
    in_t = entry.get("in")
    out_t = entry.get("out")

    lines = []
    if in_t:
        lines.append(f"🟢 出勤: **{in_t}**")
    else:
        lines.append("🟢 出勤: 未打刻")
    if out_t:
        lines.append(f"🔴 退勤: **{out_t}**")
    else:
        lines.append("🔴 退勤: 未打刻")
    if in_t and out_t:
        lines.append(f"⏱️ 勤務時間: {calc_work_hours(in_t, out_t)}")
    lines.append("")
    lines.append("ボタンで操作してください 👇")

    embed = discord.Embed(
        title=f"⏰ タイムカード  {d.month}月{d.day}日({WEEKDAYS[d.weekday()]})",
        description="\n".join(lines),
        color=0x43B583 if (in_t and out_t) else 0xFEE75C if in_t else 0x5865F2,
    )
    return embed


class TimecardView(discord.ui.View):
    """タイムカード操作パネル（日付ファースト方式）"""

    def __init__(self, embed_builder=None, target_date: str | None = None, month_offset: int = 0,
                 edit_mode: str | None = None, selected_hour: int | None = None,
                 selected_minute: int | None = None, date_page: int = 0):
        super().__init__(timeout=None)
        self.embed_builder = embed_builder or build_timecard_embed
        self.target_date = target_date or datetime.now(JST).strftime("%Y-%m-%d")
        self.edit_mode = edit_mode
        self.selected_hour = selected_hour
        self.selected_minute = selected_minute
        self.month_offset = month_offset
        self.date_page = date_page
        self._rebuild_items()

    def _rebuild_items(self) -> None:
        """edit_modeに応じて子ウィジェットを再構築"""
        self.clear_items()

        if self.edit_mode in ("in", "out"):
            # 時刻選択モード: 時Select → 分Select
            if self.selected_hour is None:
                h_select = discord.ui.Select(
                    placeholder="時を選択...",
                    options=self._build_hour_options(),
                    custom_id="tc_sel_hour",
                    row=0,
                )
                h_select.callback = self._make_hour_callback()
                self.add_item(h_select)
            else:
                m_select = discord.ui.Select(
                    placeholder="分を選択...",
                    options=self._build_minute_options(),
                    custom_id="tc_sel_minute",
                    row=0,
                )
                m_select.callback = self._make_minute_callback()
                self.add_item(m_select)

            # 戻るボタン
            back_btn = discord.ui.Button(label="↩️ 戻る", style=discord.ButtonStyle.gray, row=1, custom_id="tc_back")
            back_btn.callback = self._make_back_callback()
            self.add_item(back_btn)
        else:
            # メインモード: 日付Select + ページング + ボタン
            date_opts = self._build_date_options()
            if date_opts:
                d_select = discord.ui.Select(
                    placeholder="日付を選択...",
                    options=date_opts,
                    custom_id="tc_sel_date",
                    row=0,
                )
                d_select.callback = self._make_date_callback()
                self.add_item(d_select)

            # ページングボタン
            year, month = self._get_view_month()
            _, last_day = monthrange(year, month)
            if last_day > 15:
                page_label = "1〜15日" if self.date_page == 1 else "16〜末日"
                page_btn = discord.ui.Button(
                    label=page_label, style=discord.ButtonStyle.gray, row=1,
                    custom_id="tc_page",
                )
                page_btn.callback = self._make_page_callback()
                self.add_item(page_btn)

            # 前月/次月ボタン
            prev_btn = discord.ui.Button(label="◀前月", style=discord.ButtonStyle.gray, row=1, custom_id="tc_prev_month")
            prev_btn.callback = lambda i: self._month_shift(i, -1)
            self.add_item(prev_btn)

            next_btn = discord.ui.Button(label="次月▶", style=discord.ButtonStyle.gray, row=1, custom_id="tc_next_month")
            next_btn.callback = lambda i: self._month_shift(i, 1)
            self.add_item(next_btn)

            # 行2: 出勤 / 退勤 / 削除
            in_btn = discord.ui.Button(label="🟢 出勤", style=discord.ButtonStyle.green, row=2, custom_id="tc_in")
            in_btn.callback = lambda i: self._start_edit(i, "in")
            self.add_item(in_btn)

            out_btn = discord.ui.Button(label="🔴 退勤", style=discord.ButtonStyle.red, row=2, custom_id="tc_out")
            out_btn.callback = lambda i: self._start_edit(i, "out")
            self.add_item(out_btn)

            del_btn = discord.ui.Button(label="🗑️ 削除", style=discord.ButtonStyle.gray, row=2, custom_id="tc_del")
            del_btn.callback = self._make_del_callback()
            self.add_item(del_btn)

            # 行3: 月間一覧 / やめる
            month_btn = discord.ui.Button(label="📅 月間一覧", style=discord.ButtonStyle.blurple, row=3, custom_id="tc_month")
            month_btn.callback = self._make_month_callback()
            self.add_item(month_btn)

            quit_btn = discord.ui.Button(label="✏️ やめる", style=discord.ButtonStyle.gray, row=3, custom_id="tc_quit")
            quit_btn.callback = self._make_quit_callback()
            self.add_item(quit_btn)

    # ── 日付計算 ──
    def _get_view_month(self) -> tuple[int, int]:
        now = datetime.now(JST)
        y, m = now.year, now.month + self.month_offset
        while m < 1:
            y -= 1
            m += 12
        while m > 12:
            y += 1
            m -= 12
        return y, m

    def _date_label(self, date_str: str) -> str:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"

    def _build_date_options(self) -> list[discord.SelectOption]:
        year, month = self._get_view_month()
        _, last_day = monthrange(year, month)
        if self.date_page == 0:
            start, end = 1, min(15, last_day)
        else:
            start, end = 16, last_day
        if start > end:
            return []
        options = []
        for day in range(start, end + 1):
            ds = f"{year}-{month:02d}-{day:02d}"
            d = datetime(year, month, day)
            label = f"{month}/{day}({WEEKDAYS[d.weekday()]})"
            options.append(discord.SelectOption(label=label, value=ds))
        return options

    def _build_hour_options(self) -> list[discord.SelectOption]:
        return [discord.SelectOption(label=f"{h:02d}時", value=str(h)) for h in range(24)]

    def _build_minute_options(self) -> list[discord.SelectOption]:
        return [discord.SelectOption(label=f"{m:02d}分", value=str(m)) for m in range(0, 60, 10)]

    def _save_time(self, mode: str) -> None:
        if self.selected_hour is None or self.selected_minute is None:
            return
        t = f"{self.selected_hour:02d}:{self.selected_minute:02d}"
        tc = load_timecard()
        if self.target_date not in tc:
            tc[self.target_date] = {}
        tc[self.target_date][mode] = t
        save_timecard(tc)

    def _build_embed(self) -> discord.Embed:
        if self.edit_mode in ("in", "out"):
            return self._build_time_embed()
        return self._build_main_embed()

    def _build_main_embed(self) -> discord.Embed:
        vc = load_timecard()
        entry = vc.get(self.target_date, {})
        d = datetime.strptime(self.target_date, "%Y-%m-%d")
        in_t = entry.get("in")
        out_t = entry.get("out")

        lines = []
        if in_t:
            lines.append(f"🟢 出勤: **{in_t}**")
        else:
            lines.append("🟢 出勤: 未打刻")
        if out_t:
            lines.append(f"🔴 退勤: **{out_t}**")
        else:
            lines.append("🔴 退勤: 未打刻")
        if in_t and out_t:
            lines.append(f"⏱️ 勤務時間: {calc_work_hours(in_t, out_t)}")

        year, month = self._get_view_month()
        embed = discord.Embed(
            title=f"⏰ タイムカード  {d.month}月{d.day}日({WEEKDAYS[d.weekday()]})",
            description="\n".join(lines),
            color=0x43B583 if (in_t and out_t) else 0xFEE75C if in_t else 0x5865F2,
        )
        embed.set_footer(text=f"{year}年{month}月")
        return embed

    def _build_time_embed(self) -> discord.Embed:
        d = datetime.strptime(self.target_date, "%Y-%m-%d")
        mode_label = "出勤" if self.edit_mode == "in" else "退勤"
        parts = [f"📅 日付: **{self._date_label(self.target_date)}**"]
        parts.append(f"✏️ 種別: **{mode_label}**")
        if self.selected_hour is not None:
            parts.append(f"🕐 **{self.selected_hour:02d}時** — 分を選択してください")
        else:
            parts.append("🕐 時を選択してください")
        return discord.Embed(
            title=f"⏰ タイムカード  {d.month}月{d.day}日({WEEKDAYS[d.weekday()]})",
            description="\n".join(parts),
            color=0xFEE75C,
        )

    # ── コールバック ──
    def _make_date_callback(self):
        async def cb(interaction: discord.Interaction):
            # Selectから値を取得
            for item in self.children:
                if isinstance(item, discord.ui.Select) and item.custom_id == "tc_sel_date":
                    self.target_date = item.values[0]
                    break
            self._rebuild_items()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        return cb

    def _make_hour_callback(self):
        async def cb(interaction: discord.Interaction):
            for item in self.children:
                if isinstance(item, discord.ui.Select) and item.custom_id == "tc_sel_hour":
                    self.selected_hour = int(item.values[0])
                    break
            self._rebuild_items()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        return cb

    def _make_minute_callback(self):
        async def cb(interaction: discord.Interaction):
            for item in self.children:
                if isinstance(item, discord.ui.Select) and item.custom_id == "tc_sel_minute":
                    self.selected_minute = int(item.values[0])
                    break
            self._save_time(self.edit_mode)
            self.edit_mode = None
            self.selected_hour = None
            self.selected_minute = None
            self._rebuild_items()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        return cb

    def _make_page_callback(self):
        async def cb(interaction: discord.Interaction):
            self.date_page = 1 - self.date_page
            self._rebuild_items()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        return cb

    def _make_back_callback(self):
        async def cb(interaction: discord.Interaction):
            self.edit_mode = None
            self.selected_hour = None
            self.selected_minute = None
            self._rebuild_items()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        return cb

    async def _month_shift(self, interaction: discord.Interaction, delta: int):
        self.month_offset += delta
        self.date_page = 0
        # 切り替えた月の1日を自動選択
        year, month = self._get_view_month()
        self.target_date = f"{year}-{month:02d}-01"
        self._rebuild_items()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _start_edit(self, interaction: discord.Interaction, mode: str):
        self.edit_mode = mode
        self.selected_hour = None
        self.selected_minute = None
        self._rebuild_items()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    def _make_del_callback(self):
        async def cb(interaction: discord.Interaction):
            tc = load_timecard()
            if self.target_date in tc:
                del tc[self.target_date]
                save_timecard(tc)
            self._rebuild_items()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        return cb

    def _make_month_callback(self):
        async def cb(interaction: discord.Interaction):
            await interaction.response.defer()
            tc = load_timecard()
            entries = []
            view_year, view_month = self._get_view_month()
            for date_str, rec in sorted(tc.items()):
                try:
                    dd = datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    continue
                if dd.year == view_year and dd.month == view_month:
                    it = rec.get("in", "-")
                    ot = rec.get("out", "-")
                    wk = calc_work_hours(it, ot) if it != "-" and ot != "-" else "-"
                    mins = calc_work_minutes(it, ot) if it != "-" and ot != "-" else 0
                    wage = (mins / 60) * TIMECARD_HOURLY_WAGE if mins > 0 else 0
                    entries.append((dd, it, ot, wk, wage))
            if not entries:
                await interaction.followup.send(
                    f"📅 {view_year}年{view_month}月の打刻データはありません。", ephemeral=True
                )
                return
            lines = ["```"]
            lines.append(f" {'日付':<11} {'出勤':<6} {'退勤':<6} {'勤務時間':<10} {'給料':>8}")
            lines.append(" " + "-" * 50)
            total_min = 0
            total_wage = 0
            for dd, it, ot, wk, wage in entries[:30]:
                day_label = f"{dd.month}/{dd.day}({WEEKDAYS[dd.weekday()]})"
                wage_str = f"¥{wage:,.0f}" if wage > 0 else "-"
                lines.append(f" {day_label:<11} {it:<6} {ot:<6} {wk:<10} {wage_str:>8}")
                if wk != "-":
                    try:
                        h, m = wk.replace("時間", "h").replace("分", "m").replace("h", ":").replace("m", "").split(":")
                        total_min += int(h) * 60 + int(m)
                    except Exception:
                        pass
                total_wage += wage
            lines.append(" " + "-" * 50)
            lines.append(f" 合計勤務時間: {total_min // 60}時間{total_min % 60}分")
            lines.append(f" 時給: ¥{TIMECARD_HOURLY_WAGE:,} ／ 合計給料: ¥{total_wage:,.0f}")
            lines.append("```")
            m_embed = discord.Embed(
                title=f"📅 {view_year}年{view_month}月 タイムカード",
                description="\n".join(lines), color=0x4A90D9,
            )
            await interaction.edit_original_response(embed=m_embed, view=TimecardView(
                self.embed_builder, self.target_date, self.month_offset
            ))
        return cb

    def _make_quit_callback(self):
        async def cb(interaction: discord.Interaction):
            await interaction.message.delete()
        return cb


# ─────────────────────────────────────────────────────
# UI — 統合セレクトメニュー
# ─────────────────────────────────────────────────────
class WeatherSelect(discord.ui.Select):
    """地域 + モード選択セレクトメニュー（統合版）"""
    def __init__(self, location_key: str, mode: str = "today_tomorrow"):
        options = [
            discord.SelectOption(label="松本市", value="matsumoto"),
            discord.SelectOption(label="安曇野市", value="azumino"),
            discord.SelectOption(label="─" * 5, value="separator"),
            discord.SelectOption(label="今日＋明日", value="today_tomorrow"),
            discord.SelectOption(label="1週間予報", value="week"),
        ]
        super().__init__(
            placeholder="地域 / モードを選択...",
            options=options,
            custom_id="weather_select",
        )
        self.location_key = location_key
        self.mode = mode

    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        if val == "separator":
            return
        if val in ("today_tomorrow", "week"):
            self.mode = val
        else:
            self.location_key = val
        await interaction.response.defer()
        embed = build_embed(self.location_key, self.mode)
        new_view = WeatherView(self.location_key, self.mode)
        await interaction.edit_original_response(embed=embed, view=new_view)


class WeatherView(discord.ui.View):
    def __init__(self, location_key: str = "matsumoto", mode: str = "today_tomorrow"):
        super().__init__(timeout=None)
        self.location_key = location_key
        self.mode = mode
        self.add_item(WeatherSelect(location_key, mode))


# ─────────────────────────────────────────────────────
# Bot
# ─────────────────────────────────────────────────────
class WeatherBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.add_view(WeatherView())
        self.tc_messages: list[dict] = []  # on_readyでstate.jsonから復元
        self.tc_updated_date: str = ""  # 最後に更新した日付（重複更新防止）

    async def setup_hook(self):
        self.add_command(weather_cmd)
        self.add_command(add_place_cmd)
        self.add_command(remove_place_cmd)
        self.add_command(tc_cmd)
        self.add_command(quake_cmd)
        # タイムカードを永続Viewとして登録（Bot再起動後もボタン操作可能）
        self.add_view(TimecardView())

    async def on_ready(self):
        logger.info("Bot 起動: %s (ID: %s)", self.user, self.user.id)
        if not self.morning_post.is_running():
            self.morning_post.start()
        if not self.evening_post.is_running():
            self.evening_post.start()
        if not self.quake_monitor.is_running():
            self.quake_monitor.start()
        if not self.tc_date_refresh.is_running():
            self.tc_date_refresh.start()
        # 再起動時に当日キーで初期化（再起動直後の0:00更新をスキップ）
        # tc_messages を state.json から復元
        state = load_json(STATE_FILE)
        self.tc_messages = state.get("tc_messages", [])
        logger.info("tc_messages 復元: %d件", len(self.tc_messages))

        self.tc_updated_date = datetime.now(JST).strftime("%Y-%m-%d")

    # ── タイムカード日付更新（0:00検知） ──
    @tasks.loop(minutes=1)
    async def tc_date_refresh(self):
        now = datetime.now(JST)
        today = now.strftime("%Y-%m-%d")
        # 日付が変わっていたら更新（毎分チェック、重複更新防止）
        if self.tc_updated_date != today:
            self.tc_updated_date = today
            for tc_msg in self.tc_messages:
                try:
                    channel = self.get_channel(tc_msg["channel_id"])
                    if channel:
                        msg = await channel.fetch_message(tc_msg["message_id"])
                        embed = build_timecard_embed(today)
                        view = TimecardView(build_timecard_embed, target_date=today)
                        await msg.edit(embed=embed, view=view)
                        logger.info("タイムカードを翌日に更新: %s", tc_msg["message_id"])
                        # state.json の tc_messages も更新
                        state = load_json(STATE_FILE)
                        state["tc_messages"] = self.tc_messages
                        save_json(STATE_FILE, state)
                except Exception as e:
                    logger.warning("タイムカード更新失敗: %s", e)

    @tc_date_refresh.before_loop
    async def before_tc_refresh(self):
        await self.wait_until_ready()

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        logger.error("コマンドエラー: %s", error)

    # ── 朝6:00投稿 ──
    @tasks.loop(minutes=1)
    async def morning_post(self):
        now = datetime.now(JST)
        if now.hour == 6 and now.minute == 0:
            state = load_json(STATE_FILE)
            if state.get("morning_posted") == now.strftime("%Y-%m-%d"):
                return
            await self._post_weather("today_tomorrow", "朝")
            state["morning_posted"] = now.strftime("%Y-%m-%d")
            save_json(STATE_FILE, state)

    @morning_post.before_loop
    async def before_morning(self):
        await self.wait_until_ready()

    # ── 夜21:00投稿 ──
    @tasks.loop(minutes=1)
    async def evening_post(self):
        now = datetime.now(JST)
        if now.hour == 21 and now.minute == 0:
            state = load_json(STATE_FILE)
            if state.get("evening_posted") == now.strftime("%Y-%m-%d"):
                return
            await self._post_weather("today_tomorrow", "夜")
            state["evening_posted"] = now.strftime("%Y-%m-%d")
            save_json(STATE_FILE, state)

    @evening_post.before_loop
    async def before_evening(self):
        await self.wait_until_ready()

    # ── 地震監視 ──
    @tasks.loop(seconds=QUAKE_INTERVAL)
    async def quake_monitor(self):
        # オンオフ確認
        state = load_json(STATE_FILE)
        if not state.get("quake_enabled", True):
            return

        quakes = get_jma_quakes()
        if not quakes:
            return
        posted = state.get("quake_posted", [])
        if not posted:
            for q in quakes:
                key = f"{q['at']}_{q['name']}_{q['mag']}"
                if key not in posted:
                    posted.append(key)
            state["quake_posted"] = posted[-QUAKE_POSTED_MAX:]
            save_json(STATE_FILE, state)
            logger.info("初回起動: 地震情報 %d 件を既読登録しました", len(posted))
            return
        new_quakes = []
        for q in quakes:
            key = f"{q['at']}_{q['name']}_{q['mag']}"
            if key not in posted:
                new_quakes.append((key, q))
        if not new_quakes:
            return
        for key, q in new_quakes[:3]:
            posted.append(key)
        if len(posted) > QUAKE_POSTED_MAX:
            posted = posted[-QUAKE_POSTED_MAX:]
        state["quake_posted"] = posted
        save_json(STATE_FILE, state)

        # 古い地震通知を削除してから投稿
        channel = self.get_channel(CHANNEL_ID)
        if channel:
            try:
                async for hist_msg in channel.history(limit=50):
                    if hist_msg.author == self.user and hist_msg.embeds:
                        for e in hist_msg.embeds:
                            if e.title and "🌋 地震情報" in e.title:
                                await hist_msg.delete()
                                logger.info("古い地震通知を削除: %s", hist_msg.id)
                                break
            except Exception as e:
                logger.warning("古い地震通知削除失敗: %s", e)

        for key, q in new_quakes[:3]:
            embed = discord.Embed(
                title="🌋 地震情報",
                description=(
                    f"📅 {q['at']}\n"
                    f"📍 震源: {q['name']}\n"
                    f"📊 マグニチュード: **{q['mag']}**\n"
                    f"📏 深さ: {q['depth']}\n"
                    f"📳 最大震度: {q['maxInt']}"
                ),
                color=0xFF4500,
            )
            if channel:
                await channel.send(embed=embed)
                logger.info("地震情報投稿: %s M%s", q['name'], q['mag'])

    @quake_monitor.before_loop
    async def before_quake_monitor(self):
        await self.wait_until_ready()

    async def _post_weather(self, mode: str, label: str):
        channel = self.get_channel(CHANNEL_ID)
        if not channel:
            logger.error("チャンネル %s が見つかりません", CHANNEL_ID)
            return

        # 古い天気予報メッセージを削除
        try:
            async for msg in channel.history(limit=50):
                if msg.author == self.user and msg.embeds:
                    for e in msg.embeds:
                        if e.title and "🌤️" in e.title and "の天気" in e.title:
                            await msg.delete()
                            logger.info("古い天気予報メッセージを削除: %s", msg.id)
                            break
        except Exception as ex:
            logger.error("古いメッセージ削除中にエラー: %s", ex)

        warnings = get_jma_warnings()
        warning_text = ""
        if warnings:
            warning_text = "**⚠️ 警報・注意報（長野県）**\n" + "\n".join(f"🔴 {w}" for w in warnings) + "\n\n"

        embed = build_embed("matsumoto", mode)
        view = WeatherView("matsumoto", mode)

        await channel.send(content=warning_text, embed=embed, view=view)
        logger.info("%sの天気予報を投稿しました", label)


# ─────────────────────────────────────────────────────
# コマンド
# ─────────────────────────────────────────────────────
@commands.command(name="weather", help="現在の天気を即座に表示（!weather [地域名]）")
async def weather_cmd(ctx, *, args: str = ""):
    location_key = "matsumoto"
    if "安曇" in args or "azumino" in args.lower():
        location_key = "azumino"

    # チャンネル内の古い天気予報メッセージを削除
    try:
        async for hist_msg in ctx.channel.history(limit=50):
            if hist_msg.author == ctx.bot.user and hist_msg.embeds:
                for e in hist_msg.embeds:
                    if e.title and "🌤️" in e.title:
                        await hist_msg.delete()
                        break
    except Exception:
        pass

    embed = build_embed(location_key)
    view = WeatherView(location_key)
    await ctx.send(embed=embed, view=view)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@commands.command(name="add_place", help="お出かけ先を追加（!add_place 名前 緯度 経度）")
async def add_place_cmd(ctx, name: str = "", lat: str = "", lon: str = ""):
    if not name or not lat or not lon:
        await ctx.send("使い方: `!add_place 名前 緯度 経度`\n例: `!add_place 東京 35.6762 139.6503`", delete_after=10)
        return
    try:
        lat_f, lon_f = float(lat), float(lon)
    except ValueError:
        await ctx.send("⚠️ 緯度・経度は数値で指定してください。", delete_after=10)
        return
    locations = load_json(LOCATIONS_FILE)
    locations[name] = {"name": name, "lat": lat_f, "lon": lon_f}
    save_json(LOCATIONS_FILE, locations)
    LOCATIONS[name] = {"name": name, "lat": lat_f, "lon": lon_f}
    await ctx.send(f"✅ お出かけ先「{name}」を追加しました。", delete_after=10)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@commands.command(name="remove_place", help="お出かけ先を削除（!remove_place 名前）")
async def remove_place_cmd(ctx, *, name: str = ""):
    if not name:
        await ctx.send("使い方: `!remove_place 名前`\n例: `!remove_place 東京`", delete_after=10)
        return
    locations = load_json(LOCATIONS_FILE)
    if name not in locations:
        await ctx.send(f"⚠️ 「{name}」は登録されていません。", delete_after=10)
        return
    del locations[name]
    save_json(LOCATIONS_FILE, locations)
    LOCATIONS.pop(name, None)
    await ctx.send(f"✅ お出かけ先「{name}」を削除しました。", delete_after=10)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@commands.command(name="tc", help="タイムカードパネルを表示（ボタンで操作）")
async def tc_cmd(ctx):
    today = datetime.now(JST).strftime("%Y-%m-%d")
    embed = build_timecard_embed(today)
    view = TimecardView(build_timecard_embed)
    # TC_CHANNEL_ID が設定されていればそのチャンネルに投稿、なければコマンド実行チャンネル
    target_channel = ctx.channel
    if TC_CHANNEL_ID:
        tc_ch = ctx.bot.get_channel(TC_CHANNEL_ID)
        if tc_ch:
            target_channel = tc_ch

    # チャンネル内の古いタイムカードメッセージを削除（最新1件を残す）
    try:
        tc_msg_ids_to_keep = {m["message_id"] for m in ctx.bot.tc_messages}
        async for hist_msg in target_channel.history(limit=50):
            if hist_msg.author == ctx.bot.user and hist_msg.embeds:
                for e in hist_msg.embeds:
                    if e.title and "⏰ タイムカード" in e.title:
                        # 追跡リストにあるものは削除しない
                        if hist_msg.id not in tc_msg_ids_to_keep:
                            await hist_msg.delete()
                        break
    except Exception:
        pass

    msg = await target_channel.send(embed=embed, view=view)
    # メッセージを追跡リストに追加（日付更新用）
    ctx.bot.tc_messages.append({"channel_id": msg.channel.id, "message_id": msg.id})
    # 古い追跡データがたまらないよう、最新5件のみ保持
    if len(ctx.bot.tc_messages) > 5:
        ctx.bot.tc_messages = ctx.bot.tc_messages[-5:]
    # state.json にも保存
    state = load_json(STATE_FILE)
    state["tc_messages"] = ctx.bot.tc_messages
    save_json(STATE_FILE, state)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@commands.command(name="quake", help="地震速報のオンオフ（!quake on / !quake off）")
async def quake_cmd(ctx, mode: str = ""):
    mode = mode.lower().strip()
    if mode not in ("on", "off"):
        await ctx.send("使い方: `!quake on` または `!quake off`", delete_after=10)
        return
    state = load_json(STATE_FILE)
    state["quake_enabled"] = (mode == "on")
    save_json(STATE_FILE, state)
    status = "有効 ✅" if state["quake_enabled"] else "無効 ❌"
    await ctx.send(f"地震速報を{status}にしました。", delete_after=10)
    try:
        await ctx.message.delete()
    except Exception:
        pass


# ─────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN が設定されていません")
    if not CHANNEL_ID:
        raise RuntimeError("CHANNEL_ID が設定されていません")

    ensure_data_dir()

    bot = WeatherBot()
    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Weather Bot — Discord Bot 版
朝6:00 / 夜21:00 に天気予報を投稿、セレクトメニューで地域切替・1週間予報・お出かけ先対応
"""

import urllib.request
import urllib.parse
import json
import re
import os
import math
import asyncio
import logging
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

DATA_DIR = Path(__file__).resolve().parent / "data"
STATE_FILE = DATA_DIR / "state.json"
LOCATIONS_FILE = DATA_DIR / "locations.json"

LOCATIONS = {
    "matsumoto": {"name": "松本市",   "lat": 36.230, "lon": 137.972},
    "azumino":   {"name": "安曇野市", "lat": 36.320, "lon": 137.900},
}

JMA_WARNING_URL = "https://www.jma.go.jp/bosai/warning/data/warning/200000.json"
JMA_QUAKE_URL = "https://www.jma.go.jp/bosai/quake/data/list.json"
JMA_TYPHOON_URL = "https://www.jma.go.jp/bosai/typhoon/data/list.json"

WARNING_CODES = {
    "00": "なし",
    "02": "注意報",
    "03": "警報",
    "14": "特別警報",
}

# タイムカードデータファイル
TIMECARD_FILE = DATA_DIR / "timecard.json"

JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"


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
# 天気取得（リトライ付き）
# ─────────────────────────────────────────────────────
def fetch(url: str, timeout: int = 15, retries: int = 3) -> str:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "weather-news-bot/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            wait = 2 ** attempt
            logger.warning("fetch リトライ %d/%d (%s): %s", attempt + 1, retries, url, e)
            import time
            time.sleep(wait)
    raise RuntimeError(f"fetch 失敗 (retries={retries}): {url}") from last_err


def get_weather(lat: float, lon: float, days: int = 2) -> dict:
    """Open-Meteo から天気を取得。days=2 で今日+明日、days=7 で1週間"""
    daily_params = "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode,windspeed_10m_max,uv_index_max"
    hourly_params = "temperature_2m,weathercode,precipitation_probability,windspeed_10m"
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "daily": daily_params,
        "hourly": hourly_params,
        "timezone": "Asia/Tokyo",
        "forecast_days": days,
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

    days_list = [day_info(i) for i in range(len(d["weathercode"]))]

    # 時間帯予報（今日＋明日）
    hourly = data["hourly"]
    hours_data = []
    tomorrow_hours_data = []
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    tomorrow_str = (datetime.now(JST) + timedelta(days=1)).strftime("%Y-%m-%d")
    for i, t in enumerate(hourly["time"]):
        date_part, time_part = t.split("T")
        h = int(time_part.split(":")[0])
        if h not in (6, 9, 12, 15, 18, 21):
            continue
        wc = hourly["weathercode"][i]
        entry = {
            "hour": h,
            "temp": hourly["temperature_2m"][i],
            "icon": WEATHER_CODES.get(wc, ("❓", "不明"))[0],
            "precip": hourly["precipitation_probability"][i],
            "wind": hourly["windspeed_10m"][i],
        }
        if date_part == today_str:
            hours_data.append(entry)
        elif date_part == tomorrow_str:
            tomorrow_hours_data.append(entry)

    return {
        "days": days_list,
        "hours": hours_data,
        "tomorrow_hours": tomorrow_hours_data,
    }


def get_pollen_info(lat: float, lon: float) -> str:
    """Open-Meteo pollen API を試みる。失敗時はフォールバック文字列"""
    try:
        params = urllib.parse.urlencode({
            "latitude": lat,
            "longitude": lon,
            "daily": "pollen_tree,pollen_grass,pollen_weed",
            "timezone": "Asia/Tokyo",
            "forecast_days": 1,
        })
        data = json.loads(fetch(f"https://air-quality-api.open-meteo.com/v1/air-quality?{params}"))
        d = data.get("daily", {})
        tree = d.get("pollen_tree", [None])[0]
        grass = d.get("pollen_grass", [None])[0]
        weed = d.get("pollen_weed", [None])[0]
        parts = []
        if tree is not None:
            parts.append(f"🌲 樹花粉: {'多い' if tree > 50 else 'やや多い' if tree > 20 else '少ない' if tree is not None else '不明'}")
        if grass is not None:
            parts.append(f"🌿 草花粉: {'多い' if grass > 50 else 'やや多い' if grass > 20 else '少ない' if grass is not None else '不明'}")
        if weed is not None:
            parts.append(f"🌾 雑草花粉: {'多い' if weed > 50 else 'やや多い' if weed > 20 else '少ない' if weed is not None else '不明'}")
        return "\n".join(parts) if parts else "花粉データを取得できませんでした"
    except Exception:
        return "花粉情報は現在取得できません"


def get_jma_warnings() -> list[str]:
    """気象庁から警報・注意報を取得"""
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


# ─────────────────────────────────────────────────────
# 地震情報
# ─────────────────────────────────────────────────────
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の距離を km で返す（Haversine 公式）"""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_jma_quakes() -> list[dict]:
    """気象庁から地震情報を取得して、発表から1時間以内の有感地震を返す"""
    try:
        raw = fetch(JMA_QUAKE_URL)
        data = json.loads(raw)
        now = datetime.now(JST)
        one_hour_ago = now - timedelta(hours=1)
        results = []
        for item in data:
            # 発表時刻
            date_str = item.get("at", "")
            if not date_str:
                continue
            try:
                # ISO 8601 形式をパース
                if date_str.endswith("Z"):
                    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00")).                    dt = dt.astimezone(JST)
                elif "+" in date_str or date_str.count("-") > 2:
                    dt = datetime.fromisoformat(date_str)
                    dt = dt.astimezone(JST)
                else:
                    dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")
                    dt = dt.replace(tzinfo=JST)
            except Exception:
                continue
            if dt < one_hour_ago:
                continue
            # 震度1以上
            scale = item.get("maxScale", 0)
            if scale < 10:  # 気象庁の震度コード: 10=震度1, 20=震度2 ...
                continue
            results.append({
                "id": item.get("eid", ""),
                "at": dt,
                "hypo": item.get("hypo", ""),
                "scale": scale,
                "mag": item.get("mag", 0),
            })
        return results
    except Exception as e:
        logger.error("地震情報取得失敗: %s", e)
        return []


def get_jma_typhoons() -> list[dict]:
    """気象庁から台風情報を取得"""
    try:
        raw = fetch(JMA_TYPHOON_URL)
        data = json.loads(raw)
        results = []
        for item in data:
            # 台風番号
            num = item.get("number", "")
            if not num:
                continue
            name = item.get("name", "")
            # 現在位置
            lat = item.get("lat", 0)
            lon = item.get("lon", 0)
            # 中心気圧・最大風速
            pressure = item.get("pressure", 0)
            wind_max = item.get("wind", 0)
            # 更新時刻
            dt_str = item.get("at", "")
            results.append({
                "number": num,
                "name": name,
                "lat": lat,
                "lon": lon,
                "pressure": pressure,
                "wind_max": wind_max,
                "at": dt_str,
            })
        return results
    except Exception as e:
        logger.error("台風情報取得失敗: %s", e)
        return []


def is_typhoon_near_japan(lat: float, lon: float, threshold_km: float = 500.0) -> bool:
    """台風が日本（本州中心付近）から threshold_km 以内かを判定"""
    # 日本本州の中心付近（東京）を代表点とする
    tokyo_lat, tokyo_lon = 35.6762, 139.6503
    return haversine(lat, lon, tokyo_lat, tokyo_lon) <= threshold_km


# ─────────────────────────────────────────────────────
# タイムカード
# ─────────────────────────────────────────────────────
def load_timecard() -> dict:
    return load_json(TIMECARD_FILE)


def save_timecard(data: dict) -> None:
    save_json(TIMECARD_FILE, data)


def parse_time(time_str: str) -> str:
    """HH:MM 形式をバリデーションして返す"""
    m = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if not m:
        return ""
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return f"{h:02d}:{mi:02d}"
    return ""


def calc_work_hours(in_time: str, out_time: str) -> str:
    """勤務時間を計算して 'X時間Y分' で返す"""
    ih, im = map(int, in_time.split(":"))
    oh, om = map(int, out_time.split(":"))
    total_min = (oh * 60 + om) - (ih * 60 + im)
    if total_min < 0:
        total_min += 24 * 60
    h, m = divmod(total_min, 60)
    return f"{h}時間{m}分"


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
    uv = w.get("uv", 0)
    if uv is not None and uv > 7:
        comments.append("🧴 UVが強いです。日焼け止め必須。")
    if not comments:
        comments.append("😊 過ごしやすい一日になりそうです。")
    return " ".join(comments)


def uv_label(uv: float | None) -> str:
    if uv is None:
        return "不明"
    if uv <= 2:
        return f"{uv}（弱）"
    if uv <= 5:
        return f"{uv}（中）"
    if uv <= 7:
        return f"{uv}（強）"
    if uv <= 10:
        return f"{uv}（非常に強）"
    return f"{uv}（危険）"


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


def get_day_label(idx: int) -> str:
    d = datetime.now(JST) + timedelta(days=idx)
    return f"{d.month}月{d.day}日({WEEKDAYS[d.weekday()]})"


def build_embed(location_key: str, mode: str = "today_tomorrow") -> discord.Embed:
    """指定地域・モードの天気 Embed を作成

    mode:
      - 'today_tomorrow': 今日＋明日（デフォルト投稿）
      - 'tomorrow_dayafter': 明日＋明後日（夜21:00投稿）
      - 'weekly': 1週間予報
    """
    state = load_json(STATE_FILE)
    channel_states = state.get("channels", {})
    # location_key はカスタム座標の場合は 'custom:名前' 形式

    if location_key.startswith("custom:"):
        loc_name = location_key[7:]
        custom_locs = load_json(LOCATIONS_FILE)
        loc_data = custom_locs.get(loc_name, {"lat": 36.230, "lon": 137.972, "name": loc_name})
        loc = {"name": loc_data.get("name", loc_name), "lat": loc_data["lat"], "lon": loc_data["lon"]}
    else:
        loc = LOCATIONS.get(location_key, LOCATIONS["matsumoto"])

    if mode == "weekly":
        w = get_weather(loc["lat"], loc["lon"], days=7)
    else:
        w = get_weather(loc["lat"], loc["lon"], days=3)

    now = datetime.now(JST)

    if mode == "weekly":
        embed = discord.Embed(
            title=f"📆 {loc['name']}の1週間予報",
            description=f"🗓️ {now.month}月{now.day}日({WEEKDAYS[now.weekday()]}) 更新",
            color=0x4A90D9,
        )
        for i, day in enumerate(w["days"]):
            day_label = get_day_label(i)
            embed.add_field(
                name=f"📅 {day_label}",
                value=(
                    f"{day['icon']} {day['desc']}\n"
                    f"🌡️ 最高 **{day['temp_max']}**℃ / 最低 **{day['temp_min']}**℃\n"
                    f"🌧️ 降水量 {day['precip']}mm ／ 風 {day['wind']}km/h ／ UV {day['uv']}\n"
                    f"💡 {weather_comment(day)}"
                ),
                inline=False,
            )
        embed.set_footer(text="1週間予報 | Open-Meteo / 気象庁")
        return embed

    # 今日＋明日 or 明日＋明後日
    offset = 1 if mode == "tomorrow_dayafter" else 0

    day0 = w["days"][offset]
    day1 = w["days"][offset + 1]
    label0 = get_day_label(offset)
    label1 = get_day_label(offset + 1)

    if mode == "tomorrow_dayafter":
        mode_label = "明日の天気"
    else:
        mode_label = "今日の天氣"

    embed = discord.Embed(
        title=f"🌤️ {loc['name']}の天気",
        description=f"🕐 {now.strftime('%H:%M')} 更新",
        color=0x4A90D9,
    )

    # 今日
    embed.add_field(
        name=f"📅 {label0}",
        value=(
            f"{day0['icon']} {day0['desc']}\n"
            f"🌡️ 最高 **{day0['temp_max']}**℃ / 最低 **{day0['temp_min']}**℃\n"
            f"🌧️ 降水量 {day0['precip']}mm ／ 風 {day0['wind']}km/h ／ UV {day0['uv']}\n"
            f"💡 {weather_comment(day0)}"
        ),
        inline=False,
    )

    # 今日の時間帯予報
    if offset == 0 and w.get("hours"):
        graph = temp_graph(w["hours"])
        embed.add_field(name="⏰ 今日の時間帯予報", value=f"```{graph}```", inline=False)

    # UV / 花粉情報
    uv_text = uv_label(day0['uv'])
    try:
        pollen_text = get_pollen_info(loc["lat"], loc["lon"])
    except Exception:
        pollen_text = "花粉情報は現在取得できません"
    embed.add_field(
        name="🌞 UV・花粉",
        value=f"UV指数: {uv_text}\n{pollen_text}",
        inline=False,
    )

    # 明日
    embed.add_field(
        name=f"📅 {label1}",
        value=(
            f"{day1['icon']} {day1['desc']}\n"
            f"🌡️ 最高 **{day1['temp_max']}**℃ / 最低 **{day1['temp_min']}**℃\n"
            f"🌧️ 降水量 {day1['precip']}mm ／ 風 {day1['wind']}km/h ／ UV {day1['uv']}\n"
            f"💡 {weather_comment(day1)}"
        ),
        inline=False,
    )

    # 明日の時間帯予報
    if offset == 0 and w.get("tomorrow_hours"):
        graph2 = temp_graph(w["tomorrow_hours"])
        embed.add_field(name="⏰ 明日の時間帯予報", value=f"```{graph2}```", inline=False)
    elif offset == 1 and w.get("tomorrow_hours"):
        graph = temp_graph(w["tomorrow_hours"])
        embed.add_field(name="⏰ 明日の時間帯予報", value=f"```{graph}```", inline=False)

    embed.set_footer(text=f"{'朝6' if offset == 0 else '夜21'}:00更新 | Open-Meteo / 気象庁")
    return embed


# ─────────────────────────────────────────────────────
# セレクトメニュー
# ─────────────────────────────────────────────────────
class CombinedSelect(discord.ui.Select):
    """地域＋予報モード統合セレクトメニュー"""
    def __init__(self, location_key: str, mode: str = "today_tomorrow"):
        self.location_key = location_key
        self.mode = mode
        options = [
            discord.SelectOption(label="📅 松本市（今日＋明日）", value="matsumoto:today_tomorrow",
                                 default=(location_key == "matsumoto" and mode == "today_tomorrow")),
            discord.SelectOption(label="📅 安曇野市（今日＋明日）", value="azumino:today_tomorrow",
                                 default=(location_key == "azumino" and mode == "today_tomorrow")),
            discord.SelectOption(label="📆 松本市（1週間予報）", value="matsumoto:weekly",
                                 default=(location_key == "matsumoto" and mode == "weekly")),
            discord.SelectOption(label="📆 安曇野市（1週間予報）", value="azumino:weekly",
                                 default=(location_key == "azumino" and mode == "weekly")),
        ]
        # お出かけ先（最大21件まで追加、合計25件以内）
        custom_locs = load_json(LOCATIONS_FILE)
        for name in custom_locs:
            if len(options) >= 25:
                break
            options.append(
                discord.SelectOption(label=f"📍 {name}（今日＋明日）", value=f"custom:{name}:today_tomorrow",
                                     default=(location_key == f"custom:{name}" and mode == "today_tomorrow"))
            )
            if len(options) >= 25:
                break
            options.append(
                discord.SelectOption(label=f"📍 {name}（1週間予報）", value=f"custom:{name}:weekly",
                                     default=(location_key == f"custom:{name}" and mode == "weekly"))
            )
        super().__init__(placeholder="地域・予報モードを選択...", options=options, custom_id="combined_select")

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        parts = value.split(":", 2)
        if len(parts) == 3:
            self.location_key = f"custom:{parts[1]}"
            self.mode = parts[2]
        else:
            self.location_key = parts[0]
            self.mode = parts[1]

        # state.json にデフォルト地域を保存
        state = load_json(STATE_FILE)
        channel_id = str(interaction.channel_id)
        if "channels" not in state:
            state["channels"] = {}
        state["channels"][channel_id] = state["channels"].get(channel_id, {})
        state["channels"][channel_id]["location"] = self.location_key
        save_json(STATE_FILE, state)

        # 重い処理があるので defer で応答を遅延
        await interaction.response.defer()
        try:
            embed = build_embed(self.location_key, mode=self.mode)
            new_view = WeatherView(self.location_key, mode=self.mode)
            await interaction.edit_original_response(embed=embed, view=new_view)
        except Exception as e:
            logger.error("セレクトメニュー更新失敗: %s", e)


class WeatherView(discord.ui.View):
    def __init__(self, location_key: str = "matsumoto", mode: str = "today_tomorrow"):
        super().__init__(timeout=None)
        self.add_item(CombinedSelect(location_key, mode))


# ─────────────────────────────────────────────────────
# Bot
# ─────────────────────────────────────────────────────
class WeatherBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.add_view(WeatherView())

    async def setup_hook(self):
        # コマンドをここで登録（discord.py 2.7.1 対応）
        self.add_command(weather_cmd)
        self.add_command(add_place_cmd)
        self.add_command(remove_place_cmd)
        self.add_command(tc_cmd)

    async def on_ready(self):
        logger.info("Bot 起動: %s (ID: %s)", self.user, self.user.id)
        # タスクは on_ready で開始
        if not self.morning_post.is_running():
            self.morning_post.start()
        if not self.evening_post.is_running():
            self.evening_post.start()
        if not self.quake_monitor.is_running():
            self.quake_monitor.start()
        if not self.typhoon_monitor.is_running():
            self.typhoon_monitor.start()

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        logger.error("コマンドエラー: %s", error)

    # ── 地震監視（5分ごと） ──
    @tasks.loop(minutes=5)
    async def quake_monitor(self):
        try:
            quakes = get_jma_quakes()
            if not quakes:
                return
            # 重複防止: 投稿済みIDを state.json に記録
            state = load_json(STATE_FILE)
            posted = set(state.get("quake_posted_ids", []))
            new_quakes = [q for q in quakes if q["id"] not in posted]
            if not new_quakes:
                return
            channel = self.get_channel(CHANNEL_ID)
            if not channel:
                return
            for q in new_quakes:
                scale_label = f"震度{q['scale'] // 10}" if q['scale'] >= 10 else f"震度{q['scale']}"
                embed = discord.Embed(
                    title="🔴 地震情報",
                    color=0xFF4500,
                )
                embed.add_field(name="📅 発生時刻", value=q["at"].strftime("%Y/%m/%d %H:%M"), inline=True)
                embed.add_field(name="📍 震源地", value=q["hypo"] or "不明", inline=True)
                embed.add_field(name="📊 震度", value=scale_label, inline=True)
                embed.add_field(name="🌊 マグニチュード", value=f"M{q['mag']}", inline=True)
                await channel.send(embed=embed)
                posted.add(q["id"])
                logger.info("地震情報投稿: %s %s", q["hypo"], scale_label)
            # 投稿済みIDを保存（最大100件まで）
            state["quake_posted_ids"] = list(posted)[-100:]
            save_json(STATE_FILE, state)
        except Exception as e:
            logger.error("地震監視タスク失敗: %s", e)

    @quake_monitor.before_loop
    async def before_quake_monitor(self):
        await self.wait_until_ready()

    # ── 台風監視（10分ごと） ──
    @tasks.loop(minutes=10)
    async def typhoon_monitor(self):
        try:
            typhoons = get_jma_typhoons()
            if not typhoons:
                return
            state = load_json(STATE_FILE)
            today_str = datetime.now(JST).strftime("%Y-%m-%d")
            posted_key = f"typhoon_posted_{today_str}"
            posted = set(state.get(posted_key, []))
            new_typhoons = [t for t in typhoons if t["number"] not in posted]
            if not new_typhoons:
                return
            channel = self.get_channel(CHANNEL_ID)
            if not channel:
                return
            for t in new_typhoons:
                near = is_typhoon_near_japan(t["lat"], t["lon"])
                title = "🌀 台風情報"
                desc_parts = [f"🌀 台風{t['number']}号"]
                if t.get("name"):
                    desc_parts.append(f"（{t['name']}）")
                desc_parts.append(f"📍 現在位置: {t['lat']:.1f}°N, {t['lon']:.1f}°E")
                desc_parts.append(f"💨 最大風速: {t['wind_max']}m/s")
                desc_parts.append(f"🔵 中心気圧: {t['pressure']}hPa")
                desc_parts.append(f"📅 更新時刻: {t['at']}")
                if near:
                    desc_parts.append("⚠️ 接近中")
                embed = discord.Embed(
                    title=title,
                    description="\n".join(desc_parts),
                    color=0x8B0000 if near else 0x4169E1,
                )
                await channel.send(embed=embed)
                posted.add(t["number"])
                logger.info("台風情報投稿: 台風%s号", t["number"])
            state[posted_key] = list(posted)
            save_json(STATE_FILE, state)
        except Exception as e:
            logger.error("台風監視タスク失敗: %s", e)

    @typhoon_monitor.before_loop
    async def before_typhoon_monitor(self):
        await self.wait_until_ready()

    # ── 朝6:00 投稿 ──
    @tasks.loop(hours=24)
    async def morning_post(self):
        now = datetime.now(JST)
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        logger.info("朝6:00 投稿まで %.0f 秒待機", wait)
        await asyncio.sleep(wait)
        await self._post_weather("today_tomorrow", "朝6:00")

    @morning_post.before_loop
    async def before_morning(self):
        await self.wait_until_ready()

    # ── 夜21:00 投稿 ──
    @tasks.loop(hours=24)
    async def evening_post(self):
        now = datetime.now(JST)
        target = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        logger.info("夜21:00 投稿まで %.0f 秒待機", wait)
        await asyncio.sleep(wait)
        await self._post_weather("tomorrow_dayafter", "夜21:00")

    @evening_post.before_loop
    async def before_evening(self):
        await self.wait_until_ready()

    async def _post_weather(self, mode: str, label: str):
        channel = self.get_channel(CHANNEL_ID)
        if not channel:
            logger.error("チャンネル %d が見つかりません", CHANNEL_ID)
            return

        # デフォルト地域を state.json から取得
        state = load_json(STATE_FILE)
        channel_state = state.get("channels", {}).get(str(CHANNEL_ID), {})
        location_key = channel_state.get("location", "matsumoto")

        # 警報・注意報
        warnings = get_jma_warnings()
        warning_text = ""
        if warnings:
            warning_text = "**⚠️ 警報・注意報（長野県）**\n" + "\n".join(f"🔴 {w}" for w in warnings) + "\n\n"

        try:
            embed = build_embed(location_key, mode=mode)
            view = WeatherView(location_key, mode=mode)
        except Exception as e:
            logger.error("Embed 作成失敗: %s", e)
            await channel.send(f"⚠️ 天気情報の取得に失敗しました。しばらくしてからもう一度お試しください。\nエラー: {e}")
            return

        # 古いメッセージ削除
        channel_id_str = str(CHANNEL_ID)
        last_msg_id = channel_state.get("last_msg_id")
        if last_msg_id:
            try:
                old_msg = await channel.fetch_message(int(last_msg_id))
                await old_msg.delete()
                logger.info("古いメッセージ（ID: %s）を削除しました", last_msg_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                logger.warning("古いメッセージ削除失敗: %s", e)

        # 新規投稿
        try:
            msg = await channel.send(content=warning_text, embed=embed, view=view)
            # メッセージIDを保存
            state = load_json(STATE_FILE)
            if "channels" not in state:
                state["channels"] = {}
            if channel_id_str not in state["channels"]:
                state["channels"][channel_id_str] = {}
            state["channels"][channel_id_str]["last_msg_id"] = str(msg.id)
            save_json(STATE_FILE, state)
            logger.info("[%s] 天気予報を投稿しました (msg id: %s)", label, msg.id)
        except Exception as e:
            logger.error("投稿失敗: %s", e)

# ─────────────────────────────────────────────────────
# コマンド定義（モジュールレベル）
# ─────────────────────────────────────────────────────
@commands.command(name="weather", help="現在の天気を即座に表示（!weather [地域名]）")
async def weather_cmd(ctx, *, args: str = ""):
    location_key = "matsumoto"
    if args:
        args_clean = args.strip()
        matched = False
        for k, v in LOCATIONS.items():
            if args_clean in v["name"]:
                location_key = k
                matched = True
                break
        if not matched:
            custom_locs = load_json(LOCATIONS_FILE)
            for cname in custom_locs:
                if args_clean in cname:
                    location_key = f"custom:{cname}"
                    matched = True
                    break
            if not matched:
                await ctx.send(f"⚠️ 「{args_clean}」という地域が見つかりません。松本市で表示します。")

    async with ctx.typing():
        try:
            # 古い天気メッセージを削除
            state = load_json(STATE_FILE)
            cid = str(ctx.channel.id)
            last_msg_id = state.get("channels", {}).get(cid, {}).get("last_msg_id")
            if last_msg_id:
                try:
                    old_msg = await ctx.channel.fetch_message(int(last_msg_id))
                    await old_msg.delete()
                    logger.info("古い天気メッセージ（ID: %s）を削除しました", last_msg_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

            embed = build_embed(location_key, mode="today_tomorrow")
            view = WeatherView(location_key)
            msg = await ctx.send(embed=embed, view=view)
            # メッセージIDを保存
            state = load_json(STATE_FILE)
            if "channels" not in state:
                state["channels"] = {}
            if cid not in state["channels"]:
                state["channels"][cid] = {}
            state["channels"][cid]["last_msg_id"] = str(msg.id)
            save_json(STATE_FILE, state)
            # コマンドメッセージを削除
            await ctx.message.delete()
        except Exception as e:
            logger.error("!weather コマンド失敗: %s", e)
            await ctx.send(f"⚠️ 天気情報の取得に失敗しました: {e}")


@commands.command(name="add_place", help="お出かけ先を追加（!add_place 名前 緯度 経度）")
async def add_place_cmd(ctx, name: str = "", lat: str = "", lon: str = ""):
    if not name or not lat or not lon:
        await ctx.send("⚠️ 使い方: `!add_place 名前 緯度 経度`\n例: `!add_place 東京タワー 35.6586 139.7454`")
        return
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except ValueError:
        await ctx.send("⚠️ 緯度・経度は数値で指定してください。")
        return
    custom_locs = load_json(LOCATIONS_FILE)
    custom_locs[name] = {"name": name, "lat": lat_f, "lon": lon_f}
    while len(custom_locs) > 23:
        oldest_key = next(iter(custom_locs))
        del custom_locs[oldest_key]
    save_json(LOCATIONS_FILE, custom_locs)
    await ctx.send(f"📍 「{name}」を追加しました（緯度: {lat_f}, 経度: {lon_f}）\nセレクトメニューの「📍 お出かけ先」から選択できます。")
    await ctx.message.delete()


@commands.command(name="remove_place", help="お出かけ先を削除（!remove_place 名前）")
async def remove_place_cmd(ctx, *, name: str = ""):
    if not name:
        await ctx.send("⚠️ 使い方: `!remove_place 名前`\n例: `!remove_place 東京タワー`")
        return
    custom_locs = load_json(LOCATIONS_FILE)
    if name not in custom_locs:
        await ctx.send(f"⚠️ 「{name}」は登録されていません。")
        return
    del custom_locs[name]
    save_json(LOCATIONS_FILE, custom_locs)
    await ctx.send(f"📍 「{name}」を削除しました。")
    await ctx.message.delete()


# ─────────────────────────────────────────────────────
# タイムカード（GUI — ボタン＋モーダル）
# ─────────────────────────────────────────────────────
TIMEcard_FILE = DATA_DIR / "timecard.json"


def load_timecard() -> dict:
    if not TIMEcard_FILE.exists():
        TIMEcard_FILE.write_text("{}", encoding="utf-8")
    try:
        return json.loads(TIMEcard_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_timecard(data: dict) -> None:
    TIMEcard_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_time(s: str) -> str | None:
    m = re.match(r"^(\d{1,2}):(\d{2})$", s.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return f"{h:02d}:{mi:02d}"
    return None


def calc_work_hours(in_t: str, out_t: str) -> str:
    try:
        ih, im = map(int, in_t.split(":"))
        oh, om = map(int, out_t.split(":"))
        diff = (oh * 60 + om) - (ih * 60 + im)
        if diff < 0:
            diff += 24 * 60
        return f"{diff // 60}時間{diff % 60}分"
    except Exception:
        return "-"


class TimeInputModal(discord.ui.Modal):
    """時刻入力モーダル。OKボタンで直接打刻する。"""
    def __init__(self, mode: str, embed_builder):
        super().__init__(title=f"{'出勤' if mode == 'in' else '退勤'}時刻入力", timeout=60)
        self.mode = mode
        self.embed_builder = embed_builder
        self.time_input = discord.ui.TextInput(
            label="時刻 (HH:MM)",
            placeholder="例: 09:30",
            max_length=5,
            required=False,
        )
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.time_input.value.strip()
        now = datetime.now(JST)
        today = now.strftime("%Y-%m-%d")

        if raw:
            t = parse_time(raw)
            if not t:
                await interaction.response.send_message("⚠️ 正しい時刻を HH:MM 形式で入力してください。", ephemeral=True)
                return
        else:
            t = now.strftime("%H:%M")

        tc = load_timecard()
        if today not in tc:
            tc[today] = {}
        tc[today][self.mode] = t
        save_timecard(tc)

        kind = "出勤" if self.mode == "in" else "退勤"
        await interaction.response.edit_message(
            embed=self.embed_builder(today),
            view=TimecardView(self.embed_builder),
        )


def build_timecard_embed(today: str) -> discord.Embed:
    """タイムカードパネル Embed（今日の状況＋ボタン説明）"""
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
    def __init__(self, embed_builder=None):
        super().__init__(timeout=300)
        self.embed_builder = embed_builder or build_timecard_embed

    @discord.ui.button(label="🟢 出勤", style=discord.ButtonStyle.green, custom_id="tc_in", row=0)
    async def in_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimeInputModal("in", self.embed_builder))

    @discord.ui.button(label="🔴 退勤", style=discord.ButtonStyle.red, custom_id="tc_out", row=0)
    async def out_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimeInputModal("out", self.embed_builder))

    @discord.ui.button(label="📅 月間一覧", style=discord.ButtonStyle.blurple, custom_id="tc_month", row=1)
    async def month_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        now = datetime.now(JST)
        tc = load_timecard()
        entries = []
        for date_str, rec in sorted(tc.items()):
            try:
                dd = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue
            if dd.year == now.year and dd.month == now.month:
                it = rec.get("in", "-")
                ot = rec.get("out", "-")
                wk = calc_work_hours(it, ot) if it != "-" and ot != "-" else "-"
                entries.append((dd, it, ot, wk))
        if not entries:
            await interaction.followup.send(f"📅 {now.year}年{now.month}月の打刻データはありません。", ephemeral=True)
            return
        lines = ["```"]
        lines.append(f" {'日付':<11} {'出勤':<7} {'退勤':<7} {'勤務時間'}")
        lines.append(" " + "-" * 38)
        total_min = 0
        for dd, it, ot, wk in entries[:30]:
            day_label = f"{dd.month}/{dd.day}({WEEKDAYS[dd.weekday()]})"
            lines.append(f" {day_label:<11} {it:<7} {ot:<7} {wk}")
            if wk != "-":
                try:
                    h, m = wk.replace("時間", "h").replace("分", "m").replace("h", ":").replace("m", "").split(":")
                    total_min += int(h) * 60 + int(m)
                except Exception:
                    pass
        lines.append(" " + "-" * 38)
        lines.append(f" 合計勤務時間: {total_min // 60}時間{total_min % 60}分")
        lines.append("```")
        m_embed = discord.Embed(
            title=f"📅 {now.year}年{now.month}月 タイムカード",
            description="\n".join(lines),
            color=0x4A90D9,
        )
        await interaction.edit_original_response(embed=m_embed, view=TimecardView(self.embed_builder))

    @discord.ui.button(label="🗑️ 削除", style=discord.ButtonStyle.gray, custom_id="tc_del", row=1)
    async def del_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        today = datetime.now(JST).strftime("%Y-%m-%d")
        tc = load_timecard()
        if today in tc:
            del tc[today]
            save_timecard(tc)
            e = self.embed_builder(today)
        else:
            e = self.embed_builder(today)
        await interaction.response.edit_message(embed=e, view=TimecardView(self.embed_builder))

    @discord.ui.button(label="✏️ やめる", style=discord.ButtonStyle.gray, custom_id="tc_quit", row=2)
    async def quit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.message.delete()


@commands.command(name="tc", help="タイムカードパネルを表示（ボタンで操作）")
async def tc_cmd(ctx):
    today = datetime.now(JST).strftime("%Y-%m-%d")
    embed = build_timecard_embed(today)
    view = TimecardView(build_timecard_embed)
    await ctx.send(embed=embed, view=view)
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

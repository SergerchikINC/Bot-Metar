from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import telebot
import requests
import csv
from telebot.types import Message, CallbackQuery
import pymongo
import os
from flask import Flask, request, abort
from groq import Groq
import json
import logging
import datetime

logging.basicConfig(
    filename='groq_logs.txt',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)

BOT_TOKEN = os.environ.get('BOT_TOKEN')
MONGODB_URI = os.environ.get('MONGODB_URI')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')

if not BOT_TOKEN or not MONGODB_URI or not GROQ_API_KEY:
    raise ValueError("Не все переменные окружения установлены! BOT_TOKEN, MONGODB_URI, GROQ_API_KEY")

client = pymongo.MongoClient(MONGODB_URI)
db = client['bot_db']
users_collection = db['users']

AIRPORTS_CSV = 'airports.csv'
AIRPORTS_LIST = []


def load_airports():
    global AIRPORTS_LIST
    try:
        with open(AIRPORTS_CSV, encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                icao = row['ident'].strip().upper()
                airport_type = row['type']
                if icao and len(icao) == 4 and airport_type in ['large_airport', 'medium_airport']:
                    AIRPORTS_LIST.append(icao)
        print(f"Загружено {len(AIRPORTS_LIST)} аэропортов")
    except FileNotFoundError:
        print("Файл airports.csv не найден! Резервный список.")
        AIRPORTS_LIST = ['UUEE', 'ULLI', 'UNNT', 'UOOO', 'URSS']


load_airports()

bot = telebot.TeleBot(BOT_TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY)

user_pages = {}
last_data = {}


def parse_user_request(text: str):
    prompt = f"""Ты эксперт по авиации, знаешь все коды аэропортов мира.
Из сообщения пользователя извлеки:
- ICAO-код аэропорта (ровно 4 заглавные буквы, например UUEE, ULLI, KJFK).
  Если написано название города, аэропорта или IATA (3 буквы) — обязательно конвертируй в ICAO.
  Примеры:
  - Пулково, Санкт-Петербург, LED → ULLI
  - Шереметьево, SVO → UUEE
  - Домодедово, DME → UUDD
  - JFK, Нью-Йорк → KJFK
  - Хитроу, Лондон → EGLL
  - Сочи → URSS
- Что именно нужно: METAR, TAF или BOTH.
  Если просто "погода", "какая погода", "что в аэропорту", "погода в ..." — BOTH.
  Если явно "METAR", "метар" — METAR.
  Если "TAF", "прогноз" — TAF.

Ответь **строго только JSON**, без лишних слов и переносов строк:
{{"icao": "ULLI", "type": "METAR"|"TAF"|"BOTH"}}

Если не можешь определить — верни "icao": "UNKNOWN"

Сообщение пользователя: {text}"""

    logging.info(f"Запрос к Groq: {text}")

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=150
        )
        answer = response.choices[0].message.content.strip()
        logging.info(f"Ответ от Groq: {answer}")

        result = json.loads(answer)
        return result
    except Exception as e:
        logging.error(f"Ошибка парсинга Groq: {str(e)}")
        return None


def recognize_voice(file_id):
    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)

        with open("voice.ogg", "wb") as f:
            f.write(downloaded)

        with open("voice.ogg", "rb") as audio:
            transcription = groq_client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=audio,
                response_format="text",
                language="ru",
                temperature=0.0,
                prompt="Погода, METAR, TAF, аэропорты: Пулково, Шереметьево, Домодедово, Внуково, LED, ULLI, UUEE, UUDD, UUWW, Санкт-Петербург, Москва"
            )

        os.remove("voice.ogg")
        return transcription.strip()
    except Exception as e:
        logging.error(f"Ошибка распознавания голоса: {e}")
        return None


@bot.message_handler(content_types=['text', 'voice'])
def handle_natural_language(message: Message):
    if message.text and message.text.startswith('/'):
        return

    if message.voice:
        text = recognize_voice(message.voice.file_id)
        if not text:
            bot.reply_to(message, "Не удалось распознать голосовое сообщение.")
            return
        bot.reply_to(message, f"🎤 Распознаю голосовое, ожидайте ответ")
    else:
        text = message.text.strip()

    if not text:
        return

    result = parse_user_request(text)
    if not result or result.get("icao") == "UNKNOWN":
        bot.reply_to(message, "Не понял запрос. Примеры:\n• Погода в Пулково\n• METAR LED\n• Что в Шереметьево")
        return

    icao = result["icao"].upper()
    req_type = result.get("type", "BOTH").upper()

    metar, taf = get_metar_taf(icao)

    if req_type == "METAR":
        txt = f"<b>{icao}</b>\nMETAR: {metar}\nРасшифровка:\n{decode_metar(metar)}"
        markup = get_normal_refresh_markup(icao)
    elif req_type == "TAF":
        txt = f"<b>{icao}</b>\n{get_taf_text(taf)}"
        markup = get_normal_refresh_markup(icao)
    else:
        txt = f"<b>{icao}</b>\nMETAR: {metar}\nРасшифровка:\n{decode_metar(metar)}\n\n{get_taf_text(taf)}"
        markup = get_normal_refresh_markup(icao)

    sent = bot.reply_to(message, txt, parse_mode='HTML', reply_markup=markup)
    last_data[sent.message_id] = metar + taf


def get_metar_taf(icao: str):
    try:
        headers = {'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
        metar_url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json"
        metar_resp = requests.get(metar_url, timeout=15, headers=headers)
        metar_data = metar_resp.json()
        metar = metar_data[0].get('rawOb', 'METAR не найден') if metar_data and isinstance(metar_data, list) and len(
            metar_data) > 0 else "METAR не найден"

        taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao}&format=json"
        taf_resp = requests.get(taf_url, timeout=15, headers=headers)
        taf_data = taf_resp.json()
        taf = taf_data[0].get('rawTAF', 'TAF не найден') if taf_data and isinstance(taf_data, list) and len(
            taf_data) > 0 else "TAF не найден"

        logging.info(f"Получены данные для {icao}: METAR={metar[:50]}..., TAF={taf[:50]}...")
        return metar, taf
    except Exception as e:
        logging.error(f"Ошибка получения данных для {icao}: {str(e)}")
        return f"Ошибка получения данных: {str(e)}", "TAF не найден"


def decode_metar(metar: str):
    if not metar or "не найден" in metar or "Ошибка" in metar:
        return "Расшифровка недоступна"

    parts = metar.split()
    decoded = []
    i = 0

    if i < len(parts) and parts[i] in ['METAR', 'SPECI', 'COR']:
        i += 1

    if i >= len(parts):
        return "Расшифровка недоступна"
    decoded.append(f"Аэропорт: {parts[i]}")
    i += 1

    if i < len(parts) and len(parts[i]) == 7 and parts[i].endswith('Z'):
        day = parts[i][:2]
        time = parts[i][2:6]
        decoded.append(f"Время наблюдения: {day}-е число, {time[:2]}:{time[2:]} UTC")
        i += 1

    if i < len(parts) and (parts[i].endswith('KT') or parts[i].endswith('MPS') or 'G' in parts[i]):
        wind = parts[i]
        if wind.startswith('VRB'):
            speed = wind[3:wind.find('KT' if 'KT' in wind else 'MPS')]
            unit = 'узлов' if 'KT' in wind else 'м/с'
            decoded.append(f"Ветер переменного направления {speed} {unit}")
        else:
            direction = wind[:3]
            if 'G' in wind:
                speed = wind[3:wind.index('G')]
                gust = wind[wind.index('G') + 1:wind.find('KT' if 'KT' in wind else 'MPS')]
            else:
                speed = wind[3:wind.find('KT' if 'KT' in wind else 'MPS')]
                gust = ''
            unit = 'узлов' if 'KT' in wind else 'м/с'
            wind_str = f"Ветер: {direction}° {speed} {unit}"
            if gust:
                wind_str += f", порывы {gust} {unit}"
            decoded.append(wind_str)
        i += 1

    if i < len(parts) and (parts[i].isdigit() or parts[i] == 'CAVOK'):
        if parts[i] == 'CAVOK':
            decoded.append("Видимость: CAVOK (≥10 км, без значимой облачности)")
        else:
            decoded.append(f"Видимость: {parts[i]} метров")
        i += 1

    if i < len(parts):
        weather_code = parts[i]
        intensity = ''
        if weather_code.startswith('-'):
            intensity = 'слабый '
            weather_code = weather_code[1:]
        elif weather_code.startswith('+'):
            intensity = 'сильный '
            weather_code = weather_code[1:]

        weather_dict = {
            'RA': 'дождь', 'SN': 'снег', 'DZ': 'морось', 'GR': 'град', 'GS': 'мелкий град/снежные зерна',
            'PL': 'ледяные гранулы', 'BR': 'дымка', 'FG': 'туман', 'HZ': 'дымка', 'FU': 'дым',
            'SH': 'ливневый', 'TS': 'гроза', 'FZ': 'переохлаждённый'
        }

        desc_parts = []
        j = 0
        while j < len(weather_code):
            found = False
            for length in [2, 3]:
                if j + length <= len(weather_code):
                    code = weather_code[j:j + length]
                    if code in weather_dict:
                        desc_parts.append(weather_dict[code])
                        j += length
                        found = True
                        break
            if not found:
                j += 1

        if desc_parts:
            decoded.append(f"Погода: {intensity}{' '.join(desc_parts)}")
        i += 1

    cloud_dict = {'FEW': 'мало', 'SCT': 'рассеянная', 'BKN': 'значительная', 'OVC': 'сплошная'}
    while i < len(parts) and len(parts[i]) == 6 and parts[i][:3] in cloud_dict:
        level = cloud_dict[parts[i][:3]]
        height = int(parts[i][3:6]) * 100
        decoded.append(f"Облачность: {level} на {height} футов")
        i += 1

    if i < len(parts) and '/' in parts[i]:
        temp_dew = parts[i].split('/')
        temp = temp_dew[0].replace('M', '-')
        dew = temp_dew[1].replace('M', '-')
        decoded.append(f"Температура: {temp}°C, точка росы: {dew}°C")
        i += 1

    qnh_found = False
    for j in range(i, len(parts)):
        if parts[j].startswith('Q'):
            decoded.append(f"Давление QNH: {parts[j][1:]} гПа")
            qnh_found = True
            break
        elif parts[j].startswith('A'):
            inhg = parts[j][1:3] + '.' + parts[j][3:]
            decoded.append(f"Давление: {inhg} дюймов рт.ст.")
            qnh_found = True
            break
    if not qnh_found:
        decoded.append("Давление QNH: не указано")

    return "\n".join(decoded)


def decode_taf(taf: str):
    if not taf or "не найден" in taf or "Ошибка" in taf:
        return "Расшифровка TAF недоступна"

    parts = taf.split()
    decoded = []
    i = 0

    if i < len(parts) and parts[i] in ['TAF', 'AMD', 'COR']:
        i += 1

    if i >= len(parts):
        return "Расшифровка TAF недоступна"
    decoded.append(f"Аэропорт: {parts[i]}")
    i += 1

    if i < len(parts) and len(parts[i]) == 7 and parts[i].endswith('Z'):
        issue_day = parts[i][:2]
        issue_time = parts[i][2:4] + ':' + parts[i][4:6]
        decoded.append(f"Время выдачи: {issue_day}-е число, {issue_time} UTC")
        i += 1


    if i < len(parts) and len(parts[i]) == 5 and parts[i].isdigit():
        start_day = parts[i][:2]
        start_time = parts[i][2:4] + ':' + parts[i][4:5] + '0'
        end_day = parts[i][5:7]
        end_time = parts[i][7:9] + ':' + parts[i][9:10] + '0'
        decoded.append(f"Период действия: с {start_day}-е {start_time} UTC по {end_day}-е {end_time} UTC")
        i += 1

    while i < len(parts) and not parts[i].startswith('TX') and not parts[i].startswith('TN') and not parts[
        i].startswith('TEMPO') and not parts[i].startswith('BECMG') and not parts[i].startswith('FM'):
        if parts[i].endswith('KT') or parts[i].endswith('MPS') or 'G' in parts[i]:
            wind = parts[i]
            direction = wind[:3]
            if 'G' in wind:
                speed = wind[3:wind.index('G')]
                gust = wind[wind.index('G') + 1:wind.index('KT' if 'KT' in wind else 'MPS')]
            else:
                speed = wind[3:wind.index('KT' if 'KT' in wind else 'MPS')]
                gust = ''
            unit = 'узлов' if 'KT' in wind else 'м/с'
            wind_str = f"Ветер: {direction}° {speed} {unit}"
            if gust:
                wind_str += f", порывы {gust} {unit}"
            decoded.append(wind_str)
            i += 1

        elif parts[i].isdigit() or parts[i] == 'CAVOK':
            if parts[i] == 'CAVOK':
                decoded.append("Видимость: CAVOK (≥10 км, без значимой погоды и облачности)")
            else:
                decoded.append(f"Видимость: {parts[i]} метров")
            i += 1

        elif parts[i].startswith('-') or parts[i].startswith('+') or parts[i] in ['RA', 'SN', 'DZ', 'BR', 'FG', 'SHRA',
                                                                                  'TSRA', 'SH', 'TS', 'SHSN']:
            weather = parts[i]
            intensity = ''
            if weather.startswith('-'):
                intensity = 'слабый '
                weather = weather[1:]
            elif weather.startswith('+'):
                intensity = 'сильный '
                weather = weather[1:]

            weather_dict = {
                'RA': 'дождь', 'SN': 'снег', 'DZ': 'морось', 'SH': 'ливневый', 'TS': 'гроза',
                'BR': 'дымка', 'FG': 'туман', 'SHSN': 'ливневый снег'
            }
            desc = weather_dict.get(weather, weather)
            decoded.append(f"Погода: {intensity}{desc}")
            i += 1

        elif len(parts[i]) == 6 and parts[i][:3] in ['FEW', 'SCT', 'BKN', 'OVC']:
            level_dict = {'FEW': 'мало', 'SCT': 'рассеянная', 'BKN': 'значительная', 'OVC': 'сплошная'}
            level = level_dict[parts[i][:3]]
            height = int(parts[i][3:]) * 100
            decoded.append(f"Облачность: {level} на {height} футов")
            i += 1

        else:
            i += 1

    while i < len(parts) and (parts[i].startswith('TX') or parts[i].startswith('TN')):
        if parts[i].startswith('TX'):
            temp = parts[i][2:parts[i].index('/')]
            time_str = parts[i].split('/')[1][:-1]
            decoded.append(f"Макс. температура: {temp}°C в {time_str}")
            i += 1
        elif parts[i].startswith('TN'):
            temp = parts[i][2:parts[i].index('/')]
            time_str = parts[i].split('/')[1][:-1]
            decoded.append(f"Мин. температура: {temp}°C в {time_str}")
            i += 1

    while i < len(parts):
        if parts[i].startswith('TEMPO'):
            decoded.append("Временно (TEMPO):")
            i += 1
        elif parts[i].startswith('BECMG'):
            decoded.append("Постепенно изменяется (BECMG):")
            i += 1
        elif parts[i].startswith('FM'):
            fm_time = parts[i][2:4] + ':' + parts[i][4:6]
            decoded.append(f"С {fm_time} UTC:")
            i += 1

        while i < len(parts) and not parts[i].startswith('TEMPO') and not parts[i].startswith('BECMG') and not parts[
            i].startswith('FM'):
            if parts[i].endswith('KT') or parts[i].endswith('MPS') or 'G' in parts[i]:
                wind = parts[i]
                direction = wind[:3]
                if 'G' in wind:
                    speed = wind[3:wind.index('G')]
                    gust = wind[wind.index('G') + 1:wind.index('KT' if 'KT' in wind else 'MPS')]
                else:
                    speed = wind[3:wind.index('KT' if 'KT' in wind else 'MPS')]
                    gust = ''
                unit = 'узлов' if 'KT' in wind else 'м/с'
                wind_str = f"Ветер: {direction}° {speed} {unit}"
                if gust:
                    wind_str += f", порывы {gust} {unit}"
                decoded.append(wind_str)
                i += 1

            elif parts[i].isdigit() or parts[i] == 'CAVOK':
                if parts[i] == 'CAVOK':
                    decoded.append("Видимость: CAVOK (≥10 км, без значимой погоды и облачности)")
                else:
                    decoded.append(f"Видимость: {parts[i]} метров")
                i += 1

            elif parts[i].startswith('-') or parts[i].startswith('+') or parts[i] in ['RA', 'SN', 'DZ', 'BR', 'FG',
                                                                                      'SHRA', 'TSRA', 'SH', 'TS',
                                                                                      'SHSN']:
                weather = parts[i]
                intensity = ''
                if weather.startswith('-'):
                    intensity = 'слабый '
                    weather = weather[1:]
                elif weather.startswith('+'):
                    intensity = 'сильный '
                    weather = weather[1:]

                weather_dict = {
                    'RA': 'дождь', 'SN': 'снег', 'DZ': 'морось', 'SH': 'ливневый', 'TS': 'гроза',
                    'BR': 'дымка', 'FG': 'туман', 'SHSN': 'ливневый снег'
                }
                desc = weather_dict.get(weather, weather)
                decoded.append(f"Погода: {intensity}{desc}")
                i += 1

            elif len(parts[i]) == 6 and parts[i][:3] in ['FEW', 'SCT', 'BKN', 'OVC']:
                level_dict = {'FEW': 'мало', 'SCT': 'рассеянная', 'BKN': 'значительная', 'OVC': 'сплошная'}
                level = level_dict[parts[i][:3]]
                height = int(parts[i][3:]) * 100
                decoded.append(f"Облачность: {level} на {height} футов")
                i += 1

            elif parts[i] == 'NSW':
                decoded.append("Погода: NSW (без значимых явлений)")
                i += 1

            if parts[i - 1].endswith('CB'):
                decoded[-1] += " CB (кучево-дождевые)"

            else:
                i += 1

    return "\n".join(decoded)


def get_taf_text(taf: str):
    if "не найден" in taf or "Ошибка" in taf:
        return "TAF не найден"

    decoded = decode_taf(taf)
    return f"TAF (сырой): {taf}\n\nРасшифровка TAF:\n{decoded}"


def get_vatsim_airports(cid: str):
    try:
        url = "https://data.vatsim.net/v3/vatsim-data.json"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for pilot in data.get("pilots", []):
            if str(pilot.get("cid")) == str(cid):
                fp = pilot.get("flight_plan", {})
                if not fp:
                    return []
                dep = fp.get("departure", "").strip().upper()
                arr = fp.get("arrival", "").strip().upper()
                airports = []
                if len(dep) == 4:
                    airports.append(dep)
                if len(arr) == 4:
                    airports.append(arr)
                return airports
        return []
    except Exception:
        return []


def get_normal_refresh_markup(icao: str):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh_normal_{icao}"))
    return markup


def get_flight_markup(icao: str):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh_flight_{icao}"))
    markup.row(InlineKeyboardButton("🔙 Назад к аэропортам плана", callback_data="back_to_flight"))
    return markup


def show_weather_page(msg_or_call, user_id, edit=False):
    page = user_pages.get(user_id, 0)
    per_page = 10
    user_doc = users_collection.find_one({"user_id": user_id})
    cid = user_doc.get("cid") if user_doc else None
    vatsim_aps = get_vatsim_airports(cid) if cid else []
    prioritized = vatsim_aps + [a for a in AIRPORTS_LIST if a not in vatsim_aps]
    total = len(prioritized)
    start = page * per_page
    end = start + per_page
    page_aps = prioritized[start:end]

    text = f"METAR/TAF (стр. {page + 1} из {(total - 1) // per_page + 1})\n\n"
    for icao in page_aps:
        metar, taf = get_metar_taf(icao)
        text += (f"<b>{icao}</b>\n"
                 f"METAR: {metar}\n"
                 f"Расшифровка:\n{decode_metar(metar)}\n"
                 f"{get_taf_text(taf)}\n\n")

    markup = InlineKeyboardMarkup()
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀ Назад", callback_data=f"page_{page - 1}"))
    if end < total:
        row.append(InlineKeyboardButton("Далее ▶", callback_data=f"page_{page + 1}"))
    if row:
        markup.row(*row)

    if edit:
        bot.edit_message_text(chat_id=msg_or_call.message.chat.id,
                              message_id=msg_or_call.message.message_id,
                              text=text, parse_mode='HTML', reply_markup=markup)
    else:
        bot.send_message(msg_or_call.chat.id, text, parse_mode='HTML', reply_markup=markup)


@bot.message_handler(commands=['start'])
def start(message: Message):
    bot.reply_to(message, "Привет! Я бот METAR/TAF.\n"
                          "Пиши просто текстом или отправляй голосовое:\n"
                          "• Погода в Пулково\n"
                          "• METAR LED\n"
                          "• Что в Шереметьево\n\n"
                          "Команды:\n"
                          "/cid <CID> — привязка VATSIM\n"
                          "/flight — аэропорты из плана\n"
                          "/metar <ICAO> — конкретный аэропорт\n"
                          "/weather [ICAO] — список или конкретный")


@bot.message_handler(commands=['cid'])
def set_cid(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Использование: /cid 123456")
        return
    cid = parts[1].strip()
    user_id = message.from_user.id
    users_collection.update_one({"user_id": user_id}, {"$set": {"cid": cid}}, upsert=True)
    bot.reply_to(message, f"CID сохранён: {cid}")


@bot.message_handler(commands=['metar'])
def metar_handler(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Использование: /metar UUEE")
        return
    icao = parts[1].upper()
    if len(icao) != 4:
        bot.reply_to(message, "ICAO должен быть 4 буквы")
        return
    metar, taf = get_metar_taf(icao)
    txt = f"<b>{icao}</b>\nMETAR: {metar}\nРасшифровка:\n{decode_metar(metar)}\n\n{get_taf_text(taf)}"
    markup = get_normal_refresh_markup(icao)
    sent = bot.reply_to(message, txt, parse_mode='HTML', reply_markup=markup)
    last_data[sent.message_id] = metar + taf


@bot.message_handler(commands=['weather'])
def weather_handler(message: Message):
    parts = message.text.split()
    user_id = message.from_user.id
    if len(parts) >= 2:
        icao = parts[1].upper()
        if len(icao) != 4:
            bot.reply_to(message, "ICAO должен быть 4 символа")
            return
        metar, taf = get_metar_taf(icao)
        txt = f"<b>{icao}</b>\nMETAR: {metar}\nРасшифровка:\n{decode_metar(metar)}\n\n{get_taf_text(taf)}"
        markup = get_normal_refresh_markup(icao)
        sent = bot.reply_to(message, txt, parse_mode='HTML', reply_markup=markup)
        last_data[sent.message_id] = metar + taf
    else:
        user_pages[user_id] = 0
        show_weather_page(message, user_id)


@bot.message_handler(commands=['flight'])
def flight_handler(message: Message):
    user_id = message.from_user.id
    user_doc = users_collection.find_one({"user_id": user_id})
    cid = user_doc.get("cid") if user_doc else None
    if not cid:
        bot.reply_to(message, "Сначала выполните /cid <ваш_CID>")
        return
    airports = get_vatsim_airports(cid)
    if not airports:
        bot.reply_to(message, "План полёта не найден или вы не онлайн на VATSIM")
        return

    markup = InlineKeyboardMarkup()
    row = []
    for ap in airports[:2]:
        row.append(InlineKeyboardButton(ap, callback_data=f"apt_{ap}"))
    markup.row(*row)

    bot.reply_to(message, f"Аэропорты из вашего плана VATSIM (CID {cid}):", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('apt_'))
def apt_handler(call: CallbackQuery):
    icao = call.data[len('apt_'):].upper()
    metar, taf = get_metar_taf(icao)
    text = f"<b>{icao}</b>\nMETAR: {metar}\nРасшифровка:\n{decode_metar(metar)}\n\n{get_taf_text(taf)}"
    markup = get_flight_markup(icao)
    bot.edit_message_text(chat_id=call.message.chat.id,
                          message_id=call.message.message_id,
                          text=text, parse_mode='HTML', reply_markup=markup)
    last_data[call.message.message_id] = metar + taf


@bot.callback_query_handler(func=lambda call: call.data.startswith('page_'))
def page_handler(call: CallbackQuery):
    user_id = call.from_user.id
    new_page = int(call.data.split('_')[1])
    user_pages[user_id] = new_page
    show_weather_page(call, user_id, edit=True)


@bot.callback_query_handler(func=lambda call: call.data.startswith('refresh_'))
def refresh_handler(call: CallbackQuery):
    prefix = 'refresh_normal_' if call.data.startswith('refresh_normal_') else 'refresh_flight_'
    icao = call.data[len(prefix):].upper()
    from_flight = prefix == 'refresh_flight_'

    metar, taf = get_metar_taf(icao)
    new_data = metar + taf

    message_id = call.message.message_id
    previous_data = last_data.get(message_id)

    if previous_data == new_data:
        bot.answer_callback_query(call.id, "Данные и так актуальны", show_alert=False)
        return

    text = f"<b>{icao}</b>\nMETAR: {metar}\nРасшифровка:\n{decode_metar(metar)}\n\n{get_taf_text(taf)}"
    markup = get_flight_markup(icao) if from_flight else get_normal_refresh_markup(icao)

    bot.edit_message_text(chat_id=call.message.chat.id,
                          message_id=call.message.message_id,
                          text=text, parse_mode='HTML', reply_markup=markup)

    last_data[message_id] = new_data
    bot.answer_callback_query(call.id, "Данные обновлены!")


@bot.callback_query_handler(func=lambda call: call.data == "back_to_flight")
def back_to_flight_handler(call: CallbackQuery):
    user_id = call.from_user.id
    user_doc = users_collection.find_one({"user_id": user_id})
    cid = user_doc.get("cid") if user_doc else None
    if not cid:
        bot.answer_callback_query(call.id, "CID не установлен")
        return
    airports = get_vatsim_airports(cid)
    if not airports:
        bot.answer_callback_query(call.id, "План полёта не найден")
        return

    text = f"Аэропорты из вашего плана VATSIM (CID {cid}):"
    markup = InlineKeyboardMarkup()
    row = []
    for ap in airports[:2]:
        row.append(InlineKeyboardButton(ap, callback_data=f"apt_{ap}"))
    markup.row(*row)

    bot.edit_message_text(chat_id=call.message.chat.id,
                          message_id=call.message.message_id,
                          text=text, parse_mode='HTML', reply_markup=markup)


app = Flask(__name__)


@app.route('/' + BOT_TOKEN, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(json_string)
        if update:
            bot.process_new_updates([update])
        return '', 200
    abort(403)


@app.route('/')
def index():
    return 'Bot is running!'


if __name__ == '__main__':
    try:
        bot.delete_webhook(drop_pending_updates=True)
        logging.info("Старый webhook удалён")
    except Exception as e:
        logging.warning(f"Не удалось удалить webhook: {e}")

    if os.environ.get('RENDER') is None:
        logging.info("Запуск в режиме polling")
        bot.infinity_polling(none_stop=True, timeout=30)
    else:
        url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}"
        webhook_url = f"{url}/{BOT_TOKEN}"
        logging.info(f"Установка webhook на {webhook_url}")

        try:
            bot.set_webhook(url=webhook_url)
            logging.info("Webhook успешно установлен")
        except Exception as e:
            logging.error(f"Ошибка установки webhook: {e}")

        port = int(os.environ.get('PORT', 5000))
        app.run(host='0.0.0.0', port=port, debug=False)
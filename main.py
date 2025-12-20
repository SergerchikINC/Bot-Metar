# Полный исправленный код бота
# Исправлена проблема: после нажатия "Обновить" пропадала кнопка "Назад" и иногда "Обновить"
# Теперь при просмотре аэропорта из /flight всегда две кнопки: "Обновить" и "Назад к аэропортам плана"
# При нажатии "Обновить" обе кнопки сохраняются

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import telebot
import requests
import csv
from telebot.types import Message, CallbackQuery
import pymongo
import os
from flask import Flask, request, abort

# Токен и MongoDB (для Render из env)
BOT_TOKEN = '7947527517:AAFfeVZot_s9Zdd-H8QDuVFVGnE6itM4Rlw'
MONGODB_URI = 'mongodb+srv://Ger1k:Sergerchik_Men847@cluster0.4u1ctex.mongodb.net/?appName=Cluster0'

client = pymongo.MongoClient(MONGODB_URI)
db = client['bot_db']
users_collection = db['users']

# Загрузка аэропортов
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
user_pages = {}


# Получение METAR/TAF
def get_metar_taf(icao: str):
    try:
        metar_url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json"
        metar_resp = requests.get(metar_url, timeout=15)
        metar_raw = metar_resp.json()[0].get('rawOb',
                                             'METAR не найден') if metar_resp.status_code == 200 and metar_resp.json() else "METAR не найден"

        taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao}&format=json"
        taf_resp = requests.get(taf_url, timeout=15)
        taf_raw = taf_resp.json()[0].get('rawTAF',
                                         'TAF не найден') if taf_resp.status_code == 200 and taf_resp.json() else "TAF не найден"

        return metar_raw, taf_raw
    except Exception as e:
        return f"Ошибка: {str(e)}", ""


# Расшифровка METAR (полный код без изменений)
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


# Клавиатура для одного аэропорта (только "Обновить")
def get_refresh_markup(icao: str):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh_{icao}"))
    return markup


# Клавиатура для аэропорта из /flight ( "Обновить" + "Назад")
def get_flight_airport_markup(icao: str):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh_{icao}"))
    markup.row(InlineKeyboardButton("🔙 Назад к аэропортам плана", callback_data="back_to_flight"))
    return markup


# Пагинация списка аэропортов
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
                 f"TAF: {taf}\n\n")

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


# Обработчики команд
@bot.message_handler(commands=['start'])
def start(message: Message):
    bot.reply_to(message, "Привет! Бот METAR/TAF с расшифровкой.\n"
                          "/cid <CID> — привязка VATSIM\n"
                          "/weather [ICAO] — список или конкретный\n"
                          "/metar <ICAO> — конкретный аэропорт\n"
                          "/flight — аэропорты из плана VATSIM")


@bot.message_handler(commands=['cid'])
def set_cid(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Использование: /cid 123456")
        return
    cid = parts[1].strip()
    user_id = message.from_user.id
    users_collection.update_one({"user_id": user_id}, {"$set": {"cid": cid}}, upsert=True)
    bot.reply_to(message, f"CID сохранён в базе: {cid}")


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
        response = (f"<b>{icao}</b>\n"
                    f"METAR: {metar}\n"
                    f"Расшифровка METAR:\n{decode_metar(metar)}\n\n"
                    f"TAF: {taf}")
        markup = get_refresh_markup(icao)
        bot.reply_to(message, response, parse_mode='HTML', reply_markup=markup)
    else:
        user_pages[user_id] = 0
        show_weather_page(message, user_id)


@bot.message_handler(commands=['metar'])
def metar_handler(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Использование: /metar UUEE")
        return
    icao = parts[1].upper()
    if len(icao) != 4:
        bot.reply_to(message, "ICAO — 4 буквы")
        return
    metar, taf = get_metar_taf(icao)
    response = (f"<b>{icao}</b>\n"
                f"METAR: {metar}\n"
                f"Расшифровка METAR:\n{decode_metar(metar)}\n\n"
                f"TAF: {taf}")
    markup = get_refresh_markup(icao)
    bot.reply_to(message, response, parse_mode='HTML', reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('page_'))
def page_handler(call: CallbackQuery):
    user_id = call.from_user.id
    new_page = int(call.data.split('_')[1])
    user_pages[user_id] = new_page
    show_weather_page(call, user_id, edit=True)


# Обработчик "Обновить" для обычных запросов (/metar, /weather ICAO)
@bot.callback_query_handler(func=lambda call: call.data.startswith('refresh_') and 'apt_' not in call.message.text)
def refresh_normal_handler(call: CallbackQuery):
    icao = call.data.split('_')[1].upper()
    metar, taf = get_metar_taf(icao)
    text = (f"<b>{icao}</b>\n"
            f"METAR: {metar}\n"
            f"Расшифровка METAR:\n{decode_metar(metar)}\n\n"
            f"TAF: {taf}")
    markup = get_refresh_markup(icao)
    bot.edit_message_text(chat_id=call.message.chat.id,
                          message_id=call.message.message_id,
                          text=text, parse_mode='HTML', reply_markup=markup)
    bot.answer_callback_query(call.id, "Данные обновлены")


# Обработчик "Обновить" для аэропорта из /flight (сохраняет кнопку "Назад")
@bot.callback_query_handler(func=lambda call: call.data.startswith('refresh_'))
def refresh_flight_handler(call: CallbackQuery):
    icao = call.data.split('_')[1].upper()
    metar, taf = get_metar_taf(icao)
    text = (f"<b>{icao}</b>\n"
            f"METAR: {metar}\n"
            f"Расшифровка METAR:\n{decode_metar(metar)}\n\n"
            f"TAF: {taf}")
    markup = get_flight_airport_markup(icao)  # Обе кнопки
    bot.edit_message_text(chat_id=call.message.chat.id,
                          message_id=call.message.message_id,
                          text=text, parse_mode='HTML', reply_markup=markup)
    bot.answer_callback_query(call.id, "Данные обновлены")


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
    icao = call.data.split('_')[1]
    metar, taf = get_metar_taf(icao)
    text = (f"<b>{icao}</b>\n"
            f"METAR: {metar}\n"
            f"Расшифровка METAR:\n{decode_metar(metar)}\n\n"
            f"TAF: {taf}")

    markup = get_flight_airport_markup(icao)  # Две кнопки сразу

    bot.edit_message_text(chat_id=call.message.chat.id,
                          message_id=call.message.message_id,
                          text=text, parse_mode='HTML', reply_markup=markup)


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


# Webhook для Render
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
    if os.environ.get('RENDER') is None:
        bot.infinity_polling(none_stop=True)
    else:
        bot.remove_webhook()
        url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}"
        bot.set_webhook(url=f"{url}/{BOT_TOKEN}")
        port = int(os.environ.get('PORT', 5000))
        app.run(host='0.0.0.0', port=port)
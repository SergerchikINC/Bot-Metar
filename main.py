

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import telebot
import requests
import json
import csv
from telebot.types import Message, CallbackQuery
import pymongo
import os

# Токен и MongoDB (для Render берём из env)
BOT_TOKEN = 'BOT_TOKEN'
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


# Расшифровка METAR и TAF (как раньше)
def decode_metar(metar: str):
    # (весь код decode_metar из предыдущей версии — без изменений)
    # ... (вставьте полный код decode_metar здесь, чтобы не дублировать)
    # Для краткости оставляю комментарий — код идентичен предыдущему сообщению
    pass  # Замените на полный код функции


def decode_taf(taf: str):
    if "не найден" in taf or "Ошибка" in taf:
        return "Расшифровка недоступна"
    return f"TAF (сырой прогноз): {taf}"


# VATSIM
def get_vatsim_airports(cid: str):
    # (код без изменений)
    pass  # Замените на полный код


# Клавиатура с кнопкой "Обновить"
def get_refresh_markup(icao: str):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh_{icao}"))
    return markup


# ПЕРЕМЕЩЕНА ВВЕРХ: функция show_weather_page (пагинация всех аэропортов)
def show_weather_page(msg_or_call, user_id, edit=False):
    """Отображение страницы со списком аэропортов (пагинация)"""
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


# Теперь обработчики могут использовать show_weather_page без ошибки
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
                    f"TAF: {taf}\n{decode_taf(taf)}")
        markup = get_refresh_markup(icao)
        bot.reply_to(message, response, parse_mode='HTML', reply_markup=markup)
    else:
        user_pages[user_id] = 0
        show_weather_page(message, user_id)  # Теперь функция уже определена выше — ошибка исчезнет


# Остальные обработчики (page_handler, metar_handler, flight_handler, refresh_handler, apt_handler, back_to_flight_handler)
# (вставьте их код из предыдущей версии — без изменений)

# Webhook для Render
from flask import Flask, request, abort

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
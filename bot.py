import telebot
from telebot import types
import datetime
import time
import json
import os
import threading
import pytz
from telebot.apihelper import ApiTelegramException
import logging
import re
from dotenv import load_dotenv
load_dotenv()
# --- НАСТРОЙКИ ЛОГИРОВАНИЯ ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# ИЗМЕНЕНО: Обновлен ID целевого чата
TARGET_CHAT_ID = int(os.getenv('TARGET_CHAT_ID'))

ALLOWED_USER_IDS = json.loads(os.getenv("ALLOWED_USER_IDS", "[]"))
CURRENCY_MENTIONS = json.loads(os.getenv("CURRENCY_MENTIONS", "{}"))

KAM_USERNAME = os.getenv('KAM_USERNAME')

REPORTS_FILE = 'anomaly_reports.json'
MOSCOW_TZ = pytz.timezone('Europe/Moscow')
SCHEDULE_TIME_MSK = datetime.time(10, 0)
last_run_date_msk = None

bot = telebot.TeleBot(BOT_TOKEN)

# --- Управление данными ---
user_data = {}
anomaly_reports = {}


def save_reports():
    """Сохраняет активные аномалии в JSON файл."""
    reports_to_save = {str(k): v for k, v in anomaly_reports.items()}
    with open(REPORTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(reports_to_save, f, ensure_ascii=False, indent=4)
    logging.info("Данные инцидентов сохранены.")


def load_reports():
    """Загружает активные аномалии из JSON файла при старте."""
    global anomaly_reports
    if os.path.exists(REPORTS_FILE):
        with open(REPORTS_FILE, 'r', encoding='utf-8') as f:
            try:
                reports_from_file = json.load(f)
                anomaly_reports = {int(k): v for k, v in reports_from_file.items()}
                logging.info(f"История инцидентов загружена из файла: {len(anomaly_reports)} пользователей.")
            except json.JSONDecodeError:
                logging.error("Ошибка чтения файла с инцидентами.")
                anomaly_reports = {}
            except ValueError:
                logging.error("Ошибка преобразования ключей user_id в файле.")
                anomaly_reports = {}
    else:
        anomaly_reports = {}
        logging.info("Файл с инцидентами не найден, начинаем с чистого листа.")


def escape_markdown(text: str) -> str:
    """Экранирует специальные символы для Markdown V1."""
    if not isinstance(text, str): return ''
    escape_chars = r'_*`['
    text = re.sub(r'@(?![a-zA-Z0-9_]{5,})', r'\@', text)
    for char in escape_chars:
        text = text.replace(char, f'\\{char}')
    return text


def pluralize(number, one, two, five):
    """Возвращает правильное склонение слова в зависимости от числа."""
    n = abs(number) % 100
    if 11 <= n <= 19: return five
    n = n % 10
    if n == 1: return one
    if 2 <= n <= 4: return two
    return five


# --- Декоратор для проверки доступа ---
def access_control(func):
    """Декоратор, ограничивающий доступ к функциям бота."""

    def wrapper(message_or_call):
        user_id = message_or_call.from_user.id
        chat_type = getattr(getattr(message_or_call, 'chat', None), 'type', None)

        if chat_type != 'private' and isinstance(message_or_call,
                                                 types.Message) and message_or_call.text and message_or_call.text.startswith(
                '/'):
            logging.warning(f"Ignored command in group chat {getattr(message_or_call.chat, 'id', 'N/A')}.")
            return

        if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
            if isinstance(message_or_call, types.CallbackQuery):
                bot.answer_callback_query(message_or_call.id, "❌ Доступ запрещен.", show_alert=True)
            elif chat_type == 'private':
                bot.reply_to(message_or_call, "❌ У вас нет доступа к данной системе.")
            return

        if user_id not in user_data:
            user_data[user_id] = {}
        return func(message_or_call)

    return wrapper


# --- Клавиатуры ---

def create_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton('🚨 Зарегистрировать инцидент', callback_data='report_anomaly'),
               types.InlineKeyboardButton('📊 Сообщить о стабильности', callback_data='no_anomaly'),
               types.InlineKeyboardButton('✅ Зарегистрировать восстановление', callback_data='report_recovery'),
               types.InlineKeyboardButton('🌙 Завершить ночной мониторинг', callback_data='end_night_shift'))
    return markup


def create_platform_menu(selected_platforms=[]):
    markup = types.InlineKeyboardMarkup(row_width=2)
    platforms = ['Platcore', 'Easysend']
    row_btns = [types.InlineKeyboardButton(f"✔ {p}" if p.lower() in selected_platforms else p,
                                           callback_data=f'toggle_platform_{p.lower()}') for p in platforms]
    markup.add(*row_btns)
    if selected_platforms:
        markup.add(types.InlineKeyboardButton('Далее ➡️', callback_data='platforms_done'))
    markup.add(types.InlineKeyboardButton('❌ Отменить', callback_data='cancel_report'))
    return markup


def create_currency_menu(selected_currencies=[]):
    markup = types.InlineKeyboardMarkup(row_width=2)
    currencies = ['KGS', 'UZS', 'RUB', 'ARS', 'AZN', 'AUD', 'AED', 'EGP', 'DOP', 'CNY', 'DZD', 'CRC']
    for i in range(0, len(currencies), 2):
        row_btns = [types.InlineKeyboardButton(f"✔ {c}" if c in selected_currencies else c,
                                               callback_data=f'toggle_currency_{c}') for c in currencies[i:i + 2]]
        markup.add(*row_btns)

    # Кнопка Тех. работ
    markup.add(types.InlineKeyboardButton('🛠 Тех. работы (Вся площадка)', callback_data='tech_maintenance'))

    if selected_currencies:
        markup.add(types.InlineKeyboardButton('Далее ➡️', callback_data='currencies_done'))
    markup.add(types.InlineKeyboardButton('⬅️ Назад', callback_data='back_to_platform'),
               types.InlineKeyboardButton('❌ Отменить', callback_data='cancel_report'))
    return markup


def create_reason_menu():
    markup = types.InlineKeyboardMarkup(row_width=1)
    reasons = {
        'liquidity': 'Нехватка ликвидности', 'trader_off': 'Трейдер отключился от работы',
        'bank_maintenance': 'Технические работы в банке', 'antispam_sum': 'Антиспам (одинаковая сумма)',
        'queued': 'Статус Queued', 'merchant_issue': 'Проблемы на стороне мерчанта',
        'automation_issue': 'Проблема с автоматикой (бот)', 'payout_traffic_delay': 'Долго не поступал выплатной трафик'
    }
    for key, text in reasons.items():
        markup.add(types.InlineKeyboardButton(text, callback_data=f'reason_{key}'))
    markup.add(types.InlineKeyboardButton('⬅️ Назад', callback_data='back_to_currency'),
               types.InlineKeyboardButton('❌ Отменить', callback_data='cancel_report'))
    return markup


def create_incident_status_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton('Да, активен', callback_data='incident_active_yes'),
               types.InlineKeyboardButton('Нет, уже восстановлен', callback_data='incident_active_no'))
    markup.add(types.InlineKeyboardButton('⬅️ Назад к причине', callback_data='back_to_reason'),
               types.InlineKeyboardButton('❌ Отменить', callback_data='cancel_report'))
    return markup


def create_merchant_notification_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton('Да', callback_data='merchant_notified_yes'),
               types.InlineKeyboardButton('Нет', callback_data='merchant_notified_no'))
    markup.add(types.InlineKeyboardButton('⬅️ Назад к доп. инфо', callback_data='back_to_description'),
               types.InlineKeyboardButton('❌ Отменить', callback_data='cancel_report'))
    return markup


def create_confirmation_menu(is_resolved_immediately=False):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton('✅ Подтвердить и отправить', callback_data='confirm_send'),
               types.InlineKeyboardButton('✏️ Ввести заново', callback_data='report_anomaly'))

    # Если это тех работы, возвращаем к описанию, так как других шагов не было
    markup.add(types.InlineKeyboardButton('⬅️ Назад к доп. инфо', callback_data='back_to_description'),
               types.InlineKeyboardButton('❌ Отменить', callback_data='cancel_report'))
    return markup


def create_end_shift_confirmation_menu(count):
    markup = types.InlineKeyboardMarkup(row_width=1)
    incident_word = pluralize(count, "инцидент", "инцидента", "инцидентов")
    markup.add(types.InlineKeyboardButton(f'✅ Да, закрыть {count} {incident_word}', callback_data='confirm_end_shift'),
               types.InlineKeyboardButton('❌ Нет, отменить', callback_data='cancel_end_shift'))
    return markup


def create_recovery_menu(user_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    user_reports = anomaly_reports.get(user_id, {})
    active_reports = {k: v for k, v in user_reports.items() if v.get('status') == 'active'}

    if not active_reports:
        return None

    for report_id, report_data in active_reports.items():
        platforms_list = report_data.get('platforms', [])
        platforms_str = ', '.join(platforms_list) if platforms_list else "Неизвестно"
        currency = report_data.get('currency', '?')
        markup.add(
            types.InlineKeyboardButton(f"Инцидент: {platforms_str} - {currency}", callback_data=f'recover_{report_id}'))

    markup.add(types.InlineKeyboardButton('⬅️ Главное меню', callback_data='back_to_main'))
    return markup


def create_persistent_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton('🏠 Главное меню'))
    return markup


# --- Обработчики команд ---

@bot.message_handler(commands=['start', 'help'])
@access_control
def send_welcome(message):
    user_id = message.from_user.id
    user_data[user_id] = {}
    if message.chat.type == 'private':
        bot.send_message(message.chat.id, "Система мониторинга готова к работе. Выберите 'Главное меню'.",
                         reply_markup=create_persistent_keyboard())


@bot.message_handler(commands=['nightinfo'])
@access_control
def handle_night_info_command(message):
    user_id = message.from_user.id
    logging.info(f"User {user_id} requested nightly summary manually.")
    bot.reply_to(message, "Запрашиваю ночную сводку...")
    if send_nightly_summary():
        bot.reply_to(message, "Сводка отправлена в основной чат.")
    else:
        bot.reply_to(message, "Не удалось отправить сводку. Проверьте логи.")


@bot.message_handler(func=lambda message: message.text == '🏠 Главное меню')
@access_control
def handle_main_menu_button(message):
    user_id = message.from_user.id
    if message.chat.type == 'private':
        user_data[user_id] = {}
        bot.send_message(message.chat.id, "Выберите необходимое действие:", reply_markup=create_main_menu())


@bot.message_handler(func=lambda message: True)
@access_control
def handle_text(message):
    """Обработка ввода текста (только для описания и только в личке)."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    data = user_data.get(user_id, {})
    current_message_id = data.get('current_message_id')

    if message.chat.type == 'private' and data.get('awaiting_description'):
        if not data.get('reason_text'):
            bot.send_message(chat_id, "Ошибка сессии (нет причины). Начните заново.", reply_markup=create_main_menu())
            user_data[user_id] = {}
            return

        data['description'] = message.text
        data.pop('awaiting_description', None)

        # Логика для тех. работ
        is_tech = data.get('is_tech_maintenance', False)

        # Определяем следующий шаг
        if data.get('resolved_immediately'):
            data['awaiting_confirmation'] = True
            preview_text = generate_preview_text(data, is_resolved_immediately=True)
            markup = create_confirmation_menu(is_resolved_immediately=True)
        elif is_tech:
            # Для тех. работ сразу к подтверждению (минуя мерчантов)
            data['awaiting_confirmation'] = True
            preview_text = generate_preview_text(data, is_resolved_immediately=False)
            markup = create_confirmation_menu(
                is_resolved_immediately=False)  # Используем False, чтобы кнопка назад вела к описанию (изменено в create_confirmation_menu)
        else:
            data['awaiting_merchant_notification'] = True
            # Для обычного флоу
            if current_message_id:
                try:
                    bot.edit_message_text("Шаг 5: Мерчанты были оповещены?", chat_id, current_message_id,
                                          reply_markup=create_merchant_notification_menu())
                except ApiTelegramException:
                    new_msg = bot.send_message(chat_id, "Шаг 5: Мерчанты были оповещены?",
                                               reply_markup=create_merchant_notification_menu())
                    data['current_message_id'] = new_msg.message_id
            else:
                new_msg = bot.send_message(chat_id, "Шаг 5: Мерчанты были оповещены?",
                                           reply_markup=create_merchant_notification_menu())
                data['current_message_id'] = new_msg.message_id
            try:
                bot.delete_message(chat_id, message.message_id)
            except Exception:
                pass
            return  # Выходим, так как мы отправили новое меню, а не подтверждение

        # Отправка превью (для resolved или tech maintenance)
        if current_message_id:
            try:
                bot.edit_message_text(preview_text, chat_id, current_message_id, parse_mode='Markdown',
                                      reply_markup=markup)
            except ApiTelegramException:
                new_msg = bot.send_message(chat_id, preview_text, parse_mode='Markdown', reply_markup=markup)
                data['current_message_id'] = new_msg.message_id
        else:
            new_msg = bot.send_message(chat_id, preview_text, parse_mode='Markdown', reply_markup=markup)
            data['current_message_id'] = new_msg.message_id

        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass


# --- Ночная сводка ---
def send_nightly_summary():
    now_msk = datetime.datetime.now(MOSCOW_TZ)
    report_end_time = now_msk.replace(hour=8, minute=0, second=0, microsecond=0)
    report_start_time = (now_msk - datetime.timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    night_incidents = []
    load_reports()

    for user_id_key, reports in anomaly_reports.items():
        for report_id, report in reports.items():
            incident_start_ts = report.get('start_time')
            if not incident_start_ts: continue

            incident_time_msk = datetime.datetime.fromtimestamp(incident_start_ts, MOSCOW_TZ)
            if report_start_time <= incident_time_msk < report_end_time:
                night_incidents.append(report)

    summary_text = f"**🌙 Ночная сводка ({report_start_time.strftime('%H:%M %d.%m')} - {report_end_time.strftime('%H:%M %d.%m')} МСК)**\n\n"
    if not night_incidents:
        summary_text += "За прошедшую ночь инцидентов не зафиксировано."
    else:
        count = len(night_incidents);
        incident_word = pluralize(count, "инцидент", "инцидента", "инцидентов")
        summary_text += f"За прошедшую ночь зафиксировано: {count} {incident_word}\n\n"
        night_incidents.sort(key=lambda x: x['start_time'])
        for report in night_incidents:
            report_time_str = datetime.datetime.fromtimestamp(report['start_time'], MOSCOW_TZ).strftime('%H:%M')
            platforms_data = report.get('platforms');
            platforms = 'Не указано'
            if isinstance(platforms_data, list) and platforms_data:
                platforms = ', '.join(platforms_data)
            elif isinstance(platforms_data, str) and platforms_data:
                platforms = platforms_data
            currency = report.get('currency', '?');
            reason = report.get('reason', 'Не указано')
            status_mark = " (устранен сразу)" if report.get('status') == 'resolved_immediately' else ""
            summary_text += f"▪️ *{report_time_str}* - {platforms} / {currency} - {reason}{status_mark}\n"

    try:
        bot.send_message(TARGET_CHAT_ID, summary_text, parse_mode="Markdown")
        return True
    except Exception as e:
        logging.error(f"Ошибка при отправке ночной сводки: {e}", exc_info=True)
        return False


def run_scheduler():
    global last_run_date_msk
    last_run_date_msk = (datetime.datetime.now(MOSCOW_TZ) - datetime.timedelta(days=1)).date()
    while True:
        try:
            now_msk = datetime.datetime.now(MOSCOW_TZ)
            current_time = now_msk.time();
            current_date = now_msk.date()
            if current_time >= SCHEDULE_TIME_MSK and current_date > last_run_date_msk:
                if send_nightly_summary(): last_run_date_msk = current_date
            time.sleep(60)
        except Exception:
            time.sleep(60)


# --- Callbacks ---
@bot.callback_query_handler(func=lambda call: True)
@access_control
def callback_inline(call):
    user_id = call.from_user.id
    data = user_data.setdefault(user_id, {'platforms': [], 'currencies': []})
    current_message_id = call.message.message_id

    def edit_message(text, chat_id, message_id, reply_markup=None, parse_mode=None):
        try:
            bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id,
                                  reply_markup=reply_markup, parse_mode=parse_mode)
            data['current_message_id'] = message_id
        except ApiTelegramException as e:
            if 'message is not modified' in str(e):
                data['current_message_id'] = message_id
            else:
                raise e

    try:
        if call.data == 'cancel_report':
            user_data[user_id] = {}
            bot.delete_message(call.message.chat.id, current_message_id)
            bot.send_message(call.message.chat.id, "Операция отменена. Выберите новое действие:",
                             reply_markup=create_main_menu())
            bot.answer_callback_query(call.id)
            return

        if call.data == 'back_to_main':
            user_data[user_id] = {}
            edit_message("Выберите необходимое действие:", call.message.chat.id, current_message_id,
                         reply_markup=create_main_menu())
            bot.answer_callback_query(call.id)
            return

        if call.data == 'report_anomaly':
            user_data[user_id] = {'platforms': [], 'currencies': []}
            edit_message("Шаг 1: Выберите одну или несколько платформ.", call.message.chat.id, current_message_id,
                         reply_markup=create_platform_menu())

        elif call.data == 'no_anomaly':
            try:
                bot.send_message(TARGET_CHAT_ID, "✅ Проведен плановый мониторинг. Отклонений не зафиксировано.")
                bot.answer_callback_query(call.id, "Сообщение отправлено.")
            except Exception as e:
                bot.answer_callback_query(call.id, f"Ошибка отправки: {e}")

        elif call.data == 'report_recovery':
            recovery_menu = create_recovery_menu(user_id)
            if recovery_menu:
                edit_message("Выберите инцидент для регистрации восстановления:", call.message.chat.id,
                             current_message_id, reply_markup=recovery_menu)
            else:
                bot.answer_callback_query(call.id, "Активные инциденты отсутствуют.", show_alert=True)

        elif call.data == 'end_night_shift':
            user_reports = anomaly_reports.get(user_id, {})
            active_incidents = {k: v for k, v in user_reports.items() if v.get('status') == 'active'}
            count = len(active_incidents)
            if count == 0:
                bot.answer_callback_query(call.id, "У вас нет активных инцидентов для закрытия.", show_alert=True)
            else:
                incident_word = pluralize(count, "инцидент", "инцидента", "инцидентов")
                edit_message(f"Вы уверены, что хотите закрыть {count} {incident_word}?", call.message.chat.id,
                             current_message_id, reply_markup=create_end_shift_confirmation_menu(count))

        elif call.data == 'confirm_end_shift':
            user_reports = anomaly_reports.get(user_id, {})
            closed_count = 0
            close_time = time.time()
            if user_reports:
                ids_to_update = [k for k, v in user_reports.items() if v.get('status') == 'active']
                for report_id in ids_to_update:
                    anomaly_reports[user_id][report_id]['status'] = 'closed_shift_end'
                    anomaly_reports[user_id][report_id]['close_time'] = close_time
                    closed_count += 1
                if closed_count > 0:
                    save_reports()
                    bot.answer_callback_query(call.id, f"Успешно закрыто {closed_count} инцидентов.")
                else:
                    bot.answer_callback_query(call.id, "Не найдено активных инцидентов.")
            edit_message("Выберите необходимое действие:", call.message.chat.id, current_message_id,
                         reply_markup=create_main_menu())

        elif call.data == 'cancel_end_shift':
            bot.answer_callback_query(call.id, "Отменено.")
            edit_message("Выберите необходимое действие:", call.message.chat.id, current_message_id,
                         reply_markup=create_main_menu())

        elif call.data.startswith('toggle_platform_'):
            platform = call.data.replace('toggle_platform_', '')
            current_platforms = data.setdefault('platforms', [])
            if platform in current_platforms:
                current_platforms.remove(platform)
            else:
                current_platforms.append(platform)
            bot.edit_message_reply_markup(call.message.chat.id, current_message_id,
                                          reply_markup=create_platform_menu(data.get('platforms', [])))

        elif call.data == 'platforms_done':
            if not data.get('platforms'): bot.answer_callback_query(call.id, "Выберите хотя бы одну платформу.",
                                                                    show_alert=True); return
            edit_message("Шаг 2: Выберите одну или несколько валют.", call.message.chat.id, current_message_id,
                         reply_markup=create_currency_menu(data.get('currencies', [])))

        # --- ОБРАБОТКА ТЕХ. РАБОТ ---
        elif call.data == 'tech_maintenance':
            data['currencies'] = ['ВСЕ']  # Обозначаем, что это все валюты
            data['reason_text'] = 'Технические работы на площадке'
            data['is_tech_maintenance'] = True  # Флаг тех. работ
            data['resolved_immediately'] = False  # Обычно тех. работы длятся какое-то время
            data['awaiting_description'] = True

            # Чистим ненужные флаги
            data.pop('merchant_notified', None)
            data.pop('awaiting_merchant_notification', None)
            data.pop('awaiting_confirmation', None)

            logging.info(f"User {user_id} selected Tech Maintenance.")
            edit_message("Шаг 3: Укажите сроки работ или доп. информацию.", call.message.chat.id, current_message_id)

        elif call.data.startswith('toggle_currency_'):
            currency = call.data.replace('toggle_currency_', '')
            current_currencies = data.setdefault('currencies', [])
            if currency in current_currencies:
                current_currencies.remove(currency)
            else:
                current_currencies.append(currency)
            bot.edit_message_reply_markup(call.message.chat.id, current_message_id,
                                          reply_markup=create_currency_menu(data.get('currencies', [])))

        elif call.data == 'currencies_done':
            if not data.get('currencies'): bot.answer_callback_query(call.id, "Выберите хотя бы одну валюту.",
                                                                     show_alert=True); return
            edit_message("Шаг 3: Укажите общую причину.", call.message.chat.id, current_message_id,
                         reply_markup=create_reason_menu())

        elif call.data == 'back_to_platform':
            data['currencies'] = [];
            data.pop('reason_text', None);
            data.pop('description', None)
            data.pop('is_tech_maintenance', None)  # Сброс флага
            edit_message("Шаг 1: Выберите одну или несколько платформ.", call.message.chat.id, current_message_id,
                         reply_markup=create_platform_menu(data.get('platforms', [])))

        elif call.data == 'back_to_currency':
            data.pop('reason_text', None);
            data.pop('description', None)
            edit_message("Шаг 2: Выберите одну или несколько валют.", call.message.chat.id, current_message_id,
                         reply_markup=create_currency_menu(data.get('currencies', [])))

        elif call.data == 'back_to_reason':
            data.pop('description', None)
            edit_message("Шаг 3: Укажите общую причину.", call.message.chat.id, current_message_id,
                         reply_markup=create_reason_menu())

        elif call.data == 'back_to_incident_status':
            data.pop('description', None);
            data['awaiting_incident_status'] = True
            edit_message("Шаг 4: Инцидент все еще активен?", call.message.chat.id, current_message_id,
                         reply_markup=create_incident_status_menu())

        elif call.data == 'back_to_description':
            data['awaiting_description'] = True;
            data.pop('description', None)
            edit_message("Шаг 4: Ввод общей дополнительной информации.", call.message.chat.id, current_message_id)

        elif call.data == 'back_to_merchant_notification':
            data.pop('awaiting_confirmation', None);
            data['awaiting_merchant_notification'] = True
            edit_message("Шаг 5: Мерчанты были оповещены?", call.message.chat.id, current_message_id,
                         reply_markup=create_merchant_notification_menu())

        elif call.data.startswith('recover_'):
            report_id = call.data.replace('recover_', '')
            report = anomaly_reports.get(user_id, {}).get(report_id)

            if report and report.get('status') == 'active':
                duration_str = str(datetime.timedelta(seconds=int(time.time() - report['start_time'])))
                platforms_list = report.get('platforms', ['?'])
                platforms_str = ', '.join(platforms_list)
                recovery_text = (f"✅ **Зафиксировано восстановление системы**\n"
                                 f"Платформы: {platforms_str}\n"
                                 f"Валюта: {report.get('currency')}\n"
                                 f"🕒 **Общее время простоя:** {duration_str}")
                try:
                    if report.get('message_id'):
                        bot.send_message(TARGET_CHAT_ID, recovery_text, reply_to_message_id=report.get('message_id'))
                    else:
                        bot.send_message(TARGET_CHAT_ID, recovery_text)
                    bot.answer_callback_query(call.id, "Информация о восстановлении успешно отправлена.")
                except Exception:
                    bot.send_message(TARGET_CHAT_ID, recovery_text)

                anomaly_reports[user_id][report_id]['status'] = 'resolved'
                anomaly_reports[user_id][report_id]['resolve_time'] = time.time()
                save_reports()
            else:
                bot.answer_callback_query(call.id, "Инцидент уже закрыт или не найден.", show_alert=True)
            edit_message("Выберите необходимое действие:", call.message.chat.id, current_message_id,
                         reply_markup=create_main_menu())

        elif call.data.startswith('reason_'):
            reason_map = {
                'liquidity': 'Нехватка ликвидности', 'trader_off': 'Трейдер отключился от работы',
                'bank_maintenance': 'Технические работы в банке', 'antispam_sum': 'Антиспам (одинаковая сумма)',
                'queued': 'Статус Queued', 'merchant_issue': 'Проблемы на стороне мерчанта',
                'automation_issue': 'Проблема с автоматикой (бот)',
                'payout_traffic_delay': 'Долго не поступал выплатной трафик'
            }
            reason_key = call.data.split('_', 1)[1]
            data['reason_text'] = reason_map.get(reason_key)
            data['awaiting_incident_status'] = True
            edit_message("Шаг 4: Инцидент все еще активен?", call.message.chat.id, current_message_id,
                         reply_markup=create_incident_status_menu())

        elif call.data in ['incident_active_yes', 'incident_active_no']:
            data.pop('awaiting_incident_status', None)
            data['resolved_immediately'] = (call.data == 'incident_active_no')
            data['awaiting_description'] = True
            edit_message("Шаг 4: Ввод общей дополнительной информации.", call.message.chat.id, current_message_id)

        elif call.data in ['merchant_notified_yes', 'merchant_notified_no']:
            data['merchant_notified'] = (call.data == 'merchant_notified_yes')
            data.pop('awaiting_merchant_notification', None)
            data['awaiting_confirmation'] = True
            preview_text = generate_preview_text(data, is_resolved_immediately=False)
            edit_message(preview_text, call.message.chat.id, current_message_id, parse_mode='Markdown',
                         reply_markup=create_confirmation_menu(is_resolved_immediately=False))

        elif call.data == 'confirm_send':
            is_resolved = data.get('resolved_immediately')
            platforms = data.get('platforms', [])
            currencies = data.get('currencies', [])
            reason = data.get('reason_text')
            description = data.get('description', '')
            merchant_notified = data.get('merchant_notified', False) if not is_resolved else False
            is_tech = data.get('is_tech_maintenance', False)

            sent_count = 0
            platforms_str = ', '.join([p.capitalize() for p in platforms])

            for currency in currencies:
                report_text = generate_final_report_text(platforms_str, currency, reason, description,
                                                         merchant_notified, is_resolved, is_tech)
                try:
                    sent_message = bot.send_message(TARGET_CHAT_ID, report_text, parse_mode='Markdown')
                    if not is_resolved:
                        report_id = f"{int(time.time())}_{currency}"
                        if user_id not in anomaly_reports: anomaly_reports[user_id] = {}
                        anomaly_reports[user_id][report_id] = {
                            'start_time': time.time(), 'message_id': sent_message.message_id,
                            'platforms': [p.capitalize() for p in platforms], 'currency': currency,
                            'reason': reason, 'status': 'active', 'merchant_notified': merchant_notified
                        }
                    # Если решено сразу, тоже сохраняем для истории, но статус другой
                    else:
                        report_id = f"{int(time.time())}_{currency}_res"
                        if user_id not in anomaly_reports: anomaly_reports[user_id] = {}
                        anomaly_reports[user_id][report_id] = {
                            'start_time': time.time(), 'message_id': sent_message.message_id,
                            'platforms': [p.capitalize() for p in platforms], 'currency': currency,
                            'reason': reason, 'status': 'resolved_immediately', 'merchant_notified': merchant_notified
                        }
                    sent_count += 1
                except Exception as e:
                    logging.error(f"Ошибка при отправке: {e}")

            if sent_count > 0: save_reports()

            edit_message(f"✅ Успешно отправлено отчетов: {sent_count}.", call.message.chat.id, current_message_id)
            user_data[user_id] = {}
            time.sleep(2)
            bot.send_message(call.message.chat.id, "Выберите следующее действие:", reply_markup=create_main_menu())

    except ApiTelegramException as e:
        if 'message is not modified' in str(e):
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "Произошла ошибка API.")


# --- Генерация текстов ---
def generate_preview_text(data, is_resolved_immediately=False):
    platforms_str = ', '.join([p.capitalize() for p in data.get('platforms', [])])
    currencies_str = ', '.join(data.get('currencies', []))
    reason = data.get('reason_text') or 'Не указано'
    description_raw = data.get('description', '')
    description = escape_markdown(description_raw) if description_raw else 'Отсутствует'
    merchant_notified = data.get('merchant_notified', False)
    is_tech = data.get('is_tech_maintenance', False)

    mentions = {escape_markdown(CURRENCY_MENTIONS.get(c)) for c in data.get('currencies', []) if
                CURRENCY_MENTIONS.get(c)}
    responsible_text = f"— **Ответственные:** {', '.join(mentions)}\n" if mentions and not is_tech else ""
    merchant_status_text = ""
    kam_mention_text = ""

    header = f"🚨 **Зафиксирована техническая аномалия** 🚨\n"
    if is_tech:
        header = f"🛠 **Проводятся технические работы** 🛠\n"

    description_line = f"📋 **Доп. информация:**\n{description}\n"

    if is_resolved_immediately:
        header = f"⚠️ **Зафиксирована и устранена кратковременная аномалия**\n"
    elif not is_tech and merchant_notified:
        merchant_status_text = "✅ Оповестили мерчантов о просадках\n"
        if KAM_USERNAME:
            kam_mention_text = f"— **Ответственный КАМ:** {escape_markdown(KAM_USERNAME)}\n"

    return (
        f"**Предварительный просмотр отчета:**\n"
        f"{header}"
        f"▪️ **Платформы:** {platforms_str}\n"
        f"▪️ **Валюты:** {currencies_str}\n"
        f"▪️ **Причина:** {reason}\n"
        f"{description_line}"
        f"{merchant_status_text}"
        f"{responsible_text}"
        f"{kam_mention_text}"
        f"**Подтвердите отправку.**"
    )


def generate_final_report_text(platforms_str, currency, reason, description_raw, merchant_notified, is_resolved,
                               is_tech=False):
    mention = escape_markdown(CURRENCY_MENTIONS.get(currency, ''))
    responsible_text = f"— **Ответственный сотрудник:** {mention}\n" if mention and not is_tech else ""
    merchant_status_text = ""
    kam_mention_text = ""
    description = escape_markdown(description_raw) if description_raw else 'Отсутствует'
    description_line = f"📋 **Доп. информация:**\n{description}\n"

    header = f"🚨 **Зафиксирована техническая аномалия** 🚨\n"
    if is_tech:
        header = f"🛠 **Проводятся технические работы** 🛠\n"

    if is_resolved:
        header = f"⚠️ **Зафиксирована и устранена кратковременная аномалия**\n"
    elif not is_tech and merchant_notified:
        merchant_status_text = "✅ Оповестили мерчантов о просадках\n"
        if KAM_USERNAME:
            kam_mention_text = f"— **Ответственный КАМ:** {escape_markdown(KAM_USERNAME)}\n"

    return (
        f"{header}"
        f"▪️ **Платформы:** {platforms_str}\n"
        f"▪️ **Валюта:** {currency}\n"
        f"▪️ **Причина:** {reason}\n"
        f"{description_line}"
        f"{merchant_status_text}"
        f"{responsible_text}"
        f"{kam_mention_text}"
        f"ℹ️ Мониторинг по смежным направлениям проведен."
    )


if __name__ == '__main__':
    load_reports()
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    logging.info("Бот запущен...")
    while True:
        try:
            bot.polling(none_stop=True)
        except Exception as e:
            logging.error(f"Ошибка polling: {e}")
            time.sleep(15)

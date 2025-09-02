import logging
import os
import sqlite3
import json
from datetime import datetime, timedelta
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackContext
from telegram import Update
from openai import OpenAI
import base64
import asyncio

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурационный файл
CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "telegram_bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
    "openrouter_api_key": "YOUR_OPENROUTER_API_KEY",
    "model": "google/gemini-2.0-flash-lite-001",
    "max_message_length": 4000,
    "max_messages_per_day": 50,
    "memory_size": 10,
    "admin_ids": []
}

def load_config():
    """Загрузка конфигурации из файла"""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # Объединяем с дефолтными значениями
                return {**DEFAULT_CONFIG, **config}
        else:
            # Создаем файл с дефолтными настройками
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
            return DEFAULT_CONFIG
    except Exception as e:
        logger.error(f"Ошибка загрузки конфига: {e}")
        return DEFAULT_CONFIG

def save_config(config):
    """Сохранение конфигурации в файл"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Ошибка сохранения конфига: {e}")

# Загружаем конфигурацию
config = load_config()

# Инициализация клиента OpenRouter
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=config["openrouter_api_key"],
)

# Инициализация базы данных
def init_database():
    """Инициализация базы данных SQLite"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    # Таблица для хранения истории сообщений
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS message_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        message_text TEXT NOT NULL,
        message_type TEXT NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Таблица для учета лимитов сообщений
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_limits (
        user_id INTEGER PRIMARY KEY,
        message_count INTEGER DEFAULT 0,
        last_reset_date DATE DEFAULT CURRENT_DATE
    )
    ''')
    
    conn.commit()
    conn.close()

def get_user_message_history(user_id, limit=10):
    """Получение истории сообщений пользователя"""
    try:
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT message_text, message_type, timestamp 
        FROM message_history 
        WHERE user_id = ? 
        ORDER BY timestamp DESC 
        LIMIT ?
        ''', (user_id, limit))
        
        history = cursor.fetchall()
        conn.close()
        
        return [{"text": row[0], "type": row[1], "timestamp": row[2]} for row in history]
    except Exception as e:
        logger.error(f"Ошибка получения истории: {e}")
        return []

def add_message_to_history(user_id, message_text, message_type="text"):
    """Добавление сообщения в историю"""
    try:
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        
        # Удаляем старые сообщения, если превышен лимит
        cursor.execute('''
        DELETE FROM message_history 
        WHERE user_id = ? AND id NOT IN (
            SELECT id FROM message_history 
            WHERE user_id = ? 
            ORDER BY timestamp DESC 
            LIMIT ?
        )
        ''', (user_id, user_id, config["memory_size"]))
        
        # Добавляем новое сообщение
        cursor.execute('''
        INSERT INTO message_history (user_id, message_text, message_type)
        VALUES (?, ?, ?)
        ''', (user_id, message_text, message_type))
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка добавления в историю: {e}")

def check_user_limit(user_id):
    """Проверка лимита сообщений пользователя"""
    try:
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        
        # Проверяем, нужно ли сбросить счетчик
        cursor.execute('''
        SELECT message_count, last_reset_date 
        FROM user_limits 
        WHERE user_id = ?
        ''', (user_id,))
        
        result = cursor.fetchone()
        
        today = datetime.now().date()
        
        if result:
            message_count, last_reset_date = result
            last_reset_date = datetime.strptime(last_reset_date, '%Y-%m-%d').date()
            
            # Если последний сброс был не сегодня, сбрасываем счетчик
            if last_reset_date != today:
                cursor.execute('''
                UPDATE user_limits 
                SET message_count = 1, last_reset_date = ?
                WHERE user_id = ?
                ''', (today.strftime('%Y-%m-%d'), user_id))
                conn.commit()
                conn.close()
                return True, 1
            else:
                # Проверяем лимит
                if message_count >= config["max_messages_per_day"]:
                    conn.close()
                    return False, message_count
                else:
                    # Увеличиваем счетчик
                    cursor.execute('''
                    UPDATE user_limits 
                    SET message_count = message_count + 1 
                    WHERE user_id = ?
                    ''', (user_id,))
                    conn.commit()
                    conn.close()
                    return True, message_count + 1
        else:
            # Создаем новую запись
            cursor.execute('''
            INSERT INTO user_limits (user_id, message_count, last_reset_date)
            VALUES (?, 1, ?)
            ''', (user_id, today.strftime('%Y-%m-%d')))
            conn.commit()
            conn.close()
            return True, 1
            
    except Exception as e:
        logger.error(f"Ошибка проверки лимита: {e}")
        return True, 0  # В случае ошибки пропускаем проверку

async def reset_daily_limits(context: CallbackContext):
    """Ежедневный сброс лимитов (вызывается по расписанию)"""
    try:
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        
        today = datetime.now().date()
        cursor.execute('''
        UPDATE user_limits 
        SET message_count = 0, last_reset_date = ?
        WHERE last_reset_date != ?
        ''', (today.strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')))
        
        conn.commit()
        conn.close()
        logger.info("Ежедневные лимиты сброшены")
    except Exception as e:
        logger.error(f"Ошибка сброса лимитов: {e}")

def split_long_message(text: str, max_length: int = config["max_message_length"]) -> list:
    """Разбивает длинное сообщение на части"""
    if len(text) <= max_length:
        return [text]
    
    parts = []
    while text:
        if len(text) <= max_length:
            parts.append(text)
            break
        
        # Ищем последний перенос строки или точку для красивого разбиения
        split_index = text.rfind('\n', 0, max_length)
        if split_index == -1:
            split_index = text.rfind('. ', 0, max_length)
        if split_index == -1:
            split_index = text.rfind(' ', 0, max_length)
        if split_index == -1:
            split_index = max_length
        
        parts.append(text[:split_index].strip())
        text = text[split_index:].strip()
    
    return parts

async def send_long_message(update: Update, text: str):
    """Отправляет длинное сообщение частями"""
    parts = split_long_message(text)
    for i, part in enumerate(parts):
        if len(parts) > 1:
            part = f"📄 Часть {i+1}/{len(parts)}\n\n{part}"
        await update.message.reply_text(part)

async def download_image(file_id: str, bot) -> str:
    """Скачивает изображение и возвращает base64 строку"""
    try:
        file = await bot.get_file(file_id)
        file_data = await file.download_as_bytearray()
        
        # Конвертируем в base64
        image_base64 = base64.b64encode(file_data).decode('utf-8')
        return image_base64
        
    except Exception as e:
        logger.error(f"Ошибка загрузки изображения: {e}")
        raise

async def process_image_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик сообщений с изображениями"""
    try:
        if not update.message.photo:
            return
        
        user_id = update.message.from_user.id
        
        # Проверяем лимит
        allowed, count = check_user_limit(user_id)
        if not allowed:
            await update.message.reply_text(
                f"❌ Вы исчерпали лимит сообщений на сегодня ({count}/{config['max_messages_per_day']}). "
                f"Лимит сбросится в 00:00 по UTC."
            )
            return
        
        # Показываем статус "печатает..."
        await update.message.chat.send_action(action="typing")
        
        # Берем самое большое изображение (последнее в списке)
        photo = update.message.photo[-1]
        caption = update.message.caption or "Что на этом изображении?"
        
        # Сохраняем в историю
        add_message_to_history(user_id, f"Изображение: {caption}", "image")
        
        # Скачиваем изображение
        image_base64 = await download_image(photo.file_id, context.bot)
        
        # Отправляем запрос к OpenRouter с изображением
        response = await generate_ai_response_with_image(caption, image_base64, user_id)
        
        # Сохраняем ответ в историю
        add_message_to_history(user_id, response, "bot_response")
        
        # Отправляем ответ пользователю
        await send_long_message(update, response)
        
    except Exception as e:
        logger.error(f"Ошибка при обработке изображения: {e}")
        await update.message.reply_text("⚠️ Не удалось обработать изображение. Попробуйте позже.")

async def process_document_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик документов"""
    try:
        user_id = update.message.from_user.id
        
        # Проверяем лимит
        allowed, count = check_user_limit(user_id)
        if not allowed:
            await update.message.reply_text(
                f"❌ Вы исчерпали лимит сообщений на сегодня ({count}/{config['max_messages_per_day']}). "
                f"Лимит сбросится в 00:00 по UTC."
            )
            return
        
        document = update.message.document
        caption = update.message.caption or "Расскажи об этом файле"
        
        # Сохраняем в историю
        add_message_to_history(user_id, f"Файл: {document.file_name} - {caption}", "document")
        
        # Простая проверка по расширению файла для изображений
        image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']
        file_name = document.file_name.lower() if document.file_name else ""
        
        if any(file_name.endswith(ext) for ext in image_extensions):
            # Пытаемся обработать как изображение
            await update.message.reply_text("🖼️ Получено изображение. Обрабатываю...")
            # Для документов-изображений используем другой подход
            await handle_image_document(update, context)
            return
        
        # Для других типов файлов просто отправляем текст
        response = f"📎 Получен файл: {document.file_name or 'без имени'}\nТип: {document.mime_type or 'неизвестно'}\n\nК сожалению, я пока не умею анализировать содержимое файлов. Отправьте текст или изображение для анализа."
        
        # Сохраняем ответ в историю
        add_message_to_history(user_id, response, "bot_response")
        
        await update.message.reply_text(response)
        
    except Exception as e:
        logger.error(f"Ошибка при обработке документа: {e}")
        await update.message.reply_text("⚠️ Не удалось обработать файл.")

async def handle_image_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик документов-изображений"""
    try:
        user_id = update.message.from_user.id
        document = update.message.document
        caption = update.message.caption or "Что на этом изображении?"
        
        # Показываем статус "печатает..."
        await update.message.chat.send_action(action="typing")
        
        # Скачиваем изображение
        file = await context.bot.get_file(document.file_id)
        file_data = await file.download_as_bytearray()
        image_base64 = base64.b64encode(file_data).decode('utf-8')
        
        # Отправляем запрос к OpenRouter с изображением
        response = await generate_ai_response_with_image(caption, image_base64, user_id)
        
        # Сохраняем ответ в историю
        add_message_to_history(user_id, response, "bot_response")
        
        # Отправляем ответ пользователю
        await send_long_message(update, response)
        
    except Exception as e:
        logger.error(f"Ошибка при обработке изображения-документа: {e}")
        await update.message.reply_text("⚠️ Не удалось обработать изображение. Попробуйте позже.")

async def generate_ai_response_with_image(prompt: str, image_base64: str, user_id: int) -> str:
    """Генерация ответа с изображением"""
    try:
        # Получаем историю сообщений
        history = get_user_message_history(user_id, 5)  # Берем последние 5 сообщений для контекста
        
        # Формируем системное сообщение с учетом истории
        system_message = "Ты полезный AI ассистент. Анализируй изображения и отвечай на вопросы о них. Будь дружелюбным и helpful."
        
        if history:
            system_message += "\n\nКонтекст предыдущих сообщений:"
            for msg in reversed(history):  # В хронологическом порядке
                if msg["type"] == "text":
                    system_message += f"\nПользователь: {msg['text']}"
                elif msg["type"] == "bot_response":
                    system_message += f"\nБот: {msg['text']}"
        
        messages = [
            {
                "role": "system", 
                "content": system_message
            },
            {
                "role": "user", 
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}"
                        }
                    }
                ]
            }
        ]
        
        completion = client.chat.completions.create(
            model=config["model"],
            messages=messages,
            max_tokens=1000,
            temperature=0.7,
            extra_headers={
                "HTTP-Referer": "https://github.com/your-username/telegram-ai-bot",
                "X-Title": "Telegram AI Bot"
            }
        )
        
        return completion.choices[0].message.content.strip()
        
    except Exception as e:
        logger.error(f"Ошибка OpenRouter с изображением: {e}")
        return "❌ Не удалось проанализировать изображение. Попробуйте позже."

async def generate_ai_response(prompt: str, user_id: int) -> str:
    """Генерация ответа через OpenRouter"""
    try:
        # Получаем историю сообщений
        history = get_user_message_history(user_id, 5)  # Берем последние 5 сообщений для контекста
        
        # Формируем системное сообщение с учетом истории
        system_message = "Ты полезный AI ассистент в Telegram чате. Отвечай кратко и по делу. Будь дружелюбным и helpful."
        
        if history:
            system_message += "\n\nКонтекст предыдущих сообщений:"
            for msg in reversed(history):  # В хронологическом порядке
                if msg["type"] == "text":
                    system_message += f"\nПользователь: {msg['text']}"
                elif msg["type"] == "bot_response":
                    system_message += f"\nБot: {msg['text']}"
        
        messages = [
            {
                "role": "system", 
                "content": system_message
            },
            {
                "role": "user", 
                "content": prompt
            }
        ]
        
        completion = client.chat.completions.create(
            model=config["model"],
            messages=messages,
            max_tokens=1000,
            temperature=0.7,
            extra_headers={
                "HTTP-Referer": "https://github.com/your-username/telegram-ai-bot",
                "X-Title": "Telegram AI Bot"
            }
        )
        
        return completion.choices[0].message.content.strip()
        
    except Exception as e:
        logger.error(f"Ошибка OpenRouter: {e}")
        return "❌ Не удалось получить ответ от нейросети. Попробуйте позже."

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    welcome_text = """
🤖 Привет! Я работаю на {}

Отправь мне:
• Любое текстовое сообщение
• Изображение с подписью или без
• Файлы (только информация о файле)

Я постараюсь ответить с помощью нейросети!

📊 Лимиты:
• Сообщений в день: {}
• Память: {} сообщений

Используемые технологии:
• Telegram Bot API
• OpenRouter AI
• Модель Google Gemini

Разработан и профинансирован @flamie36621
    """.format(config["model"], config["max_messages_per_day"], config["memory_size"])
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    help_text = """
📖 Доступные команды:
/start - Начать работу с ботом
/help - Показать эту справку
/about - Информация о боте
/history - Показать истории сообщений
/stats - Показать статистику использования

📋 Что можно отправлять:
• Текстовые сообщения
• Изображения (с подписью или без)
• Файлы (только информация)

🔧 Технологии:
• Модель: {}
• Макс. длина: {} символов
• Поддержка изображений: ✅
• Память: {} сообщений
• Лимит в день: {} сообщений
    """.format(config["model"], config["max_message_length"], config["memory_size"], config["max_messages_per_day"])
    await update.message.reply_text(help_text)

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /about"""
    about_text = """
🤖 О боте:
Этот бot использует нейросети через OpenRouter API для генерации ответов.

🔧 Технологии:
• Python Telegram Bot
• OpenRouter AI
• Google Gemini 2.0 Flash Lite
• SQLite для хранения данных

📊 Возможности:
• Текстовые ответы
• Анализ изображений
• Автоматическое разделение длинных сообщений
• Обработка ошибок
• История сообщений ({} последних)
• Лимиты использования ({} в день)

📝 Ограничения:
• Максимальная длина сообщения: {} символов
• Поддержка файлов: ограниченная
    """.format(config["memory_size"], config["max_messages_per_day"], config["max_message_length"])
    await update.message.reply_text(about_text)

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /history"""
    try:
        user_id = update.message.from_user.id
        history = get_user_message_history(user_id, config["memory_size"])
        
        if not history:
            await update.message.reply_text("📝 История сообщений пуста.")
            return
        
        history_text = "📝 История ваших сообщений:\n\n"
        for i, msg in enumerate(reversed(history), 1):
            timestamp = datetime.strptime(msg["timestamp"], '%Y-%m-%d %H:%M:%S').strftime('%H:%M:%S')
            msg_type = "👤" if msg["type"] in ["text", "image", "document"] else "🤖"
            preview = msg["text"][:50] + "..." if len(msg["text"]) > 50 else msg["text"]
            history_text += f"{i}. {msg_type} [{timestamp}]: {preview}\n"
        
        await update.message.reply_text(history_text)
        
    except Exception as e:
        logger.error(f"Ошибка при получении истории: {e}")
        await update.message.reply_text("⚠️ Не удалось получить историю сообщений.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /stats"""
    try:
        user_id = update.message.from_user.id
        
        # Получаем статистику использования
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT message_count, last_reset_date 
        FROM user_limits 
        WHERE user_id = ?
        ''', (user_id,))
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            message_count, last_reset_date = result
            remaining = max(0, config["max_messages_per_day"] - message_count)
            
            stats_text = f"""
📊 Ваша статистика использования:

• Использовано сегодня: {message_count}/{config["max_messages_per_day"]}
• Осталось сегодня: {remaining}
• Последний сброс: {last_reset_date}
• Лимит сбросится: в 00:00 UTC

💾 Память: {config["memory_size"]} последних сообщений
            """
        else:
            stats_text = "📊 Вы еще не отправляли сообщений сегодня."
        
        await update.message.reply_text(stats_text)
        
    except Exception as e:
        logger.error(f"Ошибка при получении статистики: {e}")
        await update.message.reply_text("⚠️ Не удалось получить статистику.")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    try:
        user_id = update.message.from_user.id
        user_message = update.message.text
        
        # Проверяем лимит
        allowed, count = check_user_limit(user_id)
        if not allowed:
            await update.message.reply_text(
                f"❌ Вы исчерпали лимит сообщений на сегодня ({count}/{config['max_messages_per_day']}). "
                f"Лимит сбросится в 00:00 по UTC."
            )
            return
        
        # Сохраняем сообщение в историю
        add_message_to_history(user_id, user_message, "text")
        
        # Показываем статус "печатает..."
        await update.message.chat.send_action(action="typing")
        
        # Отправляем запрос к OpenRouter
        response = await generate_ai_response(user_message, user_id)
        
        # Сохраняем ответ в историю
        add_message_to_history(user_id, response, "bot_response")
        
        # Отправляем ответ пользователю (с разбивкой если нужно)
        await send_long_message(update, response)
        
    except Exception as e:
        logger.error(f"Ошибка при обработке сообщения: {e}")
        await update.message.reply_text("⚠️ Произошла ошибка при обработке запроса. Попробуйте позже.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Ошибка: {context.error}")
    if update and update.message:
        await update.message.reply_text("⚠️ Произошла непредвиденная ошибка.")

async def daily_reset_job(context: CallbackContext):
    """Задача для ежедневного сброса лимитов"""
    await reset_daily_limits(context)

def main():
    print("🤖 Запуск Telegram AI бота...")
    
    # Проверяем обязательные параметры
    if config["telegram_bot_token"] == "YOUR_TELEGRAM_BOT_TOKEN":
        print("❌ Ошибка: Установите telegram_bot_token в config.json")
        return
        
    if config["openrouter_api_key"] == "YOUR_OPENROUTER_API_KEY":
        print("❌ Ошибка: Установите openrouter_api_key в config.json")
        return
    
    # Инициализация базы данных
    init_database()
    print("✅ База данных инициализирована")

    # Создаем приложение
    application = Application.builder().token(config["telegram_bot_token"]).build()

    # Добавляем задачу для ежедневного сброса лимитов
    job_queue = application.job_queue
    if job_queue:
        # Сбрасываем каждый день в полночь по UTC
        job_queue.run_daily(daily_reset_job, time=datetime.strptime("00:00", "%H:%M").time())
        print("✅ Планировщик лимитов настроен")

    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("stats", stats_command))

    # Регистрируем обработчики сообщений
    application.add_handler(MessageHandler(filters.PHOTO, process_image_message))
    application.add_handler(MessageHandler(filters.Document.ALL, process_document_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # Регистрируем обработчик ошибок
    application.add_error_handler(error_handler)

    print("✅ Бот запущен и готов к работе!")
    print(f"📊 Конфигурация: {config}")
    application.run_polling()

if __name__ == "__main__":
    main()
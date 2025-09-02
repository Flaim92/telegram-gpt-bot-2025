"# telegram-gpt-bot-2025" 

Current and 100% working telegram bot with memory and work from OPENROUTER

To configure, set the configuration parameters in config.json 

{
    "telegram_bot_token": "",
    "openrouter_api_key": "",
    "model": "google/gemini-2.0-flash-lite-001",
    "max_message_length": 4000,
    "max_messages_per_day": 50,
    "memory_size": 10,
    "admin_ids": []
}


Windows instructions

mkdir telegram-ai-bot
cd telegram-ai-bot

python -m venv venv

.\venv\Scripts\activate

pip install python-telegram-bot openai sqlite3

python Main.py

Linux instructions

mkdir telegram-ai-bot
cd telegram-ai-bot

python3 -m venv venv

source venv/bin/activate

pip install python-telegram-bot openai

python Main.py



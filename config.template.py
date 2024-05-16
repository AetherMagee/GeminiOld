import os  # ignore this line

TG_BOT_TOKEN: str = ""
GEMINI_API_LINK: str = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro-latest:generateContent?key="
GEMINI_TOKENS: list[str] = []
DATA_FOLDER: str = "/data/" if os.path.exists(".docker") else ""
ENABLE_PERMA_MEMORY: bool = True
MEMORY_LIMIT_MESSAGES: int = 500
ADMIN_ID: int = 0

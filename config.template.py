import os  # ignore this line

TG_BOT_TOKEN: str = ""
GEMINI_TOKENS: list[str] = []
DATA_FOLDER: str = "/data/" if os.path.exists(".docker") else ""
ENABLE_PERMA_MEMORY: bool = True
MEMORY_LIMIT_MESSAGES: int = 500
ADMIN_ID: int = 0

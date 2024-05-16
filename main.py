import asyncio
import importlib
import os
import pickle
import aiohttp
from aiogram import Bot, Dispatcher, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, User
from loguru import logger

import config as cfg

logger.add(cfg.DATA_FOLDER + "logs/log_{time}.log", rotation="1 day")
logger.debug("Initializing...")

dp = Dispatcher()
bot = Bot(cfg.TG_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

self_entity: User
current_token_index = 0
message_log = {
    # chat_id: [message1, message2, ..., messageN]
}
message_counter = 0  # TEMP FIX REMOVE LATER
if os.path.exists(cfg.DATA_FOLDER + "prompt.txt"):
    with open(cfg.DATA_FOLDER + "prompt.txt", "r") as prompt_file:
        base_prompt = prompt_file.read()
else:
    logger.exception("prompt.txt not found!")


def save(file_name: str = "chats.pki") -> None:
    logger.info("Saving message log...")
    with open(cfg.DATA_FOLDER + file_name, "wb") as le_file:
        pickle.dump(message_log, le_file)
        logger.success("Message log saved")


# ===================================
# Gemini API Part
# ===================================
def get_gemini_api_url():
    global current_token_index
    url = cfg.GEMINI_API_LINK + cfg.GEMINI_TOKENS[current_token_index]
    current_token_index = (current_token_index + 1) % len(cfg.GEMINI_TOKENS)
    return url


async def query_api(prompt: str) -> tuple[str, bool]:
    headers = {
        'Content-Type': 'application/json'
    }
    data = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "safetySettings": [
            {
                "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_HATE_SPEECH",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_HARASSMENT",
                "threshold": "BLOCK_NONE"
            }
        ],
        "generationConfig": {
            "temperature": 1,
            "maxOutputTokens": 80000,
            "topP": 1,
            "topK": 200
        }
    }
    url = get_gemini_api_url()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=data) as response:
                response.raise_for_status()
                result = await response.json()

                if 'candidates' in result and 'content' in result['candidates'][0] and 'parts' in \
                        result['candidates'][0]['content']:
                    return result['candidates'][0]['content']['parts'][0]['text'], False
                else:
                    if "blockReason" in str(result) or "safetyRatings" in str(result):
                        logger.error("Request blocked due to filtering")
                        return "❌ Запрос был заблокирован на стороне Gemini API. ", True
                    logger.error(f"Unexpected response structure: {result}")
                    return "❌ Сбой обработки ответа Gemini API. Попробуйте снова. ", True
    except aiohttp.ClientError as error:
        logger.error(f"Error querying Gemini API: {str(error)}")
        if error.status:
            if error.status == 500:
                return "❌ Сбои на стороне Gemini API. Пожалуйста, подождите пару минут. ", True
            if error.status == 429:
                return "❌ Бот перегружен запросами, пожалуйста, попробуйте снова через пару минут. ", True

        return f"❌ Сбой Gemini API. Попробуйте снова. ", True


async def ask_gemini(message: Message) -> str:
    global message_log
    logger.info(
        f"Generating for {message.from_user.id} in {message.chat.id}. Context: {len(message_log[message.chat.id])}")
    all_messages = "\n".join(message_log[message.chat.id])

    prompt = base_prompt.format(
        chat_type="direct message (DM)" if message.from_user.id == message.chat.id else "group",
        all_messages=all_messages, target_message=message_log[message.chat.id][-1])

    output, has_errored = await query_api(prompt)
    output = output[:-1].replace("  ", " ")
    current_list = message_log[message.chat.id]
    if not has_errored:
        current_list.append("You: " + output)
        if len(current_list) > cfg.MEMORY_LIMIT_MESSAGES:
            current_list.pop(0)
        message_log[message.chat.id] = current_list
        logger.success(
            f"Generated for {message.from_user.id} in {message.chat.id}. Context: {len(message_log[message.chat.id])}")
    else:
        current_list.append("You: *Failed to reply for some reason. Be better next time.*")
        if len(current_list) > cfg.MEMORY_LIMIT_MESSAGES:
            current_list.pop(0)
        message_log[message.chat.id] = current_list
    return output


# ===================================
# Message Handling Part
# ===================================
async def append_to_message_log(message: Message) -> None:
    global message_log
    if message.chat.id not in message_log.keys():
        message_log[message.chat.id] = [await get_message_text_from_message(message)]
    else:
        current_list: list = message_log[message.chat.id]
        current_list.append(await get_message_text_from_message(message))
        if len(current_list) > cfg.MEMORY_LIMIT_MESSAGES:
            current_list.pop(0)
        message_log[message.chat.id] = current_list


def format_reply_text(reply_text, max_length=50) -> str:
    """Black magic."""
    reply_text = reply_text.replace('\n', ' ')
    if len(reply_text) > max_length:
        part_length = max_length // 2 - len(" {...} ") // 2
        start = reply_text[:part_length]
        end = reply_text[-part_length:]
        start_slice = start.rsplit(' ', 1)[0] if ' ' in start else start
        end_slice = end.split(' ', 1)[1] if ' ' in end else end
        return f"{start_slice} ... {end_slice}"
    elif len(reply_text) > max_length // 2:
        truncated_text = reply_text[:max_length - 3]
        return f"{truncated_text.rsplit(' ', 1)[0]}..." if ' ' in truncated_text else truncated_text + "..."
    return reply_text


async def get_message_text_from_message(message: Message) -> str:
    user_display = message.from_user.first_name
    if message.from_user.username and message.from_user.username != message.from_user.first_name:
        user_display += f" ({message.from_user.username})"

    text_content = message.text if message.text is not None else "*No Text*"
    text = f"{user_display}: {text_content}"

    if message.reply_to_message:
        reply_text = message.reply_to_message.text if message.reply_to_message.text is not None else "*No Text*"
        text = f"[REPLYING TO: {format_reply_text(reply_text)}] " + text

    if self_entity.username in text_content or (
            message.reply_to_message and message.reply_to_message.from_user.id == self_entity.id) or (
            message.from_user.id == message.chat.id):
        text = "*** " + text

    return text


@dp.message(Command("reset"))
async def reset_command(message: Message) -> None:
    if not message.chat.id == message.from_user.id and message.from_user.id != cfg.ADMIN_ID:
        admins = await bot.get_chat_administrators(message.chat.id)
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if member not in admins:
            await message.reply("❌ У вас недостаточно прав.")
            return

    global message_log
    try:
        message_log[message.chat.id] = []
        await message.reply("✅ Память очищена.")
        logger.info(f"Memory reset for {message.chat.id}")
    except KeyError:
        await message.reply("✅ Память уже пуста.")


@dp.message(Command("partialreset"))
async def partial_reset_command(message: Message) -> None:
    if not message.chat.id == message.from_user.id and message.from_user.id != cfg.ADMIN_ID:
        admins = await bot.get_chat_administrators(message.chat.id)
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if member not in admins:
            await message.reply("❌ У вас недостаточно прав.")
            return

    global message_log
    new_msglog = []
    for stored_message in message_log[message.chat.id]:
        if not stored_message.startswith("You: "):
            new_msglog.append(stored_message)
    message_log[message.chat.id] = new_msglog
    await message.reply("✅ Память частично очищена - бот не помнит, как отвечал вам.")


@dp.message(CommandStart())
async def start_command(message: Message) -> None:
    if message.chat.id == message.from_user.id:
        await message.reply("👋")
        await asyncio.sleep(2)
        await message.reply(f"""{html.bold("Привет!")}
🤖 Я - бот Gemini. Чтобы задать вопрос - просто напиши мне сообщение. {html.italic(
            "(в чате нужно либо ответить на моё сообщение, либо упомянуть меня через @)")}
🔔 Проверить статус бота можно {html.link("тут", "https://t.me/aetherlounge/2")} или через /status
💬 Сбросить мою память - /reset {html.italic("(в чате - только администраторы)")}
""",
                            disable_web_page_preview=True)


@dp.message(Command("broadcast"))
async def broadcast(message: Message) -> None:
    if not message.from_user.id == cfg.ADMIN_ID:
        return
    if not message.from_user.id == message.chat.id:
        return

    text = message.text.replace("/broadcast ", "💬 Сообщение от разработчика бота: ")
    for chat_id in message_log.keys():
        if not str(chat_id).startswith("-100"):
            continue
        try:
            await bot.send_message(chat_id, text)
            logger.success(f"Broadcast to {chat_id}")
        except Exception as error:
            logger.error(f"Failed to broadcast to {chat_id} - {str(error)}")


@dp.message(Command("status"))
async def status_command(message: Message) -> None:
    response_text = f"""✅ Бот активен!

💬 Контекст: {len(message_log[message.chat.id])}/{cfg.MEMORY_LIMIT_MESSAGES} сообщений
🆔 ID чата: {message.chat.id}"""
    await message.reply(response_text)


@dp.message(Command("reload"))
async def reload_command(message: Message) -> None:
    if not message.from_user.id == cfg.ADMIN_ID:
        return
    if not message.from_user.id == message.chat.id:
        return

    importlib.reload(cfg)
    logger.info("Configuration reloaded")

    global base_prompt
    if os.path.exists(cfg.DATA_FOLDER + "prompt.txt"):
        with open(cfg.DATA_FOLDER + "prompt.txt", "r") as pf:
            base_prompt = pf.read()
    else:
        logger.exception("prompt.txt not found!")

    logger.info("Prompt reloaded")


@dp.message(Command("directsend"))
async def directsend_command(message: Message) -> None:
    if not message.from_user.id == cfg.ADMIN_ID:
        return
    if not message.from_user.id == message.chat.id:
        return

    a = message.text.split(" ")
    a.pop(0)
    target_chat_id = a.pop(0)
    await bot.send_message(target_chat_id, " ".join(a))


@dp.message()
async def main_message_handler(message: Message) -> None:
    if not message.text:
        return
    if message.text.startswith("/"):
        return
    await append_to_message_log(message)
    text_content = message.text
    if self_entity.username in text_content or (
            message.reply_to_message and message.reply_to_message.from_user.id == self_entity.id) or (
            message.from_user.id == message.chat.id):
        out = await ask_gemini(message)
        try:
            await message.reply(html.quote(out))
        except Exception as error:
            logger.error(f"Failed to send response to {message.chat.id} - {str(error)}")

    global message_counter
    message_counter += 1
    if message_counter % 50 == 0:
        save()


async def main():
    global self_entity
    self_entity = await bot.get_me()

    await bot.delete_webhook(drop_pending_updates=True)

    if cfg.ENABLE_PERMA_MEMORY and os.path.exists(cfg.DATA_FOLDER + "chats.pki"):
        logger.info("Loading saved message log...")
        global message_log
        with open(cfg.DATA_FOLDER + "chats.pki", "rb") as file:
            message_log = pickle.load(file)
        logger.success("Loaded.")

    logger.success("Working...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Crashed: {str(e)}")
        logger.error("Saving data to temp storage...")
        save("chats_crash.pki")
        exit()
    save()

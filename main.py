import asyncio
import importlib
import os
import pickle
import PIL.Image
from aiogram import Bot, Dispatcher, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, User, ReactionTypeEmoji
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted
from loguru import logger

import config as cfg

logger.add(cfg.DATA_FOLDER + "logs/log_{time}.log", rotation="1 day")
logger.debug("Initializing...")

dp = Dispatcher()
bot = Bot(cfg.TG_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))

self_entity: User
current_token_index = 0
message_log = {
    # chat_id: [message1, message2, ..., messageN]
}
banned_users = []

message_counter = 0
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

    logger.info("Saving banned users...")
    with open(cfg.DATA_FOLDER + "banned.pki", "wb") as le_file:
        pickle.dump(banned_users, le_file)
        logger.success("Banned users saved")


# ===================================
# Gemini API Part
# ===================================
def get_gemini_token():
    global current_token_index
    current_token_index += 1
    return cfg.GEMINI_TOKENS[current_token_index % len(cfg.GEMINI_TOKENS)]


async def simulate_typing(message: Message) -> None:
    while True:
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        await asyncio.sleep(4)


async def query_api(prompt: str, photo: bytes = None):
    genai.configure(api_key=get_gemini_token())
    model = genai.GenerativeModel("gemini-1.5-pro-latest")
    safety = {
        "SEXUALLY_EXPLICIT": "block_none",
        "HARASSMENT": "block_none",
        "HATE_SPEECH": "block_none",
        "DANGEROUS": "block_none",
    }
    try:
        if not photo:
            response = await model.generate_content_async(prompt, safety_settings=safety)
        else:
            response = await model.generate_content_async([prompt, photo], safety_settings=safety)

        return response
    except Exception as error:
        logger.error(error)
        return None


async def ask_gemini(message: Message, photo_file_id: str) -> str:
    global message_log
    logger.info(
        f"Generating for {message.from_user.id} in {message.chat.id}. Context: {len(message_log[message.chat.id])}")
    all_messages = "\n".join(message_log[message.chat.id])

    prompt = base_prompt.format(
        chat_type="direct message (DM)" if message.from_user.id == message.chat.id else "group",
        all_messages=all_messages, target_message=message_log[message.chat.id][-1],
        image_warning="\n- This message contains an image. Analyze it thoroughly and MAKE SURE you describe it in your "
                      "response verbosely. It will NOT be shown again, so you will use your own text description "
                      "later on. Start your response with \"This image contains\" in User's language. After giving an "
                      "extended description to the picture, proceed to fulfill the User's request. Once again, "
                      "ALWAYS speak in the language the User is talking to you, EVEN WHEN DESCRIBING THE PICTURE." if
        photo_file_id else "")

    if photo_file_id:
        logger.debug("Working with an image...")

        filename = "/cache/" + photo_file_id + ".jpg"

        if not os.path.exists(filename):
            logger.debug(f"Saving image to {filename}...")
            await bot.download(photo_file_id, destination=filename)

        logger.debug(f"Loading {filename}...")
        photo = PIL.Image.open(filename)
    else:
        photo = None

    api_task = asyncio.create_task(query_api(prompt, photo))
    typer_task = asyncio.create_task(simulate_typing(message))

    response = await api_task
    typer_task.cancel()
    try:
        await typer_task
    except asyncio.CancelledError:
        pass

    try:
        output = response.text
        output = output[:-1].replace("  ", " ")
        current_list = message_log[message.chat.id]
        current_list.append("You: " + output)
        if len(current_list) > cfg.MEMORY_LIMIT_MESSAGES:
            current_list.pop(0)
        message_log[message.chat.id] = current_list
        logger.success(
            f"Generated for {message.from_user.id} in {message.chat.id}. Context: {len(message_log[message.chat.id])}")
    except Exception as error:
        logger.error("Failed to generate message. Exception: " + str(error))
        logger.debug(response.prompt_feedback)
        output = "❌ Произошел сбой Gemini API."
        if response.prompt_feedback.block_reason:
            output = "❌ Запрос был заблокирован цензурой Gemini API."
        current_list = message_log[message.chat.id]
        current_list.append("You: *Failed to reply due to an error. Be better next time.*")
        if len(current_list) > cfg.MEMORY_LIMIT_MESSAGES:
            current_list.pop(0)
        message_log[message.chat.id] = current_list

    return output


# ===================================
# Message Handling Part
# ===================================
async def no_markdown(text: str) -> str:
    """
    Strips the text of any markdown-related characters.
    Called if the Telegram API doesn't accept our output.
    """
    forbidden_characters = ['*', '_', ']', '[', '`', '\\']
    for character in forbidden_characters:
        text = text.replace(character, '')
    return text


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
    """
    Shorten string to given max length
    ("The quick brown fox jumped over the lazy dog", 30) -> "The quick ... lazy dog"
    """
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


async def get_message_text_from_message(message: Message, recursion: bool = False) -> str:
    user_display = message.from_user.first_name
    if message.from_user.username and message.from_user.username != message.from_user.first_name:
        user_display += f" ({message.from_user.username})"

    if message.text:
        text_content = message.text
    elif message.caption:
        text_content = "[IMAGE ATTACHED] " if not recursion else "" + message.caption
    else:
        text_content = "*No Text*"

    if recursion:
        return text_content

    text = f"{user_display}: {text_content}"

    if message.reply_to_message:
        reply_text = await get_message_text_from_message(message.reply_to_message, True)
        text = f"[REPLYING TO: {format_reply_text(reply_text)}] " + text

    if self_entity.username in text_content or (
            message.reply_to_message and message.reply_to_message.from_user.id == self_entity.id) or (
            message.from_user.id == message.chat.id):
        text = "*** " + text

    return text


@dp.message(Command("reset"))
async def reset_command(message: Message) -> None:
    global message_log
    try:
        message_log[message.chat.id] = []
        await message.reply("✅ Память очищена.")
        logger.info(f"Memory reset for {message.chat.id}")
    except KeyError:
        await message.reply("✅ Память уже пуста.")


@dp.message(Command("partialreset"))
async def partial_reset_command(message: Message) -> None:
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
        await message.reply(f"""Привет! 🤖 Я - бот Gemini. Чтобы задать вопрос - просто напиши мне сообщение. (в чате 
        нужно либо ответить на моё сообщение, либо упомянуть меня через @) 🔔 Проверить статус бота можно [тут](
        https://t.me/aetherlounge/2) или через /status 💬 Сбросить мою память - /reset (в чате - только 
        администраторы) """, disable_web_page_preview=True)


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
    text = f"""✅ Бот активен!

💬 Контекст: {len(message_log[message.chat.id])}/{cfg.MEMORY_LIMIT_MESSAGES} сообщений (⏱ Секунду...)
🆔 ID чата: `{message.chat.id}`"""

    our_reply = await message.reply(text)

    all_messages = "\n".join(message_log[message.chat.id])
    genai.configure(api_key=get_gemini_token())
    model = genai.GenerativeModel("gemini-1.5-pro-latest")
    try:
        token_count = (await model.count_tokens_async(all_messages)).total_tokens
    except Exception:
        token_count = 0

    text = text.replace("⏱ Секунду...", f"токенов: {token_count}")
    await our_reply.edit_text(text)


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


@dp.message(Command("fuck"))
async def ban(message: Message) -> None:
    if not message.from_user.id == cfg.ADMIN_ID:
        return

    global banned_users
    banned_users.append(int(message.text.split(" ")[1]))
    logger.debug(f"Banned {message.text.split(' ')[1]}")


@dp.message(Command("unfuck"))
async def unban(message: Message) -> None:
    if not message.from_user.id == cfg.ADMIN_ID:
        return

    global banned_users
    banned_users.remove(int(message.text.split(" ")[1]))
    logger.debug(f"Unbanned {message.text.split(' ')[1]}")


@dp.message()
async def main_message_handler(message: Message) -> None:
    if message.from_user.id in banned_users:
        return

    if (message.text and message.text.startswith("/")) or (message.caption and message.caption.startswith("/")):
        return

    if message.text:
        text = message.text
    elif message.caption:
        text = message.caption
    else:
        return

    await append_to_message_log(message)

    # If mentioned
    if self_entity.username in text or (
            message.reply_to_message and message.reply_to_message.from_user.id == self_entity.id) or (
            message.from_user.id == message.chat.id):

        # Looking for an image
        if message.photo:
            photo_id = message.photo[-1].file_id
        elif not message.photo and message.reply_to_message and message.reply_to_message.photo:
            photo_id = message.reply_to_message.photo[-1].file_id
        else:
            photo_id = None

        # Generating response
        try:
            out = await ask_gemini(message, photo_id)
        except ResourceExhausted:
            await message.reply("❌ Бот перегружен. Подождите пару минут.")
            return

        # Sending the response
        try:
            logger.debug(out)
            await message.reply(out)
        except TelegramRetryAfter:
            logger.error(f"Flood wait! Requester: {message.from_user.id} | Chat: {message.chat.id}")
            await message.react([ReactionTypeEmoji(emoji="🤯")])
            return
        except Exception as error:
            logger.error(f"Failed to send response to {message.chat.id}!")
            logger.error(error)
            await message.reply(await no_markdown(text))

    global message_counter
    message_counter += 1
    if message_counter % 50 == 0:
        save()


async def main():
    global self_entity
    self_entity = await bot.get_me()

    await bot.delete_webhook(drop_pending_updates=True)

    if cfg.ENABLE_PERMA_MEMORY:
        global message_log, banned_users
        if os.path.exists(cfg.DATA_FOLDER + "chats.pki"):
            logger.info("Loading saved message log...")
            with open(cfg.DATA_FOLDER + "chats.pki", "rb") as file:
                message_log = pickle.load(file)

        if os.path.exists(cfg.DATA_FOLDER + "banned.pki"):
            logger.info("Loading banned users...")
            with open(cfg.DATA_FOLDER + "banned.pki", "rb") as file:
                banned_users = pickle.load(file)
            logger.debug(f"Banned: {banned_users}")
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

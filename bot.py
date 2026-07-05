# -*- coding: utf-8 -*-
"""
Бот-экзамен на админку для проекта "Аурелия".

Логика:
1. /start - приветствие + правила оформления постов и поведения админа.
   Пользователь должен нажать "Согласен(на)", иначе тест не начнется.
2. Бот задает случайную выборку вопросов с вариантами ответа (авто-проверка,
   баллы начисляются сразу и известны проверяющим).
3. Бот задает несколько открытых вопросов (ответ текстом, для ручной оценки
   админами - авто-баллы по ним не начисляются, но лимит баллов известен заранее).
4. По итогам бот формирует отчет: кто проходил, сколько баллов набрано
   автоматически из скольки возможных, разбивка по темам, ответы на открытые
   вопросы - и отправляет этот отчет в группу проверяющих (ADMIN_GROUP_ID).
5. Пользователю приходит подтверждение, что результаты отправлены на проверку.

Запуск:
    pip install -r requirements.txt
    export BOT_TOKEN="токен_от_BotFather"
    python bot.py
"""

import asyncio
import logging
import random
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from questions import MC_QUESTIONS, OPEN_QUESTIONS, SHORT_QUESTIONS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aurelia_exam_bot")

router = Router()


def fix_dashes(text: str) -> str:
    """Заменяет длинное тире и en-dash на обычный дефис во всем тексте бота."""
    return text.replace("—", "-").replace("–", "-")


# ---------------------------------------------------------------------------
# Тексты
# ---------------------------------------------------------------------------

WELCOME_TEXT = fix_dashes("""
🏳️ ЭКЗАМЕН НА АДМИНКУ АУРЕЛИИ 🏳️

Привет! Это небольшой тест, который проходит каждый, кто хочет получить админку в проекте.

Ты подтверждаешь, что ознакомлен(а) с правилами оформления постов и поведения админа в проекте и готов(а) пройти тест.

Дальше будет несколько вопросов с вариантами ответа - баллы за них начисляются автоматически. Результат вместе с баллами уйдет в закрытую группу админов - именно они принимают финальное решение, выдавать админку или нет.

Нажми "Согласен(на)" ниже, чтобы начать тест.
""").strip()

DONE_TEXT = fix_dashes("""
✅ Тест пройден!

Твои ответы и баллы отправлены в группу проверяющих. Админы посмотрят результат и примут решение - жди обратной связи.

Спасибо, что нашел(нашла) время пройти экзамен на админку Аурелии!
""").strip()

CANCEL_TEXT = fix_dashes("""
Хорошо, тест отменен. Если передумаешь - просто отправь /start заново.
""").strip()

APPROVED_TEXT = fix_dashes("""
🎉 Поздравляем! По итогам экзамена тебе выдана админка в проекте "Аурелия".

Добро пожаловать в команду!
""").strip()

REJECTED_TEXT = fix_dashes("""
По итогам экзамена админы решили пока не выдавать тебе админку в проекте "Аурелия".

Если хочешь - можно попробовать пройти тест снова позже.
""").strip()


# ---------------------------------------------------------------------------
# Состояния
# ---------------------------------------------------------------------------

class ExamStates(StatesGroup):
    waiting_agreement = State()
    in_mc_test = State()
    in_short_test = State()
    in_open_test = State()


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def build_agreement_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Согласен(на), начать тест", callback_data="agree")
    kb.button(text="❌ Отмена", callback_data="disagree")
    kb.adjust(1)
    return kb.as_markup()


def truncate_button_text(text: str, max_len: int = 42) -> str:
    """Подстраховка на случай, если кто-то добавит слишком длинный вариант ответа.

    Все варианты в questions.py уже вручную сжаты и укладываются в этот лимит,
    так что в норме эта функция ничего не обрезает - она лишь не даст будущим
    длинным вопросам сломать вид кнопки.
    """
    text = fix_dashes(text)
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]  # не резать слово посередине
    return truncated.rstrip(" ,.-") + "…"


def build_mc_keyboard(q_index: int, options: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        kb.button(text=truncate_button_text(opt), callback_data=f"ans:{q_index}:{i}")
    kb.adjust(1)
    return kb.as_markup()


def build_decision_kb(user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"decision:approve:{user_id}")
    kb.button(text="❌ Отклонить", callback_data=f"decision:reject:{user_id}")
    kb.adjust(2)
    return kb.as_markup()


def format_mc_question_text(number: int, total: int, question: str) -> str:
    return fix_dashes(f"Вопрос {number}/{total}:\n\n{question}")


def format_open_question_text(number: int, total: int, question: str, max_points: int) -> str:
    return fix_dashes(
        f"Открытый вопрос {number}/{total} (максимум {max_points} баллов, оценивают проверяющие):\n\n{question}"
    )


def format_short_question_text(number: int, total: int, question: str) -> str:
    return fix_dashes(f"Короткий вопрос {number}/{total} (ответь одним словом или короткой фразой):\n\n{question}")


def normalize_short_answer(text: str) -> str:
    return text.strip().lower().replace("ё", "е")


def check_short_answer(user_text: str, accepted_answers: list[str]) -> bool:
    norm = normalize_short_answer(user_text)
    return norm in (normalize_short_answer(a) for a in accepted_answers)


async def send_next_short_question(message_or_cb, state: FSMContext):
    data = await state.get_data()
    short_questions = data["short_questions"]
    idx = data["short_index"]

    question = short_questions[idx]
    text = format_short_question_text(idx + 1, len(short_questions), question["question"])

    if isinstance(message_or_cb, CallbackQuery):
        await message_or_cb.message.answer(text)
    else:
        await message_or_cb.answer(text)


async def send_next_mc_question(message_or_cb, state: FSMContext):
    data = await state.get_data()
    mc_questions = data["mc_questions"]
    idx = data["mc_index"]

    question = mc_questions[idx]
    text = format_mc_question_text(idx + 1, len(mc_questions), question["question"])
    kb = build_mc_keyboard(idx, question["options"])

    if isinstance(message_or_cb, CallbackQuery):
        await message_or_cb.message.answer(text, reply_markup=kb)
    else:
        await message_or_cb.answer(text, reply_markup=kb)


async def send_next_open_question(message: Message, state: FSMContext):
    data = await state.get_data()
    open_questions = data["open_questions"]
    idx = data["open_index"]

    question = open_questions[idx]
    text = format_open_question_text(
        idx + 1, len(open_questions), question["question"], question["max_points"]
    )
    await message.answer(text)


def format_duration(seconds: float) -> str:
    total_seconds = int(seconds)
    minutes, secs = divmod(total_seconds, 60)
    return f"{minutes} мин {secs} сек"


def build_report(user, data: dict, elapsed_seconds: float | None = None) -> str:
    mc_questions = data["mc_questions"]
    mc_log = data["mc_log"]
    short_questions = data["short_questions"]
    short_log = data["short_log"]
    open_questions = data["open_questions"]
    open_answers = data["open_answers"]

    total_earned = sum(entry["points_earned"] for entry in mc_log) + sum(
        entry["points_earned"] for entry in short_log
    )
    total_possible = sum(q["points"] for q in mc_questions) + sum(q["points"] for q in short_questions)
    open_possible = sum(q["max_points"] for q in open_questions)

    username = f"@{user.username}" if user.username else "(нет username)"
    full_name = user.full_name or "Без имени"

    lines = []
    lines.append("📋 РЕЗУЛЬТАТ ЭКЗАМЕНА НА АДМИНКУ")
    lines.append("")
    lines.append(f"Кандидат: {full_name} {username}")
    lines.append(f"ID: {user.id}")
    if elapsed_seconds is not None:
        lines.append(f"⏱ Время прохождения теста: {format_duration(elapsed_seconds)}")
    lines.append("")
    lines.append(f"АВТО-БАЛЛЫ (вопросы с вариантами + короткие вопросы): {total_earned} из {total_possible}")
    lines.append("")
    lines.append("Разбивка по вопросам с вариантами ответа:")
    for entry in mc_log:
        mark = "✅" if entry["correct"] else "❌"
        lines.append(
            f"{mark} [{entry['topic']}] {entry['question']}\n"
            f"   Ответ кандидата: {entry['chosen']}\n"
            f"   Правильный ответ: {entry['correct_option']}\n"
            f"   Баллы: {entry['points_earned']}/{entry['points_max']}"
        )
    if short_log:
        lines.append("")
        lines.append("Разбивка по коротким вопросам:")
        for entry in short_log:
            mark = "✅" if entry["correct"] else "❌"
            lines.append(
                f"{mark} [{entry['topic']}] {entry['question']}\n"
                f"   Ответ кандидата: {entry['chosen']}\n"
                f"   Правильный ответ: {entry['correct_option']}\n"
                f"   Баллы: {entry['points_earned']}/{entry['points_max']}"
            )
    lines.append("")
    lines.append(f"ОТКРЫТЫЕ ВОПРОСЫ (ручная оценка, максимум {open_possible} баллов суммарно):")
    for q, ans in zip(open_questions, open_answers):
        lines.append(f"- [{q['topic']}] {q['question']} (макс. {q['max_points']} баллов)")
        lines.append(f"  Ответ: {ans}")
    lines.append("")
    lines.append(
        f"ИТОГО ИЗВЕСТНО ПРОВЕРЯЮЩИМ: {total_earned}/{total_possible} авто-баллов "
        f"+ до {open_possible} баллов на усмотрение админов."
    )

    report = "\n".join(lines)
    return fix_dashes(report)


# ---------------------------------------------------------------------------
# Хендлеры
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME_TEXT, reply_markup=build_agreement_kb())
    await state.set_state(ExamStates.waiting_agreement)


@router.callback_query(ExamStates.waiting_agreement, F.data == "disagree")
async def on_disagree(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(CANCEL_TEXT)
    await callback.answer()


@router.callback_query(ExamStates.waiting_agreement, F.data == "agree")
async def on_agree(callback: CallbackQuery, state: FSMContext):
    mc_pool = MC_QUESTIONS.copy()
    short_pool = SHORT_QUESTIONS.copy()
    open_pool = OPEN_QUESTIONS.copy()

    random.shuffle(mc_pool)
    random.shuffle(short_pool)
    random.shuffle(open_pool)

    mc_selected = mc_pool[: min(config.NUM_MC_QUESTIONS, len(mc_pool))]
    short_selected = short_pool[: min(config.NUM_SHORT_QUESTIONS, len(short_pool))]
    open_selected = open_pool[: min(config.NUM_OPEN_QUESTIONS, len(open_pool))]

    # Перемешиваем варианты ответа внутри каждого вопроса, сохраняя правильный индекс
    prepared_mc = []
    for q in mc_selected:
        options = list(enumerate(q["options"]))  # [(original_index, text), ...]
        random.shuffle(options)
        new_options = [text for _, text in options]
        new_correct = [i for i, (orig_idx, _) in enumerate(options) if orig_idx == q["correct"]][0]
        prepared_mc.append(
            {
                "question": q["question"],
                "options": new_options,
                "correct": new_correct,
                "points": q["points"],
                "topic": q["topic"],
            }
        )

    await state.update_data(
        mc_questions=prepared_mc,
        mc_index=0,
        mc_log=[],
        short_questions=short_selected,
        short_index=0,
        short_log=[],
        open_questions=open_selected,
        open_index=0,
        open_answers=[],
        start_time=datetime.now(timezone.utc).isoformat(),
    )

    await callback.message.edit_text(fix_dashes("Отлично! Начинаем тест 👇"))
    await state.set_state(ExamStates.in_mc_test)
    await send_next_mc_question(callback, state)
    await callback.answer()


@router.callback_query(ExamStates.in_mc_test, F.data.startswith("ans:"))
async def on_mc_answer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mc_questions = data["mc_questions"]
    idx = data["mc_index"]

    _, q_index_str, opt_index_str = callback.data.split(":")
    q_index = int(q_index_str)
    opt_index = int(opt_index_str)

    if q_index != idx:
        # устаревшая кнопка от прошлого вопроса - игнорируем
        await callback.answer("Этот вопрос уже пройден.", show_alert=False)
        return

    question = mc_questions[idx]
    is_correct = opt_index == question["correct"]
    points_earned = question["points"] if is_correct else 0

    mc_log = data["mc_log"]
    mc_log.append(
        {
            "question": question["question"],
            "topic": question["topic"],
            "chosen": question["options"][opt_index],
            "correct_option": question["options"][question["correct"]],
            "correct": is_correct,
            "points_earned": points_earned,
            "points_max": question["points"],
        }
    )

    # Не сообщаем пользователю, правильный это ответ или нет - это известно только проверяющим.
    await callback.message.edit_text(
        fix_dashes(
            f"{callback.message.text}\n\n"
            f"Ответ принят: {question['options'][opt_index]}"
        )
    )

    new_idx = idx + 1
    await state.update_data(mc_log=mc_log, mc_index=new_idx)

    if new_idx < len(mc_questions):
        await send_next_mc_question(callback, state)
    else:
        short_questions = data["short_questions"]
        open_questions = data["open_questions"]
        if short_questions:
            await callback.message.answer(
                fix_dashes(
                    "С вопросами по вариантам ответа покончено 🎉\n\n"
                    "Теперь пара коротких вопросов - отвечай одним словом или короткой фразой."
                )
            )
            await state.set_state(ExamStates.in_short_test)
            await send_next_short_question(callback, state)
        elif open_questions:
            await callback.message.answer(
                fix_dashes(
                    "С вопросами по вариантам ответа покончено 🎉\n\n"
                    "Теперь несколько открытых вопросов - отвечай текстом одним сообщением на каждый."
                )
            )
            await state.set_state(ExamStates.in_open_test)
            await send_next_open_question(callback.message, state)
        else:
            await finish_exam(callback.message, callback.from_user, state)

    await callback.answer()


async def finish_exam(message: Message, user, state: FSMContext):
    """Формирует и отправляет итоговый отчет в группу проверяющих."""
    data = await state.get_data()

    elapsed_seconds = None
    start_time_str = data.get("start_time")
    if start_time_str:
        try:
            start_time = datetime.fromisoformat(start_time_str)
            elapsed_seconds = (datetime.now(timezone.utc) - start_time).total_seconds()
        except ValueError:
            elapsed_seconds = None

    report = build_report(user, data, elapsed_seconds)

    bot: Bot = message.bot
    try:
        await bot.send_message(
            config.ADMIN_GROUP_ID, report, reply_markup=build_decision_kb(user.id)
        )
    except Exception as e:
        log.exception("Не удалось отправить отчет в группу проверяющих: %s", e)
        await message.answer(
            fix_dashes(
                "⚠️ Тест пройден, но не удалось отправить отчет админам автоматически "
                "(проверь, что бот добавлен в группу проверяющих и имеет права писать туда)."
            )
        )
        await state.clear()
        return

    await message.answer(DONE_TEXT)
    await state.clear()


@router.message(ExamStates.in_short_test)
async def on_short_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    short_questions = data["short_questions"]
    idx = data["short_index"]
    short_log = data["short_log"]

    question = short_questions[idx]
    user_answer = (message.text or "").strip()
    is_correct = check_short_answer(user_answer, question["answers"])
    points_earned = question["points"] if is_correct else 0

    short_log.append(
        {
            "question": question["question"],
            "topic": question["topic"],
            "chosen": user_answer or "(пустой ответ)",
            "correct_option": question["answers"][0],
            "correct": is_correct,
            "points_earned": points_earned,
            "points_max": question["points"],
        }
    )

    # Не сообщаем пользователю, правильный это ответ или нет.
    await message.answer(fix_dashes(f"Ответ принят: {user_answer or '(пустой ответ)'}"))

    new_idx = idx + 1
    await state.update_data(short_log=short_log, short_index=new_idx)

    if new_idx < len(short_questions):
        await send_next_short_question(message, state)
        return

    open_questions = data["open_questions"]
    if open_questions:
        await message.answer(
            fix_dashes(
                "С короткими вопросами покончено 🎉\n\n"
                "Теперь несколько открытых вопросов - отвечай текстом одним сообщением на каждый."
            )
        )
        await state.set_state(ExamStates.in_open_test)
        await send_next_open_question(message, state)
        return

    await finish_exam(message, message.from_user, state)


@router.message(ExamStates.in_open_test)
async def on_open_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    open_answers = data["open_answers"]
    idx = data["open_index"]
    open_questions = data["open_questions"]

    open_answers.append(message.text or "(пустой ответ)")
    new_idx = idx + 1
    await state.update_data(open_answers=open_answers, open_index=new_idx)

    if new_idx < len(open_questions):
        await send_next_open_question(message, state)
        return

    await finish_exam(message, message.from_user, state)


@router.callback_query(F.data.startswith("decision:"))
async def on_admin_decision(callback: CallbackQuery):
    # Реагируем только на нажатия внутри группы проверяющих
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer()
        return

    _, action, user_id_str = callback.data.split(":")
    candidate_id = int(user_id_str)

    current_text = callback.message.text or ""
    if "РЕШЕНИЕ:" in current_text:
        await callback.answer("Решение по этому кандидату уже принято.", show_alert=True)
        return

    admin = callback.from_user
    admin_name = f"@{admin.username}" if admin.username else (admin.full_name or "аноним")

    if action == "approve":
        decision_line = f"РЕШЕНИЕ: ✅ ОДОБРЕНО (принял(а) {admin_name})"
        candidate_message = APPROVED_TEXT
    else:
        decision_line = f"РЕШЕНИЕ: ❌ ОТКЛОНЕНО (принял(а) {admin_name})"
        candidate_message = REJECTED_TEXT

    new_text = fix_dashes(f"{current_text}\n\n{decision_line}")

    try:
        await callback.message.edit_text(new_text, reply_markup=None)
    except Exception as e:
        log.exception("Не удалось обновить сообщение с решением: %s", e)
        await callback.answer(f"Не удалось обновить отчет: {e}", show_alert=True)

    bot: Bot = callback.bot
    try:
        await bot.send_message(candidate_id, candidate_message)
    except TelegramForbiddenError:
        log.warning(
            "Кандидат %s заблокировал бота или ни разу не писал ему в личку.", candidate_id
        )
        await callback.answer(
            "Решение сохранено, но кандидат заблокировал бота (или ни разу не открывал с ним личный чат) - написать ему не получилось.",
            show_alert=True,
        )
        return
    except TelegramBadRequest as e:
        log.exception("Telegram отклонил отправку кандидату %s: %s", candidate_id, e)
        await callback.answer(f"Решение сохранено, но Telegram отклонил сообщение: {e}", show_alert=True)
        return
    except Exception as e:
        log.exception("Не удалось отправить решение кандидату %s: %s", candidate_id, e)
        await callback.answer(f"Решение сохранено, но написать кандидату не удалось: {e}", show_alert=True)
        return

    await callback.answer("Решение отправлено кандидату.")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main():
    if config.BOT_TOKEN == "ВСТАВЬ_СЮДА_ТОКЕН_БОТА":
        raise RuntimeError(
            "Не задан токен бота. Укажи его в config.py или через переменную окружения BOT_TOKEN."
        )

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    log.info("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

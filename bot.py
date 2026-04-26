import os
import json
import sqlite3
import asyncio
import logging
from datetime import datetime, time
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
import anthropic

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
YOUR_TELEGRAM_ID = int(os.environ["YOUR_TELEGRAM_ID"])

MORNING_HOUR = 9   # час отправки новых глаголов (UTC+1 = Portugal)
EVENING_HOUR = 20  # час отправки заданий

DB_PATH = "progress.db"
VERBS_PATH = "verbs.json"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Database ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day INTEGER NOT NULL,
            verb TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS exercise_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day INTEGER NOT NULL,
            exercise_type TEXT NOT NULL,  -- 'fill_in' or 'full_form'
            sent_at TEXT NOT NULL,
            answered INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def get_current_day() -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT MAX(day) FROM progress")
    row = c.fetchone()
    conn.close()
    return row[0] if row[0] else 0

def get_verbs_for_day(day: int) -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT verb FROM progress WHERE day = ?", (day,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_all_learned_verbs() -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT verb FROM progress ORDER BY day")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def save_day_verbs(day: int, verbs: list[str]):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().isoformat()
    for verb in verbs:
        c.execute("INSERT INTO progress (day, verb, sent_at) VALUES (?, ?, ?)",
                  (day, verb, now))
    conn.commit()
    conn.close()

def get_next_exercise_type(day: int) -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT exercise_type FROM exercise_log WHERE day < ? ORDER BY day DESC LIMIT 1", (day,))
    row = c.fetchone()
    conn.close()
    if not row or row[0] == "full_form":
        return "fill_in"
    return "full_form"

def log_exercise(day: int, exercise_type: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO exercise_log (day, exercise_type, sent_at) VALUES (?, ?, ?)",
              (day, exercise_type, datetime.now().isoformat()))
    conn.commit()
    conn.close()

# ─── Verbs ─────────────────────────────────────────────────────────────────────
def load_verbs() -> list[dict]:
    with open(VERBS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def get_next_two_verbs(already_learned: list[str]) -> list[dict]:
    all_verbs = load_verbs()
    remaining = [v for v in all_verbs if v["verb"] not in already_learned]
    if not remaining:
        return []

    # Try to find a verb whose family partner is also not yet learned → send them together
    family_groups = {}
    for v in remaining:
        fam = v.get("family", v["verb"])
        family_groups.setdefault(fam, []).append(v)

    # Pick the first family that has 2 members still remaining → send as a pair
    for fam, members in family_groups.items():
        if len(members) >= 2:
            return members[:2]

    # If no complete family pair left, just send next 2 by order
    return sorted(remaining, key=lambda v: v.get("order", 99))[:2]

def format_verb_card(verb_data: dict) -> str:
    v = verb_data["verb"]
    t = verb_data["tenses"]
    lines = [f"📚 *{v.upper()}* — {verb_data['translation_ru']}"]
    lines.append(f"_({verb_data.get('type', '')})_")

    if verb_data.get("comment_ru"):
        lines.append(f"\n💡 {verb_data['comment_ru']}")

    if verb_data.get("family_note"):
        lines.append(f"🔗 _{verb_data['family_note']}_")

    lines.append("")

    tense_names = {
        "presente": "🔵 Presente",
        "preterito_perfeito": "🟠 Pretérito Perfeito",
        "preterito_imperfeito": "🟣 Pretérito Imperfeito"
    }
    pronouns_display = ["eu", "tu", "ele/ela", "nós", "eles/elas"]
    indices_display = [0, 1, 2, 3, 5]

    for tense_key, tense_label in tense_names.items():
        if tense_key in t:
            forms = t[tense_key]
            lines.append(f"*{tense_label}*")
            for pronoun, idx in zip(pronouns_display, indices_display):
                if idx < len(forms) and forms[idx] != "-":
                    lines.append(f"  {pronoun} → _{forms[idx]}_")
            lines.append("")

    if verb_data.get("atenção"):
        lines.append(f"⚠️ *Atenção:* {verb_data['atenção']}")
        lines.append("")

    if verb_data.get("examples"):
        lines.append("💬 *Exemplos:*")
        for ex in verb_data["examples"]:
            lines.append(f"• {ex['pt']}")
            lines.append(f"  _{ex['ru']}_")

    return "\n".join(lines)

# ─── Claude AI ─────────────────────────────────────────────────────────────────
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def generate_exercise(verbs: list[str], exercise_type: str, all_verb_data: list[dict]) -> str:
    verb_info = []
    for v in verbs:
        for vd in all_verb_data:
            if vd["verb"] == v:
                verb_info.append(vd)
                break

    # Detect family pairs for contrast exercises
    families = {}
    for vd in verb_info:
        fam = vd.get("family", vd["verb"])
        families.setdefault(fam, []).append(vd["verb"])
    contrast_pairs = [members for members in families.values() if len(members) >= 2]
    contrast_note = ""
    if contrast_pairs:
        pairs_str = ", ".join([f"{p[0]}/{p[1]}" for p in contrast_pairs])
        contrast_note = f"\nTenta incluir pelo menos um exercício de contraste entre: {pairs_str}."

    verb_summary = json.dumps(verb_info, ensure_ascii=False, indent=2)

    if exercise_type == "fill_in":
        instruction = (
            "Cria um exercício de preenchimento de lacunas em português europeu. "
            "Para cada verbo, escreve 1-2 frases com uma lacuna (_____) onde o utilizador deve escrever a forma correta. "
            "Indica entre parênteses o tempo verbal e o sujeito. "
            "Usa contextos variados: casa, trabalho, rotina, viagem, amigos, supermercado, saúde, tempo livre."
            + contrast_note
        )
    else:
        instruction = (
            "Cria um exercício de conjugação em português europeu. "
            "Para cada verbo, pede ao utilizador que escreva todas as formas (eu/tu/ele/nós/eles) de um tempo verbal específico. "
            "Varia os tempos verbais entre os diferentes verbos."
            + contrast_note
        )

    prompt = f"""Tens estes verbos com as suas conjugações:
{verb_summary}

{instruction}

Responde APENAS com o exercício, sem soluções. Em português europeu. Usa numeração clara (1., 2., 3...). Não uses mais de 2 frases por verbo."""

    message = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

def check_answers(user_answer: str, verbs: list[str], exercise_type: str, all_verb_data: list[dict]) -> str:
    verb_info = []
    for v in verbs:
        for vd in all_verb_data:
            if vd["verb"] == v:
                verb_info.append(vd)
                break

    verb_summary = json.dumps(verb_info, ensure_ascii=False, indent=2)

    prompt = f"""És um professor de português europeu. O aluno respondeu a um exercício.

Conjugações corretas dos verbos:
{verb_summary}

Resposta do aluno:
{user_answer}

Tipo de exercício: {"preenchimento de lacunas" if exercise_type == "fill_in" else "conjugação completa"}

Por favor:
1. Indica quais respostas estão corretas ✅ e quais estão erradas ❌
2. Para as erradas, mostra a forma correta
3. Dá uma nota de encorajamento no final
4. Responde em russo (но примеры оставь на португальском)"""

    message = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ─── Bot state (simple in-memory for current exercise) ─────────────────────────
user_state = {}  # { user_id: { "waiting_for_answer": bool, "exercise_type": str, "verbs": list } }

# ─── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != YOUR_TELEGRAM_ID:
        return
    await update.message.reply_text(
        "👋 Olá! Sou o teu bot de verbos portugueses.\n\n"
        "Comandos disponíveis:\n"
        "/hoje — receber os verbos de hoje\n"
        "/exercicio — fazer o exercício agora\n"
        "/progresso — ver quantos verbos já aprendeste\n"
        "/ajuda — ajuda"
    )

async def cmd_hoje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != YOUR_TELEGRAM_ID:
        return
    await send_morning_lesson(context.bot, YOUR_TELEGRAM_ID)

async def cmd_exercicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != YOUR_TELEGRAM_ID:
        return
    await send_evening_exercise(context.bot, YOUR_TELEGRAM_ID)

async def cmd_progresso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != YOUR_TELEGRAM_ID:
        return
    learned = get_all_learned_verbs()
    day = get_current_day()
    if not learned:
        await update.message.reply_text("Ainda não começaste! Usa /hoje para começar. 🌱")
    else:
        await update.message.reply_text(
            f"📊 *O teu progresso:*\n"
            f"Dia atual: {day}\n"
            f"Verbos aprendidos: {len(learned)}\n"
            f"Verbos: {', '.join(learned)}",
            parse_mode="Markdown"
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != YOUR_TELEGRAM_ID:
        return

    uid = update.effective_user.id
    state = user_state.get(uid, {})

    if state.get("waiting_for_answer"):
        await update.message.reply_text("⏳ A verificar as tuas respostas...")
        all_verb_data = load_verbs()
        feedback = check_answers(
            update.message.text,
            state["verbs"],
            state["exercise_type"],
            all_verb_data
        )
        user_state[uid] = {}
        await update.message.reply_text(feedback, parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "Usa /hoje para novos verbos ou /exercicio para praticar! 💪"
        )

# ─── Core logic ────────────────────────────────────────────────────────────────
async def send_morning_lesson(bot, chat_id: int):
    learned = get_all_learned_verbs()
    new_verbs = get_next_two_verbs(learned)

    if not new_verbs:
        await bot.send_message(chat_id, "🎉 Parabéns! Já aprendeste todos os verbos disponíveis!")
        return

    current_day = get_current_day() + 1
    save_day_verbs(current_day, [v["verb"] for v in new_verbs])

    await bot.send_message(chat_id, f"🌅 *Dia {current_day} — Novos verbos!*", parse_mode="Markdown")

    for verb_data in new_verbs:
        card = format_verb_card(verb_data)
        await bot.send_message(chat_id, card, parse_mode="Markdown")
        await asyncio.sleep(1)

    await bot.send_message(
        chat_id,
        "📝 Hoje às 15h vais receber um exercício com estes e todos os verbos anteriores. Boa sorte! 💪"
    )

async def send_evening_exercise(bot, chat_id: int):
    current_day = get_current_day()
    if current_day == 0:
        await bot.send_message(chat_id, "Ainda não recebeste verbos hoje! Usa /hoje primeiro. 🌱")
        return

    all_learned = get_all_learned_verbs()
    exercise_type = get_next_exercise_type(current_day)
    log_exercise(current_day, exercise_type)

    all_verb_data = load_verbs()

    await bot.send_message(chat_id, "⏰ *Hora de praticar!* A preparar o exercício...", parse_mode="Markdown")
    exercise_text = generate_exercise(all_learned, exercise_type, all_verb_data)

    type_label = "preenchimento de lacunas" if exercise_type == "fill_in" else "conjugação completa"
    await bot.send_message(
        chat_id,
        f"📝 *Exercício do dia {current_day}* ({type_label}):\n\n{exercise_text}\n\n"
        f"_Escreve as tuas respostas numa mensagem e eu vou verificar!"
    )

    user_state[YOUR_TELEGRAM_ID] = {
        "waiting_for_answer": True,
        "exercise_type": exercise_type,
        "verbs": all_learned
    }

# ─── Scheduled jobs ────────────────────────────────────────────────────────────
async def scheduled_morning(context: ContextTypes.DEFAULT_TYPE):
    await send_morning_lesson(context.bot, YOUR_TELEGRAM_ID)

async def scheduled_evening(context: ContextTypes.DEFAULT_TYPE):
    await send_evening_exercise(context.bot, YOUR_TELEGRAM_ID)

async def scheduled_answers(context: ContextTypes.DEFAULT_TYPE):
    """At 20:00 — if user hasn't answered today's exercise, send correct answers."""
    uid = YOUR_TELEGRAM_ID
    state = user_state.get(uid, {})
    if not state.get("waiting_for_answer"):
        return  # already answered, nothing to do

    verbs = state.get("verbs", [])
    exercise_type = state.get("exercise_type", "fill_in")
    if not verbs:
        return

    all_verb_data = load_verbs()
    verb_info = [vd for vd in all_verb_data if vd["verb"] in verbs]

    # Build answer sheet
    lines = ["📋 *Respostas de hoje:*\n"]
    pronouns_display = ["eu", "tu", "ele/ela", "nós", "eles/elas"]
    indices_display = [0, 1, 2, 3, 5]
    tense_names = {
        "presente": "Presente",
        "preterito_perfeito": "Pretérito Perfeito",
        "preterito_imperfeito": "Pretérito Imperfeito"
    }
    for vd in verb_info:
        lines.append(f"*{vd['verb'].upper()}*")
        for tense_key, tense_label in tense_names.items():
            forms = vd["tenses"].get(tense_key, [])
            if forms:
                forms_str = " / ".join(f for f in forms if f != "-")
                lines.append(f"  {tense_label}: _{forms_str}_")
        lines.append("")

    user_state[uid] = {}  # clear state
    await context.bot.send_message(
        uid,
        "\n".join(lines),
        parse_mode="Markdown"
    )

# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("hoje", cmd_hoje))
    app.add_handler(CommandHandler("exercicio", cmd_exercicio))
    app.add_handler(CommandHandler("progresso", cmd_progresso))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Portugal = UTC+1 (summer time)
    # 9:00 Portugal  = 8:00 UTC  → morning lesson
    # 15:00 Portugal = 14:00 UTC → exercise
    # 20:00 Portugal = 19:00 UTC → auto-answers if not replied
    job_queue = app.job_queue
    job_queue.run_daily(scheduled_morning, time=time(8, 0))
    job_queue.run_daily(scheduled_evening, time=time(14, 0))
    job_queue.run_daily(scheduled_answers, time=time(19, 0))

    logger.info("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()

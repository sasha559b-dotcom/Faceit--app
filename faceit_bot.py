import json
import os
import random
import time
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

# ════════════════════════════════════════════════
#                    НАСТРОЙКИ
# ════════════════════════════════════════════════

BOT_TOKEN   = "СЮДА_СВОЙ_ТОКЕН"   # ← вставь свой токен от @BotFather
ADMIN_IDS   = [5839642306]         # ← твои Telegram ID
DATA_FILE   = "faceit_db.json"

MAPS_LIST       = ["Dust2", "Inferno", "Mirage", "Nuke", "Overpass", "Anubis", "Vertigo"]
LOBBY_5V5_SIZE  = 10
LOBBY_2V2_SIZE  = 4
PICK_TIMEOUT    = 60   # секунд на пик/бан

ELO_WIN  = 25
ELO_LOSS = 20
ELO_MIN  = 100

# Тестовые боты получают отрицательные ID начиная с -100001
BOT_ID_START = -100000

# ════════════════════════════════════════════════
#                   ДАТАКЛАСС
# ════════════════════════════════════════════════

@dataclass
class Player:
    user_id:     int
    nickname:    str
    external_id: str   = ""
    elo:         int   = 1000
    wins:        int   = 0
    losses:      int   = 0
    avg:         float = 0.0
    is_bot:      bool  = False   # тестовый бот — не попадает в /top, /stats, /elo

    def lvl_icon(self) -> str:
        if self.elo >= 2000: return "💎"
        if self.elo >= 1500: return "🔥"
        if self.elo >= 1300: return "⭐"
        if self.elo >= 1100: return "⚡"
        return "🟢"

    def tg_link(self) -> str:
        if self.is_bot:
            return f"🤖 <b>{self.nickname}</b>"
        return f'<a href="tg://user?id={self.user_id}">{self.nickname}</a>'

# ════════════════════════════════════════════════
#                  БАЗА ДАННЫХ
# ════════════════════════════════════════════════

def load_db() -> Dict[str, Any]:
    default: Dict[str, Any] = {
        "players":        {},
        "match_counter":  0,
        "active_matches": {},
        "queue_5v5":      [],
        "queue_2v2":      [],
        "muted":          {},
        "banned":         {},
        "bot_counter":    0,
    }
    if not os.path.exists(DATA_FILE):
        return default
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return default


def save_db(db: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=4, ensure_ascii=False)


def get_player(uid: int, name: str = "Player") -> Player:
    db = load_db()
    s  = str(uid)
    if s not in db["players"]:
        db["players"][s] = asdict(Player(uid, name))
        save_db(db)
    d = db["players"][s]
    for field, val in [("wins",0),("losses",0),("avg",0.0),
                       ("elo",1000),("external_id",""),("is_bot",False)]:
        d.setdefault(field, val)
    return Player(**d)

# ════════════════════════════════════════════════
#             ПРОВЕРКИ БАН / МУТ / РЕГ
# ════════════════════════════════════════════════

def check_banned(uid: int) -> bool:
    db    = load_db()
    until = db["banned"].get(str(uid))
    return bool(until and datetime.now().timestamp() < until)


def check_muted(uid: int) -> bool:
    db    = load_db()
    until = db["muted"].get(str(uid))
    return bool(until and datetime.now().timestamp() < until)


def is_registered(uid: int) -> bool:
    db = load_db()
    s  = str(uid)
    return s in db["players"] and bool(db["players"][s].get("external_id"))


async def gate(update: Update, need_reg: bool = True, need_unmute: bool = False) -> bool:
    """
    Единая проверка доступа. Возвращает True если нужно заблокировать.
    Админы всегда проходят.
    """
    if not update.message:
        return False
    uid = update.effective_user.id
    if uid in ADMIN_IDS:
        return False
    if check_banned(uid):
        await update.message.reply_text("🚫 Вы забанены.")
        return True
    if need_unmute and check_muted(uid):
        await update.message.reply_text("🚫 Вы замьючены — вставать в очередь нельзя.")
        return True
    if need_reg and not is_registered(uid):
        await update.message.reply_text(
            "❌ <b>Вы не зарегистрированы!</b>\n\n"
            "Для регистрации введите:\n"
            "<code>/reg FACEIT_ID Никнейм</code>\n\n"
            "Пример: <code>/reg abc123 ProPlayer</code>",
            parse_mode=ParseMode.HTML
        )
        return True
    return False

# ════════════════════════════════════════════════
#               УТИЛИТЫ ЛОББИ
# ════════════════════════════════════════════════

def parse_duration(s: str) -> Optional[int]:
    units = {"m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in units:
        try:
            return int(s[:-1]) * units[s[-1]]
        except ValueError:
            pass
    try:
        return int(s) * 60
    except ValueError:
        return None


def lobby_text(mode: str, queue: List[int]) -> str:
    size  = LOBBY_5V5_SIZE if mode == "5v5" else LOBBY_2V2_SIZE
    emoji = "🎮" if mode == "5v5" else "⚡"
    lines = [f"{emoji} <b>Лобби {mode.upper()}</b>  {len(queue)}/{size}\n━━━━━━━━━━━━━━━━━━━━━"]
    if queue:
        for i, uid in enumerate(queue, 1):
            p = get_player(uid)
            lines.append(
                f"{i}. {p.lvl_icon()} {p.tg_link()} "
                f"<code>[{p.external_id or '?'}]</code> • <b>{p.elo}</b> ELO"
            )
    else:
        lines.append("  <i>Очередь пуста</i>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def lobby_kb(mode: str, uid: int, queue: List[int]) -> InlineKeyboardMarkup:
    """
    Кнопка всегда показывает актуальное состояние ТОЛЬКО для того юзера,
    который нажимает — но так как в Telegram одна клавиатура на всех,
    мы показываем "Присоединиться" если юзер НЕ в очереди, и "Выйти" если в очереди.
    Каждый раз при /play5 или /play2 отправляется НОВОЕ сообщение.
    """
    if uid in queue:
        btn = InlineKeyboardButton("❌ Выйти из очереди", callback_data=f"leave_{mode}")
    else:
        btn = InlineKeyboardButton("✅ Присоединиться",   callback_data=f"join_{mode}")
    return InlineKeyboardMarkup([[btn]])

# ════════════════════════════════════════════════
#              СОЗДАНИЕ МАТЧА
# ════════════════════════════════════════════════

def _pick_buttons(m_id: str, pool: List[int]) -> List[List[InlineKeyboardButton]]:
    rows = []
    for uid in pool:
        p = get_player(uid)
        label = f"{p.lvl_icon()} {p.nickname} [{p.external_id or '?'}] | {p.avg:.1f}%"
        rows.append([InlineKeyboardButton(label, callback_data=f"pk_{m_id}_{uid}")])
    return rows


def _is_bot_uid(uid: int) -> bool:
    """Проверяет, является ли uid тестовым ботом (отрицательный ID)."""
    return uid < 0


async def _bot_auto_pick(m_id: str, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """
    Авто-пик/бан для бота-капитана. Вызывается через job_queue с задержкой.
    Бот случайно выбирает игрока из пула или банит карту.
    """
    import asyncio
    await asyncio.sleep(2)   # небольшая задержка для реалистичности

    db = load_db()
    m  = db["active_matches"].get(m_id)
    if not m:
        return

    turn = m["turn"]
    if not _is_bot_uid(turn):
        return  # сейчас ход живого игрока — не вмешиваемся

    ct_cap = m["ct"][0]
    t_cap  = m["t"][0]
    phase  = m.get("phase", "pick")

    if phase == "pick" and m["pool"]:
        # Бот выбирает случайного игрока
        chosen = random.choice(m["pool"])
        if turn == ct_cap:
            m["ct"].append(chosen)
        else:
            m["t"].append(chosen)
        m["pool"].remove(chosen)

        # Авто-добавляем последнего если остался 1
        if len(m["pool"]) == 1:
            last = m["pool"].pop(0)
            if len(m["ct"]) <= len(m["t"]):
                m["ct"].append(last)
            else:
                m["t"].append(last)

        bot_p = get_player(turn)

        if m["pool"]:
            m["turn"] = t_cap if turn == ct_cap else ct_cap
            cur_side  = "🔵 CT" if m["turn"] == ct_cap else "🔴 T"
            txt = (
                f"🤖 <b>{bot_p.nickname}</b> выбрал {get_player(chosen).nickname}\n\n"
                f"🎯 <b>Пик | Матч #{m_id} [{m['mode'].upper()}]</b>\n"
                f"CT: {len(m['ct'])} | T: {len(m['t'])}\n"
                f"Ход: {cur_side}"
            )
            save_db(db)
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=txt,
                    reply_markup=InlineKeyboardMarkup(_pick_buttons(m_id, m["pool"])),
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
            # Если следующий тоже бот — снова авто-пик
            if _is_bot_uid(m["turn"]):
                await _bot_auto_pick(m_id, context, chat_id)
        else:
            # Пик завершён
            m["phase"] = "ban"
            m["turn"]  = ct_cap

            def pline(u: int) -> str:
                p = get_player(u)
                return f"  • {p.tg_link()} <code>[{p.external_id or '?'}]</code>"

            ct_list = "\n".join(pline(u) for u in m["ct"])
            t_list  = "\n".join(pline(u) for u in m["t"])
            txt = (
                f"🤖 <b>{bot_p.nickname}</b> выбрал {get_player(chosen).nickname}\n\n"
                f"✅ <b>Матч #{m_id} — пик завершён</b>\n\n"
                f"🔵 CT:\n{ct_list}\n\n"
                f"🔴 T:\n{t_list}\n\n"
                f"🗺 <b>Баны карт — ход: {'🔵 CT' if ct_cap == m['turn'] else '🔴 T'}</b>"
            )
            ban_btns = [
                [InlineKeyboardButton(f"🚫 {mn}", callback_data=f"bn_{m_id}_{mn}")]
                for mn in m["maps"]
            ]
            save_db(db)
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=txt,
                    reply_markup=InlineKeyboardMarkup(ban_btns),
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
            # Если капитан банов тоже бот — авто-бан
            if _is_bot_uid(m["turn"]):
                await _bot_auto_ban(m_id, context, chat_id)

    elif phase == "ban" and m.get("maps"):
        await _bot_auto_ban(m_id, context, chat_id)


async def _bot_auto_ban(m_id: str, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Авто-бан карты ботом-капитаном."""
    import asyncio
    await asyncio.sleep(2)

    db = load_db()
    m  = db["active_matches"].get(m_id)
    if not m or not m.get("maps"):
        return

    turn = m["turn"]
    if not _is_bot_uid(turn):
        return

    ct_cap   = m["ct"][0]
    t_cap    = m["t"][0]
    map_name = random.choice(m["maps"])
    bot_p    = get_player(turn)

    m["maps"].remove(map_name)
    m["banned_maps"].append(map_name)

    if len(m["maps"]) == 1:
        final_map  = m["maps"][0]
        banned_str = ", ".join(m["banned_maps"])

        def pline(u: int) -> str:
            p = get_player(u)
            return f"  • {p.tg_link()} <code>[{p.external_id or '?'}]</code>"

        ct_list = "\n".join(pline(u) for u in m["ct"])
        t_list  = "\n".join(pline(u) for u in m["t"])
        txt = (
            f"🤖 <b>{bot_p.nickname}</b> забанил {map_name}\n\n"
            f"🏁 <b>Матч #{m_id} [{m['mode'].upper()}] — всё готово!</b>\n\n"
            f"🔵 CT:\n{ct_list}\n\n"
            f"🔴 T:\n{t_list}\n\n"
            f"🗺 Карта: <b>{final_map}</b>\n"
            f"🚫 Забанены: {banned_str}\n\n"
            f"Введите результат:\n"
            f"<code>/win {m_id} ct</code>  или  <code>/win {m_id} t</code>"
        )
        save_db(db)
        try:
            await context.bot.send_message(chat_id=chat_id, text=txt, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    m["turn"] = t_cap if turn == ct_cap else ct_cap
    cur_side  = "🔵 CT" if m["turn"] == ct_cap else "🔴 T"
    ban_btns  = [
        [InlineKeyboardButton(f"🚫 {mn}", callback_data=f"bn_{m_id}_{mn}")]
        for mn in m["maps"]
    ]
    txt = (
        f"🤖 <b>{bot_p.nickname}</b> забанил {map_name}\n\n"
        f"🗺 <b>Баны карт | Матч #{m_id}</b>\n"
        f"Осталось: {len(m['maps'])} карт\n"
        f"Ход: {cur_side}"
    )
    save_db(db)
    try:
        await context.bot.send_message(
            chat_id=chat_id, text=txt,
            reply_markup=InlineKeyboardMarkup(ban_btns),
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass
    # Если следующий тоже бот
    if _is_bot_uid(m["turn"]):
        await _bot_auto_ban(m_id, context, chat_id)


async def start_match(players: List[int], mode: str, db: Dict,
                      context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    db["match_counter"] += 1
    m_id   = str(db["match_counter"])

    random.shuffle(players)
    ct_cap = players[0]
    t_cap  = players[1]
    pool   = players[2:]

    db["active_matches"][m_id] = {
        "mode":            mode,
        "ct":              [ct_cap],
        "t":               [t_cap],
        "pool":            pool,
        "turn":            ct_cap,
        "phase":           "pick",
        "maps":            MAPS_LIST.copy(),
        "banned_maps":     [],
        "pick_start_time": time.time(),
        "pick_timeout":    PICK_TIMEOUT,
        "chat_id":         chat_id,
    }
    save_db(db)

    ct_p = get_player(ct_cap)
    t_p  = get_player(t_cap)

    txt = (
        f"🆕 <b>Матч #{m_id} [{mode.upper()}]</b>\n\n"
        f"🔵 CT капитан: {ct_p.tg_link()} <code>[{ct_p.external_id or '?'}]</code>\n"
        f"🔴 T  капитан: {t_p.tg_link()} <code>[{t_p.external_id or '?'}]</code>\n\n"
        f"👥 В пуле: {len(pool)} игроков\n"
        f"⏳ На пик: <b>{PICK_TIMEOUT} сек</b>\n\n"
        f"Ход: 🔵 CT — выбирает первого игрока"
    )
    btns = _pick_buttons(m_id, pool)
    await context.bot.send_message(
        chat_id      = chat_id,
        text         = txt,
        reply_markup = InlineKeyboardMarkup(btns) if btns else None,
        parse_mode   = ParseMode.HTML
    )
    # Если первый капитан — бот, запускаем авто-пик
    if _is_bot_uid(ct_cap):
        await _bot_auto_pick(m_id, context, chat_id)

# ════════════════════════════════════════════════
#              ПУБЛИЧНЫЕ КОМАНДЫ
# ════════════════════════════════════════════════

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_registered(uid) or uid in ADMIN_IDS:
        p   = get_player(uid)
        txt = (
            f"👋 Привет, <b>{p.nickname}</b>!\n"
            f"🆔 FACEIT ID: <code>{p.external_id or '—'}</code>\n"
            f"{p.lvl_icon()} <b>{p.elo}</b> ELO\n\n"
            "<b>Команды:</b>\n"
            "/play5 — Лобби 5v5\n"
            "/play2 — Лобби 2v2\n"
            "/stats — Профиль\n"
            "/top — Топ игроков\n"
            "/queue — Статус очередей"
        )
    else:
        txt = (
            "👋 <b>Добро пожаловать!</b>\n\n"
            "Для работы с ботом нужна регистрация:\n"
            "<code>/reg FACEIT_ID Никнейм</code>\n\n"
            "Пример: <code>/reg abc123 ProPlayer</code>\n\n"
            "⚠️ Без регистрации другие команды недоступны."
        )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)


async def reg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await gate(update, need_reg=False): return
    uid = update.effective_user.id
    s   = str(uid)
    db  = load_db()

    if s in db["players"] and db["players"][s].get("external_id"):
        await update.message.reply_text(
            "🚫 Вы уже зарегистрированы.\n"
            "Для смены данных обратитесь к администратору."
        )
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "📝 Формат:\n<code>/reg FACEIT_ID Никнейм</code>\n\n"
            "Пример: <code>/reg abc123 ProPlayer</code>",
            parse_mode=ParseMode.HTML
        )
        return

    faceit_id = context.args[0]
    nickname  = " ".join(context.args[1:])

    if len(nickname) > 32:
        await update.message.reply_text("🚫 Никнейм слишком длинный (максимум 32 символа).")
        return

    # Уникальность FACEIT ID среди живых игроков
    for d in db["players"].values():
        if d.get("external_id") == faceit_id and not d.get("is_bot"):
            await update.message.reply_text("🚫 Этот FACEIT ID уже зарегистрирован.")
            return

    db["players"][s] = asdict(Player(uid, nickname, faceit_id))
    save_db(db)
    await update.message.reply_text(
        f"✅ <b>Зарегистрирован!</b>\n\n"
        f"👤 Никнейм: <b>{nickname}</b>\n"
        f"🆔 FACEIT ID: <code>{faceit_id}</code>\n\n"
        f"Вставай в очередь: /play5 или /play2",
        parse_mode=ParseMode.HTML
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await gate(update): return
    uid    = update.effective_user.id
    target = uid
    if context.args:
        try:
            target = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Формат: /stats [user_id]"); return

    p = get_player(target)
    if p.is_bot:
        await update.message.reply_text("🤖 Это тестовый бот — статистики нет.")
        return

    total = p.wins + p.losses
    wr    = f"{p.avg:.1f}%" if total else "—"
    await update.message.reply_text(
        f"✦ {p.tg_link()} ✦\n"
        f"🆔 <code>{p.external_id or 'не указан'}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{p.lvl_icon()} <b>{p.elo}</b> ELO\n"
        f"🏆 Побед: <b>{p.wins}</b>  💀 Поражений: <b>{p.losses}</b>\n"
        f"📈 Winrate: <b>{wr}</b>  🎮 Матчей: <b>{total}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML
    )


async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await gate(update): return
    db      = load_db()
    # Боты полностью исключены
    players = [
        Player(**d) for d in db["players"].values()
        if d.get("external_id") and not d.get("is_bot")
    ]
    if not players:
        await update.message.reply_text("🏆 Рейтинг пока пуст.")
        return

    players.sort(key=lambda p: p.elo, reverse=True)
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines  = ["🏆 <b>Топ-10 игроков</b>\n━━━━━━━━━━━━━━━"]
    for i, p in enumerate(players[:10]):
        wr = f"{p.avg:.1f}%" if (p.wins+p.losses) else "—"
        lines.append(
            f"{medals[i]} {p.lvl_icon()} {p.tg_link()} <code>[{p.external_id}]</code>\n"
            f"    ELO: <b>{p.elo}</b> | WR: <b>{wr}</b> | Игр: <b>{p.wins+p.losses}</b>"
        )
    if len(players) > 10:
        lines.append(f"\n... и ещё {len(players)-10} в рейтинге")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def play5_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открывает лобби 5v5 — всегда отправляет НОВОЕ сообщение."""
    if await gate(update, need_unmute=True): return
    uid = update.effective_user.id
    db  = load_db()
    q   = db.get("queue_5v5", [])
    await update.message.reply_text(
        lobby_text("5v5", q),
        reply_markup=lobby_kb("5v5", uid, q),
        parse_mode=ParseMode.HTML
    )


async def play2_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открывает лобби 2v2 — всегда отправляет НОВОЕ сообщение."""
    if await gate(update, need_unmute=True): return
    uid = update.effective_user.id
    db  = load_db()
    q   = db.get("queue_2v2", [])
    await update.message.reply_text(
        lobby_text("2v2", q),
        reply_markup=lobby_kb("2v2", uid, q),
        parse_mode=ParseMode.HTML
    )


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await gate(update): return
    db = load_db()
    q5 = db.get("queue_5v5", [])
    q2 = db.get("queue_2v2", [])
    await update.message.reply_text(
        f"📊 <b>Очереди</b>\n\n"
        f"🎮 5v5: {len(q5)}/{LOBBY_5V5_SIZE}\n"
        f"⚡ 2v2: {len(q2)}/{LOBBY_2V2_SIZE}",
        parse_mode=ParseMode.HTML
    )

# ════════════════════════════════════════════════
#             CALLBACK — ЛОББИ / ПИК / БАН
# ════════════════════════════════════════════════

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    cb  = q.data

    try:
        await q.answer()
    except Exception:
        return

    # ── JOIN / LEAVE ────────────────────────────────────────────────────────
    if cb in ("join_5v5", "leave_5v5", "join_2v2", "leave_2v2"):
        action, mode = cb.split("_", 1)   # "join"/"leave" + "5v5"/"2v2"

        if check_banned(uid):
            await q.answer("🚫 Вы забанены!", show_alert=True); return
        if check_muted(uid):
            await q.answer("🚫 Вы замьючены!", show_alert=True); return
        if not is_registered(uid) and uid not in ADMIN_IDS:
            await q.answer("🚫 Сначала /reg", show_alert=True); return

        db    = load_db()
        key   = f"queue_{mode}"
        okey  = "queue_2v2" if mode == "5v5" else "queue_5v5"
        queue = db.get(key, [])
        size  = LOBBY_5V5_SIZE if mode == "5v5" else LOBBY_2V2_SIZE

        if action == "join":
            if uid in queue:
                await q.answer("Вы уже в этой очереди!", show_alert=True); return
            if uid in db.get(okey, []):
                other = "2v2" if mode == "5v5" else "5v5"
                await q.answer(f"Вы уже в очереди {other}!", show_alert=True); return
            queue.append(uid)
            await q.answer(f"✅ Вы в очереди {mode.upper()} ({len(queue)}/{size})")
        else:
            if uid not in queue:
                await q.answer("Вас нет в этой очереди!", show_alert=True); return
            queue.remove(uid)
            await q.answer(f"❌ Вы вышли из очереди {mode.upper()}")

        db[key] = queue
        save_db(db)

        # Обновляем сообщение: кнопка меняется под того, кто нажал
        try:
            await q.edit_message_text(
                lobby_text(mode, queue),
                reply_markup=lobby_kb(mode, uid, queue),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

        # Запуск матча если очередь полная
        if len(queue) >= size:
            match_players = queue[:size]
            db[key]       = queue[size:]
            save_db(db)
            await start_match(match_players, mode, db, context, q.message.chat_id)
        return

    # ── PICK ────────────────────────────────────────────────────────────────
    if cb.startswith("pk_"):
        parts = cb.split("_")
        if len(parts) != 3:
            return
        _, m_id, p_str = parts
        try:
            p_id = int(p_str)
        except ValueError:
            return

        db = load_db()
        m  = db["active_matches"].get(m_id)
        if not m:
            await q.answer("Матч уже завершён", show_alert=True); return

        ct_cap = m["ct"][0]
        t_cap  = m["t"][0]

        # Только капитаны могут пикать
        if uid not in (ct_cap, t_cap):
            await q.answer("🚫 Только капитан может выбирать игроков!", show_alert=True); return

        if uid != m["turn"]:
            whose = get_player(m["turn"]).nickname
            await q.answer(f"Сейчас ход {whose}!", show_alert=True); return

        # Таймаут
        if time.time() - m["pick_start_time"] > m["pick_timeout"]:
            try:
                await q.edit_message_text("⏰ Время на пик вышло! Матч отменён.")
            except Exception:
                pass
            db["active_matches"].pop(m_id, None)
            save_db(db)
            return

        if p_id not in m["pool"]:
            await q.answer("Этот игрок уже выбран!", show_alert=True); return

        # Добавляем в команду
        if uid == ct_cap:
            m["ct"].append(p_id)
        else:
            m["t"].append(p_id)
        m["pool"].remove(p_id)

        # Если остался 1 — авто-добавляем
        if len(m["pool"]) == 1:
            last = m["pool"].pop(0)
            if len(m["ct"]) <= len(m["t"]):
                m["ct"].append(last)
            else:
                m["t"].append(last)

        if m["pool"]:
            m["turn"]   = t_cap if uid == ct_cap else ct_cap
            elapsed     = time.time() - m["pick_start_time"]
            remaining   = max(0, int(m["pick_timeout"] - elapsed))
            cur_side    = "🔵 CT" if m["turn"] == ct_cap else "🔴 T"
            txt = (
                f"🎯 <b>Пик | Матч #{m_id} [{m['mode'].upper()}]</b>\n"
                f"CT: {len(m['ct'])} | T: {len(m['t'])}\n"
                f"Ход: {cur_side}  ⏳ {remaining} сек"
            )
            try:
                await q.edit_message_text(
                    txt,
                    reply_markup=InlineKeyboardMarkup(_pick_buttons(m_id, m["pool"])),
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
            save_db(db)
            # Если следующий ход — бот, запускаем авто-пик
            if _is_bot_uid(m["turn"]):
                await _bot_auto_pick(m_id, context, m.get("chat_id", q.message.chat_id))
        else:
            # Пик завершён → переходим к банам карт
            m["phase"] = "ban"
            m["turn"]  = ct_cap

            def player_line(u: int) -> str:
                p = get_player(u)
                return f"  • {p.tg_link()} <code>[{p.external_id or '?'}]</code>"

            ct_list = "\n".join(player_line(u) for u in m["ct"])
            t_list  = "\n".join(player_line(u) for u in m["t"])
            txt = (
                f"✅ <b>Матч #{m_id} — пик завершён</b>\n\n"
                f"🔵 CT:\n{ct_list}\n\n"
                f"🔴 T:\n{t_list}\n\n"
                f"🗺 <b>Баны карт — ход: 🔵 CT</b>"
            )
            ban_btns = [
                [InlineKeyboardButton(f"🚫 {mn}", callback_data=f"bn_{m_id}_{mn}")]
                for mn in m["maps"]
            ]
            try:
                await q.edit_message_text(
                    txt,
                    reply_markup=InlineKeyboardMarkup(ban_btns),
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
            save_db(db)
            # Если капитан банов — бот, запускаем авто-бан
            if _is_bot_uid(ct_cap):
                await _bot_auto_ban(m_id, context, m.get("chat_id", q.message.chat_id))

        save_db(db)
        return

    # ── BAN MAP ─────────────────────────────────────────────────────────────
    if cb.startswith("bn_"):
        parts = cb.split("_", 2)
        if len(parts) != 3:
            return
        _, m_id, map_name = parts

        db = load_db()
        m  = db["active_matches"].get(m_id)
        if not m:
            await q.answer("Матч не найден", show_alert=True); return

        ct_cap = m["ct"][0]
        t_cap  = m["t"][0]

        # Только капитаны банят карты
        if uid not in (ct_cap, t_cap):
            await q.answer("🚫 Только капитан может банить карты!", show_alert=True); return

        if uid != m["turn"]:
            whose = get_player(m["turn"]).nickname
            await q.answer(f"Сейчас ход {whose}!", show_alert=True); return

        if map_name not in m.get("maps", []):
            await q.answer("Карта уже забанена", show_alert=True); return

        m["maps"].remove(map_name)
        m["banned_maps"].append(map_name)

        if len(m["maps"]) == 1:
            final_map  = m["maps"][0]
            banned_str = ", ".join(m["banned_maps"])

            def player_line(u: int) -> str:
                p = get_player(u)
                return f"  • {p.tg_link()} <code>[{p.external_id or '?'}]</code>"

            ct_list = "\n".join(player_line(u) for u in m["ct"])
            t_list  = "\n".join(player_line(u) for u in m["t"])
            txt = (
                f"🏁 <b>Матч #{m_id} [{m['mode'].upper()}] — всё готово!</b>\n\n"
                f"🔵 CT:\n{ct_list}\n\n"
                f"🔴 T:\n{t_list}\n\n"
                f"🗺 Карта: <b>{final_map}</b>\n"
                f"🚫 Забанены: {banned_str}\n\n"
                f"Введите результат (только для администратора):\n"
                f"<code>/win {m_id} ct</code>  или  <code>/win {m_id} t</code>"
            )
            try:
                await q.edit_message_text(txt, parse_mode=ParseMode.HTML)
            except Exception:
                pass
            save_db(db)
            return

        m["turn"] = t_cap if uid == ct_cap else ct_cap
        cur_side  = "🔵 CT" if m["turn"] == ct_cap else "🔴 T"
        ban_btns  = [
            [InlineKeyboardButton(f"🚫 {mn}", callback_data=f"bn_{m_id}_{mn}")]
            for mn in m["maps"]
        ]
        try:
            await q.edit_message_text(
                f"🗺 <b>Баны карт | Матч #{m_id}</b>\n"
                f"Осталось: {len(m['maps'])} карт\n"
                f"Ход: {cur_side}",
                reply_markup=InlineKeyboardMarkup(ban_btns),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        save_db(db)
        # Если следующий ход — бот, авто-бан
        if _is_bot_uid(m["turn"]):
            await _bot_auto_ban(m_id, context, m.get("chat_id", q.message.chat_id))

# ════════════════════════════════════════════════
#              АДМИН-КОМАНДЫ
# ════════════════════════════════════════════════

async def win_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Формат: /win <match_id> <ct|t>"); return

    m_id = context.args[0]
    side = context.args[1].lower()
    if side not in ("ct", "t"):
        await update.message.reply_text("Сторона: ct или t"); return

    db = load_db()
    m  = db["active_matches"].get(m_id)
    if not m:
        await update.message.reply_text(f"❌ Матч #{m_id} не найден"); return

    winners = m["ct"] if side == "ct" else m["t"]
    losers  = m["t"]  if side == "ct" else m["ct"]
    w_nicks, l_nicks = [], []

    for uid in winners + losers:
        s = str(uid)
        if s not in db["players"]:
            db["players"][s] = asdict(Player(uid, "Unknown"))
        for f, v in [("wins",0),("losses",0),("elo",1000),("avg",0.0),("is_bot",False)]:
            db["players"][s].setdefault(f, v)

    for uid in winners:
        p = db["players"][str(uid)]
        if not p.get("is_bot"):
            p["wins"] += 1
            p["elo"]  += ELO_WIN
            total      = p["wins"] + p["losses"]
            p["avg"]   = round(p["wins"] / total * 100, 1)
        w_nicks.append(p.get("nickname","?"))

    for uid in losers:
        p = db["players"][str(uid)]
        if not p.get("is_bot"):
            p["losses"] += 1
            p["elo"]     = max(ELO_MIN, p["elo"] - ELO_LOSS)
            total        = p["wins"] + p["losses"]
            p["avg"]     = round(p["wins"] / total * 100, 1) if total else 0.0
        l_nicks.append(p.get("nickname","?"))

    db["active_matches"].pop(m_id, None)
    save_db(db)

    mode = m.get("mode","5v5").upper()
    await update.message.reply_text(
        f"✅ <b>Матч #{m_id} [{mode}] закрыт</b>\n\n"
        f"🏆 Победа {side.upper()}\n"
        f"Победители: {', '.join(w_nicks)}\n"
        f"Проигравшие: {', '.join(l_nicks)}\n\n"
        f"📈 +{ELO_WIN} ELO победителям / -{ELO_LOSS} ELO проигравшим",
        parse_mode=ParseMode.HTML
    )


async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 1:
        await update.message.reply_text("Формат: /mute <user_id> [30m|2h|1d]"); return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Неверный user_id"); return

    duration = parse_duration(context.args[1]) if len(context.args) >= 2 else 3600
    if duration is None:
        await update.message.reply_text("Неверный формат. Примеры: 30m 2h 1d"); return

    db = load_db()
    db["muted"][str(target)] = datetime.now().timestamp() + duration
    save_db(db)
    await update.message.reply_text(f"🔇 Пользователь {target} замьючен на {duration//60} мин.")


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("Формат: /unmute <user_id>"); return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Неверный user_id"); return
    db = load_db()
    db["muted"].pop(str(target), None)
    save_db(db)
    await update.message.reply_text(f"🔊 Мут снят с {target}")


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 1:
        await update.message.reply_text("Формат: /ban <user_id> [30m|2h|1d|perm]"); return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Неверный user_id"); return

    db = load_db()
    if len(context.args) >= 2 and context.args[1].lower() == "perm":
        db["banned"][str(target)] = 9_999_999_999
        save_db(db)
        await update.message.reply_text(f"🚫 Пользователь {target} перманентно забанен.")
        return

    duration = parse_duration(context.args[1]) if len(context.args) >= 2 else 86400
    if duration is None:
        await update.message.reply_text("Неверный формат. Примеры: 30m 2h 1d perm"); return

    db["banned"][str(target)] = datetime.now().timestamp() + duration
    save_db(db)
    await update.message.reply_text(f"🚫 Пользователь {target} забанен на {duration//3600} ч.")


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("Формат: /unban <user_id>"); return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Неверный user_id"); return
    db = load_db()
    db["banned"].pop(str(target), None)
    save_db(db)
    await update.message.reply_text(f"✅ Бан снят с {target}")


async def elo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    db   = load_db()
    rows = []
    for d in db["players"].values():
        if not d.get("external_id") or d.get("is_bot"):
            continue
        try:
            p     = Player(**d)
            total = p.wins + p.losses
            wr    = f"{p.avg:.1f}%" if total else "—"
            rows.append((p.nickname, p.external_id, p.elo, wr, total, p.lvl_icon()))
        except Exception:
            continue
    if not rows:
        await update.message.reply_text("Нет зарегистрированных игроков."); return

    rows.sort(key=lambda x: x[2], reverse=True)
    lines = ["📊 <b>ELO таблица</b>\n━━━━━━━━━━━━━━━━━━━━━"]
    for i, (nick, ext_id, elo, wr, games, icon) in enumerate(rows[:30], 1):
        lines.append(
            f"{i:2}. {icon} {nick} <code>[{ext_id}]</code>\n"
            f"    ELO: <b>{elo}</b> | WR: {wr} | Игр: {games}"
        )
    if len(rows) > 30:
        lines.append(f"\n... и ещё {len(rows)-30} игроков")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def setelo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Формат: /setelo <user_id> <elo>"); return
    try:
        target  = int(context.args[0])
        new_elo = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Неверные аргументы"); return

    db = load_db()
    s  = str(target)
    if s not in db["players"]:
        await update.message.reply_text("Игрок не найден"); return

    db["players"][s]["elo"] = max(ELO_MIN, new_elo)
    save_db(db)
    p = get_player(target)
    await update.message.reply_text(f"✅ ELO игрока {p.nickname} → {new_elo}")


async def clearqueue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    db    = load_db()
    which = context.args[0].lower() if context.args else "all"

    # Удаляем ботов из базы при очистке очереди
    for q_key in (["queue_5v5"] if which == "5v5" else
                  ["queue_2v2"] if which == "2v2" else
                  ["queue_5v5", "queue_2v2"]):
        for uid in db.get(q_key, []):
            if uid < 0:  # это бот
                db["players"].pop(str(uid), None)
        db[q_key] = []

    save_db(db)
    await update.message.reply_text(f"🗑 Очередь [{which}] очищена.")


async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    db      = load_db()
    matches = db.get("active_matches", {})
    if not matches:
        await update.message.reply_text("Нет активных матчей."); return
    lines = [f"📋 <b>Активные матчи ({len(matches)})</b>"]
    for m_id, m in matches.items():
        ct_n  = get_player(m["ct"][0]).nickname if m["ct"] else "?"
        t_n   = get_player(m["t"][0]).nickname  if m["t"]  else "?"
        phase = m.get("phase","?")
        lines.append(
            f"#{m_id} [{m.get('mode','?').upper()}] "
            f"{ct_n} vs {t_n} | {phase} | пул: {len(m['pool'])}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── /bots1 — 5v5: 1 реальный игрок + 9 ботов ────────────────────────────────
# ── /bots2 — 2v2: 1 реальный игрок + 3 бота  ────────────────────────────────
# Обе команды секретные, не видны в меню. Боты НЕ попадают в /top, /stats, /elo.

BOT_NAMES = [
    "Zeus","Simple","KennyS","Device","Guardian","Cold",
    "ElectroNic","Perfecto","B1T","Monesy","JL","Zywoo",
    "Faker","NaVi_Bot","Twistzz","Ropz","NAF","sh1ro","Ax1Le"
]


def _create_fake_bot(db: Dict) -> int:
    """Создаёт одного тестового бота в БД и возвращает его uid."""
    db["bot_counter"] += 1
    bot_uid  = BOT_ID_START - db["bot_counter"]
    bot_nick = random.choice(BOT_NAMES) + f"#{db['bot_counter']}"
    bot_elo  = random.randint(800, 1800)
    wins     = random.randint(0, 60)
    losses   = random.randint(0, 60)
    avg      = round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0.0
    db["players"][str(bot_uid)] = asdict(Player(
        user_id     = bot_uid,
        nickname    = bot_nick,
        external_id = f"bot_{db['bot_counter']}",
        elo         = bot_elo,
        wins        = wins,
        losses      = losses,
        avg         = avg,
        is_bot      = True
    ))
    return bot_uid


async def bots1_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /bots1 — тест 5v5.
    Берёт реального игрока из очереди 5v5 (или самого админа),
    добавляет 9 ботов и запускает матч.
    Капитан-бот автоматически пикает и банит карты.
    """
    if update.effective_user.id not in ADMIN_IDS:
        return  # молча игнорируем

    db    = load_db()
    uid   = update.effective_user.id
    queue = db.get("queue_5v5", [])

    # Реальный игрок — берём из очереди или добавляем самого вызывающего
    if uid in queue:
        queue.remove(uid)
    real_players = [uid]

    # Добавляем 9 ботов
    for _ in range(LOBBY_5V5_SIZE - 1):
        real_players.append(_create_fake_bot(db))

    db["queue_5v5"] = queue  # очередь не трогаем
    save_db(db)

    await update.message.reply_text(
        f"🤖 Тестовый матч 5v5 запускается!\n"
        f"👤 Реальный игрок: 1\n"
        f"🤖 Ботов: {LOBBY_5V5_SIZE - 1}"
    )
    await start_match(real_players, "5v5", db, context, update.message.chat_id)


async def bots2_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /bots2 — тест 2v2.
    Берёт реального игрока (самого вызывающего),
    добавляет 3 бота и запускает матч 2v2.
    Капитан-бот автоматически пикает и банит карты.
    """
    if update.effective_user.id not in ADMIN_IDS:
        return  # молча игнорируем

    db  = load_db()
    uid = update.effective_user.id

    real_players = [uid]
    for _ in range(LOBBY_2V2_SIZE - 1):
        real_players.append(_create_fake_bot(db))

    save_db(db)

    await update.message.reply_text(
        f"🤖 Тестовый матч 2v2 запускается!\n"
        f"👤 Реальный игрок: 1\n"
        f"🤖 Ботов: {LOBBY_2V2_SIZE - 1}"
    )
    await start_match(real_players, "2v2", db, context, update.message.chat_id)

# ════════════════════════════════════════════════
#              МЕНЮ КОМАНД (только публичные)
# ════════════════════════════════════════════════

async def set_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",  "Начало работы"),
        BotCommand("reg",    "Регистрация"),
        BotCommand("play5",  "Лобби 5v5"),
        BotCommand("play2",  "Лобби 2v2"),
        BotCommand("stats",  "Мой профиль"),
        BotCommand("top",    "Топ игроков"),
        BotCommand("queue",  "Статус очередей"),
        # /bots и все админские команды здесь НЕ указаны — они невидимы
    ])

# ════════════════════════════════════════════════
#                    ЗАПУСК
# ════════════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Публичные команды
    app.add_handler(CommandHandler("start",  start_cmd))
    app.add_handler(CommandHandler("reg",    reg_cmd))
    app.add_handler(CommandHandler("stats",  stats_cmd))
    app.add_handler(CommandHandler("top",    top_cmd))
    app.add_handler(CommandHandler("play5",  play5_cmd))
    app.add_handler(CommandHandler("play2",  play2_cmd))
    app.add_handler(CommandHandler("queue",  queue_cmd))

    # Скрытые админские команды
    app.add_handler(CommandHandler("win",        win_cmd))
    app.add_handler(CommandHandler("mute",       mute_cmd))
    app.add_handler(CommandHandler("unmute",     unmute_cmd))
    app.add_handler(CommandHandler("ban",        ban_cmd))
    app.add_handler(CommandHandler("unban",      unban_cmd))
    app.add_handler(CommandHandler("elo",        elo_cmd))
    app.add_handler(CommandHandler("setelo",     setelo_cmd))
    app.add_handler(CommandHandler("clearqueue", clearqueue_cmd))
    app.add_handler(CommandHandler("matches",    matches_cmd))
    app.add_handler(CommandHandler("bots1",      bots1_cmd))   # ← секретная: 5v5 тест
    app.add_handler(CommandHandler("bots2",      bots2_cmd))   # ← секретная: 2v2 тест

    app.add_handler(CallbackQueryHandler(callback_handler))

    app.post_init = set_commands

    print("🤖 Бот запускается...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        poll_interval=0.5,
        timeout=10,
    )
    print("✅ Бот остановлен.")


if __name__ == "__main__":
    main()

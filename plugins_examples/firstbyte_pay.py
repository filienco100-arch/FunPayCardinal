from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from cardinal import Cardinal

import os
import json
import html
import logging
import requests

from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


# --------------------------------------------------------------------------- #
#  Обязательные поля плагина FPC                                              #
# --------------------------------------------------------------------------- #
NAME = "FirstByte Pay"
VERSION = "1.0"
DESCRIPTION = (
    "Генерация ссылок на пополнение баланса хостинга FirstByte (billing.firstbyte.ru / "
    "BILLmanager). Вход по cookie, первичная настройка способа оплаты (СБП и др.) и "
    "сохранённой суммы, кнопка «Оплатить» присылает ссылку на оплату."
)
CREDITS = "@FunPayCardinal"
UUID = "3cf084a1-0d6a-47af-90e6-95b347c00206"
SETTINGS_PAGE = True


# --------------------------------------------------------------------------- #
#  Константы                                                                  #
# --------------------------------------------------------------------------- #
LOGGER_PREFIX = "[FIRSTBYTE PAY]"
logger = logging.getLogger("FPC.firstbyte_pay")

CB = "fbp:"  # префикс callback_data, чтобы не конфликтовать с ядром / другими плагинами

BILLING_HOST = "https://billing.firstbyte.ru"
BILLMGR = BILLING_HOST + "/billmgr"
REQUEST_TIMEOUT = 40

CONFIG_DIR = os.path.join("storage", "plugins", "firstbyte_pay")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "cookie": "",                # значение cookie billmgrses5
    "payment_currency": "126",   # 126 = RUB, 153 = USD, 50 = EUR
    "paymethod": "",             # id способа оплаты (напр. 83 = ЮКасса СБП)
    "paymethod_name": "",        # человекочитаемое имя выбранного способа
    "amount": "",                # сохранённая сумма по умолчанию
    "profile_id": "",            # кэш id профиля плательщика (чтобы не плодить дубли)
    "payer": {                   # данные для авто-создания профиля, если его ещё нет
        "profiletype": "1",      # 1 = физлицо, 2 = юрлицо, 3 = ИП
        "name": "Основной",
        "person": "",            # ФИО
        "country": "182",        # 182 = Российская Федерация
        "postcode": "",
        "city": "",
        "address": "",
    },
}

# in-memory состояние
config: dict = {}
bot = None                    # telebot.TeleBot
cardinal_instance: "Optional[Cardinal]" = None
waiting_state: dict = {}      # chat_id -> tag ожидаемого ввода (на всякий случай)


# --------------------------------------------------------------------------- #
#  Конфиг                                                                      #
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config() -> dict:
    global config
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # глубокая копия
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = _deep_merge(cfg, json.load(f))
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Не удалось прочитать конфиг: {e}")
            logger.debug("TRACEBACK", exc_info=True)
    config = cfg
    return config


def save_config() -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Не удалось сохранить конфиг: {e}")
        logger.debug("TRACEBACK", exc_info=True)


def is_configured() -> bool:
    return bool(config.get("cookie") and config.get("paymethod"))


# --------------------------------------------------------------------------- #
#  Клиент BILLmanager                                                          #
# --------------------------------------------------------------------------- #
class BillmgrError(Exception):
    def __init__(self, message: str, type_: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.type = type_

    def is_auth(self) -> bool:
        return self.type in ("auth", "access")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Referer": BILLMGR,  # BILLmanager требует валидный Referer для действий
        "User-Agent": f"FPC-FirstBytePay/{VERSION}",
    })
    cookie = (config.get("cookie") or "").strip()
    # пользователь может вставить как "billmgrses5=xxx", так и просто "xxx"
    if cookie.startswith("billmgrses5="):
        cookie = cookie.split("=", 1)[1]
    s.cookies.set("billmgrses5", cookie, domain="billing.firstbyte.ru")
    return s


def _error_message(err: dict) -> str:
    for key in ("msg", "detail", "default"):
        node = err.get(key)
        if isinstance(node, dict) and node.get("$"):
            return node["$"]
    return "Неизвестная ошибка BILLmanager"


def billmgr_api(params: dict, timeout: int = REQUEST_TIMEOUT) -> dict:
    """
    Выполняет запрос к BILLmanager и возвращает doc. Кидает BillmgrError при ошибке.
    """
    if not (config.get("cookie") or "").strip():
        raise BillmgrError("Cookie не задана. Выполните первичную настройку.", "auth")

    query = dict(params)
    query["out"] = "json"
    resp = _session().get(BILLMGR, params=query, timeout=timeout)
    try:
        data = resp.json()
    except Exception:
        raise BillmgrError(f"Некорректный ответ сервера (HTTP {resp.status_code}).")
    doc = data.get("doc", {})
    err = doc.get("error")
    if err:
        raise BillmgrError(_error_message(err), err.get("$type"))
    return doc


def check_auth() -> bool:
    """True, если cookie валидна (сессия активна)."""
    try:
        billmgr_api({"func": "profile"})
        return True
    except BillmgrError as e:
        if e.is_auth():
            return False
        # не-auth ошибка означает, что запрос дошёл и сессия жива
        return True


def list_profiles() -> list[dict]:
    doc = billmgr_api({"func": "profile"})
    elem = doc.get("elem") or []
    return [{"id": (e.get("id") or {}).get("$"),
             "name": (e.get("name") or {}).get("$", "")} for e in elem]


def _payer_params() -> dict:
    p = config.get("payer", {})
    return {
        "profiletype": p.get("profiletype", "1"),
        "country": p.get("country", "182"),
        "profile": "add_new",
        "name": p.get("name", "Основной"),
        "person": p.get("person", ""),
        "country_physical": p.get("country", "182"),
        "postcode_physical": p.get("postcode", ""),
        "city_physical": p.get("city", ""),
        "address_physical": p.get("address", ""),
    }


def _profile_params() -> dict:
    """
    Возвращает параметры выбора плательщика: существующий профиль, либо данные
    для создания нового (add_new + поля плательщика).
    """
    pid = (config.get("profile_id") or "").strip()
    if pid:
        return {"profile": pid}
    profiles = list_profiles()
    if profiles and profiles[0]["id"]:
        config["profile_id"] = profiles[0]["id"]
        save_config()
        return {"profile": profiles[0]["id"]}
    return _payer_params()


def list_currencies() -> list[tuple[str, str]]:
    params = {"func": "payment.add.method", "amount": "100"}
    params.update(_profile_params())
    doc = billmgr_api(params)
    for s in doc.get("slist", []):
        if s.get("$name") == "payment_currency":
            return [(v.get("$key"), v.get("$")) for v in s.get("val", [])]
    return []


def list_methods(currency: str, amount: str = "100") -> list[dict]:
    """
    Список доступных способов оплаты для валюты. Требует контекст плательщика.
    Возвращает [{id, name, min, currency}].
    """
    params = {"func": "payment.add.method", "payment_currency": currency, "amount": amount}
    params.update(_profile_params())
    doc = billmgr_api(params)
    lst = doc.get("list")
    lst = [lst] if isinstance(lst, dict) else (lst or [])
    methods: list[dict] = []
    for block in lst:
        if block.get("$name") != "methodlist":
            continue
        for e in block.get("elem", []):
            methods.append({
                "id": (e.get("paymethod") or {}).get("$"),
                "name": (e.get("name") or {}).get("$", ""),
                "min": (e.get("payment_minamount") or {}).get("$", ""),
                "currency": (e.get("paymethod_currency_iso") or {}).get("$", ""),
            })
    return methods


def create_payment_link(amount: str) -> str:
    """
    Создаёт платёж и возвращает полную ссылку на оплату.
    """
    params = {
        "func": "payment.add",
        "payment_currency": config.get("payment_currency", "126"),
        "paymethod": config.get("paymethod"),
        "amount": str(amount),
        "sok": "ok",
    }
    params.update(_profile_params())
    doc = billmgr_api(params)

    # запоминаем созданный профиль, чтобы не плодить дубли
    new_profile = (doc.get("profile.id") or doc.get("profile") or {})
    if isinstance(new_profile, dict) and new_profile.get("$") and not config.get("profile_id"):
        config["profile_id"] = new_profile["$"]
        save_config()

    ok = doc.get("ok")
    if isinstance(ok, dict) and ok.get("$"):
        path = ok["$"]
        return path if path.startswith("http") else BILLING_HOST + path
    raise BillmgrError("Платёж создан, но ссылка не получена.")


# --------------------------------------------------------------------------- #
#  Утилиты Telegram                                                            #
# --------------------------------------------------------------------------- #
def _is_admin(chat_id) -> bool:
    try:
        return cardinal_instance is not None and chat_id in cardinal_instance.telegram.authorized_users
    except Exception:
        return True


def _esc(s) -> str:
    return html.escape(str(s))


def main_menu_text() -> str:
    cur_map = {"126": "RUB", "153": "USD", "50": "EUR"}
    cur = cur_map.get(config.get("payment_currency", "126"), config.get("payment_currency", ""))
    method = config.get("paymethod_name") or "не выбран"
    amount = config.get("amount") or "не задана"
    cookie_ok = "✅ задана" if config.get("cookie") else "❌ нет"
    return (
        f"💳 <b>{_esc(NAME)} v{VERSION}</b>\n\n"
        f"🍪 Cookie: {cookie_ok}\n"
        f"🏦 Способ оплаты: <b>{_esc(method)}</b>\n"
        f"💱 Валюта: <b>{_esc(cur)}</b>\n"
        f"💰 Сумма по умолчанию: <b>{_esc(amount)}</b>\n"
    )


def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    if is_configured():
        if config.get("amount"):
            kb.add(InlineKeyboardButton(
                f"💸 Оплатить {config['amount']}", callback_data=f"{CB}pay_saved"))
        kb.add(InlineKeyboardButton("✏️ Оплатить другую сумму", callback_data=f"{CB}pay_custom"))
    kb.add(InlineKeyboardButton("🏦 Способ оплаты", callback_data=f"{CB}methods"))
    kb.add(InlineKeyboardButton("💰 Сумма по умолчанию", callback_data=f"{CB}set_amount"))
    kb.add(InlineKeyboardButton("👤 Данные плательщика", callback_data=f"{CB}payer"))
    kb.add(InlineKeyboardButton("🍪 Обновить cookie", callback_data=f"{CB}set_cookie"))
    return kb


def open_panel(chat_id: int, message_id: Optional[int] = None) -> None:
    text, kb = main_menu_text(), main_menu_kb()
    if message_id is not None:
        try:
            bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)


def _back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔙 В меню", callback_data=f"{CB}menu"))
    return kb


def _send_payment_link(chat_id: int, amount: str) -> None:
    try:
        amount_f = float(str(amount).replace(",", "."))
        if amount_f <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(chat_id, "❌ Некорректная сумма.", reply_markup=_back_kb())
        return
    try:
        link = create_payment_link(str(amount_f if amount_f % 1 else int(amount_f)))
    except BillmgrError as e:
        if e.is_auth():
            bot.send_message(
                chat_id,
                "❌ Сессия недействительна или истекла. Обновите cookie billmgrses5.",
                reply_markup=_back_kb())
        else:
            bot.send_message(chat_id, f"❌ Ошибка: {_esc(e.message)}", reply_markup=_back_kb())
        return
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} create_payment_link: {e}")
        logger.debug("TRACEBACK", exc_info=True)
        bot.send_message(chat_id, f"❌ Не удалось создать платёж: {_esc(e)}", reply_markup=_back_kb())
        return

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔗 Перейти к оплате", url=link))
    kb.add(InlineKeyboardButton("🔙 В меню", callback_data=f"{CB}menu"))
    bot.send_message(
        chat_id,
        f"✅ Ссылка на оплату ({_esc(config.get('paymethod_name') or 'оплата')}) "
        f"на сумму <b>{_esc(amount)}</b>:\n\n{_esc(link)}",
        parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


# --------------------------------------------------------------------------- #
#  Регистрация хэндлеров Telegram (BIND_TO_PRE_INIT)                           #
# --------------------------------------------------------------------------- #
def init_commands(cardinal: "Cardinal", *args) -> None:
    global bot, cardinal_instance
    cardinal_instance = cardinal

    if cardinal.telegram is None:
        logger.warning(f"{LOGGER_PREFIX} Telegram-бот отключён, плагин не активен.")
        return

    bot = cardinal.telegram.bot
    load_config()
    logger.info(f"{LOGGER_PREFIX} init_commands()")

    cardinal.add_telegram_commands(UUID, [
        ("firstbyte", "Оплата FirstByte (СБП и др.)", True),
    ])

    # ---- команда открытия панели ---- #
    @bot.message_handler(commands=["firstbyte"])
    def cmd_panel(message: types.Message):
        if not _is_admin(message.chat.id):
            return
        open_panel(message.chat.id)

    # ---- кнопка «Настройки» в карточке плагина FPC ---- #
    try:
        from tg_bot import CBT as _CBT
        settings_prefix = f"{_CBT.PLUGIN_SETTINGS}:{UUID}:"
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} импорт CBT не удался: {e}")
        settings_prefix = None

    if settings_prefix:
        @bot.callback_query_handler(func=lambda c: c.data.startswith(settings_prefix))
        def open_from_settings(call: types.CallbackQuery):
            open_panel(call.message.chat.id, call.message.message_id)
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass

    # ---- навигация / меню ---- #
    @bot.callback_query_handler(func=lambda c: c.data == f"{CB}menu")
    def cb_menu(call: types.CallbackQuery):
        open_panel(call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id)

    # ---- оплата сохранённой суммой ---- #
    @bot.callback_query_handler(func=lambda c: c.data == f"{CB}pay_saved")
    def cb_pay_saved(call: types.CallbackQuery):
        bot.answer_callback_query(call.id, "Создаю ссылку...")
        _send_payment_link(call.message.chat.id, config.get("amount", ""))

    # ---- оплата произвольной суммой ---- #
    @bot.callback_query_handler(func=lambda c: c.data == f"{CB}pay_custom")
    def cb_pay_custom(call: types.CallbackQuery):
        msg = bot.send_message(call.message.chat.id, "Введите сумму для оплаты:")
        bot.register_next_step_handler(msg, step_pay_custom)
        bot.answer_callback_query(call.id)

    def step_pay_custom(message: types.Message):
        _send_payment_link(message.chat.id, message.text.strip())

    # ---- сохранённая сумма ---- #
    @bot.callback_query_handler(func=lambda c: c.data == f"{CB}set_amount")
    def cb_set_amount(call: types.CallbackQuery):
        msg = bot.send_message(call.message.chat.id, "Введите сумму по умолчанию:")
        bot.register_next_step_handler(msg, step_set_amount)
        bot.answer_callback_query(call.id)

    def step_set_amount(message: types.Message):
        val = message.text.strip().replace(",", ".")
        try:
            float(val)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Некорректная сумма.", reply_markup=_back_kb())
            return
        config["amount"] = val
        save_config()
        bot.send_message(message.chat.id, f"✅ Сумма по умолчанию: {val}", reply_markup=_back_kb())

    # ---- cookie ---- #
    @bot.callback_query_handler(func=lambda c: c.data == f"{CB}set_cookie")
    def cb_set_cookie(call: types.CallbackQuery):
        msg = bot.send_message(
            call.message.chat.id,
            "Пришлите значение cookie <b>billmgrses5</b> с billing.firstbyte.ru\n"
            "(F12 → Application → Cookies → billmgrses5).",
            parse_mode="HTML")
        bot.register_next_step_handler(msg, step_set_cookie)
        bot.answer_callback_query(call.id)

    def step_set_cookie(message: types.Message):
        config["cookie"] = message.text.strip()
        config["profile_id"] = ""  # сбрасываем кэш профиля — возможно, другой аккаунт
        save_config()
        if check_auth():
            bot.send_message(message.chat.id, "✅ Cookie сохранена, сессия активна.",
                             reply_markup=_back_kb())
        else:
            bot.send_message(
                message.chat.id,
                "⚠️ Cookie сохранена, но сессия не подтвердилась. Проверьте значение.",
                reply_markup=_back_kb())

    # ---- выбор способа оплаты ---- #
    @bot.callback_query_handler(func=lambda c: c.data == f"{CB}methods")
    def cb_methods(call: types.CallbackQuery):
        bot.answer_callback_query(call.id)
        if not config.get("cookie"):
            bot.send_message(call.message.chat.id,
                             "Сначала задайте cookie (🍪 Обновить cookie).",
                             reply_markup=_back_kb())
            return
        try:
            methods = list_methods(config.get("payment_currency", "126"))
        except BillmgrError as e:
            hint = " Обновите cookie." if e.is_auth() else ""
            bot.send_message(call.message.chat.id, f"❌ {_esc(e.message)}.{hint}",
                             reply_markup=_back_kb())
            return
        if not methods:
            bot.send_message(call.message.chat.id,
                             "Нет доступных способов оплаты. Проверьте данные плательщика.",
                             reply_markup=_back_kb())
            return
        kb = InlineKeyboardMarkup(row_width=1)
        for m in methods:
            label = f"{m['name']} (от {m['min']} {m['currency']})"
            kb.add(InlineKeyboardButton(label[:64], callback_data=f"{CB}m:{m['id']}"))
        kb.add(InlineKeyboardButton("🔙 В меню", callback_data=f"{CB}menu"))
        bot.edit_message_text("Выберите способ оплаты:", call.message.chat.id,
                              call.message.message_id, reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB}m:"))
    def cb_pick_method(call: types.CallbackQuery):
        method_id = call.data[len(f"{CB}m:"):]
        name = ""
        try:
            for m in list_methods(config.get("payment_currency", "126")):
                if m["id"] == method_id:
                    name = m["name"]
                    break
        except BillmgrError:
            pass
        config["paymethod"] = method_id
        config["paymethod_name"] = name or method_id
        save_config()
        bot.answer_callback_query(call.id, "Способ оплаты сохранён.")
        open_panel(call.message.chat.id, call.message.message_id)

    # ---- данные плательщика ---- #
    @bot.callback_query_handler(func=lambda c: c.data == f"{CB}payer")
    def cb_payer(call: types.CallbackQuery):
        p = config.get("payer", {})
        text = (
            "👤 <b>Данные плательщика</b> (для создания профиля при первом платеже):\n\n"
            f"ФИО: <b>{_esc(p.get('person') or '—')}</b>\n"
            f"Индекс: <b>{_esc(p.get('postcode') or '—')}</b>\n"
            f"Город: <b>{_esc(p.get('city') or '—')}</b>\n"
            f"Адрес: <b>{_esc(p.get('address') or '—')}</b>\n"
        )
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("ФИО", callback_data=f"{CB}payer_set:person"),
            InlineKeyboardButton("Индекс", callback_data=f"{CB}payer_set:postcode"),
        )
        kb.add(
            InlineKeyboardButton("Город", callback_data=f"{CB}payer_set:city"),
            InlineKeyboardButton("Адрес", callback_data=f"{CB}payer_set:address"),
        )
        kb.add(InlineKeyboardButton("🔙 В меню", callback_data=f"{CB}menu"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              parse_mode="HTML", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB}payer_set:"))
    def cb_payer_set(call: types.CallbackQuery):
        field = call.data[len(f"{CB}payer_set:"):]
        titles = {"person": "ФИО", "postcode": "индекс", "city": "город", "address": "адрес"}
        msg = bot.send_message(call.message.chat.id, f"Введите {titles.get(field, field)}:")
        bot.register_next_step_handler(msg, step_payer_set, field)
        bot.answer_callback_query(call.id)

    def step_payer_set(message: types.Message, field: str):
        config.setdefault("payer", {})[field] = message.text.strip()
        config["profile_id"] = ""  # данные изменились — пересоздадим профиль при необходимости
        save_config()
        bot.send_message(message.chat.id, "✅ Сохранено.", reply_markup=_back_kb())

    logger.info(f"{LOGGER_PREFIX} Хэндлеры Telegram зарегистрированы.")


def on_plugin_unload(*args, **kwargs) -> None:
    """Вызывается FPC при выгрузке / перезагрузке плагина."""
    logger.info(f"{LOGGER_PREFIX} Плагин выгружается.")


# --------------------------------------------------------------------------- #
#  Привязка к событиям FPC                                                     #
# --------------------------------------------------------------------------- #
BIND_TO_PRE_INIT = [init_commands]
BIND_TO_DELETE = on_plugin_unload

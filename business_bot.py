"""Telegram Business assistant for non-official gift proposals.
Logic: Edits the command message immediately to show the offer.
"""

import asyncio
import html
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
# Изменено: по умолчанию 5 часов (плюс 59 минут добавляются в таймере)
TTL_HOURS = int(os.getenv("OFFER_TTL_HOURS", "5"))
router = Router()
COMMAND = re.compile(r"^купить\s+(https?://t\.me/nft/[^\s?#/]+)\s+(\S+)(?:\s+(.*))?$", re.IGNORECASE)
GIFT_EMOJI_HTML = '<tg-emoji emoji-id="6032937473162614352">🎁</tg-emoji>'


@dataclass
class Offer:
    id: str
    connection_id: str
    chat_id: int
    message_id: int
    buyer_id: int
    buyer_name: str
    link: str
    slug: str
    gift_title: str
    price: str
    currency: str
    expires_at: datetime
    status: str = "active"


offers: dict[str, Offer] = {}


def gift_slug_from_link(link: str) -> str:
    match = re.search(r"/nft/([^/?#]+)", link)
    return match.group(1) if match else ""


def gift_title_from_slug(slug: str) -> str:
    name, _, num = slug.rpartition("-")
    if name and num.isdigit():
        return f"{name} #{int(num):,}"
    return slug or "подарок"


def parse_command(text: str) -> tuple[str, str, str] | None:
    match = COMMAND.fullmatch(text.strip())
    if not match:
        return None
    link, raw_price, flags = match.groups()
    try:
        price = Decimal(raw_price.replace(",", "."))
        if price <= 0:
            return None
    except InvalidOperation:
        return None
    currency = "TON" if "gram" in set((flags or "").lower().split()) else "Stars"
    return link, format(price.normalize(), "f"), currency


def remaining(offer: Offer) -> str:
    seconds = max(0, int((offer.expires_at - datetime.now(UTC)).total_seconds()))
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    return f"{hours}ч {minutes:02d}м"


def proposal_text(offer: Offer) -> str:
    # Ссылка в названии подарка теперь также выделена жирным, как и таймер
    return (
        f"Пользователь <b>{html.escape(offer.buyer_name)}</b> предлагает Вам\n"
        f"<b>{offer.price} {offer.currency}</b> за подарок "
        f'<b><a href="{offer.link}">{html.escape(offer.gift_title)}</a></b>.\n\n'
        f'<b>Предложение действует ещё {remaining(offer)}.</b>'
    )


def sold_text(offer: Offer) -> str:
    return (
        f"{GIFT_EMOJI_HTML} Подарок: {html.escape(offer.gift_title)} успешно продан!\n\n"
        f"• Передайте подарок покупателю для зачисления {offer.currency}."
    )


def sold_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Передать", url=f"tg://user?id={offer.buyer_id}")],
    ])


def proposal_keyboard(offer_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✕ Отклонить", callback_data=f"decline:{offer_id}"),
        InlineKeyboardButton(text="✓ Принять", callback_data=f"accept:{offer_id}"),
    ]])


def proposal_preview_options() -> LinkPreviewOptions:
    # show_above_text=True делает картинку сверху (карточка)
    return LinkPreviewOptions(is_disabled=False, show_above_text=True)


@router.business_message(F.text)
async def create_offer(message: Message, bot: Bot) -> None:
    """Редактирует сообщение с командой 'на месте', превращая его в оффер."""
    if not message.text or not message.business_connection_id:
        return
    parsed = parse_command(message.text)
    if not parsed:
        return
    link, price, currency = parsed
    try:
        connection = await bot.get_business_connection(message.business_connection_id)
        buyer = connection.user
        buyer_id = buyer.id
        buyer_name = (
            f"@{buyer.username}" if getattr(buyer, "username", None)
            else (buyer.full_name if hasattr(buyer, "full_name") else f"id{buyer.id}")
        )
        
        slug = gift_slug_from_link(link)
        temporary = Offer(
            id=secrets.token_urlsafe(8), connection_id=message.business_connection_id,
            chat_id=message.chat.id, message_id=message.message_id, 
            buyer_id=buyer_id,
            buyer_name=buyer_name, link=link, slug=slug,
            gift_title=gift_title_from_slug(slug),
            price=price, currency=currency,
            # Изменено: + 59 минут к указанным часам
            expires_at=datetime.now(UTC) + timedelta(hours=TTL_HOURS, minutes=59), 
        )
        
        # Редактируем сообщение НА МЕСТЕ (без удаления и отправки)
        await bot.edit_message_text(
            business_connection_id=message.business_connection_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=proposal_text(temporary),
            reply_markup=proposal_keyboard(temporary.id),
            link_preview_options=proposal_preview_options(),
        )
        offers[temporary.id] = temporary

    except Exception as e:
        # Если ошибка (например, PEER_FLOOD), удаляем команду
        error_text = str(e)
        if "PEER_FLOOD" in error_text or "FLOOD_WAIT" in error_text:
            logging.warning("PEER_FLOOD encountered, command removed.")
        else:
            logging.exception("Could not create business proposal: %s", e)

        # Очищаем команду в любом случае при ошибке
        try:
            await bot.delete_business_messages(
                business_connection_id=message.business_connection_id,
                message_ids=[message.message_id],
            )
        except:
            pass


@router.callback_query(F.data.startswith(("accept:", "decline:")))
async def resolve_offer(callback: CallbackQuery, bot: Bot) -> None:
    action, offer_id = callback.data.split(":", 1)
    offer = offers.get(offer_id)
    if not offer or offer.status != "active":
        await callback.answer("Предложение уже закрыто или бот был перезапущен.", show_alert=True)
        return
    if datetime.now(UTC) >= offer.expires_at:
        offer.status = "expired"
        await callback.answer("Срок предложения истёк.", show_alert=True)
        return
    if action == "decline":
        offer.status = "declined"
        await bot.edit_message_text(
            business_connection_id=offer.connection_id, chat_id=offer.chat_id,
            message_id=offer.message_id,
            text="❌ <b>Предложение отклонено</b>\nСредства не резервировались и никакая оплата не выполнялась.",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        await callback.answer("Предложение отклонено.")
        return
    offer.status = "accepted"
    await bot.edit_message_text(
        business_connection_id=offer.connection_id, chat_id=offer.chat_id,
        message_id=offer.message_id,
        text=sold_text(offer),
        reply_markup=sold_keyboard(offer),
    )
    await callback.answer("Оффер принят. Оплата не выполнялась.")


async def refresh_loop(bot: Bot) -> None:
    """Live-update remaining time on active offers, then expire them."""
    while True:
        await asyncio.sleep(30)
        now = datetime.now(UTC)
        for offer in list(offers.values()):
            if offer.status != "active":
                continue
            if now >= offer.expires_at:
                offer.status = "expired"
                try:
                    await bot.edit_message_text(
                        business_connection_id=offer.connection_id, chat_id=offer.chat_id,
                        message_id=offer.message_id,
                        text="⚠️ <b>Предложение истекло</b>\nСредства не резервировались и никакая оплата не выполнялась.",
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                except Exception:
                    logging.exception("Could not expire proposal %s", offer.id)
                continue
            try:
                await bot.edit_message_text(
                    business_connection_id=offer.connection_id, chat_id=offer.chat_id,
                    message_id=offer.message_id,
                    text=proposal_text(offer),
                    reply_markup=proposal_keyboard(offer.id),
                    link_preview_options=proposal_preview_options(),
                )
            except Exception:
                logging.debug("Could not refresh timer for %s", offer.id)


async def main() -> None:
    if not TOKEN:
        raise RuntimeError("Заполните BOT_TOKEN в .env")
    if not 1 <= TTL_HOURS <= 72:
        raise RuntimeError("OFFER_TTL_HOURS должен быть от 1 до 72.")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    task = asyncio.create_task(refresh_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

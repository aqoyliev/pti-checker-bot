import logging
from aiogram.utils.exceptions import (Unauthorized, InvalidQueryID, TelegramAPIError,
                                      CantDemoteChatCreator, MessageNotModified, MessageToDeleteNotFound,
                                      MessageTextIsEmpty, RetryAfter,
                                      CantParseEntities, MessageCantBeDeleted)

from loader import dp


@dp.errors_handler()
async def errors_handler(update, exception):
    if isinstance(exception, CantDemoteChatCreator):
        logging.warning("Can't demote chat creator")
        return True

    if isinstance(exception, MessageNotModified):
        logging.warning('Message is not modified')
        return True

    if isinstance(exception, MessageCantBeDeleted):
        logging.warning('Message cant be deleted')
        return True

    if isinstance(exception, MessageToDeleteNotFound):
        logging.warning('Message to delete not found')
        return True

    if isinstance(exception, MessageTextIsEmpty):
        logging.warning('MessageTextIsEmpty')
        return True

    if isinstance(exception, Unauthorized):
        logging.warning(f'Unauthorized: {exception}')
        return True

    if isinstance(exception, InvalidQueryID):
        logging.warning(f'InvalidQueryID: {exception} \nUpdate: {update}')
        return True

    if isinstance(exception, RetryAfter):
        logging.warning(f'RetryAfter: {exception} \nUpdate: {update}')
        return True

    if isinstance(exception, CantParseEntities):
        logging.warning(f'CantParseEntities: {exception} \nUpdate: {update}')
        return True

    if isinstance(exception, TelegramAPIError):
        logging.exception(f'TelegramAPIError: {exception} \nUpdate: {update}')
        return True

    logging.exception(f'Update: {update} \n{exception}')
    return True

from aiogram import Dispatcher

from loader import dp
from .group_activity import GroupActivityMiddleware
from .group_title import GroupTitleMiddleware
from .throttling import ThrottlingMiddleware


if __name__ == "middlewares":
    dp.middleware.setup(ThrottlingMiddleware())
    dp.middleware.setup(GroupTitleMiddleware())
    dp.middleware.setup(GroupActivityMiddleware())

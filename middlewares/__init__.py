from loader import dp
from .group_activity import GroupActivityMiddleware
from .group_title import GroupTitleMiddleware


if __name__ == "middlewares":
    dp.middleware.setup(GroupTitleMiddleware())
    dp.middleware.setup(GroupActivityMiddleware())

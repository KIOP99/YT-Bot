"""
models/__init__.py
------------------
Import all models here so SQLAlchemy's metadata is fully populated
before create_all or Alembic autogenerate runs.
"""

from models.base import Base, TimestampMixin
from models.user import User
from models.channel import YouTubeChannel
from models.video import Video
from models.thumbnail import Thumbnail
from models.schedule_config import ScheduleConfig
from models.upload_history import UploadHistory
from models.discord_config import DiscordConfig
from models.discord_bot import DiscordBot
from models.comment import CommentConfig, CommentTemplate, CommentHistory
from models.seller_config import SellerConfig

__all__ = [
    "Base",
    "TimestampMixin",
    "User",
    "YouTubeChannel",
    "Video",
    "Thumbnail",
    "ScheduleConfig",
    "UploadHistory",
    "DiscordConfig",
    "DiscordBot",
    "CommentConfig",
    "CommentTemplate",
    "CommentHistory",
    "SellerConfig",
]


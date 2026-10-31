from sqlalchemy import Boolean, Column, DateTime, Integer, LargeBinary, String, Text
from datetime import datetime

from database import Base


class BlogPost(Base):
    """One post on the public /blog pages, written and edited from the admin console.

    The body is an ordered list of blocks (heading, paragraph, list, image, cta) stored as
    JSON, so the author decides exactly where each heading, paragraph and picture sits —
    the same shape the frontend renders. Images are referenced by BlogImage id, never by a
    URL, so the backend's address can change without rewriting every post.
    """
    __tablename__ = "blog_posts"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, nullable=False, unique=True, index=True)   # the URL: /blog/<slug>
    title = Column(String, nullable=False)
    category = Column(String, nullable=True)
    highlight = Column(String, nullable=True)      # part of the title shown in the accent colour
    author = Column(String, nullable=True)
    excerpt = Column(String, nullable=True)        # card summary + meta description
    keywords = Column(String, nullable=True)       # meta keywords
    tags = Column(Text, nullable=False, default="[]")      # JSON list of strings
    blocks = Column(Text, nullable=False, default="[]")    # JSON list of blocks
    cover_image_id = Column(Integer, nullable=True)

    # A post is public only when published AND its date has come — a future date schedules it.
    published = Column(Boolean, nullable=False, default=False)
    publish_date = Column(String, nullable=False)  # "YYYY-MM-DD", shown on the post

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String, nullable=True)     # the admin's email


class BlogImage(Base):
    """An uploaded picture for a post. Kept in the database rather than on the server's disk,
    which is replaced on every deploy — a file saved there would vanish with the next one."""
    __tablename__ = "blog_images"

    id = Column(Integer, primary_key=True, index=True)
    content_type = Column(String, nullable=False)
    data = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String, nullable=True)

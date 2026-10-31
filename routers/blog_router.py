"""The blog: public reading at /blog/..., writing from the admin console at /admin/blog/...

Posts used to live in a frontend source file, so every new post or typo fix needed a code
change and a deploy. They are database rows now, edited from the admin panel.

Public routes return only what a visitor may see: published posts whose date has arrived.
Admin routes go through get_admin_user (404 for everyone else, like the rest of /admin).
"""
import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import SessionLocal, get_db
from dependencies import get_admin_user
from models.blog_model import BlogImage, BlogPost
from models.user_model import User

logger = logging.getLogger("blog")
public = APIRouter(prefix="/blog", tags=["Blog"])
admin = APIRouter(prefix="/admin/blog", tags=["Admin"], dependencies=[Depends(get_admin_user)])

BLOCK_TYPES = {"heading", "paragraph", "list", "image", "cta"}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _today() -> str:
    return date.today().isoformat()


def _out(p: BlogPost) -> dict:
    return {
        "id": p.id, "slug": p.slug, "title": p.title, "category": p.category,
        "highlight": p.highlight, "author": p.author, "excerpt": p.excerpt,
        "keywords": p.keywords, "tags": json.loads(p.tags or "[]"),
        "blocks": json.loads(p.blocks or "[]"), "cover_image_id": p.cover_image_id,
        "published": bool(p.published), "date": p.publish_date,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "updated_by": p.updated_by,
    }


def _visible(db: Session):
    return (db.query(BlogPost)
              .filter(BlogPost.published.is_(True), BlogPost.publish_date <= _today()))


# ── public ───────────────────────────────────────────────────────────────────────

@public.get("/posts")
def list_posts(db: Session = Depends(get_db)):
    rows = _visible(db).order_by(BlogPost.publish_date.desc(), BlogPost.id.asc()).all()
    return {"posts": [_out(p) for p in rows]}


@public.get("/posts/{slug}")
def get_post(slug: str, db: Session = Depends(get_db)):
    p = _visible(db).filter(BlogPost.slug == slug).first()
    if not p:
        raise HTTPException(status_code=404, detail="Post not found")
    return _out(p)


@public.get("/images/{image_id}")
def get_image(image_id: int, db: Session = Depends(get_db)):
    img = db.get(BlogImage, image_id)
    if not img:
        raise HTTPException(status_code=404, detail="Image not found")
    # An image id is never reused for different bytes, so it can be cached for good.
    return Response(content=img.data, media_type=img.content_type,
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ── admin ────────────────────────────────────────────────────────────────────────

class PostIn(BaseModel):
    slug: str = ""
    title: str
    category: Optional[str] = None
    highlight: Optional[str] = None
    author: Optional[str] = None
    excerpt: Optional[str] = None
    keywords: Optional[str] = None
    tags: list[str] = []
    blocks: list[dict[str, Any]] = []
    cover_image_id: Optional[int] = None
    published: bool = False
    date: Optional[str] = None


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:80]


def _clean(body: PostIn, db: Session, post_id: Optional[int] = None) -> dict:
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="A title is required.")
    slug = slugify(body.slug or title)
    if not slug:
        raise HTTPException(status_code=400, detail="The URL name needs letters or numbers.")
    clash = db.query(BlogPost).filter(BlogPost.slug == slug, BlogPost.id != (post_id or 0)).first()
    if clash:
        raise HTTPException(status_code=409, detail=f"Another post already uses the URL /blog/{slug}.")
    day = (body.date or "").strip() or _today()
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=400, detail="The date must look like 2026-10-31.")
    bad = [b.get("type") for b in body.blocks if b.get("type") not in BLOCK_TYPES]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown block type(s): {', '.join(map(str, bad))}")
    tags = [t.strip().lstrip("#") for t in body.tags if t and t.strip().lstrip("#")]
    return dict(
        slug=slug, title=title,
        category=(body.category or "").strip() or None,
        highlight=(body.highlight or "").strip() or None,
        author=(body.author or "").strip() or None,
        excerpt=(body.excerpt or "").strip() or None,
        keywords=(body.keywords or "").strip() or None,
        tags=json.dumps(tags), blocks=json.dumps(body.blocks),
        cover_image_id=body.cover_image_id, published=bool(body.published), publish_date=day,
    )


@admin.get("/posts")
def admin_list(db: Session = Depends(get_db)):
    rows = db.query(BlogPost).order_by(BlogPost.publish_date.desc(), BlogPost.id.asc()).all()
    return {"posts": [_out(p) for p in rows], "today": _today()}


@admin.post("/posts")
def admin_create(body: PostIn, db: Session = Depends(get_db),
                 user: User = Depends(get_admin_user)):
    p = BlogPost(**_clean(body, db), updated_by=user.email)
    db.add(p)
    db.commit()
    logger.warning("admin: %s created blog post %s", user.email, p.slug)
    return _out(p)


@admin.put("/posts/{post_id}")
def admin_update(post_id: int, body: PostIn, db: Session = Depends(get_db),
                 user: User = Depends(get_admin_user)):
    p = db.get(BlogPost, post_id)
    if not p:
        raise HTTPException(status_code=404, detail="Post not found")
    for k, v in _clean(body, db, post_id).items():
        setattr(p, k, v)
    p.updated_by = user.email
    db.commit()
    logger.warning("admin: %s updated blog post %s", user.email, p.slug)
    return _out(p)


@admin.delete("/posts/{post_id}")
def admin_delete(post_id: int, db: Session = Depends(get_db),
                 user: User = Depends(get_admin_user)):
    p = db.get(BlogPost, post_id)
    if not p:
        raise HTTPException(status_code=404, detail="Post not found")
    db.delete(p)
    db.commit()
    logger.warning("admin: %s deleted blog post %s", user.email, p.slug)
    return {"deleted": post_id}


@admin.post("/images")
async def admin_upload_image(file: UploadFile = File(...), db: Session = Depends(get_db),
                             user: User = Depends(get_admin_user)):
    if file.content_type not in IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Upload a PNG, JPG, WebP or GIF image.")
    data = await file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="That image is over 5 MB — please use a smaller one.")
    img = BlogImage(content_type=file.content_type, data=data, created_by=user.email)
    db.add(img)
    db.commit()
    return {"id": img.id}


# ── first-run seed ───────────────────────────────────────────────────────────────

SEED_DIR = Path(__file__).resolve().parent.parent / "seed" / "blog"


def seed_blog_posts() -> None:
    """Load the launch posts (seed/blog/posts.json + their covers) into an EMPTY table.

    Runs on every startup but only ever writes once: as soon as one post exists — seeded or
    written by an admin — it does nothing, so a post an admin deletes never comes back.
    """
    db = SessionLocal()
    try:
        if db.query(BlogPost.id).first() is not None:
            return
        posts = json.loads((SEED_DIR / "posts.json").read_text(encoding="utf-8"))
        for s in posts:
            cover_id = None
            cover = SEED_DIR / s.get("cover_file", "")
            if s.get("cover_file") and cover.is_file():
                img = BlogImage(content_type="image/png", data=cover.read_bytes(),
                                created_by="seed")
                db.add(img)
                db.flush()
                cover_id = img.id
            db.add(BlogPost(
                slug=s["slug"], title=s["title"], category=s.get("category"),
                highlight=s.get("highlight"), author=s.get("author"),
                excerpt=s.get("excerpt"), keywords=s.get("keywords"),
                tags=json.dumps(s.get("tags", [])), blocks=json.dumps(s.get("blocks", [])),
                cover_image_id=cover_id, published=True, publish_date=s["publish_date"],
                updated_by="seed"))
        db.commit()
        logger.info("blog: seeded %d posts", len(posts))
    except Exception:
        db.rollback()
        logger.exception("blog: seeding failed")
    finally:
        db.close()

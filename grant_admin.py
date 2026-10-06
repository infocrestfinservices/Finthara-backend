"""Grant or revoke admin access, by email, against the database.

Deliberately not an API. Admin is the one privilege that must not be reachable from a
request: an endpoint that grants it is one authorisation bug away from anyone granting it to
themselves, and there is no product reason to hand it out at runtime — it happens once per
staff member. This also adds the column, since there is no Alembic in this project and
`create_all` never alters an existing table.

    python grant_admin.py                       # who is an admin right now
    python grant_admin.py you@example.com       # grant
    python grant_admin.py you@example.com --revoke
    python grant_admin.py you@example.com --super   # admin + may change who is an admin
"""
import sys

from sqlalchemy import text

from database import SessionLocal, engine
from models.user_model import User


def ensure_column():
    with engine.connect() as con:
        exists = con.execute(text(
            "select 1 from information_schema.columns where table_name='users' "
            "and column_name='is_admin' and table_schema = current_schema()")).first()
        if exists:
            return
        con.execute(text("alter table users add column is_admin boolean not null default false"))
        con.commit()
        print("column users.is_admin: added")


def main():
    ensure_column()
    db = SessionLocal()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    revoke = "--revoke" in sys.argv
    make_super = "--super" in sys.argv and not revoke

    if not args:
        admins = db.query(User).filter(User.is_admin.is_(True)).all()
        print(f"admins: {len(admins)}")
        for u in admins:
            print(f"   {u.email}")
        if not admins:
            print("   (none — pass an email to grant)")
            print("\naccounts available:")
            for u in db.query(User).order_by(User.id).all():
                print(f"   {u.email}")
        return

    email = args[0].strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if not user:
        print(f"no account with the email {email!r}")
        return
    user.is_admin = not revoke
    if make_super:
        user.is_super_admin = True
    elif revoke:
        user.is_super_admin = False
    db.commit()
    print(f"{email} is {'NO LONGER' if revoke else 'now'} an admin"
          f"{' (super admin)' if make_super else ''}")


if __name__ == "__main__":
    main()

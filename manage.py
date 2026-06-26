"""Lille CLI til at administrere brugere/roller.

  python manage.py init                         # opret tabeller
  python manage.py adduser 7713099063 Dan pro   # tilføj/ret en bruger (rolle: pro|jun)
  python manage.py users                        # vis alle brugere
"""
import sys
from app import db


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd = args[0]
    db.init_db()

    if cmd == "init":
        print("Tabeller oprettet.")
    elif cmd == "adduser":
        telegram_id, navn, rolle = args[1], args[2], (args[3] if len(args) > 3 else "jun")
        db.upsert_user(telegram_id, navn, rolle)
        print(f"Gemt: {navn} ({rolle}) [{telegram_id}]")
    elif cmd == "users":
        for u in db.all_users():
            print(f"{u['telegram_id']:>14}  {u['rolle']:>3}  {u['navn']}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

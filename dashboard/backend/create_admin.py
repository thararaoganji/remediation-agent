"""One-time bootstrap: creates the first admin user. There's no
self-registration in this app, so this script (run once per environment,
against whichever Firestore GCP_PROJECT_ID points at) is the only way an
admin account comes into existence -- after that, admins create further
accounts from the dashboard's Users page.

Usage (from dashboard/backend/, with GCP_PROJECT_ID set):
    python create_admin.py --email you@example.com --password 'a-real-password'
"""

import argparse

from app import auth, firestore_db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()
    email = args.email.lower()  # matches login's/create_user's normalization

    if firestore_db.get_doc("users", email) is not None:
        print(f"A user with email {email} already exists -- not overwriting it.")
        return

    firestore_db.create_doc("users", {
        "email": email,
        "password_hash": auth.hash_password(args.password),
        "role": "admin",
        # Unlike an account created via the Users page, you're choosing
        # your own real password directly here -- no forced reset needed.
        "must_reset_password": False,
    }, doc_id=email)
    print(f"Created admin user: {email}")


if __name__ == "__main__":
    main()

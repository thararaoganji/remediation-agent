"""One-time bootstrap: creates the first admin user. There's no
self-registration in this app, so this script (run once per environment,
against whichever store CLOUD_PROVIDER/GCP_PROJECT_ID or
AZURE_COSMOS_ENDPOINT points at) is the only way an admin account comes
into existence -- after that, admins create further accounts from the
dashboard's Users page.

Usage (from dashboard/backend/, with CLOUD_PROVIDER and that provider's
own required env vars set):
    python create_admin.py --email you@example.com --password 'a-real-password'
"""

import argparse

from app import auth, storage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()
    email = args.email.lower()  # matches login's/create_user's normalization

    if storage.get_doc("users", email) is not None:
        print(f"A user with email {email} already exists -- not overwriting it.")
        return

    storage.create_doc("users", {
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

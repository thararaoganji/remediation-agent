"""One-time bootstrap: creates the first admin user. There's no
self-registration in this app, so this script (run once per environment,
against whichever store CLOUD_PROVIDER/GCP_PROJECT_ID or
AZURE_COSMOS_ENDPOINT points at) is the only way an admin account comes
into existence -- after that, admins create further accounts from the
dashboard's Users page.

Usage (from dashboard/backend/, with CLOUD_PROVIDER and that provider's
own required env vars set):
    python create_admin.py --email you@example.com --password 'a-real-password'

If the password you're passing is a temporary/placeholder one that someone
else (or an automated process) chose -- rather than the admin's own real
password entered directly -- add --must-reset-password so the app forces
a change on first login, same as an account created from the Users page.
"""

import argparse

from app import auth, storage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--must-reset-password",
        action="store_true",
        help="Force a password change on first login (use when --password is a temporary/placeholder value, not the admin's own chosen one).",
    )
    args = parser.parse_args()
    email = args.email.lower()  # matches login's/create_user's normalization

    if storage.get_doc("users", email) is not None:
        print(f"A user with email {email} already exists -- not overwriting it.")
        return

    storage.create_doc("users", {
        "email": email,
        "password_hash": auth.hash_password(args.password),
        "role": "admin",
        "must_reset_password": args.must_reset_password,
    }, doc_id=email)
    print(f"Created admin user: {email}")


if __name__ == "__main__":
    main()

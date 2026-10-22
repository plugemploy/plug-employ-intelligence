#!/bin/zsh
# Copies this Mac's database (leads, notes, accounts, settings) to the Render database.
# Run it once, right after the first Render deploy:  ~/Documents/plug-employ-intelligence/scripts/copy_db_to_render.sh
# It asks for Render's "External Database URL" (Render > plug-employ-db > Connect). Nothing is saved.
set -e
cd "$(dirname "$0")/.."
PG=/Applications/Postgres.app/Contents/Versions/latest/bin

LOCAL=$(grep '^DATABASE_URL=' .env | cut -d= -f2- | sed 's#^postgresql+psycopg://#postgresql://#')
[[ -n "$LOCAL" ]] || { echo "No DATABASE_URL in .env"; exit 1; }

echo "Paste Render's External Database URL, then press Return (it won't show on screen):"
read -s TARGET
echo
[[ "$TARGET" == postgres* ]] || { echo "That doesn't look like a database URL. Nothing was copied."; exit 1; }
[[ "$TARGET" == *"?"* ]] || TARGET="$TARGET?sslmode=require"

echo "This REPLACES everything in the Render database with this Mac's data."
read "ok?Type yes to continue: "
[[ "$ok" == "yes" ]] || { echo "Stopped. Nothing was copied."; exit 1; }

DUMP=$(mktemp -t plugdb).sql
"$PG/pg_dump" --no-owner --no-acl --clean --if-exists "$LOCAL" > "$DUMP"
"$PG/psql" --quiet --output=/dev/null --set ON_ERROR_STOP=1 "$TARGET" < "$DUMP"
rm -f "$DUMP"
echo "Done. Leads on Render: $("$PG/psql" -At "$TARGET" -c 'select count(*) from leads where deleted_at is null')"

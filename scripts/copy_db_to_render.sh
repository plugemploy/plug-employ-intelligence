#!/bin/zsh
# Copies this Mac's database (leads, notes, accounts, settings) to the Render database.
# Run it once, right after the first Render deploy:  ~/Documents/plug-employ-intelligence/scripts/copy_db_to_render.sh
# It asks for Render's "External Database URL" (Render > plug-employ-db > Connect). Nothing is saved.
# Or copy that URL, then run it with --clipboard to read the URL from the clipboard (no prompts).
set -e
cd "$(dirname "$0")/.."
PG=/Applications/Postgres.app/Contents/Versions/latest/bin

LOCAL=$(grep '^DATABASE_URL=' .env | cut -d= -f2- | sed 's#^postgresql+psycopg://#postgresql://#')
[[ -n "$LOCAL" ]] || { echo "No DATABASE_URL in .env"; exit 1; }

if [[ "$1" == "--clipboard" ]]; then
  # Copy the URL in Render first. Running with --clipboard means "yes, replace the Render data".
  TARGET=$(pbpaste)
else
  echo "Paste Render's External Database URL, then press Return (it won't show on screen):"
  read -s TARGET
  echo
fi
TARGET=$(echo "$TARGET" | tr -d '[:space:]')
[[ "$TARGET" == postgres* ]] || { echo "The clipboard doesn't hold a database URL. Copy it in Render (plug-employ-db > Connect > External) and try again. Nothing was copied."; exit 1; }
[[ "$TARGET" == *"?"* ]] || TARGET="$TARGET?sslmode=require"

if [[ "$1" != "--clipboard" ]]; then
  echo "This REPLACES everything in the Render database with this Mac's data."
  read "ok?Type yes to continue: "
  [[ "$ok" == "yes" ]] || { echo "Stopped. Nothing was copied."; exit 1; }
fi
echo "Copying this Mac's database to Render..."

DUMP=$(mktemp -t plugdb).sql
"$PG/pg_dump" --no-owner --no-acl --clean --if-exists "$LOCAL" > "$DUMP"
"$PG/psql" --quiet --output=/dev/null --set ON_ERROR_STOP=1 "$TARGET" < "$DUMP"
rm -f "$DUMP"
echo "Done. Leads on Render: $("$PG/psql" -At "$TARGET" -c 'select count(*) from leads where deleted_at is null')"

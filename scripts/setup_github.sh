#!/bin/bash
# GitHub に公開リポジトリを作り、Secrets を登録して、手動実行を1回起こす（ステップ6）。
# 認証情報は入札監視の .env から読む（値は画面に出さない）。
set -euo pipefail
cd "$(dirname "$0")/.."

REPO="jibunroom/naha-air-watch"
SRC_ENV="../nyusatsu/.env"
MAIL_TO="y.sota0820@gmail.com"

[ -f "$SRC_ENV" ] || { echo "❌ $SRC_ENV がありません"; exit 1; }

if ! gh repo view "$REPO" >/dev/null 2>&1; then
  gh repo create "$REPO" --public --source . --push
else
  git push -u origin HEAD
fi

# Actions が data/ をコミットできるように
gh api -X PUT "repos/$REPO/actions/permissions/workflow" \
  -f default_workflow_permissions=write -F can_approve_pull_request_reviews=false >/dev/null

for key in GEMINI_API_KEY SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS; do
  val=$(grep -E "^${key}=" "$SRC_ENV" | head -1 | cut -d= -f2-)
  [ -n "$val" ] || { echo "❌ $key が $SRC_ENV にありません"; exit 1; }
  printf '%s' "$val" | gh secret set "$key" --repo "$REPO"
done
printf '%s' "$MAIL_TO" | gh secret set MAIL_TO --repo "$REPO"
echo "✅ Secrets:"; gh secret list --repo "$REPO"

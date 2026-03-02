#!/usr/bin/env bash
# Migrate this repository to a new private GitHub repo.
#
# Usage: ./setup-private-repo.sh <new-private-repo-url>
# Example: ./setup-private-repo.sh https://github.com/gerza-lab/kalshi-mm-private.git

set -euo pipefail

NEW_ORIGIN="${1:-}"

if [[ -z "$NEW_ORIGIN" ]]; then
  echo "Usage: $0 <new-private-repo-url>"
  echo "Example: $0 https://github.com/gerza-lab/kalshi-mm-private.git"
  exit 1
fi

echo "Removing current origin remote..."
git remote remove origin

echo "Adding new origin: $NEW_ORIGIN"
git remote add origin "$NEW_ORIGIN"

echo "Pushing all branches and tags..."
git push -u origin --all
git push origin --tags

echo "Done. Repository is now pointing to: $NEW_ORIGIN"

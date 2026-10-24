# Psyndica System Map

Source of the published map page:
https://claude.ai/artifact/HwffprZMFv6Dnf2aaxz2KQ (private; share from the page's Share menu).

One self-contained file, no build step and no dependencies beyond Google Fonts.

## What lives where
- **This file:** the structure, every description, and each mechanic's
  **default** status. Treasury defaults reflect this repo as of 2026-10-24;
  every other mechanic defaults to Draft (a proposal, not agreed).
- **The page's shared database** (`statuses` collection): status changes
  operators make on the page, with who changed it and when. Each document
  is keyed `<node>--<slugified mechanic label>`. **Renaming a mechanic's
  label detaches its saved status**, so rename deliberately.

## Runtime capabilities
Declared at publish: `sample` (Analyse button: asks Claude on the viewer's
own account, no API key in the page), `db` (shared statuses; Contributors
and up write, Viewers read), `user` with the `profile` scope (names in the
change trail; only ids are stored).

## Updating
Edit `psyndica-system-map.html`, then republish to the same URL from a
Claude session (pass the URL so it updates in place rather than creating a
new page). Opening the file directly in a browser works as a read-only
preview: statuses show the defaults and Analyse stays hidden.

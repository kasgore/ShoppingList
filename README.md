# Family Shopping List

A self-hosted Python web app for managing your family's recipes,
generating a consolidated shopping list from picked recipes, and cooking
on your phone. Built to cover the everyday Paprika feature set without
needing a paid app or cloud account.

## Features

- **Recipe library** — add, edit, or delete recipes with photo, prep/cook
  times, servings, category (Dinner, Dessert, …), notes, rating, and
  favorite toggle. Card-grid listing with search and filters.
- **Import from a URL** — paste a recipe URL on the Recipes page and the app
  pulls the title, image, prep/cook time, ingredients, and instructions
  automatically (powered by
  [`recipe-scrapers`](https://github.com/hhursev/recipe-scrapers)). It guesses
  ingredient units and aisle categories; you review on the edit page.
- **Cook page** — phone-friendly recipe view with checkable ingredients,
  numbered steps, screen wake-lock, and print mode.
- **Pick & scale** — add a recipe to the list and bump the multiplier
  (`2` = double batch).
- **Smart aggregation** — ingredients are merged by name + unit, with the
  source recipe(s) shown next to each line so you can see why something
  is on the list.
- **Ad-hoc items** — anyone in the family can drop in "Diet Coke" or
  "Toilet Paper" without editing a recipe. Each ad-hoc item stays as its
  own line, optionally tagged with who added it.
- **Shop in the store** — check off items from your phone; the state is
  saved server-side so the rest of the family sees the same list.
- **Categorized list** — the list is grouped by aisle (Produce, Dairy,
  Pantry, Beverages, …) so you can shop top-to-bottom.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Then open <http://localhost:5000> on any device on your home network. To
let other family members hit it from their phones, find the host machine's
LAN IP and visit `http://<that-ip>:5000`.

### Configuration

Environment variables:

- `HOST` — bind address (default `0.0.0.0`)
- `PORT` — port (default `5000`)
- `DEBUG` — set to any value to enable Flask debug mode
- `SHOPPINGLIST_DB` — path to the SQLite file (default
  `shoppinglist.db` next to `app.py`)
- `SHOPPINGLIST_SECRET` — Flask secret key for flash messages

The SQLite database is created automatically on first run and seeded
with a starter recipe set if it's empty.

## How it merges ingredients

Two ingredient lines are combined when both their name and their unit
normalize to the same value. The unit normalizer maps common aliases
(e.g., `tbsp`, `Tablespoon`, `tablespoons` → `tbsp`), so the list stays
clean without you having to be perfectly consistent when entering recipes.

If the same ingredient is recorded with mismatched units (e.g., `8 oz`
in one recipe and `1 lb` in another), the two lines are kept separate so
the math stays honest — convert manually if you'd like them merged.

Ad-hoc items always render as their own line (even if they happen to
match a recipe ingredient) because you usually want the explicit thing
that was requested.

## Project layout

```
app.py                 Flask app + DB + aggregation
requirements.txt
shoppinglist.db        Created on first run (gitignored)
templates/
  base.html
  index.html           Plan & shop home page
  recipes.html         Recipe library
  recipe_form.html     New / edit recipe
static/
  style.css
  app.js
```

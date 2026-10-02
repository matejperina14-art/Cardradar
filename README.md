# CardRadar

CardRadar is a Flask web app for comparing Pokémon card/product prices.

## Render
Build Command:
`pip install -r requirements.txt`

Start Command:
`gunicorn app:app`

The included backend queries the listed shop search pages and extracts visible product prices. E-commerce HTML changes over time, so individual shop parsers may need adjustment after live testing.

## Important
Do not present a result as a verified current shop price unless the shop page was successfully fetched and parsed. The API returns only offers it can actually extract.

SQLite is used for the initial price history. On ephemeral hosting, use a persistent disk or PostgreSQL for durable long-term history.

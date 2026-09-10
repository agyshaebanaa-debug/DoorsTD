# TD Bot-as-a-Service

Multi-bot Tower Defense platform on aiogram 3.x. The root bot registers child bot tokens, stores them encrypted, gives every child bot a separate SQLite database and runs each child in its own asyncio polling task while feeding updates into a shared Dispatcher.

## Tariffs

- Free: 5,000 players, 25 concurrent battles, 40 units, 15 maps, lobby 4.
- Coins: 4,000 in-game coins for 30 days; 15,000 players, 60 battles, 100 units, 35 maps; CSV import/export.
- Stars Lite: 50 Telegram Stars for 30 days; 40,000 players, 120 battles, 250 units, 70 maps; automatic banner rotation.
- Studio Ultra: 150 Stars or 20,000 coins for 30 days; unlimited players, battles, units and maps; lobby 6; unlimited broadcasts.

Expired paid plans soft-downgrade to Free without deleting the child bot or its data.

## Run

Python 3.11+. Install `requirements.txt`, copy `.env.example` to `.env`, set `ROOT_BOT_TOKEN`, `ROOT_ADMIN_ID`, `TOKEN_ENCRYPTION_KEY`, then run `python doorstdbot.py`.

The previous Doors-specific lore, shop, consumables and achievements are not included. Seed content is neutral and editable by each child-bot owner.

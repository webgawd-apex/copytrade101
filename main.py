                            short(master["address"]),
                        )

            except asyncio.CancelledError:
                raise

            except Exception:
                LOG.exception(
                    "Monitoring loop error"
                )

            await asyncio.sleep(POLL_SECONDS)


# ====================================================
# WALLET RECOVERY THROUGH THE VPS CONSOLE
# ====================================================

async def export_wallet_cli(uid):
    pool = await asyncpg.create_pool(
        DSN,
        min_size=1,
        max_size=1,
    )

    try:
        row = await pool.fetchrow(
            """
            SELECT address,encrypted_secret
            FROM copy_users
            WHERE id=$1
            """,
            uid,
        )

        if not row or not row["encrypted_secret"]:
            raise SystemExit(
                "No wallet found for this user ID"
            )

        keypair = Keypair.from_bytes(
            safe_keyfile().decrypt(
                row["encrypted_secret"].encode()
            )
        )

        print("Wallet:", row["address"])
        print("PRIVATE KEY — KEEP SECRET:", str(keypair))

    finally:
        await pool.close()


# ====================================================
# MAIN ENTRY POINT
# ====================================================

def main():
    if (
        len(sys.argv) == 3
        and sys.argv[1] == "--export-wallet"
    ):
        asyncio.run(
            export_wallet_cli(int(sys.argv[2]))
        )
        return

    service = BotService()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(service.startup)
        .post_shutdown(service.shutdown)
        .build()
    )

    app.add_handler(
        CommandHandler(
            ["start", "menu"],
            service.start,
        )
    )

    for command, page in (
        ("wallet", "wallet"),
        ("send", "wallet"),
        ("settings", "settings"),
        ("masters", "masters"),
        ("history", "history"),
    ):
        async def open_page(update, ctx, target=page):
            if update.effective_chat.type != "private":
                return
            await service.show(update, target)

        app.add_handler(
            CommandHandler(command, open_page)
        )

    app.add_handler(
        CommandHandler("cancel", service.cancel)
    )

    app.add_handler(
        CallbackQueryHandler(service.click)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            service.text,
        )
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()

orders[tracking_code] = {"user_id": user_id, "status": "pending", "config": None}
        for admin_id in ADMINS:
            await context.bot.send_photo(admin_id, photo=file_id, caption=f"💰 رسید پرداخت از @{update.effective_user.username or user_id}\nتایید: /approve {tracking_code}\nرد: /reject {tracking_code}")
        await update.message.reply_text(f"✅ رسید شما ارسال شد. شماره پیگیری: {tracking_code}")
        context.user_data["waiting_payment"] = False

# تایید سفارش
async def approve(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMINS or not context.args:
        return
    tracking_code = int(context.args[0])
    if tracking_code not in orders:
        await update.message.reply_text("❌ سفارش پیدا نشد.")
        return
    configs = read_configs()
    if not configs:
        await update.message.reply_text("❌ هیچ کانفیگی موجود نیست.")
        return
    cfg = configs.pop(0)
    save_configs(configs)
    orders[tracking_code]["status"] = "approved"
    orders[tracking_code]["config"] = cfg
    user_id = orders[tracking_code]["user_id"]
    await context.bot.send_message(user_id, f"🎉 خرید شما تایید شد.\n📄 کانفیگ:\n{cfg}\n🔢 شماره پیگیری: {tracking_code}")
    await context.bot.send_message(ADMIN_GROUP_ID, f"📦 کانفیگ برای {user_id} ارسال شد.\n🔢 پیگیری: {tracking_code}\n{cfg}")

# رد سفارش
async def reject(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMINS or not context.args:
        return
    tracking_code = int(context.args[0])
    if tracking_code in orders:
        user_id = orders[tracking_code]["user_id"]
        await context.bot.send_message(user_id, "❌ سفارش شما رد شد. لطفاً مجدداً اقدام کنید.")
        del orders[tracking_code]
        await update.message.reply_text("✅ سفارش رد شد.")

# پیگیری سفارش
async def track(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("📌 استفاده: /track <شماره پیگیری>")
        return
    tracking_code = int(context.args[0])
    if tracking_code not in orders:
        await update.message.reply_text("❌ سفارش پیدا نشد.")
        return
    status = orders[tracking_code]["status"]
    await update.message.reply_text(f"📦 وضعیت سفارش: {status}")

# اجرای ربات
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("broadcast", broadcast))  # --- NEW ---
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, message_handler))
    print("ربات روشن شد ...")
    app.run_polling()

if name == "main":
    main()

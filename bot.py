import os
from dotenv import load_dotenv
from notion_client import Client
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Load .env variables
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

# Initialize Notion client
notion = Client(auth=NOTION_TOKEN)

# ------------------------------------------
# Command Format:
# /add <name> | <ins/outs> | <kategori>
# ------------------------------------------
# Example:
# /add Makan Siang Warteg | -25000 | Makanan
# ------------------------------------------
async def add_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text.replace("/add", "").strip()
        parts = [t.strip() for t in text.split("|")]

        if len(parts) != 3:
            await update.message.reply_text(
                "⚠️ Format salah!\nGunakan format:\n"
                "`/add Nama | +50000 | Kategori`",
                parse_mode="Markdown"
            )
            return

        name, ins_outs, category = parts

        # Konversi nilai ins_outs ke number
        ins_outs = ins_outs.replace("IDR", "").replace("Rp", "").replace(",", "").strip()
        ins_outs_value = float(ins_outs)

        # Ambil tanggal dari waktu pesan dikirim (UTC), hanya bagian tanggal (YYYY-MM-DD)
        date = update.message.date.date().isoformat()

        # Buat item baru di Notion
        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties={
                "Name": {"title": [{"text": {"content": name}}]},
                "Ins (+)/Outs (-)": {"number": ins_outs_value},
                "Kategori": {"select": {"name": category}},
                "Tanggal": {"date": {"start": date}},
            }
        )

        # Kirim respon ke Telegram
        sign = "📈" if ins_outs_value > 0 else "📉"
        await update.message.reply_text(
            f"{sign} Data berhasil disimpan ke Notion!\n\n"
            f"🏷️ *{name}*\n"
            f"💰 `{ins_outs_value:,.0f}`\n"
            f"📂 {category}\n"
            f"📅 {date}",
            parse_mode="Markdown"
        )

    except Exception as e:
        await update.message.reply_text(f"⚠️ Gagal menambahkan data.\n\nError: `{str(e)}`", parse_mode="Markdown")

# ------------------------------------------
# Jalankan Bot
# ------------------------------------------
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("add", add_transaction))

print("🤖 Bot Telegram Finance Tracker sedang berjalan...")
app.run_polling()

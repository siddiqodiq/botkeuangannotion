import os
import asyncio
from dotenv import load_dotenv
from notion_client import Client
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# Load .env variables
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

# Initialize Notion client
notion = Client(auth=NOTION_TOKEN)

# Define States
NAMA, TIPE, JUMLAH, KATEGORI = range(4)

# Keyboard Layouts
TYPE_KEYBOARD = [["📉 Pengeluaran (-)", "📈 Pemasukan (+)"]]
CATEGORY_KEYBOARD = [
    ["🍛 Makanan", "🚋 Transportasi"],
    ["💵 Pemasukan", "📶 Internet"],
    ["💳 Tagihan", "🏠 Alat Rumah"],
    ["🔮 Kerja/Pendidikan", "⛷️ Hobi"],
    ["😁 Perawatan/kebersihan", "☁️ Investasi/tabung"],
    ["💃 Fashion", "🍿 Hiburan"],
    ["🖥️ Elektronik", "⚡ Listrik"],
    ["🕋 Sedekah", "Kerugian 🤣"],
    ["🤔 Lainnya", "👾 hutang"]
]

# Mapping to Notion actual text (without icon)
CATEGORY_MAPPING = {
    "🍛 Makanan": "🍛Makanan",
    "🏎️ Transportasi": "🏎️Transportasi",
    "💵 Pemasukan": "💵Pemasukan",
    "📶 Internet": "🛜Internet",
    "💳 Tagihan": "💳Tagihan",
    "🏠 Alat Rumah": "🏠Alat Rumah",
    "🔮 Kerja/Pendidikan": "🔮Kerja/Pendidikan",
    "🏇 Hobi": "🏇Hobi",
    "😁 Perawatan/kebersihan": "😁Perawatan/kebersihan",
    "☁️ Investasi/tabung": "🪨Investasi/tabung",
    "🧑‍🎤 Fashion": "🧑‍🎤Fashion",
    "🍿 Hiburan": "🍿Hiburan",
    "🖥️ Elektronik": "🖥️Elektronik",
    "⚡ Listrik": "⚡Listrik",
    "🕋 Sedekah": "🕋Sedekah",
    "Kerugian 🤣": "Kerugian 🤣",
    "🤔 Lainnya": "🤔Lainnya",
    "👾 hutang": "👾hutang"
}

async def start_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.replace("/add", "").strip()
    
    if text:
        # Check if there is a comma separating name and amount
        if "," in text:
            parts = [p.strip() for p in text.split(",", 1)]
            nama = parts[0]
            jumlah_str = parts[1]
            
            # Check if it has + or -
            if not (jumlah_str.startswith("+") or jumlah_str.startswith("-")):
                await update.message.reply_text("⚠️ Format salah! Jika menggunakan koma, sertakan + atau - pada nominal (contoh: /add Makan, -50k)")
                return ConversationHandler.END
                
            context.user_data['nama'] = nama
            
            # Determine type
            if jumlah_str.startswith("-"):
                context.user_data['tipe'] = "📉 Pengeluaran (-)"
            else:
                context.user_data['tipe'] = "📈 Pemasukan (+)"
                
            jumlah_text = jumlah_str.replace("+", "").replace("-", "").lower().replace("idr", "").replace("rp", "").replace(".", "")
            
            # Deteksi k
            multiplier = 1
            if jumlah_text.endswith("k"):
                multiplier = 1000
                jumlah_text = jumlah_text[:-1].strip()
                
            try:
                jumlah_float = float(jumlah_text) * multiplier
                if context.user_data['tipe'] == "📉 Pengeluaran (-)":
                    jumlah_float = -abs(jumlah_float)
                else:
                    jumlah_float = abs(jumlah_float)
                    
                context.user_data['jumlah'] = jumlah_float
                
                reply_markup = ReplyKeyboardMarkup(CATEGORY_KEYBOARD, one_time_keyboard=True, resize_keyboard=True)
                await update.message.reply_text(
                    f"Nama: *{nama}*\nJumlah: *{jumlah_float:,.0f}*\n\n"
                    "Pilih kategori transaksi:",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
                return KATEGORI
            except ValueError:
                await update.message.reply_text("⚠️ Nominal tidak valid! Batalkan (/cancel) dan ulangi.")
                return ConversationHandler.END
        else:
            # Only Name is provided
            context.user_data['nama'] = text
            reply_markup = ReplyKeyboardMarkup(TYPE_KEYBOARD, one_time_keyboard=True, resize_keyboard=True)
            await update.message.reply_text(
                f"Nama: *{context.user_data['nama']}*\n\n"
                "Pilih jenis transaksi ini:",
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            return TIPE

    # Normal flow if no arguments
    await update.message.reply_text(
        "📝 Mari tambahkan transaksi baru.\n\n"
        "Silakan masukkan **Nama Transaksi**:",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return NAMA

async def ask_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['nama'] = update.message.text.strip()
    
    reply_markup = ReplyKeyboardMarkup(TYPE_KEYBOARD, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        f"Nama: *{context.user_data['nama']}*\n\n"
        "Pilih jenis transaksi ini:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return TIPE

async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tipe_pilihan = update.message.text.strip()
    
    if tipe_pilihan not in ["📉 Pengeluaran (-)", "📈 Pemasukan (+)"]:
        await update.message.reply_text("⚠️ Harap pilih jenis menggunakan tombol di bawah.")
        return TIPE
        
    context.user_data['tipe'] = tipe_pilihan
    
    await update.message.reply_text(
        "Silakan masukkan **Jumlah Nominal** (contoh: 50000 atau 50k):",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return JUMLAH

async def ask_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    jumlah_text = update.message.text.strip().lower().replace("idr", "").replace("rp", "").replace(",", "").replace(".", "")
    
    # Deteksi huruf 'k' untuk ribuan
    multiplier = 1
    if jumlah_text.endswith("k"):
        multiplier = 1000
        jumlah_text = jumlah_text[:-1].strip()
        
    try:
        jumlah_float = float(jumlah_text) * multiplier
    except ValueError:
        await update.message.reply_text("⚠️ Nominal tidak valid! Harap masukkan angka (contoh: 50000 atau 50k):")
        return JUMLAH
        
    # Set positive/negative based on type
    if context.user_data['tipe'] == "📉 Pengeluaran (-)":
        jumlah_float = -abs(jumlah_float)
    else:
        jumlah_float = abs(jumlah_float)
        
    context.user_data['jumlah'] = jumlah_float
    
    reply_markup = ReplyKeyboardMarkup(CATEGORY_KEYBOARD, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        f"Jumlah: *{jumlah_float:,.0f}*\n\n"
        "Pilih kategori transaksi:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return KATEGORI

async def save_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    kategori_raw = update.message.text.strip()
    
    # Flatten CATEGORY_KEYBOARD array to check if the selection is valid
    valid_categories = [item for sublist in CATEGORY_KEYBOARD for item in sublist]
    
    if kategori_raw not in valid_categories:
        await update.message.reply_text("⚠️ Kategori tidak valid. Harap pilih menggunakan tombol di bawah.")
        return KATEGORI
        
    # Get the actual Notion text string (stripping the emoji icon)
    kategori_notion = CATEGORY_MAPPING.get(kategori_raw, kategori_raw)
        
    # Gather data
    nama = context.user_data['nama']
    jumlah = context.user_data['jumlah']
    date = update.message.date.date().isoformat()
    
    await update.message.reply_text("⏳ Menyimpan data ke Notion...", reply_markup=ReplyKeyboardRemove())
    
    try:
        # Create Notion item
        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties={
                "Name": {"title": [{"text": {"content": nama}}]},
                "Ins (+)/Outs (-)": {"number": jumlah},
                "Kategori": {"select": {"name": kategori_notion}},
                "Tanggal": {"date": {"start": date}},
            }
        )
        
        sign = "📈" if jumlah > 0 else "📉"
        await update.message.reply_text(
            f"{sign} Data berhasil disimpan ke Notion!\n\n"
            f"🏷️ *{nama}*\n"
            f"💰 `{jumlah:,.0f}`\n"
            f"📂 {kategori_raw}\n"
            f"📅 {date}",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Gagal menambahkan data.\n\nError: `{str(e)}`", parse_mode="Markdown")
        
    # Clear user data
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "❌ Proses dibatalkan.", 
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data.clear()
    return ConversationHandler.END

# ------------------------------------------
# Jalankan Bot
# ------------------------------------------
if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add", start_add)],
        states={
            NAMA: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_type)],
            TIPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_amount)],
            JUMLAH: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_category)],
            KATEGORI: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_transaction)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    app.add_handler(conv_handler)
    
    print("Bot Telegram Finance Tracker sedang berjalan...")
    
    # Set event loop for Python 3.12/3.14 compatibility
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
        
    app.run_polling()

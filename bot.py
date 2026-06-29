import os
import asyncio
import datetime
import httpx
from dotenv import load_dotenv
from notion_client import Client
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
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

def is_for_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Helper untuk mencegah bot merespons command tanpa mention di grup."""
    if update.message and update.message.chat.type != "private":
        text = update.message.text or ""
        bot_username = context.bot.username
        # Jika di grup dan text tidak mengandung @username_bot, abaikan
        if bot_username and f"@{bot_username.lower()}" not in text.lower():
            return False
    return True

async def start_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message if update.message else update.callback_query.message
    if update.callback_query:
        await update.callback_query.answer()
        text = ""
    else:
        if not is_for_me(update, context):
            return ConversationHandler.END
        raw_text = update.message.text
        parts = raw_text.split(maxsplit=1)
        text = parts[1].strip() if len(parts) > 1 else ""
    
    if text:
        # Check if there is a comma separating name and amount
        if "," in text:
            parts = [p.strip() for p in text.split(",", 1)]
            nama = parts[0]
            jumlah_str = parts[1]
            
            # Check if it has + or -
            if not (jumlah_str.startswith("+") or jumlah_str.startswith("-")):
                await message.reply_text("⚠️ Format salah, King Odiq! Jika menggunakan koma, sertakan + atau - pada nominal (contoh: /add Makan, -50k)")
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
                
                reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(cat, callback_data=f"cat_{cat}") for cat in row] for row in CATEGORY_KEYBOARD])
                await message.reply_text(
                    f"Nama: *{nama}*\nJumlah: *{jumlah_float:,.0f}*\n\n"
                    "Pilih kategori transaksi, King Odiq:",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
                return KATEGORI
            except ValueError:
                await message.reply_text("⚠️ Nominal tidak valid! Batalkan (/cancel) dan ulangi.")
                return ConversationHandler.END
        else:
            # Only Name is provided
            context.user_data['nama'] = text
            reply_markup = ReplyKeyboardMarkup(TYPE_KEYBOARD, one_time_keyboard=True, resize_keyboard=True, selective=True)
            await message.reply_text(
                f"Nama: *{context.user_data['nama']}*\n\n"
                "Pilih jenis transaksi ini, King Odiq:",
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            return TIPE

    # Normal flow if no arguments
    await message.reply_text(
        "📝 Mari tambahkan transaksi baru.\n\n"
        "Silakan masukkan **Nama Transaksi**, King Odiq:",
        reply_markup=ForceReply(selective=True),
        parse_mode="Markdown"
    )
    return NAMA

async def ask_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['nama'] = update.message.text.strip()
    
    reply_markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📉 Pengeluaran (-)", callback_data="tipe_pengeluaran"),
            InlineKeyboardButton("📈 Pemasukan (+)", callback_data="tipe_pemasukan")
        ]
    ])
    await update.message.reply_text(
        f"Nama: *{context.user_data['nama']}*\n\n"
        "Pilih jenis transaksi ini, King Odiq:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return TIPE

async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tipe_pilihan_data = query.data
    
    if tipe_pilihan_data == "tipe_pengeluaran":
        tipe_pilihan = "📉 Pengeluaran (-)"
    elif tipe_pilihan_data == "tipe_pemasukan":
        tipe_pilihan = "📈 Pemasukan (+)"
    else:
        await query.message.reply_text("⚠️ Harap pilih jenis menggunakan tombol di bawah, King Odiq.")
        return TIPE
        
    context.user_data['tipe'] = tipe_pilihan
    
    await query.message.reply_text(
        "Silakan masukkan **Jumlah Nominal**, King Odiq (contoh: 50000 atau 50k):",
        reply_markup=ForceReply(selective=True),
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
        await update.message.reply_text("⚠️ Nominal tidak valid, King Odiq! Harap masukkan angka (contoh: 50000 atau 50k):")
        return JUMLAH
        
    # Set positive/negative based on type
    if context.user_data['tipe'] == "📉 Pengeluaran (-)":
        jumlah_float = -abs(jumlah_float)
    else:
        jumlah_float = abs(jumlah_float)
        
    context.user_data['jumlah'] = jumlah_float
    
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(cat, callback_data=f"cat_{cat}") for cat in row] for row in CATEGORY_KEYBOARD])
    await update.message.reply_text(
        f"Jumlah: *{jumlah_float:,.0f}*\n\n"
        "Pilih kategori transaksi, King Odiq:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return KATEGORI

async def save_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    kategori_raw = query.data.replace("cat_", "")
    
    # Flatten CATEGORY_KEYBOARD array to check if the selection is valid
    valid_categories = [item for sublist in CATEGORY_KEYBOARD for item in sublist]
    
    if kategori_raw not in valid_categories:
        await query.message.reply_text("⚠️ Kategori tidak valid, King Odiq. Harap pilih menggunakan tombol di bawah.")
        return KATEGORI
        
    # Get the actual Notion text string (stripping the emoji icon)
    kategori_notion = CATEGORY_MAPPING.get(kategori_raw, kategori_raw)
        
    # Gather data
    nama = context.user_data['nama']
    jumlah = context.user_data['jumlah']
    date = query.message.date.date().isoformat()
    
    await query.message.reply_text("⏳ Menyimpan data ke Notion, King Odiq...")
    
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
        await query.message.reply_text(
            f"{sign} Data berhasil disimpan ke Notion, King Odiq!\n\n"
            f"🏷️ *{nama}*\n"
            f"💰 `{jumlah:,.0f}`\n"
            f"📂 {kategori_raw}\n"
            f"📅 {date}",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD
        )
    except Exception as e:
        await query.message.reply_text(f"⚠️ Gagal menambahkan data, King Odiq.\n\nError: `{str(e)}`", parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
        
    # Clear user data
    context.user_data.clear()
    return ConversationHandler.END

async def invalid_inline_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⚠️ Harap pilih opsi menggunakan **tombol di atas**, King Odiq.", parse_mode="Markdown")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "❌ Proses dibatalkan, King Odiq.", 
        reply_markup=MAIN_KEYBOARD
    )
    context.user_data.clear()
    return ConversationHandler.END

MAIN_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("➕ Tambah Transaksi", callback_data="menu_add"),
            InlineKeyboardButton("📊 Laporan Bulan Ini", callback_data="menu_report")
        ],
        [
            InlineKeyboardButton("📋 Daftar Pengeluaran", callback_data="menu_list"),
            InlineKeyboardButton("❓ Bantuan", callback_data="menu_help")
        ]
    ]
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message if update.message else update.callback_query.message
    if update.message and not is_for_me(update, context):
        return
    pesan = (
        "Halo King Odiq! 👋 Saya adalah Bot Keuangan Notion Anda.\n\n"
        "Silakan pilih menu di bawah ini untuk mulai mencatat atau melihat pengeluaran Anda."
    )
    await message.reply_text(pesan, reply_markup=MAIN_KEYBOARD)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message if update.message else update.callback_query.message
    if update.message and not is_for_me(update, context):
        return
    pesan = (
        "💡 *Panduan Penggunaan Bot untuk King Odiq*\n\n"
        "Anda bisa menggunakan tombol menu di bawah atau mengetik perintah berikut:\n\n"
        "1️⃣ /add - Memulai pencatatan transaksi secara interaktif.\n"
        "   👉 *Cara Cepat:* `/add Makan siang, -25k`\n\n"
        "2️⃣ /report - Melihat ringkasan pemasukan, pengeluaran, dan saldo.\n"
        "   👉 *Bulan Spesifik:* `/report 5` (untuk bulan Mei)\n\n"
        "3️⃣ /list - Melihat daftar semua transaksi secara rinci.\n"
        "   👉 *Bulan Spesifik:* `/list 5` (untuk bulan Mei)\n\n"
        "4️⃣ /cancel - Membatalkan proses pencatatan."
    )
    await message.reply_text(pesan, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)

async def handle_inline_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    if query.data == "menu_report":
        await report(update, context)
    elif query.data == "menu_list" or query.data.startswith("list_all_"):
        await list_expenses(update, context)
    elif query.data == "menu_help":
        await help_command(update, context)


NAMA_BULAN = {
    1: "Januari", 2: "Februari", 3: "Maret", 4: "April",
    5: "Mei", 6: "Juni", 7: "Juli", 8: "Agustus",
    9: "September", 10: "Oktober", 11: "November", 12: "Desember"
}

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message if update.message else update.callback_query.message
    if update.message and not is_for_me(update, context):
        return
    today = datetime.date.today()
    
    # Cek apakah ada argumen bulan
    args = context.args
    if args:
        try:
            bulan = int(args[0])
            if bulan < 1 or bulan > 12:
                await message.reply_text("⚠️ Bulan harus antara 1-12, King Odiq. Contoh: /report 5")
                return
            tahun = today.year
            # Jika bulan yang diminta lebih besar dari bulan sekarang, ambil tahun lalu
            if bulan > today.month:
                tahun -= 1
        except ValueError:
            await message.reply_text("⚠️ Format salah, King Odiq. Contoh: /report 5 (untuk Mei)")
            return
    else:
        bulan = today.month
        tahun = today.year
    
    first_day = datetime.date(tahun, bulan, 1).isoformat()
    # Hitung hari pertama bulan berikutnya sebagai batas akhir
    if bulan == 12:
        last_day_next = datetime.date(tahun + 1, 1, 1).isoformat()
    else:
        last_day_next = datetime.date(tahun, bulan + 1, 1).isoformat()
    
    label_bulan = f"{NAMA_BULAN[bulan]} {tahun}"
    await message.reply_text(f"⏳ Sedang mengambil data laporan untuk King Odiq *{label_bulan}*...", parse_mode="Markdown")
    
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    payload = {
        "filter": {
            "and": [
                {
                    "property": "Tanggal",
                    "date": {"on_or_after": first_day}
                },
                {
                    "property": "Tanggal",
                    "date": {"before": last_day_next}
                }
            ]
        }
    }
    
    try:
        results = []
        has_more = True
        next_cursor = None
        
        async with httpx.AsyncClient() as client:
            while has_more:
                if next_cursor:
                    payload["start_cursor"] = next_cursor
                response = await client.post(url, headers=headers, json=payload, timeout=20.0)
                response.raise_for_status()
                data = response.json()
                results.extend(data.get('results', []))
                has_more = data.get('has_more', False)
                next_cursor = data.get('next_cursor')
            
        cats = {}
        total_pengeluaran = 0
        saldo = 0
        
        for r in results:
            p = r.get('properties', {})
            amt = p.get('Ins (+)/Outs (-)', {}).get('number') or 0
            saldo += amt
            
            if amt < 0:
                c = p.get('Kategori', {}).get('select')
                c_name = c.get('name') if c else 'Tanpa Kategori'
                cats[c_name] = cats.get(c_name, 0) + abs(amt)
                total_pengeluaran += abs(amt)
            
        pesan = f"📊 *Laporan Keuangan {label_bulan}*\n\n"
        
        if cats:
            pesan += "*Rincian Pengeluaran:*\n"
            for cat, amt in sorted(cats.items(), key=lambda x: x[1], reverse=True):
                pesan += f"▪️ {cat}: Rp {amt:,.0f}\n"
            pesan += "\n"
        else:
            pesan += "ℹ️ Belum ada pengeluaran di bulan ini, King Odiq.\n\n"
            
        pesan += f"📉 *Total Pengeluaran:* Rp {total_pengeluaran:,.0f}\n"
        pesan += f"💰 *Saldo:* Rp {saldo:,.0f}"
        
        await message.reply_text(pesan, parse_mode="Markdown")
        
    except Exception as e:
        await message.reply_text(f"❌ Terjadi kesalahan, King Odiq saat mengambil laporan:\n{e}")


async def list_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message if update.message else update.callback_query.message
    if update.message and not is_for_me(update, context):
        return
        
    today = datetime.date.today()
    query = update.callback_query
    show_all = False
    
    if query and query.data.startswith("list_all_"):
        parts = query.data.split("_")
        bulan = int(parts[2])
        tahun = int(parts[3])
        show_all = True
    else:
        args = context.args
        if args:
            try:
                bulan = int(args[0])
                if bulan < 1 or bulan > 12:
                    await message.reply_text("⚠️ Bulan harus antara 1-12, King Odiq. Contoh: /list 5")
                    return
                tahun = today.year
                if bulan > today.month:
                    tahun -= 1
            except ValueError:
                await message.reply_text("⚠️ Format salah, King Odiq. Contoh: /list 5 (untuk Mei)")
                return
        else:
            bulan = today.month
            tahun = today.year
    
    first_day = datetime.date(tahun, bulan, 1).isoformat()
    if bulan == 12:
        last_day_next = datetime.date(tahun + 1, 1, 1).isoformat()
    else:
        last_day_next = datetime.date(tahun, bulan + 1, 1).isoformat()
    
    label_bulan = f"{NAMA_BULAN[bulan]} {tahun}"
    await message.reply_text(f"⏳ Mengambil daftar transaksi *{label_bulan}* untuk King Odiq...", parse_mode="Markdown")
    
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    payload = {
        "filter": {
            "and": [
                {"property": "Tanggal", "date": {"on_or_after": first_day}},
                {"property": "Tanggal", "date": {"before": last_day_next}}
            ]
        },
        "sorts": [{"property": "Tanggal", "direction": "descending"}]
    }
    
    try:
        results = []
        has_more = True
        next_cursor = None
        
        async with httpx.AsyncClient() as client:
            while has_more:
                if next_cursor:
                    payload["start_cursor"] = next_cursor
                response = await client.post(url, headers=headers, json=payload, timeout=20.0)
                response.raise_for_status()
                data = response.json()
                results.extend(data.get('results', []))
                has_more = data.get('has_more', False)
                next_cursor = data.get('next_cursor')
        
        if not results:
            await message.reply_text(f"ℹ️ Belum ada transaksi di {label_bulan}, King Odiq.")
            return
        
        pesan = f"📋 *Daftar Transaksi {label_bulan}*\n\n"
        saldo_net = 0
        
        # Determine items to show
        items_to_show = results if show_all else results[:10]
        
        for i, r in enumerate(items_to_show, 1):
            p = r.get('properties', {})
            nama = ''
            title_list = p.get('Name', {}).get('title', [])
            if title_list:
                nama = title_list[0].get('plain_text', '-')
            
            amt = p.get('Ins (+)/Outs (-)', {}).get('number') or 0
            
            cat = p.get('Kategori', {}).get('select')
            cat_name = cat.get('name') if cat else '-'
            
            tanggal_raw = p.get('Tanggal', {}).get('date', {})
            tgl = tanggal_raw.get('start', '-') if tanggal_raw else '-'
            if tgl != '-':
                try:
                    dt = datetime.date.fromisoformat(tgl)
                    tgl = dt.strftime('%d/%m')
                except ValueError:
                    pass
            
            sign_str = "+" if amt > 0 else "-"
            pesan += f"{i}. {nama} | {sign_str}Rp {abs(amt):,.0f} | {cat_name} | {tgl}\n"
            
        for r in results:
            amt = r.get('properties', {}).get('Ins (+)/Outs (-)', {}).get('number') or 0
            saldo_net += amt
        
        if not show_all and len(results) > 10:
            pesan += f"\n_...dan {len(results) - 10} transaksi lainnya._"
            
        pesan += f"\n\n*Saldo Net:* Rp {saldo_net:,.0f} ({len(results)} transaksi)"
        
        reply_markup = None
        if not show_all and len(results) > 10:
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Tampilkan Semua Transaksi", callback_data=f"list_all_{bulan}_{tahun}")
            ]])
        
        # Telegram message limit is 4096 chars, split if needed
        if len(pesan) > 4096:
            for x in range(0, len(pesan), 4096):
                # Only attach markup to the last message
                is_last = (x + 4096) >= len(pesan)
                await message.reply_text(pesan[x:x+4096], parse_mode="Markdown", reply_markup=reply_markup if is_last else None)
        else:
            await message.reply_text(pesan, parse_mode="Markdown", reply_markup=reply_markup)
        
    except Exception as e:
        await message.reply_text(f"❌ Terjadi kesalahan, King Odiq:\n{e}")

# ------------------------------------------
# Jalankan Bot
# ------------------------------------------
if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("add", start_add),
            CallbackQueryHandler(start_add, pattern="^menu_add$")
        ],
        states={
            NAMA: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_type)],
            TIPE: [
                CallbackQueryHandler(ask_amount, pattern="^tipe_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, invalid_inline_input)
            ],
            JUMLAH: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_category)],
            KATEGORI: [
                CallbackQueryHandler(save_transaction, pattern="^cat_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, invalid_inline_input)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(handle_inline_menu, pattern="^(menu_(report|list|help)|list_all_.*)$"))
    app.add_handler(CallbackQueryHandler(list_expenses, pattern="^list_all_"))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("list", list_expenses))
    
    print("Bot Telegram Finance Tracker sedang berjalan...")
    
    # Set event loop for Python 3.12/3.14 compatibility
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
        
    app.run_polling()

import os
import re
import html
import asyncio
import logging
import datetime

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()

# Database Transaksi (arus) dan Aset (stok) pada page 💸 Keuangan.
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "3ab5c76555ba81248b75cdde93fc28a0").strip()
NOTION_ASET_DATABASE_ID = os.getenv("NOTION_ASET_DATABASE_ID", "5d9fcd61eaf34364b13b9fd5069e1ec7").strip()

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Waktu Indonesia Barat. Host deploy umumnya UTC, tanpa ini transaksi
# malam hari akan tercatat di tanggal yang salah.
WIB = datetime.timezone(datetime.timedelta(hours=7))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Define States
NAMA, TIPE, JUMLAH, KATEGORI = range(4)
TRF_DARI, TRF_KE, TRF_JUMLAH = range(10, 13)

# Nama kategori ditulis persis seperti opsi select di Notion supaya tidak perlu
# tabel pemetaan — sumber bug lama di mana label tombol dan opsi Notion beda.
CATEGORIES = [
    "🍛Makanan", "🚋 Transportasi",
    "💵Pemasukan", "🛜Internet",
    "💳Tagihan", "🏠Alat Rumah",
    "🔮Kerja/Pendidikan", "⛷️ Hobi",
    "😁Perawatan/kebersihan", "🪨Investasi/tabung",
    "💃 Fashion", "🍿Hiburan",
    "🖥️Elektronik", "⚡Listrik",
    "🕋Sedekah", "Kerugian 🤣",
    "🤔Lainnya", "👾hutang",
]

# Kategori penanda transfer aset — bukan pengeluaran (lihat brief bagian 1).
CAT_TRANSFER = "🪨Investasi/tabung"
CAT_PEMASUKAN = "💵Pemasukan"

# Kantong default untuk transaksi harian.
DEFAULT_ACCOUNT = "Kas"


# ------------------------------------------
# Rekap harian otomatis — konfigurasi
# ------------------------------------------

# Jam kirim rekap dalam WIB. Default 22:30, bisa digeser lewat config var
# tanpa menyentuh kode.
REKAP_JAM = int(os.getenv("REKAP_JAM", "").strip() or 22)
REKAP_MENIT = int(os.getenv("REKAP_MENIT", "").strip() or 30)


def _baca_daftar_id(nama: str) -> list:
    """Baca config var berisi id Telegram dipisah koma. Id grup boleh negatif."""
    hasil = []
    for potongan in os.getenv(nama, "").replace(" ", "").split(","):
        try:
            hasil.append(int(potongan))
        except ValueError:
            continue
    return hasil


# Chat tujuan rekap. Kalau REKAP_CHAT_IDS kosong, dipakai ALLOWED_USER_IDS yang
# sudah ada untuk dashboard — supaya hal yang sama tidak perlu diisi dua kali.
REKAP_CHAT_IDS = _baca_daftar_id("REKAP_CHAT_IDS") or _baca_daftar_id("ALLOWED_USER_IDS")

# Matikan rekap dengan REKAP_AKTIF=0, tanpa harus mengosongkan daftar chat.
REKAP_AKTIF = os.getenv("REKAP_AKTIF", "1").strip().lower() not in ("0", "false", "off", "no")

# ------------------------------------------
# Tebak kategori otomatis dari nama transaksi
# ------------------------------------------
#
# Kata kunci di bawah diturunkan dari analisis 758 transaksi yang sudah ada di
# database Notion "Transaksi": setiap kata pada judul dihitung sebarannya per
# kategori, lalu hanya kata yang konsisten (mayoritas jatuh ke satu kategori)
# yang dipakai di sini. Kata generik yang tersebar rata — "beli", "baru",
# "lari", "kado", "sepatu" — sengaja tidak dipakai supaya bot tidak menebak
# asal; nama seperti itu tetap jatuh ke pemilihan manual.
#
# Prioritas menyelesaikan judul yang mengandung beberapa kata kunci sekaligus:
#   3 = penanda kuat, hampir selalu benar meski ketemu kata lain
#       ("Grab ngopi" -> Transportasi, "Sedekah sarapan" -> Sedekah)
#   2 = kata kunci normal
#   1 = petunjuk lemah, kalah dari kata kunci lain mana pun
#       ("makan malam" -> Makanan, tapi "gojek malam" tetap Transportasi)
# Pada prioritas sama, frasa yang lebih panjang menang: "uang makan" (Pemasukan)
# mengalahkan "makan" (Makanan), "gojek plus" (Tagihan) mengalahkan "gojek".
KEYWORD_RULES = [
    # --- Penanda transportasi: moda kendaraan mengalahkan tujuan/keperluan ---
    ("🚋 Transportasi", 3, [
        "gojek", "gohek", "gojeg", "grab", "gocar", "go car", "gosend",
        "transport", "transpor", "ojek", "ojol", "busway", "transjakarta",
        "angkot", "bajaj", "taksi", "taxi", "hiace", "mrt", "krl", "lrt", "tj",
    ]),
    ("🚋 Transportasi", 2, [
        "ongkos", "parkir", "bensin", "pertalite", "pertamax", "kereta",
        "pesawat", "bandara", "citilink", "soetta", "terminal", "stasiun",
        "velodrome", "asiop", "e money", "emoney", "damri", "shuttle", "tiket",
    ]),
    ("🚋 Transportasi", 1, ["tol", "ongkir", "ngirim", "pulang pergi"]),

    # --- Makanan & minuman (kategori terbesar: 435 dari 758 transaksi) ---
    ("🍛Makanan", 2, [
        "makan", "makanan", "sarapan", "sarap", "nyarap", "maksi", "minum",
        "minuman", "jajan", "nyemil", "cemilan", "ngopi", "kopi", "nongki",
        "nongkrong", "bukber", "buka puasa", "berbuka", "bukaan", "sahur",
        "takjil", "puasa", "galon", "aqua", "pocari", "isoplus", "mizone",
        "nutrisari", "floridina", "cincau", "cingcau", "eskrim", "es krim",
        "coklat", "donat", "piscok", "kurma", "yupi", "fitbar", "kripik",
        "keripik", "gorengan", "goreng", "nasgor", "nasi goreng", "miegor",
        "uduk", "padang", "gudeg", "rendang", "bakso", "indomie", "geprek",
        "bebek", "pecel", "ketoprak", "toprak", "somay", "siomay", "cuanki",
        "cilok", "cilor", "cimol", "pempek", "kebab", "kwetiaw", "kwetiau",
        "ricebox", "ketan", "telur", "telor", "beras", "gacoan", "lawson",
        "mixue", "warteg", "warung", "katering", "catering", "martabak",
        "burger", "pizza", "sushi", "dimsum", "seblak", "batagor", "rawon",
        "opor", "kerupuk", "yogurt", "boba", "matcha", "sembako", "sarden",
        "nyomay", "nyoto", "ulang tahun",
        "jeruk", "durian", "pisang", "madu", "susu", "roti", "nasi", "soto",
        "sate", "lele", "ayam", "mie", "kue", "jus", "teh", "sop",
    ]),
    # Petunjuk lemah: benar pada mayoritas judul singkat ("Buka", "Oleh2",
    # "Ultah"), tapi selalu kalah kalau ada kata kunci lain di judul yang sama.
    ("🍛Makanan", 1, [
        "siang", "malam", "pagi", "sore", "malem", "lauk", "buah",
        "buka", "ayce", "oleh2", "oleh oleh", "ultah", "es",
    ]),

    # --- Perawatan diri: penanda kuat supaya "sabun cuci sepatu" tidak
    #     tertarik ke Fashion oleh kata "sepatu" ---
    ("😁Perawatan/kebersihan", 3, [
        "sabun", "sabub", "shampoo", "sampo", "cukur", "pangkas", "laundry",
        "londri", "deodorant", "deodoran", "parfum", "pomade", "sunscreen",
        "facewash", "face wash", "skincare", "sunlight",
    ]),
    ("😁Perawatan/kebersihan", 2, ["potong rambut", "rambut", "muka", "handbody", "lotion"]),
    ("😁Perawatan/kebersihan", 1, ["cuci", "odol"]),

    # --- Rumah tangga ---
    ("🏠Alat Rumah", 2, [
        "tisu", "tisue", "tissue", "sprei", "lampu", "cermin", "amplop",
        "deterjen", "pewangi", "rapika", "porselen", "hansaplast", "sapu",
        "ember", "hanger", "gantungan", "nyamuk", "trimmer", "sikat", "gorden",
        "bantal", "kasur", "wajan", "piring", "gelas", "sendok",
        "lem sepatu", "baut", "kunci", "meja", "kursi", "lem", "pel",
    ]),

    # --- Utilitas & tagihan ---
    ("⚡Listrik", 3, ["token listrik", "listrik", "tokem", "pln"]),
    ("⚡Listrik", 2, ["token"]),
    ("🛜Internet", 2, [
        "internet", "kuota", "wifi", "indihome", "paket data", "biznet",
        "firstmedia", "inet",
    ]),
    ("💳Tagihan", 3, ["gojek plus"]),
    ("💳Tagihan", 2, [
        "admin", "bayar kosan", "kosan", "bayar kos", "sewa kos", "pulsa",
        "top up", "topup", "gopay", "shopeepay", "kartu atm", "iuran",
        "tagihan", "cicilan", "angsuran", "asuransi", "bpjs", "heroku",
        "bank", "atm", "ovo",
    ]),
    ("💳Tagihan", 1, ["biaya", "pajak"]),

    # --- Sedekah: penanda kuat, "Sedekah sarapan" bukan Makanan ---
    ("🕋Sedekah", 3, [
        "sedekah", "donasi", "infak", "infaq", "zakat", "jumat berkah",
        "kenduri", "santunan", "qurban", "kurban", "amal",
        # Kiriman rutin ke keluarga. Aman karena Sedekah otomatis tersaring
        # saat nominal positif — "Dari mamak buat tiket" tetap jadi Pemasukan.
        "buat mamak", "buat nenek", "buat kakak", "buat abang",
    ]),

    # --- Pemasukan: hanya dipertimbangkan saat nominalnya positif ---
    ("💵Pemasukan", 3, [
        "gajian", "gaji", "gapok", "tukin", "uang makan", "uang lembur",
        "uang dinas", "uang jalan", "uang saku", "lembur", "dinas", "bonus",
        "honor", "doorprize", "doorpize", "subsidi", "insentif", "komisi",
        "dividen", "cashback", "refund", "pemasukan", "jual", "jasa", "thr",
        "fee",
    ]),
    ("💵Pemasukan", 1, ["uang", "masuk", "hadiah"]),

    # --- Investasi / tabungan ---
    ("🪨Investasi/tabung", 2, [
        "reksadana", "reksa dana", "investasi", "tabungan", "nabung",
        "deposito", "obligasi", "bitcoin", "crypto", "kripto", "emas", "btc",
    ]),

    # --- Kerugian: menang atas "saham" pada "rugi saham" ---
    ("Kerugian 🤣", 3, ["kerugian", "rugi", "kehilangan", "denda"]),
    ("🪨Investasi/tabung", 2, ["saham", "xauusd", "forex"]),

    ("👾hutang", 2, ["hutang", "utang", "pinjam", "pinjem", "pinjaman"]),

    # --- Kerja & pendidikan ---
    ("🔮Kerja/Pendidikan", 2, [
        "claude", "chatgpt", "openai", "kursus", "sertifikasi", "materai",
        "seragam", "lisensi", "baju kerja", "pelatihan", "seminar", "webinar",
        "ujian", "kuliah", "sekolah", "fotokopi", "fotocopy", "print",
        "pulpen", "ms word", "office", "domain", "hosting", "dasi", "buku",
        "atk", "vpn",
    ]),

    # --- Hobi & olahraga ---
    ("⛷️ Hobi", 2, [
        "running", "long run", "marathon", "maraton", "racepack", "jersey",
        "futsal", "minsoc", "fitness", "sepeda", "renang", "badminton",
        "cupang", "sepatu lari", "kaos lari", "compression", "interval",
        "tenis", "camping", "mancing", "hobi", "fotoyu", "foto yu",
        "race", "bola", "gym",
    ]),
    ("⛷️ Hobi", 1, ["run"]),

    # --- Fashion ---
    ("💃 Fashion", 2, [
        "kemeja", "celana", "sendal", "sandal", "sepatu", "jaket", "kaus kaki",
        "sarung", "ikat pinggang", "permak", "ngecilin", "celana lari",
        "baju", "kaos", "topi", "koko", "tas",
    ]),

    # --- Elektronik ---
    ("🖥️Elektronik", 2, [
        "charger", "chrger", "adaptor", "powerbank", "power bank", "monitor",
        "laptop", "keyboard", "mouse", "headset", "earphone", "speaker",
        "flashdisk", "harddisk", "printer", "baterai", "casan", "kabel data",
        "stand laptop", "garmin", "smartwatch", "capcut", "kabel", "case",
        "ssd", "hp",
    ]),

    # --- Hiburan ---
    ("🍿Hiburan", 2, [
        "spotify", "netflix", "bioskop", "nonton", "konser", "karaoke",
        "rental ps", "steam", "disney", "vidio", "film", "game",
    ]),
]

# Kategori yang masuk akal ketika nominalnya positif (uang masuk). Selain ini,
# nominal positif berarti Pemasukan — sehingga "uang makan +200rb" tidak
# tertarik ke Makanan oleh kata "makan", dan "Jual laptop +5jt" tidak jadi
# Elektronik.
INFLOW_CATEGORIES = {
    CAT_PEMASUKAN, CAT_TRANSFER, "👾hutang", "Kerugian 🤣", "🤔Lainnya",
}


def _compile_rules():
    """Ubah KEYWORD_RULES jadi daftar (regex, kategori, kata_kunci, skor).

    Kata kunci pendek (<5 huruf) dicocokkan per kata utuh supaya "tj" tidak
    kena "tjahaya" dan "lem" tidak kena "lembur". Kata kunci panjang dicocokkan
    sebagai substring supaya tahan salah ketik dan imbuhan — "sarap" tetap kena
    "sarapann"/"nyarap", "makan" tetap kena "makanan".
    """
    compiled = []
    for kategori, prioritas, keywords in KEYWORD_RULES:
        for kw in keywords:
            if len(kw) >= 5 or " " in kw:
                pattern = re.compile(re.escape(kw))
            else:
                pattern = re.compile(rf"\b{re.escape(kw)}\b")
            skor = prioritas * 10_000 + kw.count(" ") * 100 + len(kw)
            compiled.append((pattern, kategori, kw, skor))
    return compiled


COMPILED_RULES = _compile_rules()


def normalize_name(nama: str) -> str:
    """Turunkan judul transaksi ke huruf kecil dengan pemisah spasi tunggal.

    Emoji, tanda baca dan simbol ("Odol + Pomade", "Transfer Kas → Tabungan")
    jadi spasi supaya batas kata pada regex bekerja.
    """
    teks = re.sub(r"[^a-z0-9]+", " ", str(nama).lower())
    return f" {teks.strip()} "


def guess_category(nama: str, jumlah: float = None):
    """Tebak kategori dari nama transaksi.

    Mengembalikan (kategori, kata_kunci) bila ada kata kunci yang cocok, atau
    (None, None) bila tidak — pemanggil lalu menampilkan pemilihan manual.
    """
    teks = normalize_name(nama)
    if not teks.strip():
        return None, None

    positif = jumlah is not None and jumlah > 0
    terbaik_skor, terbaik = 0, (None, None)
    for pattern, kategori, kw, skor in COMPILED_RULES:
        # Arah uang menyaring kandidat yang mustahil sebelum pencocokan.
        if positif:
            if kategori not in INFLOW_CATEGORIES:
                continue
        elif kategori == CAT_PEMASUKAN:
            continue
        if skor > terbaik_skor and pattern.search(teks):
            terbaik_skor, terbaik = skor, (kategori, kw)
    return terbaik

NAMA_BULAN = {
    1: "Januari", 2: "Februari", 3: "Maret", 4: "April",
    5: "Mei", 6: "Juni", 7: "Juli", 8: "Agustus",
    9: "September", 10: "Oktober", 11: "November", 12: "Desember",
}

# Indeks mengikuti datetime.date.weekday(): 0 = Senin.
NAMA_HARI = {
    0: "Senin", 1: "Selasa", 2: "Rabu", 3: "Kamis",
    4: "Jumat", 5: "Sabtu", 6: "Minggu",
}

ACCOUNT_ICONS = {
    "Kas": "💵",
    "Tabungan": "🏦",
    "Reksadana": "📈",
    "Emas": "🪙",
    "BTC / Crypto": "₿",
    "Saham": "📊",
}


# ------------------------------------------
# Utilitas
# ------------------------------------------

def today_wib() -> datetime.date:
    return datetime.datetime.now(WIB).date()


def esc(text) -> str:
    """Escape untuk parse_mode HTML. Nama transaksi bebas diketik user dan
    sering mengandung karakter yang merusak Markdown (mis. 'buka puasa _ minum')."""
    return html.escape(str(text), quote=False)


def rp(amount) -> str:
    return f"Rp {amount:,.0f}".replace(",", ".")


def icon_for(nama_akun: str) -> str:
    return ACCOUNT_ICONS.get(nama_akun, "💼")


def parse_amount(raw: str):
    """Baca nominal dalam berbagai format. Mengembalikan float positif, atau None.

    Didukung: 50000 · 50.000 · 50,000 · 50k · 50rb · 1.5jt · 2juta · Rp50.000
    """
    text = str(raw).strip().lower()
    for noise in ("rp", "idr", " ", "+"):
        text = text.replace(noise, "")
    text = text.lstrip("-")

    multiplier = 1
    for suffix, mult in (("juta", 1_000_000), ("jt", 1_000_000),
                         ("ribu", 1_000), ("rb", 1_000), ("k", 1_000)):
        if text.endswith(suffix):
            multiplier = mult
            text = text[: -len(suffix)].strip()
            break

    if multiplier > 1:
        # Dengan sufiks, titik/koma dibaca sebagai desimal: "1.5jt" -> 1.5 juta.
        text = text.replace(",", ".")
    else:
        # Tanpa sufiks, titik/koma adalah pemisah ribuan: "50.000" -> 50000.
        text = text.replace(".", "").replace(",", "")

    if not text:
        return None
    try:
        value = float(text) * multiplier
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def resolve_month(args, message_hint: str):
    """Kembalikan (bulan, tahun, error_text)."""
    today = today_wib()
    if not args:
        return today.month, today.year, None
    try:
        bulan = int(args[0])
    except (ValueError, TypeError):
        return None, None, f"⚠️ Format salah, King Odiq. Contoh: {message_hint}"
    if bulan < 1 or bulan > 12:
        return None, None, f"⚠️ Bulan harus antara 1-12, King Odiq. Contoh: {message_hint}"
    tahun = today.year
    if bulan > today.month:
        tahun -= 1
    return bulan, tahun, None


def month_bounds(bulan: int, tahun: int):
    first = datetime.date(tahun, bulan, 1).isoformat()
    if bulan == 12:
        after = datetime.date(tahun + 1, 1, 1).isoformat()
    else:
        after = datetime.date(tahun, bulan + 1, 1).isoformat()
    return first, after


def potong_pesan(text: str, limit: int = 3900) -> list:
    """Penggal teks di batas baris supaya muat batas 4096 karakter Telegram.

    Memotong mentah tiap 4096 karakter bisa membelah tag HTML di tengah dan
    membuat Telegram menolak seluruh pesan.
    """
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


async def send_long(message, text: str, reply_markup=None):
    """Balas dengan pesan panjang; dipenggal otomatis kalau perlu."""
    chunks = potong_pesan(text)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await message.reply_text(
            chunk,
            parse_mode="HTML",
            reply_markup=reply_markup if is_last else None,
        )


def mention_html(user) -> str:
    """Mention tak terlihat ke user tersebut.

    Tautan tg://user?id tetap dihitung klien Telegram sebagai mention meski
    labelnya cuma zero-width space, jadi ForceReply(selective=True) tetap
    menyasar orangnya tanpa menambah teks pemberitahuan yang terlihat.
    """
    if user is None:
        return ""
    zwsp = chr(0x200B)
    return f'<a href="tg://user?id={user.id}">{zwsp}</a>'


async def ask_text(update: Update, pertanyaan: str) -> None:
    """Tanya sesuatu yang jawabannya teks bebas, sambil membuka kotak balasan.

    Di grup ini satu-satunya jalur yang bisa diandalkan. Bot dengan privacy mode
    aktif — bawaan Telegram — hanya menerima pesan yang membalas pesannya
    sendiri, jadi kalau kotak balasan tidak terbuka jawaban user tidak pernah
    sampai dan bot terlihat menggantung tanpa pesan error apa pun.

    ForceReply(selective=True) hanya menyasar user yang di-mention pada teks,
    atau pengirim pesan yang dibalas. Prompt yang muncul setelah tombol ditekan
    dibalaskan ke pesan bot sendiri — tanpa mention prompt itu tidak menyasar
    siapa pun, sehingga kotak balasan tidak pernah terbuka di grup. Mention-nya
    ditempel tak terlihat (lihat mention_html) supaya tidak mengotori pesan.
    """
    message = update.effective_message
    teks = pertanyaan
    if message.chat.type != "private":
        teks += mention_html(update.effective_user)
    await message.reply_text(
        teks,
        reply_markup=ForceReply(selective=True),
        parse_mode="HTML",
    )


# ------------------------------------------
# Lapisan Notion
# ------------------------------------------

_http_client = None


def http() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
    return _http_client


async def notion_request(method: str, path: str, payload=None) -> dict:
    response = await http().request(method, f"{NOTION_API}{path}", json=payload)
    if response.status_code >= 400:
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"Notion {response.status_code}: {detail}")
    return response.json()


async def notion_query_all(database_id: str, payload: dict) -> list:
    results, cursor = [], None
    while True:
        body = dict(payload)
        if cursor:
            body["start_cursor"] = cursor
        data = await notion_request("POST", f"/databases/{database_id}/query", body)
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            return results
        cursor = data.get("next_cursor")


def p_title(props: dict, key: str = "Name") -> str:
    items = props.get(key, {}).get("title", [])
    return "".join(i.get("plain_text", "") for i in items).strip()


def p_number(props: dict, key: str):
    return props.get(key, {}).get("number")


def p_select(props: dict, key: str):
    option = props.get(key, {}).get("select")
    return option.get("name") if option else None


def p_date(props: dict, key: str):
    value = props.get(key, {}).get("date")
    return value.get("start") if value else None


def p_relation(props: dict, key: str) -> list:
    return [r.get("id") for r in props.get(key, {}).get("relation", [])]


def p_formula_number(props: dict, key: str):
    return props.get(key, {}).get("formula", {}).get("number")


def p_rollup_number(props: dict, key: str):
    return props.get(key, {}).get("rollup", {}).get("number")


def is_transfer(props: dict) -> bool:
    """Transfer aset — tidak dihitung sebagai pengeluaran (brief bagian 1 & 5)."""
    if p_select(props, "Kategori") == CAT_TRANSFER:
        return True
    return bool(p_relation(props, "Dari Kantong")) and bool(p_relation(props, "Ke Aset"))


def is_blank_row(props: dict) -> bool:
    """4 baris kosong diketahui ada di database (brief bagian 10)."""
    return p_number(props, "Ins (+)/Outs (-)") is None and not p_title(props)


# ------------------------------------------
# Cache daftar akun
# ------------------------------------------

_accounts_cache = {"data": None, "at": None}
_ACCOUNT_TTL = datetime.timedelta(minutes=5)


async def get_accounts(force: bool = False) -> list:
    """Ambil daftar akun dari database Aset. Dibaca dinamis supaya penambahan
    atau penggantian nama kantong di Notion tidak perlu ubah kode."""
    now = datetime.datetime.now(datetime.timezone.utc)
    cached = _accounts_cache["data"]
    if not force and cached and _accounts_cache["at"] and now - _accounts_cache["at"] < _ACCOUNT_TTL:
        return cached

    pages = await notion_query_all(NOTION_ASET_DATABASE_ID, {})
    accounts = []
    for page in pages:
        props = page.get("properties", {})
        nama = p_title(props, "Nama")
        if not nama:
            continue

        saldo = p_formula_number(props, "Saldo")
        if saldo is None:
            # Fallback kalau formula tidak terbaca: Saldo Awal + Masuk - Keluar.
            saldo = ((p_number(props, "Saldo Awal") or 0)
                     + (p_rollup_number(props, "Total Masuk") or 0)
                     - (p_rollup_number(props, "Total Keluar") or 0))

        accounts.append({
            "id": page["id"],
            "nama": nama,
            "jenis": p_select(props, "Jenis") or "-",
            "saldo": saldo,
        })

    accounts.sort(key=lambda a: (a["nama"] != DEFAULT_ACCOUNT, a["nama"]))
    _accounts_cache["data"] = accounts
    _accounts_cache["at"] = now
    return accounts


async def get_default_account(force: bool = False):
    for account in await get_accounts(force=force):
        if account["nama"] == DEFAULT_ACCOUNT:
            return account
    return None


async def saldo_footer() -> str:
    """Baris Kas + Saldo NET untuk ditempel di akhir laporan.

    Saldo diambil dari database Aset (stok), bukan dijumlahkan dari transaksi
    bulan berjalan — lihat brief bagian 1. Dikembalikan kosong kalau gagal,
    supaya laporan tetap terkirim.
    """
    try:
        accounts = await get_accounts(force=True)
    except Exception:
        logger.exception("Gagal membaca saldo untuk footer laporan")
        return ""
    if not accounts:
        return ""

    kas = next((a["saldo"] for a in accounts if a["nama"] == DEFAULT_ACCOUNT), None)
    net = sum(a["saldo"] for a in accounts)

    baris = ""
    if kas is not None:
        baris += f"\n{icon_for(DEFAULT_ACCOUNT)} <b>{DEFAULT_ACCOUNT}:</b> {rp(kas)}"
    baris += f"\n💎 <b>Saldo NET:</b> {rp(net)}"
    return baris


async def saldo_kas_baris() -> str:
    """Baris saldo Kas saja — dipakai rekap harian.

    saldo_footer() ikut menampilkan Saldo NET; rekap harian sengaja berhenti di
    Kas. Dikembalikan kosong kalau gagal, supaya rekap tetap terkirim.
    """
    try:
        accounts = await get_accounts(force=True)
    except Exception:
        logger.exception("Gagal membaca saldo Kas untuk rekap harian")
        return ""

    kas = next((a["saldo"] for a in accounts if a["nama"] == DEFAULT_ACCOUNT), None)
    if kas is None:
        return ""
    return f"\n{icon_for(DEFAULT_ACCOUNT)} <b>{DEFAULT_ACCOUNT}:</b> {rp(kas)}"


def account_keyboard(accounts: list, data_for, skip_id: str = None, per_row: int = 2) -> InlineKeyboardMarkup:
    """Tombol akun. Indeks yang dikirim selalu mengacu ke daftar penuh dari
    get_accounts() yang urutannya deterministik, supaya callback_data tetap sah
    walau daftarnya diambil ulang di handler berikutnya."""
    rows, row = [], []
    for i, account in enumerate(accounts):
        if skip_id is not None and account["id"] == skip_id:
            continue
        row.append(InlineKeyboardButton(
            f"{icon_for(account['nama'])} {account['nama']}",
            callback_data=data_for(i),
        ))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def category_keyboard(prefix: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, cat in enumerate(CATEGORIES):
        row.append(InlineKeyboardButton(cat, callback_data=f"{prefix}{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# ------------------------------------------
# Menu
# ------------------------------------------

# Tautan langsung Mini App. Tombol web_app dilarang Telegram di grup, tapi
# tombol URL biasa diizinkan di mana saja — dan tautan t.me?startapp= tetap
# dibuka Telegram sebagai Mini App, bukan dilempar ke browser. Ini satu-satunya
# cara resmi memunculkan Dashboard di dalam grup.
BOT_USERNAME = os.getenv("BOT_USERNAME", "uangodiqBot").strip().lstrip("@")
MINI_APP_LINK = f"https://t.me/{BOT_USERNAME}?startapp=dash"


def dashboard_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("📊 Dashboard", url=MINI_APP_LINK)


MAIN_KEYBOARD = InlineKeyboardMarkup([
    [dashboard_button()],
    [
        InlineKeyboardButton("➕ Tambah Transaksi", callback_data="menu_add"),
        InlineKeyboardButton("💼 Saldo Aset", callback_data="menu_saldo"),
    ],
    [
        InlineKeyboardButton("📊 Laporan Bulan Ini", callback_data="menu_report"),
        InlineKeyboardButton("📋 Daftar Transaksi", callback_data="menu_list"),
    ],
    [
        InlineKeyboardButton("🔁 Transfer Aset", callback_data="menu_transfer"),
        InlineKeyboardButton("❓ Bantuan", callback_data="menu_help"),
    ],
    [
        InlineKeyboardButton("❌ Batal", callback_data="menu_cancel"),
    ],
])

# Fitur yang jarang dipakai dipindah ke sini supaya menu utama tetap ringkas.
# Dibuka lewat /othermenu — sengaja tanpa tombol di menu utama.
OTHER_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("📂 Laporan Kategori", callback_data="menu_reportcat")],
])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message if update.message else update.callback_query.message
    await message.reply_text(
        "Halo King Odiq! 👋 Saya adalah Bot Keuangan Notion Anda.\n\n"
        "Silakan pilih menu di bawah ini untuk mulai mencatat atau melihat keuangan Anda.",
        reply_markup=MAIN_KEYBOARD,
    )


async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kirim tombol pembuka Mini App. Berfungsi di grup maupun chat privat."""
    message = update.message if update.message else update.callback_query.message
    await message.reply_text(
        "📊 Buka dashboard keuangan, King Odiq:",
        reply_markup=InlineKeyboardMarkup([[dashboard_button()]]),
    )


async def other_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menu tambahan berisi fitur yang jarang dipakai."""
    message = update.message if update.message else update.callback_query.message
    await message.reply_text(
        "📂 <b>Menu tambahan</b>, King Odiq:",
        parse_mode="HTML",
        reply_markup=OTHER_KEYBOARD,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message if update.message else update.callback_query.message
    pesan = (
        "💡 <b>Panduan Penggunaan Bot untuk King Odiq</b>\n\n"
        "1️⃣ /add — mencatat transaksi.\n"
        f"   👉 Cara cepat: <code>/add Makan siang, -25k</code>\n"
        f"   🤖 Kategori terisi otomatis dari nama transaksi. Kalau tidak ada kata "
        f"yang dikenali, bot minta pilih manual.\n"
        f"   Kantong default <b>{DEFAULT_ACCOUNT}</b>, kategori dan kantong bisa "
        f"diganti lewat tombol setelah tersimpan.\n\n"
        "2️⃣ /saldo — saldo tiap aset dan net worth.\n\n"
        "3️⃣ /transfer — pindah aset (mis. Kas → Emas). Tidak dihitung pengeluaran.\n\n"
        "4️⃣ /report — ringkasan arus bulan ini.\n"
        "   👉 Bulan spesifik: <code>/report 5</code>\n\n"
        "5️⃣ /list — daftar transaksi rinci.\n\n"
        "6️⃣ /dashboard — buka dashboard visual (jalan di grup juga).\n\n"
        "7️⃣ /rekap — rekap transaksi hari ini.\n"
        f"   🌙 Terkirim otomatis tiap hari pukul {REKAP_JAM:02d}.{REKAP_MENIT:02d} WIB.\n\n"
        "8️⃣ /othermenu — menu tambahan (laporan per kategori).\n\n"
        "9️⃣ /cancel — membatalkan proses.\n\n"
        "<b>Format nominal:</b> <code>50000</code> · <code>50.000</code> · "
        "<code>50k</code> · <code>1.5jt</code>\n\n"
        "<b>Di grup:</b> jawaban teks (nama & nominal) harus dikirim sebagai "
        "<b>balasan</b> atas pertanyaan bot — Telegram tidak meneruskan pesan "
        "biasa ke bot di grup. Cara paling cepat tetap "
        "<code>/add Makan siang, -25k</code> yang selesai dalam satu pesan."
    )
    await message.reply_text(pesan, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)


async def handle_inline_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "menu_report":
        await report(update, context)
    elif query.data == "menu_reportcat":
        await reportcat(update, context)
    elif query.data in ("menu_saldo", "saldo_all"):
        await saldo(update, context)
    elif query.data in ("menu_list",) or query.data.startswith("list_all_"):
        await list_expenses(update, context)
    elif query.data == "menu_help":
        await help_command(update, context)
    elif query.data == "menu_cancel":
        await query.message.reply_text("❌ Proses dibatalkan, King Odiq.", reply_markup=MAIN_KEYBOARD)


# ------------------------------------------
# Alur tambah transaksi
# ------------------------------------------

async def process_quick_add(text: str, message, context: ContextTypes.DEFAULT_TYPE, error_example: str) -> int:
    parts = [p.strip() for p in text.split(",", 1)]
    nama = parts[0]
    jumlah_str = parts[1] if len(parts) > 1 else ""

    if not (jumlah_str.startswith("+") or jumlah_str.startswith("-")):
        await message.reply_text(
            "⚠️ Format salah, King Odiq! Jika menggunakan koma, sertakan + atau - "
            f"pada nominal ({error_example})"
        )
        return ConversationHandler.END

    nilai = parse_amount(jumlah_str)
    if nilai is None:
        await message.reply_text("⚠️ Nominal tidak valid! Batalkan (/cancel) dan ulangi.")
        return ConversationHandler.END

    context.user_data["nama"] = nama
    context.user_data["jumlah"] = -nilai if jumlah_str.startswith("-") else nilai

    return await auto_or_ask_category(message, context)


async def start_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message if update.message else update.callback_query.message
    if update.callback_query:
        await update.callback_query.answer()
        text = ""
    else:
        parts = update.message.text.split(maxsplit=1)
        text = parts[1].strip() if len(parts) > 1 else ""

    # Handler ini bisa dimasuki ulang di tengah alur lama (allow_reentry), jadi
    # sisa jawaban percakapan sebelumnya harus dibuang di sini.
    context.user_data.clear()

    if text:
        if "," in text:
            return await process_quick_add(text, message, context, "contoh: /add Makan, -50k")
        context.user_data["nama"] = text
        await message.reply_text(
            f"Nama: <b>{esc(text)}</b>\n\nPilih jenis transaksi ini, King Odiq:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📉 Pengeluaran (-)", callback_data="tipe_pengeluaran"),
                InlineKeyboardButton("📈 Pemasukan (+)", callback_data="tipe_pemasukan"),
            ]]),
            parse_mode="HTML",
        )
        return TIPE

    await ask_text(
        update,
        "📝 Mari tambahkan transaksi baru.\n\n"
        "Silakan masukkan <b>Nama Transaksi</b>, King Odiq:",
    )
    return NAMA


async def ask_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "," in text:
        return await process_quick_add(text, update.message, context, "contoh: Makan, -50k")

    context.user_data["nama"] = text
    await update.message.reply_text(
        f"Nama: <b>{esc(text)}</b>\n\nPilih jenis transaksi ini, King Odiq:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📉 Pengeluaran (-)", callback_data="tipe_pengeluaran"),
            InlineKeyboardButton("📈 Pemasukan (+)", callback_data="tipe_pemasukan"),
        ]]),
        parse_mode="HTML",
    )
    return TIPE


async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    context.user_data["pengeluaran"] = query.data == "tipe_pengeluaran"
    await ask_text(
        update,
        "Silakan masukkan <b>Jumlah Nominal</b>, King Odiq (contoh: 50000, 50k, 1.5jt):",
    )
    return JUMLAH


async def receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nilai = parse_amount(update.message.text)
    if nilai is None:
        await ask_text(
            update,
            "⚠️ Nominal tidak valid, King Odiq! Harap masukkan angka "
            "(contoh: 50000, 50k, 1.5jt):",
        )
        return JUMLAH

    jumlah = -nilai if context.user_data.get("pengeluaran", True) else nilai
    context.user_data["jumlah"] = jumlah

    return await auto_or_ask_category(update.message, context)


def confirmation_markup(page_id: str, allow_change_account: bool) -> InlineKeyboardMarkup:
    ubah = [InlineKeyboardButton("🏷️ Ganti kategori", callback_data=f"chcat_{page_id}")]
    if allow_change_account:
        ubah.insert(0, InlineKeyboardButton("🔄 Ganti akun", callback_data=f"chacc_{page_id}"))
    return InlineKeyboardMarkup([
        ubah,
        [InlineKeyboardButton("🗑️ Hapus", callback_data=f"del_{page_id}")],
        [
            InlineKeyboardButton("➕ Tambah lagi", callback_data="menu_add"),
            InlineKeyboardButton("💼 Saldo", callback_data="menu_saldo"),
        ],
    ])


async def persist_transaction(message, context: ContextTypes.DEFAULT_TYPE,
                              kategori: str) -> int:
    """Tulis transaksi ke Notion lalu tampilkan konfirmasi.

    Dipakai baik oleh pemilihan kategori manual maupun hasil tebakan otomatis;
    konfirmasinya sama persis, tanpa menyebut asal kategori.
    """
    nama = context.user_data.get("nama", "-")
    jumlah = context.user_data.get("jumlah", 0)
    tanggal = today_wib().isoformat()

    await message.reply_text("⏳ Menyimpan data ke Notion, King Odiq...")

    try:
        # force=True: saldo dibaca segar sebelum menulis, supaya sisa kas yang
        # ditampilkan setelah ini akurat.
        akun = await get_default_account(force=True)
        if akun is None:
            raise RuntimeError(
                f"Akun '{DEFAULT_ACCOUNT}' tidak ditemukan di database Aset."
            )
        akun_id = akun["id"]

        properties = {
            "Name": {"title": [{"text": {"content": nama}}]},
            "Ins (+)/Outs (-)": {"number": jumlah},
            "Kategori": {"select": {"name": kategori}},
            "Tanggal": {"date": {"start": tanggal}},
        }
        # Aturan wajib brief bagian 6: tanpa relasi akun, transaksi tidak
        # menggerakkan saldo aset mana pun.
        if jumlah >= 0:
            properties["Ke Aset"] = {"relation": [{"id": akun_id}]}
        else:
            properties["Dari Kantong"] = {"relation": [{"id": akun_id}]}

        page = await notion_request("POST", "/pages", {
            "parent": {"database_id": NOTION_DATABASE_ID},
            "properties": properties,
        })

        sign = "📈" if jumlah > 0 else "📉"
        arah = "ke" if jumlah >= 0 else "dari"
        catatan = "\n\n🔁 <i>Transfer aset — tidak dihitung sebagai pengeluaran.</i>" if kategori == CAT_TRANSFER else ""

        # Dihitung dari saldo segar + nominal, bukan dibaca ulang dari Notion:
        # rollup butuh waktu menyebar setelah page dibuat.
        sisa = akun["saldo"] + jumlah

        await message.reply_text(
            f"{sign} Data berhasil disimpan ke Notion, King Odiq!\n\n"
            f"🏷️ <b>{esc(nama)}</b>\n"
            f"💰 <code>{rp(jumlah)}</code>\n"
            f"📂 {esc(kategori)}\n"
            f"💼 {arah} {icon_for(DEFAULT_ACCOUNT)} {DEFAULT_ACCOUNT}\n"
            f"📅 {tanggal}{catatan}\n\n"
            f"{icon_for(DEFAULT_ACCOUNT)} <b>Sisa {DEFAULT_ACCOUNT}:</b> {rp(sisa)}",
            parse_mode="HTML",
            reply_markup=confirmation_markup(page["id"], allow_change_account=True),
        )
    except Exception as e:
        logger.exception("Gagal menyimpan transaksi")
        await message.reply_text(
            f"⚠️ Gagal menambahkan data, King Odiq.\n\nError: <code>{esc(e)}</code>",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )

    context.user_data.clear()
    return ConversationHandler.END


async def save_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handler tombol kategori — jalur manual."""
    query = update.callback_query
    await query.answer()

    try:
        kategori = CATEGORIES[int(query.data.replace("cat_", ""))]
    except (ValueError, IndexError):
        await query.message.reply_text("⚠️ Kategori tidak valid, King Odiq. Harap pilih menggunakan tombol.")
        return KATEGORI

    return await persist_transaction(query.message, context, kategori)


async def auto_or_ask_category(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Simpan langsung kalau kategori bisa ditebak, kalau tidak minta manual."""
    nama = context.user_data.get("nama", "")
    jumlah = context.user_data.get("jumlah", 0)

    kategori, kata_kunci = guess_category(nama, jumlah)
    if kategori:
        # Tidak ditampilkan ke user, tapi tetap dicatat supaya kalau tebakannya
        # meleset bisa ditelusuri kata mana yang memicunya.
        logger.info("Kategori otomatis: %r -> %s (kata kunci %r)", nama, kategori, kata_kunci)
        return await persist_transaction(message, context, kategori)

    await message.reply_text(
        f"Nama: <b>{esc(nama)}</b>\nJumlah: <b>{rp(jumlah)}</b>\n\n"
        "Pilih kategori transaksi, King Odiq:",
        reply_markup=category_keyboard("cat_"),
        parse_mode="HTML",
    )
    return KATEGORI


async def invalid_inline_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⚠️ Harap pilih opsi menggunakan <b>tombol di atas</b>, King Odiq.",
        parse_mode="HTML",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Proses dibatalkan, King Odiq.", reply_markup=MAIN_KEYBOARD)
    context.user_data.clear()
    return ConversationHandler.END


# ------------------------------------------
# Ganti akun & hapus transaksi
# ------------------------------------------

async def ask_change_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page_id = query.data.replace("chacc_", "")

    try:
        accounts = await get_accounts()
    except Exception as e:
        await query.message.reply_text(f"❌ Gagal membaca daftar akun, King Odiq:\n<code>{esc(e)}</code>", parse_mode="HTML")
        return

    rows, row = [], []
    for i, account in enumerate(accounts):
        row.append(InlineKeyboardButton(
            f"{icon_for(account['nama'])} {account['nama']}",
            callback_data=f"setacc_{i}_{page_id}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    await query.message.reply_text(
        "💼 Pindahkan transaksi ini ke kantong mana, King Odiq?",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def set_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    rest = query.data.replace("setacc_", "")
    index_str, _, page_id = rest.partition("_")

    try:
        accounts = await get_accounts(force=True)
        account = accounts[int(index_str)]

        page = await notion_request("GET", f"/pages/{page_id}")
        props = page.get("properties", {})
        jumlah = p_number(props, "Ins (+)/Outs (-)") or 0

        field = "Ke Aset" if jumlah >= 0 else "Dari Kantong"
        await notion_request("PATCH", f"/pages/{page_id}", {
            "properties": {field: {"relation": [{"id": account["id"]}]}}
        })

        # Saldo lama sudah memasukkan transaksi ini kalau akunnya tidak berubah.
        sudah_di_akun_ini = account["id"] in p_relation(props, field)
        sisa = account["saldo"] if sudah_di_akun_ini else account["saldo"] + jumlah

        arah = "ke" if jumlah >= 0 else "dari"
        await query.message.reply_text(
            f"✅ Akun diperbarui, King Odiq — sekarang {arah} "
            f"{icon_for(account['nama'])} <b>{esc(account['nama'])}</b>.\n\n"
            f"{icon_for(account['nama'])} <b>Sisa {esc(account['nama'])}:</b> {rp(sisa)}",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )
    except Exception as e:
        logger.exception("Gagal mengganti akun")
        await query.message.reply_text(
            f"❌ Gagal mengganti akun, King Odiq:\n<code>{esc(e)}</code>", parse_mode="HTML"
        )


async def ask_change_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page_id = query.data.replace("chcat_", "")

    rows, row = [], []
    for i, kategori in enumerate(CATEGORIES):
        row.append(InlineKeyboardButton(kategori, callback_data=f"setcat_{i}_{page_id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    await query.message.reply_text(
        "🏷️ Ganti kategori transaksi ini jadi apa, King Odiq?",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def set_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    index_str, _, page_id = query.data.replace("setcat_", "").partition("_")

    try:
        kategori = CATEGORIES[int(index_str)]
        await notion_request("PATCH", f"/pages/{page_id}", {
            "properties": {"Kategori": {"select": {"name": kategori}}}
        })

        page = await notion_request("GET", f"/pages/{page_id}")
        props = page.get("properties", {})

        pesan = f"✅ Kategori diperbarui jadi <b>{esc(kategori)}</b>, King Odiq."
        if is_transfer(props):
            # Formula Tipe menandai Transfer kalau Dari Kantong dan Ke Aset
            # sama-sama terisi, apa pun kategorinya.
            pesan += ("\n\n🔁 <i>Transaksi ini tetap dihitung sebagai transfer karena "
                      "kantong asal dan tujuan sama-sama terisi — jadi tetap bukan "
                      "pengeluaran.</i>")
        elif kategori == CAT_TRANSFER:
            pesan += ("\n\n🔁 <i>Kategori ini menandai transaksi sebagai transfer — "
                      "tidak lagi dihitung sebagai pengeluaran.</i>")

        await query.message.reply_text(pesan, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.exception("Gagal mengganti kategori")
        await query.message.reply_text(
            f"❌ Gagal mengganti kategori, King Odiq:\n<code>{esc(e)}</code>", parse_mode="HTML"
        )


async def delete_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page_id = query.data.replace("del_", "")

    try:
        # Notion tidak punya hard delete lewat API; archived memindahkan ke trash
        # dan mengeluarkannya dari seluruh rollup saldo.
        await notion_request("PATCH", f"/pages/{page_id}", {"archived": True})
        await query.message.reply_text(
            "🗑️ Transaksi dihapus (dipindah ke trash Notion), King Odiq.",
            reply_markup=MAIN_KEYBOARD,
        )
    except Exception as e:
        logger.exception("Gagal menghapus transaksi")
        await query.message.reply_text(
            f"❌ Gagal menghapus, King Odiq:\n<code>{esc(e)}</code>", parse_mode="HTML"
        )


# ------------------------------------------
# Alur transfer aset
# ------------------------------------------

async def start_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message if update.message else update.callback_query.message
    if update.callback_query:
        await update.callback_query.answer()

    context.user_data.clear()

    try:
        accounts = await get_accounts(force=True)
    except Exception as e:
        await message.reply_text(f"❌ Gagal membaca daftar akun, King Odiq:\n<code>{esc(e)}</code>", parse_mode="HTML")
        return ConversationHandler.END

    await message.reply_text(
        "🔁 <b>Transfer Aset</b>\n\n"
        "Pindah aset tidak dihitung sebagai pengeluaran — saldo satu kantong "
        "berkurang, kantong lain bertambah.\n\n"
        "Dari kantong mana, King Odiq?",
        reply_markup=account_keyboard(accounts, lambda i: f"trfrom_{i}"),
        parse_mode="HTML",
    )
    return TRF_DARI


async def transfer_pick_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    src_idx = int(query.data.replace("trfrom_", ""))
    accounts = await get_accounts()
    try:
        source = accounts[src_idx]
    except IndexError:
        await query.message.reply_text("⚠️ Akun tidak valid, King Odiq. Ulangi /transfer.")
        return ConversationHandler.END

    await query.message.reply_text(
        f"Dari: {icon_for(source['nama'])} <b>{esc(source['nama'])}</b>\n\n"
        "Ke kantong mana, King Odiq?",
        reply_markup=account_keyboard(
            accounts, lambda j: f"trto_{src_idx}_{j}", skip_id=source["id"]
        ),
        parse_mode="HTML",
    )
    return TRF_KE


async def transfer_ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    src_idx, _, dst_idx = query.data.replace("trto_", "").partition("_")
    accounts = await get_accounts()
    try:
        source = accounts[int(src_idx)]
        dest = accounts[int(dst_idx)]
    except (ValueError, IndexError):
        await query.message.reply_text("⚠️ Akun tidak valid, King Odiq. Ulangi /transfer.")
        return ConversationHandler.END

    context.user_data["trf_dari"] = source
    context.user_data["trf_ke"] = dest

    await ask_text(
        update,
        f"{icon_for(source['nama'])} {esc(source['nama'])} → "
        f"{icon_for(dest['nama'])} {esc(dest['nama'])}\n\n"
        "Berapa nominalnya, King Odiq? (contoh: 500k, 1.5jt)",
    )
    return TRF_JUMLAH


async def transfer_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # user_data dipakai bersama oleh alur tambah dan transfer, jadi alur lain
    # yang dimulai di tengah jalan bisa menghapus pilihan kantong di sini.
    source = context.user_data.get("trf_dari")
    dest = context.user_data.get("trf_ke")
    if not source or not dest:
        await update.message.reply_text(
            "⌛ Sesi transfer ini sudah tidak berlaku, King Odiq. Silakan ulangi /transfer.",
            reply_markup=MAIN_KEYBOARD,
        )
        context.user_data.clear()
        return ConversationHandler.END

    nilai = parse_amount(update.message.text)
    if nilai is None:
        await ask_text(update, "⚠️ Nominal tidak valid, King Odiq. Contoh: 500k atau 1.5jt")
        return TRF_JUMLAH

    tanggal = today_wib().isoformat()
    nama = f"Transfer {source['nama']} → {dest['nama']}"

    await update.message.reply_text("⏳ Menyimpan transfer ke Notion, King Odiq...")

    try:
        # Nominal ditulis negatif demi konsistensi; rollup saldo memakai
        # formula Jumlah yang mengambil nilai absolut (brief bagian 6).
        page = await notion_request("POST", "/pages", {
            "parent": {"database_id": NOTION_DATABASE_ID},
            "properties": {
                "Name": {"title": [{"text": {"content": nama}}]},
                "Ins (+)/Outs (-)": {"number": -nilai},
                "Kategori": {"select": {"name": CAT_TRANSFER}},
                "Tanggal": {"date": {"start": tanggal}},
                "Dari Kantong": {"relation": [{"id": source["id"]}]},
                "Ke Aset": {"relation": [{"id": dest["id"]}]},
            },
        })

        await update.message.reply_text(
            "🔁 Transfer berhasil dicatat, King Odiq!\n\n"
            f"🏷️ <b>{esc(nama)}</b>\n"
            f"💰 <code>{rp(nilai)}</code>\n"
            f"📤 {icon_for(source['nama'])} {esc(source['nama'])}\n"
            f"📥 {icon_for(dest['nama'])} {esc(dest['nama'])}\n"
            f"📅 {tanggal}\n\n"
            "<i>Tidak dihitung sebagai pengeluaran — net worth tidak berubah.</i>",
            parse_mode="HTML",
            reply_markup=confirmation_markup(page["id"], allow_change_account=False),
        )
    except Exception as e:
        logger.exception("Gagal menyimpan transfer")
        await update.message.reply_text(
            f"⚠️ Gagal menyimpan transfer, King Odiq.\n\nError: <code>{esc(e)}</code>",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )

    context.user_data.clear()
    return ConversationHandler.END


# ------------------------------------------
# Saldo aset
# ------------------------------------------

async def saldo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    message = update.message if update.message else query.message

    # Tampilan ringkas (hanya Kas) kecuali diminta lengkap lewat tombol.
    show_all = bool(query and query.data == "saldo_all")

    await message.reply_text("⏳ Mengambil saldo untuk King Odiq...")

    try:
        accounts = await get_accounts(force=True)
        if not accounts:
            await message.reply_text("ℹ️ Belum ada akun di database Aset, King Odiq.")
            return

        if not show_all:
            kas = next((a for a in accounts if a["nama"] == DEFAULT_ACCOUNT), None)
            if kas is None:
                await message.reply_text(
                    f"⚠️ Akun '{DEFAULT_ACCOUNT}' tidak ditemukan di database Aset, King Odiq."
                )
                return
            pesan = (
                f"💼 <b>Sisa {esc(kas['nama'])}</b>\n\n"
                f"{icon_for(kas['nama'])} <b>{rp(kas['saldo'])}</b>"
            )
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("📊 Tampilkan aset lainnya", callback_data="saldo_all")]]
                + [list(row) for row in MAIN_KEYBOARD.inline_keyboard]
            )
            await send_long(message, pesan, reply_markup=markup)
            return

        pesan = "💼 <b>Saldo Aset</b>\n\n"
        net_worth = 0
        by_jenis = {}
        for account in accounts:
            by_jenis.setdefault(account["jenis"], []).append(account)

        for jenis in sorted(by_jenis, key=lambda j: (j != "Kas & Setara", j)):
            pesan += f"<b>{esc(jenis)}</b>\n"
            for account in by_jenis[jenis]:
                pesan += f"{icon_for(account['nama'])} {esc(account['nama'])}: {rp(account['saldo'])}\n"
                net_worth += account["saldo"]
            pesan += "\n"

        pesan += f"🏆 <b>Net worth: {rp(net_worth)}</b>\n\n"
        pesan += "<i>Aset dinilai pada modal yang disetor, bukan harga pasar.</i>"

        await send_long(message, pesan, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.exception("Gagal mengambil saldo")
        await message.reply_text(f"❌ Gagal mengambil saldo, King Odiq:\n<code>{esc(e)}</code>", parse_mode="HTML")


# ------------------------------------------
# Laporan
# ------------------------------------------

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message if update.message else update.callback_query.message

    bulan, tahun, error = resolve_month(context.args, "/report 5 (untuk Mei)")
    if error:
        await message.reply_text(error)
        return

    first_day, next_month = month_bounds(bulan, tahun)
    label_bulan = f"{NAMA_BULAN[bulan]} {tahun}"
    await message.reply_text(
        f"⏳ Sedang mengambil data laporan untuk King Odiq <b>{label_bulan}</b>...",
        parse_mode="HTML",
    )

    payload = {
        "filter": {"and": [
            {"property": "Tanggal", "date": {"on_or_after": first_day}},
            {"property": "Tanggal", "date": {"before": next_month}},
        ]}
    }

    try:
        results = await notion_query_all(NOTION_DATABASE_ID, payload)

        cats = {}
        total_pengeluaran = 0
        total_pemasukan = 0
        total_transfer = 0
        jumlah_transaksi = 0

        for r in results:
            props = r.get("properties", {})
            if is_blank_row(props):
                continue
            jumlah_transaksi += 1
            amount = p_number(props, "Ins (+)/Outs (-)") or 0

            # Transfer memindahkan uang, tidak menghabiskannya (brief bagian 1).
            if is_transfer(props):
                total_transfer += abs(amount)
                continue

            if amount < 0:
                kategori = p_select(props, "Kategori") or "Tanpa Kategori"
                cats[kategori] = cats.get(kategori, 0) + abs(amount)
                total_pengeluaran += abs(amount)
            elif amount > 0:
                total_pemasukan += amount

        pesan = f"📊 <b>Laporan Keuangan {label_bulan}</b>\n\n"
        if cats:
            pesan += "<b>Rincian Pengeluaran:</b>\n"
            for kategori, nilai in sorted(cats.items(), key=lambda x: x[1], reverse=True):
                porsi = nilai / total_pengeluaran * 100 if total_pengeluaran else 0
                pesan += f"▪️ {esc(kategori)}: {rp(nilai)} ({porsi:.0f}%)\n"
            pesan += "\n"
        else:
            pesan += "ℹ️ Belum ada pengeluaran di bulan ini, King Odiq.\n\n"

        pesan += f"📈 <b>Total Pemasukan:</b> {rp(total_pemasukan)}\n"
        pesan += f"📉 <b>Total Pengeluaran:</b> {rp(total_pengeluaran)}"
        if total_transfer:
            pesan += f"\n🔁 <b>Transfer aset:</b> {rp(total_transfer)} <i>(bukan pengeluaran)</i>"
        pesan += await saldo_footer()
        pesan += f"\n\n<i>{jumlah_transaksi} transaksi. Rincian saldo tiap aset: /saldo</i>"

        await send_long(message, pesan)
    except Exception as e:
        logger.exception("Gagal mengambil laporan")
        await message.reply_text(
            f"❌ Terjadi kesalahan, King Odiq saat mengambil laporan:\n<code>{esc(e)}</code>",
            parse_mode="HTML",
        )


async def reportcat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tampilkan pemilih kategori untuk laporan terfilter."""
    message = update.message if update.message else update.callback_query.message

    bulan, tahun, error = resolve_month(context.args, "/reportcat 5 (untuk Mei)")
    if error:
        await message.reply_text(error)
        return

    rows, row = [], []
    for i, kategori in enumerate(CATEGORIES):
        row.append(InlineKeyboardButton(kategori, callback_data=f"rcat_{bulan}_{tahun}_{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    await message.reply_text(
        f"📊 <b>Laporan Per Kategori — {NAMA_BULAN[bulan]} {tahun}</b>\n\n"
        "Pilih kategori yang ingin dilihat, King Odiq:",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
    )


async def reportcat_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    message = query.message

    parts = query.data[len("rcat_"):].split("_")
    if len(parts) != 3:
        await message.reply_text("⚠️ Data tidak valid, King Odiq.")
        return

    try:
        bulan, tahun = int(parts[0]), int(parts[1])
        kategori = CATEGORIES[int(parts[2])]
    except (ValueError, IndexError):
        await message.reply_text("⚠️ Data tidak valid, King Odiq.")
        return

    label_bulan = f"{NAMA_BULAN[bulan]} {tahun}"
    first_day, next_month = month_bounds(bulan, tahun)

    await message.reply_text(
        f"⏳ Mengambil data <b>{esc(kategori)}</b> untuk <b>{label_bulan}</b>...",
        parse_mode="HTML",
    )

    payload = {
        "filter": {"and": [
            {"property": "Tanggal", "date": {"on_or_after": first_day}},
            {"property": "Tanggal", "date": {"before": next_month}},
            {"property": "Kategori", "select": {"equals": kategori}},
        ]},
        "sorts": [{"property": "Tanggal", "direction": "descending"}],
    }

    try:
        results = await notion_query_all(NOTION_DATABASE_ID, payload)
        if not results:
            await message.reply_text(
                f"ℹ️ Belum ada transaksi untuk <b>{esc(kategori)}</b> di <b>{label_bulan}</b>, King Odiq.",
                parse_mode="HTML",
            )
            return

        total = 0
        pesan = f"📊 <b>Laporan {esc(kategori)} — {label_bulan}</b>\n\n"
        for i, r in enumerate(results, 1):
            props = r.get("properties", {})
            amount = p_number(props, "Ins (+)/Outs (-)") or 0
            total += amount
            tanggal = p_date(props, "Tanggal") or "-"
            if tanggal != "-":
                try:
                    tanggal = datetime.date.fromisoformat(tanggal[:10]).strftime("%d/%m")
                except ValueError:
                    pass
            sign = "+" if amount > 0 else "-"
            pesan += f"{i}. {esc(p_title(props) or '-')} | {sign}{rp(abs(amount))} | {tanggal}\n"

        pesan += f"\n<b>Total:</b> {rp(total)} ({len(results)} transaksi)"
        if kategori == CAT_TRANSFER:
            pesan += "\n\n🔁 <i>Kategori transfer — tidak dihitung sebagai pengeluaran.</i>"

        await send_long(message, pesan)
    except Exception as e:
        logger.exception("Gagal mengambil laporan kategori")
        await message.reply_text(f"❌ Terjadi kesalahan, King Odiq:\n<code>{esc(e)}</code>", parse_mode="HTML")


async def list_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message if update.message else update.callback_query.message

    query = update.callback_query
    show_all = False

    if query and query.data.startswith("list_all_"):
        parts = query.data.split("_")
        bulan, tahun = int(parts[2]), int(parts[3])
        show_all = True
    else:
        bulan, tahun, error = resolve_month(context.args, "/list 5 (untuk Mei)")
        if error:
            await message.reply_text(error)
            return

    first_day, next_month = month_bounds(bulan, tahun)
    label_bulan = f"{NAMA_BULAN[bulan]} {tahun}"
    await message.reply_text(
        f"⏳ Mengambil daftar transaksi <b>{label_bulan}</b> untuk King Odiq...",
        parse_mode="HTML",
    )

    payload = {
        "filter": {"and": [
            {"property": "Tanggal", "date": {"on_or_after": first_day}},
            {"property": "Tanggal", "date": {"before": next_month}},
        ]},
        "sorts": [{"property": "Tanggal", "direction": "descending"}],
    }

    try:
        raw = await notion_query_all(NOTION_DATABASE_ID, payload)
        results = [r for r in raw if not is_blank_row(r.get("properties", {}))]

        if not results:
            await message.reply_text(f"ℹ️ Belum ada transaksi di {label_bulan}, King Odiq.")
            return

        total_transfer = 0
        for r in results:
            props = r.get("properties", {})
            if is_transfer(props):
                total_transfer += abs(p_number(props, "Ins (+)/Outs (-)") or 0)

        pesan = f"📋 <b>Daftar Transaksi {label_bulan}</b>\n\n"
        items = results if show_all else results[:10]

        for i, r in enumerate(items, 1):
            props = r.get("properties", {})
            amount = p_number(props, "Ins (+)/Outs (-)") or 0
            kategori = p_select(props, "Kategori") or "-"
            tanggal = p_date(props, "Tanggal") or "-"
            if tanggal != "-":
                try:
                    tanggal = datetime.date.fromisoformat(tanggal[:10]).strftime("%d/%m")
                except ValueError:
                    pass
            marker = "🔁" if is_transfer(props) else ("+" if amount > 0 else "-")
            nominal = rp(abs(amount))
            pesan += f"{i}. {esc(p_title(props) or '-')} | {marker}{nominal} | {esc(kategori)} | {tanggal}\n"

        if not show_all and len(results) > 10:
            pesan += f"\n<i>...dan {len(results) - 10} transaksi lainnya.</i>"

        pesan += "\n"
        if total_transfer:
            pesan += f"\n🔁 <b>Transfer aset:</b> {rp(total_transfer)}"
        pesan += await saldo_footer()
        pesan += f"\n\n<i>{len(results)} transaksi. Rincian saldo tiap aset: /saldo</i>"

        reply_markup = None
        if not show_all and len(results) > 10:
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Tampilkan Semua Transaksi", callback_data=f"list_all_{bulan}_{tahun}")
            ]])

        await send_long(message, pesan, reply_markup=reply_markup)
    except Exception as e:
        logger.exception("Gagal mengambil daftar transaksi")
        await message.reply_text(f"❌ Terjadi kesalahan, King Odiq:\n<code>{esc(e)}</code>", parse_mode="HTML")


# ------------------------------------------
# Rekap harian otomatis
# ------------------------------------------

async def bangun_rekap_harian(tanggal: datetime.date) -> str:
    """Susun teks rekap seluruh transaksi pada satu tanggal.

    Dipisah dari pengirimannya supaya bentuk pesan yang sama dipakai penjadwal
    maupun perintah manual /rekap — tidak ada dua versi yang bisa menyimpang.
    """
    payload = {
        "filter": {"property": "Tanggal", "date": {"equals": tanggal.isoformat()}},
        "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
    }
    raw = await notion_query_all(NOTION_DATABASE_ID, payload)
    results = [r for r in raw if not is_blank_row(r.get("properties", {}))]

    label = (f"{NAMA_HARI[tanggal.weekday()]}, {tanggal.day} "
             f"{NAMA_BULAN[tanggal.month]} {tanggal.year}")
    judul = f"🌙 <b>Rekap Harian — {label}</b>\n\n"

    if not results:
        return (judul + "ℹ️ Belum ada transaksi tercatat hari ini, King Odiq.\n"
                + await saldo_kas_baris())

    baris = []
    for i, r in enumerate(results, 1):
        props = r.get("properties", {})
        amount = p_number(props, "Ins (+)/Outs (-)") or 0
        kategori = p_select(props, "Kategori") or "-"

        # Transfer memindahkan uang, tidak menghabiskannya (brief bagian 1),
        # jadi penandanya dibedakan dari pemasukan maupun pengeluaran.
        if is_transfer(props):
            marker = "🔁"
        elif amount > 0:
            marker = "+"
        else:
            marker = "-"

        baris.append(
            f"{i}. {esc(p_title(props) or '-')} | {marker}{rp(abs(amount))} | {esc(kategori)}"
        )

    # Ditutup baris Kas saja: total dan rincian lain sudah tersedia lewat
    # /report dan /saldo, rekap malam ini dijaga tetap sependek mungkin.
    return judul + "\n".join(baris) + "\n" + await saldo_kas_baris()


async def kirim_rekap_harian(app, tanggal: datetime.date = None) -> int:
    """Kirim rekap ke seluruh chat tujuan; kembalikan jumlah chat yang berhasil.

    Kegagalan satu chat tidak boleh menghentikan chat lain, dan kegagalan Notion
    tidak boleh mematikan penjadwal — keduanya cukup dicatat di log.
    """
    tanggal = tanggal or today_wib()

    if not REKAP_CHAT_IDS:
        logger.warning(
            "Rekap harian tidak dikirim: REKAP_CHAT_IDS dan ALLOWED_USER_IDS "
            "sama-sama kosong."
        )
        return 0

    try:
        pesan = await bangun_rekap_harian(tanggal)
    except Exception:
        logger.exception("Gagal menyusun rekap harian %s", tanggal)
        return 0

    bagian = potong_pesan(pesan)
    berhasil = 0
    for chat_id in REKAP_CHAT_IDS:
        try:
            for teks in bagian:
                await app.bot.send_message(chat_id, teks, parse_mode="HTML")
            berhasil += 1
        except Exception:
            logger.exception("Gagal mengirim rekap harian ke chat %s", chat_id)

    logger.info("Rekap harian %s terkirim ke %s dari %s chat",
                tanggal, berhasil, len(REKAP_CHAT_IDS))
    return berhasil


def _jadwal_berikutnya(sekarang: datetime.datetime = None) -> datetime.datetime:
    """Waktu kirim berikutnya, selalu di masa depan."""
    sekarang = sekarang or datetime.datetime.now(WIB)
    target = sekarang.replace(hour=REKAP_JAM, minute=REKAP_MENIT,
                              second=0, microsecond=0)
    if target <= sekarang:
        target += datetime.timedelta(days=1)
    return target


# Tanggal rekap terakhir yang dikirim penjadwal — penjaga supaya satu hari tidak
# terkirim dua kali walau loop terbangun lebih dari sekali di sekitar jam target.
_rekap_terakhir = None
_rekap_task = None


async def _loop_rekap_harian(app) -> None:
    """Tunggu sampai jam target lalu kirim rekap, berulang tiap hari.

    Sengaja tidak memakai JobQueue: extra job-queue PTB menyeret APScheduler
    yang hanya menerima timezone pytz, sedangkan WIB di sini datetime.timezone.
    Loop ini juga bangun tiap 10 menit, bukan tidur sekali 24 jam, supaya jam
    sistem yang bergeser atau proses yang sempat tertidur tidak membuat jadwal
    hari itu terlewat diam-diam.
    """
    global _rekap_terakhir

    # Kalau proses baru menyala setelah jam target, hari ini dianggap sudah
    # lewat. Tanpa ini, restart pukul 23.00 akan memuntahkan rekap susulan.
    sekarang = datetime.datetime.now(WIB)
    if (sekarang.hour, sekarang.minute) >= (REKAP_JAM, REKAP_MENIT):
        _rekap_terakhir = sekarang.date()

    logger.info("Penjadwal rekap harian aktif — kirim berikutnya %s WIB ke %s chat",
                _jadwal_berikutnya().strftime("%Y-%m-%d %H:%M"), len(REKAP_CHAT_IDS))

    while True:
        sisa = (_jadwal_berikutnya() - datetime.datetime.now(WIB)).total_seconds()
        await asyncio.sleep(max(1, min(sisa, 600)))

        sekarang = datetime.datetime.now(WIB)
        sudah_waktunya = (sekarang.hour, sekarang.minute) >= (REKAP_JAM, REKAP_MENIT)
        if sudah_waktunya and _rekap_terakhir != sekarang.date():
            _rekap_terakhir = sekarang.date()
            await kirim_rekap_harian(app, sekarang.date())


def jadwalkan_rekap_harian(app):
    """Nyalakan penjadwal sekali saja. Aman dipanggil berulang."""
    global _rekap_task

    if not REKAP_AKTIF:
        logger.info("Rekap harian dimatikan lewat REKAP_AKTIF.")
        return None
    if _rekap_task is not None and not _rekap_task.done():
        return _rekap_task

    _rekap_task = asyncio.create_task(_loop_rekap_harian(app))
    return _rekap_task


async def rekap_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rekap — rekap hari ini sekarang juga, tanpa menunggu jadwal.

    Argumen opsional: /rekap 2025-08-17 atau /rekap kemarin.
    """
    message = update.message if update.message else update.callback_query.message

    tanggal = today_wib()
    if context.args:
        arg = context.args[0].strip().lower()
        if arg in ("kemarin", "yesterday"):
            tanggal -= datetime.timedelta(days=1)
        else:
            try:
                tanggal = datetime.date.fromisoformat(arg)
            except ValueError:
                await message.reply_text(
                    "⚠️ Format tanggal salah, King Odiq. Contoh: "
                    "<code>/rekap 2025-08-17</code> atau <code>/rekap kemarin</code>",
                    parse_mode="HTML",
                )
                return

    await message.reply_text("⏳ Menyusun rekap harian untuk King Odiq...")
    try:
        await send_long(message, await bangun_rekap_harian(tanggal))
    except Exception as e:
        logger.exception("Gagal menyusun rekap harian manual")
        await message.reply_text(
            f"❌ Terjadi kesalahan, King Odiq:\n<code>{esc(e)}</code>",
            parse_mode="HTML",
        )


async def expired_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tombol dari percakapan yang sudah tidak aktif.

    State ConversationHandler hanya ada di memori, jadi restart bot membuat
    tombol lama tidak tertangani siapa pun. Tanpa handler ini bot diam total
    dan terlihat seperti rusak.
    """
    query = update.callback_query
    await query.answer("Sesi sudah berakhir", show_alert=False)
    await query.message.reply_text(
        "⌛ Sesi ini sudah berakhir, King Odiq — biasanya karena bot sempat "
        "dijalankan ulang.\n\nSilakan mulai lagi dari menu:",
        reply_markup=MAIN_KEYBOARD,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s memicu error", update, exc_info=context.error)


# ------------------------------------------
# Jalankan Bot
# ------------------------------------------
def check_config() -> None:
    """Pastikan seluruh kredensial ada sebelum apa pun dijalankan."""
    missing = [name for name, value in (
        ("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
        ("NOTION_TOKEN", NOTION_TOKEN),
        ("NOTION_DATABASE_ID", NOTION_DATABASE_ID),
        ("NOTION_ASET_DATABASE_ID", NOTION_ASET_DATABASE_ID),
    ) if not value]
    if missing:
        raise SystemExit(f"Konfigurasi belum lengkap di .env: {', '.join(missing)}")


def register_handlers(app) -> None:
    """Pasang seluruh handler ke Application.

    Dipisah dari main() supaya server.py (mode webhook) memakai persis
    rangkaian handler yang sama tanpa menduplikasinya.
    """
    add_handler = ConversationHandler(
        entry_points=[
            CommandHandler("add", start_add),
            CallbackQueryHandler(start_add, pattern="^menu_add$"),
        ],
        states={
            NAMA: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_type)],
            TIPE: [
                CallbackQueryHandler(ask_amount, pattern="^tipe_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, invalid_inline_input),
            ],
            JUMLAH: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_amount)],
            KATEGORI: [
                CallbackQueryHandler(save_transaction, pattern=r"^cat_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, invalid_inline_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        # State percakapan hanya ada di memori dan tidak pernah kedaluwarsa
        # sendiri. Tanpa ini, satu alur yang tergantung (mis. jawaban user tidak
        # sampai ke bot di grup) membuat /add dan tombol Tambah Transaksi ikut
        # mati untuk user tersebut sampai dia menemukan /cancel.
        allow_reentry=True,
    )

    transfer_handler = ConversationHandler(
        entry_points=[
            CommandHandler("transfer", start_transfer),
            CallbackQueryHandler(start_transfer, pattern="^menu_transfer$"),
        ],
        states={
            TRF_DARI: [
                CallbackQueryHandler(transfer_pick_dest, pattern=r"^trfrom_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, invalid_inline_input),
            ],
            TRF_KE: [
                CallbackQueryHandler(transfer_ask_amount, pattern=r"^trto_\d+_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, invalid_inline_input),
            ],
            TRF_JUMLAH: [MessageHandler(filters.TEXT & ~filters.COMMAND, transfer_save)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(add_handler)
    app.add_handler(transfer_handler)

    app.add_handler(CommandHandler("start", start))
    # Kalau ada percakapan aktif, fallback di ConversationHandler yang menang;
    # handler ini kebagian /cancel di luar percakapan supaya tidak pernah diam.
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("saldo", saldo))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("reportcat", reportcat))
    app.add_handler(CommandHandler("list", list_expenses))
    app.add_handler(CommandHandler("rekap", rekap_command))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("othermenu", other_menu))

    app.add_handler(CallbackQueryHandler(
        handle_inline_menu,
        pattern="^(menu_(report|reportcat|saldo|list|help|cancel)|list_all_|saldo_all$)",
    ))
    app.add_handler(CallbackQueryHandler(reportcat_result, pattern="^rcat_"))
    app.add_handler(CallbackQueryHandler(ask_change_account, pattern="^chacc_"))
    app.add_handler(CallbackQueryHandler(set_account, pattern="^setacc_"))
    app.add_handler(CallbackQueryHandler(ask_change_category, pattern="^chcat_"))
    app.add_handler(CallbackQueryHandler(set_category, pattern="^setcat_"))
    app.add_handler(CallbackQueryHandler(delete_transaction, pattern="^del_"))

    # Terakhir: tombol percakapan yang tidak lagi punya state aktif. Handler di
    # atas selalu menang duluan, jadi ini hanya kebagian tombol yatim.
    app.add_handler(CallbackQueryHandler(
        expired_callback, pattern=r"^(trfrom_|trto_|cat_|tipe_)"
    ))

    app.add_error_handler(on_error)


async def _post_init(app) -> None:
    """Dipanggil PTB tepat sebelum polling dimulai (mode lokal).

    Mode webhook tidak melewati jalur ini, jadi server.py menyalakan penjadwal
    sendiri lewat jadwalkan_rekap_harian(). Fungsi itu idempoten, jadi aman
    walaupun keduanya sempat terpanggil.
    """
    jadwalkan_rekap_harian(app)


def build_application():
    """Application siap pakai, belum dijalankan.

    server.py memakainya untuk mode webhook; main() untuk mode polling.
    """
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    register_handlers(app)
    return app


def main() -> None:
    """Mode polling — untuk pengembangan lokal.

    Di Heroku bot berjalan lewat server.py (webhook), karena satu proses web
    melayani bot dan Mini App sekaligus.
    """
    check_config()
    app = build_application()

    print("Bot Telegram Finance Tracker sedang berjalan (polling)...")

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app.run_polling()


if __name__ == "__main__":
    main()

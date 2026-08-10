"""Proses web: webhook Telegram + Mini App + API datanya, dalam satu proses.

Kenapa satu proses: Heroku hanya merutekan HTTP ke process type `web`, dan
menjalankan `worker` (polling) di samping `web` berarti dua dyno untuk pekerjaan
yang muat di satu. Modul ini menyalakan Application yang sama seperti bot.py,
tapi disuapi update lewat webhook, bukan polling.

Seluruh handler, helper Notion, dan penebak kategori dipakai ulang apa adanya
dari bot.py — tidak ada logika bot yang diduplikasi di sini.
"""

import os
import json
import time
import hmac
import hashlib
import asyncio
import logging
import datetime
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from telegram import Update, MenuButtonWebApp, WebAppInfo

import bot

logger = logging.getLogger("server")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# URL publik aplikasi, mis. https://namaapp.herokuapp.com (tanpa garis miring
# di akhir). Tanpa ini webhook dan tombol menu tidak didaftarkan otomatis.
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")

# Dicocokkan dengan header X-Telegram-Bot-Api-Secret-Token yang dikirim Telegram.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# Jalur webhook. Diturunkan dari token supaya tidak bisa ditebak tanpa tahu
# token, tapi tetap sama di tiap restart sehingga webhook tidak perlu didaftar
# ulang setelah deploy.
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "").strip() or hashlib.sha256(
    ("jalur:" + bot.TELEGRAM_TOKEN).encode()
).hexdigest()[:32]

# Daftar id Telegram yang boleh membuka dashboard. Sengaja gagal-tertutup:
# kalau belum diisi, semua ditolak. Dashboard ini memaparkan seluruh keuangan,
# jadi lupa mengisi tidak boleh berarti terbuka untuk siapa saja.
ALLOWED_USER_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",")
    if x.isdigit()
}

# initData yang lebih tua dari ini ditolak, supaya salinan yang bocor tidak
# bisa dipakai berulang selamanya.
MAX_UMUR_INIT_DATA = datetime.timedelta(hours=24).total_seconds()

# Notion butuh 8 request berantai untuk ~750 transaksi. Tanpa cache, tiap kali
# Mini App dibuka user menunggu beberapa detik.
CACHE_TTL = datetime.timedelta(seconds=90)

application = bot.build_application()

_cache = {"data": None, "at": None}
_cache_lock = asyncio.Lock()


# ------------------------------------------
# Verifikasi initData Telegram
# ------------------------------------------

def verifikasi_init_data(mentah: str) -> dict:
    """Pastikan initData benar-benar dari Telegram, lalu kembalikan datanya.

    Telegram menandatangani initData dengan HMAC-SHA256 memakai kunci turunan
    token bot. Karena hanya Telegram dan kita yang tahu token itu, tanda tangan
    yang cocok membuktikan identitas user tanpa perlu login terpisah.
    """
    if not mentah:
        raise HTTPException(status_code=401, detail="initData kosong")

    try:
        bidang = dict(parse_qsl(mentah, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        raise HTTPException(status_code=401, detail="initData rusak")

    diberikan = bidang.pop("hash", "")
    if not diberikan:
        raise HTTPException(status_code=401, detail="initData tanpa hash")

    untai = "\n".join(f"{k}={bidang[k]}" for k in sorted(bidang))
    kunci = hmac.new(b"WebAppData", bot.TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
    dihitung = hmac.new(kunci, untai.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(dihitung, diberikan):
        raise HTTPException(status_code=401, detail="tanda tangan initData tidak cocok")

    try:
        umur = time.time() - int(bidang.get("auth_date", "0"))
    except ValueError:
        umur = MAX_UMUR_INIT_DATA + 1
    if umur > MAX_UMUR_INIT_DATA:
        raise HTTPException(status_code=401, detail="sesi kedaluwarsa, buka ulang dari Telegram")

    try:
        return json.loads(bidang.get("user", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=401, detail="data user tidak terbaca")


def pastikan_diizinkan(user: dict) -> int:
    uid = user.get("id")
    if not ALLOWED_USER_IDS:
        logger.error(
            "ALLOWED_USER_IDS belum diisi — semua akses dashboard ditolak. "
            "Isi config var itu dengan id Telegram Anda."
        )
        raise HTTPException(status_code=403, detail="daftar izin belum dikonfigurasi")
    if uid not in ALLOWED_USER_IDS:
        logger.warning("Akses dashboard ditolak untuk user id %s", uid)
        raise HTTPException(status_code=403, detail="akun tidak diizinkan")
    return uid


# ------------------------------------------
# Agregasi data untuk Mini App
# ------------------------------------------

async def rakit_data() -> dict:
    """Baca Notion lalu susun jadi bentuk yang dipakai Mini App.

    Agregasi dikerjakan di sini, bukan di browser, supaya NOTION_TOKEN tidak
    pernah meninggalkan server dan payload yang dikirim tetap ramping.
    """
    akun = await bot.get_accounts(force=True)
    nama_per_id = {a["id"]: a["nama"] for a in akun}

    halaman = await bot.notion_query_all(bot.NOTION_DATABASE_ID, {})

    tx = []
    for p in halaman:
        props = p.get("properties", {})
        if bot.is_blank_row(props):
            continue
        tanggal = bot.p_date(props, "Tanggal")
        if not tanggal:
            continue

        nominal = bot.p_number(props, "Ins (+)/Outs (-)") or 0
        transfer = bot.is_transfer(props)

        baris = {
            "n": bot.p_title(props) or "(tanpa nama)",
            "a": round(nominal),
            "d": tanggal[:10],
            "k": bot.p_select(props, "Kategori") or "🤔Lainnya",
            "t": "T" if transfer else ("M" if nominal > 0 else "P"),
        }
        if transfer:
            dari = bot.p_relation(props, "Dari Kantong")
            ke = bot.p_relation(props, "Ke Aset")
            baris["f"] = nama_per_id.get(dari[0]) if dari else None
            baris["o"] = nama_per_id.get(ke[0]) if ke else None
        tx.append(baris)

    tx.sort(key=lambda r: r["d"], reverse=True)

    hari_ini = bot.today_wib().isoformat()
    bulan = {}
    for r in sorted(tx, key=lambda x: x["d"]):      # menaik: urutan bulan kronologis
        kunci = r["d"][:7]
        if kunci not in bulan:
            tahun, nomor = int(kunci[:4]), int(kunci[5:7])
            nama_bulan = bot.NAMA_BULAN[nomor]
            bulan[kunci] = {
                "key": kunci,
                "label": f"{nama_bulan} {tahun}",
                "short": nama_bulan[:3],
                "masuk": 0, "keluar": 0, "n": 0, "transfer": 0,
                "partial": kunci == hari_ini[:7],
            }
        v = bulan[kunci]
        if r["t"] == "T":
            # Transfer memindah kantong — bukan arus masuk maupun keluar.
            v["transfer"] += abs(r["a"])
            continue
        v["n"] += 1
        if r["a"] > 0:
            v["masuk"] += r["a"]
        else:
            v["keluar"] += -r["a"]
    for v in bulan.values():
        v["net"] = v["masuk"] - v["keluar"]

    # Slot warna terikat entitas lewat urutan deklaratif ACCOUNT_ICONS, bukan
    # peringkat saldo — supaya Tabungan tidak berganti warna saat urutannya
    # bergeser.
    urutan = list(bot.ACCOUNT_ICONS)
    aset = [{
        "nama": a["nama"],
        "jenis": a["jenis"],
        "saldo": a["saldo"],
        "slot": urutan.index(a["nama"]) if a["nama"] in urutan else len(urutan),
        "ikon": bot.icon_for(a["nama"]),
    } for a in sorted(akun, key=lambda x: -x["saldo"])]

    return {
        "generated": hari_ini,
        "netWorth": sum(a["saldo"] for a in akun),
        "aset": aset,
        "bulan": list(bulan.values()),
        "tx": tx,
    }


async def ambil_data() -> dict:
    """Data ter-cache; hanya satu pembacaan Notion walau banyak permintaan masuk."""
    sekarang = datetime.datetime.now(datetime.timezone.utc)
    segar = (_cache["data"] is not None and _cache["at"] is not None
             and sekarang - _cache["at"] < CACHE_TTL)
    if segar:
        return _cache["data"]

    async with _cache_lock:
        # Diperiksa ulang: permintaan lain mungkin sudah mengisinya saat antre.
        sekarang = datetime.datetime.now(datetime.timezone.utc)
        if (_cache["data"] is not None and _cache["at"] is not None
                and sekarang - _cache["at"] < CACHE_TTL):
            return _cache["data"]

        data = await rakit_data()
        _cache["data"] = data
        _cache["at"] = datetime.datetime.now(datetime.timezone.utc)
        return data


# ------------------------------------------
# Daur hidup aplikasi
# ------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    bot.check_config()
    await application.initialize()
    await application.start()

    if BASE_URL:
        url = f"{BASE_URL}/tg/{WEBHOOK_PATH}"
        await application.bot.set_webhook(
            url=url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=Update.ALL_TYPES,
            # Buang update menumpuk saat bot mati, supaya restart tidak
            # memuntahkan balasan lama sekaligus.
            drop_pending_updates=True,
        )
        logger.info("Webhook didaftarkan ke %s", url)

        try:
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Dashboard",
                    web_app=WebAppInfo(url=f"{BASE_URL}/app"),
                )
            )
        except Exception:
            logger.exception("Gagal memasang tombol menu Mini App")
    else:
        logger.warning("BASE_URL kosong — webhook dan tombol menu tidak didaftarkan.")

    yield

    await application.stop()
    await application.shutdown()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


# ------------------------------------------
# Route
# ------------------------------------------

@app.post("/tg/{jalur}")
async def terima_update(
    jalur: str,
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
):
    """Titik masuk update Telegram."""
    if not hmac.compare_digest(jalur, WEBHOOK_PATH):
        raise HTTPException(status_code=404, detail="not found")
    if WEBHOOK_SECRET and not hmac.compare_digest(
            x_telegram_bot_api_secret_token or "", WEBHOOK_SECRET):
        logger.warning("Update ditolak: secret token tidak cocok")
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        muatan = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bukan JSON")

    await application.process_update(Update.de_json(muatan, application.bot))
    return {"ok": True}


@app.get("/api/data")
async def api_data(x_init_data: str = Header(default="")):
    user = verifikasi_init_data(x_init_data)
    uid = pastikan_diizinkan(user)
    try:
        data = await ambil_data()
    except Exception:
        logger.exception("Gagal merakit data untuk user %s", uid)
        raise HTTPException(status_code=502, detail="gagal membaca Notion")
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.get("/")
@app.get("/app")
async def halaman_mini_app():
    # no-store: webview Telegram menyimpan cache dengan agresif, sampai versi
    # baru tidak muncul walau sudah di-deploy.
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
async def health():
    return {"ok": True, "webhook": bool(BASE_URL), "izin": len(ALLOWED_USER_IDS)}

import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from html import escape
from io import BytesIO
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
VERIFY_API_URL = os.getenv("VERIFY_API_URL", "https://api.usun.cash/api/bank/verify").strip()
PARTNER_ID = int(os.getenv("PARTNER_ID", "160"))
ALLOWED_USER_IDS = {
    int(item.strip())
    for item in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if item.strip().isdigit()
}
DB_PATH = Path(__file__).with_name("bank_checker.db")

BANK_COLORS = {
    "BBL": "#1E4E9D", "KBANK": "#138A48", "KTB": "#10A5D8",
    "SCB": "#553087", "TTB": "#0B3A82", "BAY": "#E8B51A",
    "GSB": "#EB4B8A", "GHB": "#F08222", "BAAC": "#278B50",
    "KKP": "#1E8B8A", "CIMBT": "#B51F2E", "UOBT": "#214F9B",
    "TISCO": "#315C93", "LHBANK": "#8AC443",
}

BANKS = {
    "KTB": "กรุงไทย", "KBANK": "กสิกรไทย",
    "SCB": "ไทยพาณิชย์", "BBL": "กรุงเทพ", "BAY": "กรุงศรีอยุธยา",
    "TTB": "ทหารไทยธนชาต", "GSB": "ออมสิน", "BAAC": "ธ.ก.ส.",
    "GHB": "อาคารสงเคราะห์", "CIMBT": "ซีไอเอ็มบี ไทย",
    "KKP": "เกียรตินาคินภัทร", "UOBT": "ยูโอบี",
    "TISCO": "ทิสโก้", "LHBANK": "แลนด์ แอนด์ เฮ้าส์",
}
ALIASES = {
    "กรุงไทย": "KTB", "ktb": "KTB", "กสิกร": "KBANK", "กสิกรไทย": "KBANK",
    "kbank": "KBANK", "ไทยพาณิชย์": "SCB", "scb": "SCB", "กรุงเทพ": "BBL",
    "bbl": "BBL", "กรุงศรี": "BAY", "กรุงศรีอยุธยา": "BAY", "bay": "BAY",
    "ttb": "TTB", "ทหารไทยธนชาต": "TTB", "ออมสิน": "GSB", "gsb": "GSB",
    "ธกส": "BAAC", "ธ.ก.ส.": "BAAC", "baac": "BAAC", "ghb": "GHB",
    "อาคารสงเคราะห์": "GHB", "cimbt": "CIMBT", "kkp": "KKP", "uob": "UOBT",
    "ธนาคารกสิกรไทย": "KBANK", "ธนาคารกรุงเทพ": "BBL", "ธนาคารกรุงไทย": "KTB",
    "ธนาคารไทยพาณิชย์": "SCB", "ธนาคารทหารไทยธนชาต": "TTB",
    "tisco": "TISCO", "ทิสโก้": "TISCO", "lh bank": "LHBANK",
    "ธนาคารแลนด์ แอนด์ เฮ้าส์": "LHBANK",
    "ธนาคารกรุงศรีอยุธยา": "BAY", "ธนาคารออมสิน": "GSB",
    "ธนาคารอาคารสงเคราะห์": "GHB",
    "ธนาคารเพื่อการเกษตรและสหกรณ์การเกษตร": "BAAC",
    "ธนาคารเกียรตินาคินภัทร": "KKP", "ธนาคารซีไอเอ็มบี ไทย": "CIMBT",
    "ธนาคารยูโอบี": "UOBT", "ธนาคารทิสโก้": "TISCO",
}
MENU = ReplyKeyboardMarkup(
    [
        ["🔎 ตรวจบัญชีธนาคาร", "🏦 ธนาคารที่รองรับ"],
        ["📋 ประวัติล่าสุด", "📊 สถิติการตรวจ"],
        ["❓ วิธีใช้งาน", "🪪 ดู User ID"],
    ],
    resize_keyboard=True,
)


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS checks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,bank_code TEXT,"
            "masked_account TEXT,fullname TEXT,success INTEGER,checked_at TEXT)"
        )


def clean_fullname(fullname: str | None) -> str:
    if not fullname:
        return ""
    value = str(fullname).strip()

    # Remove honorifics/prefixes from the beginning of the name.
    honorific_pattern = re.compile(
        r"^(?:"
        # Thai common prefixes / abbreviations
        r"นาย|นางสาว|นาง|น\.ส\.?|"
        r"เด็กชาย|เด็กหญิง|ด\.ช\.|ด\.ญ\.|"
        r"คุณหญิง|คุณ|หม่อมหลวง|หม่อมราชวงศ์|หม่อมเจ้า|"
        r"ศาสตราจารย์|ศ\.|รองศาสตราจารย์|รศ\.|ผู้ช่วยศาสตราจารย์|ผศ\.|"
        r"ดอกเตอร์|ดร\.?|"
        r"พลเอก|พลโท|พลตรี|พลจัตวา|พล\.อ\.?|พล\.ท\.?|พล\.ต\.?|"
        r"พันเอก|พันโท|พันตรี|พ\.อ\.?|พ\.ท\.?|พ\.ต\.?|"
        r"นาวาอากาศเอก|นาวาอากาศโท|นาวาอากาศตรี|น\.อ\.อ\.?|น\.อ\.ท\.?|น\.อ\.ต\.?|"
        r"พันตำรวจเอก|พันตำรวจโท|พันตำรวจตรี|พ\.ต\.อ\.?|พ\.ต\.ท\.?|พ\.ต\.ต\.?|"
        r"ร้อยตำรวจเอก|ร้อยตำรวจโท|ร้อยตำรวจตรี|ร\.ต\.อ\.?|ร\.ต\.ท\.?|ร\.ต\.ต\.?|"
        r"ร้อยเอก|ร้อยโท|ร้อยตรี|ร\.อ\.?|ร\.ท\.?|ร\.ต\.?|"
        r"สิบเอก|สิบโท|สิบตรี|ส\.อ\.?|ส\.ท\.?|ส\.ต\.?|"
        r"จ่าสิบเอก|จ่าสิบโท|จ่าสิบตรี|จ\.ส\.อ\.?|จ\.ส\.ท\.?|จ\.ส\.ต\.?|"
        r"พระ|พระภิกษุ|แม่ชี|"
        # English
        r"MR\.?|MRS\.?|MS\.?|MISS\.?|DR\.?"
        r")(?=\s|$)",
        re.IGNORECASE,
    )

    # Keep removing while the API returns multiple prefixes in a row.
    previous = None
    while value != previous:
        previous = value
        value = honorific_pattern.sub("", value, count=1).strip()

    return value


def save_history(user_id: int, bank: str, account: str, fullname: str | None, success: bool) -> None:
    # Store full account number in history.
    fullname = clean_fullname(fullname)
    masked = account
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO checks(user_id,bank_code,masked_account,fullname,success,checked_at)"
            " VALUES(?,?,?,?,?,?)",
            (user_id, bank, masked, fullname, int(success), datetime.now().strftime("%d/%m/%Y %H:%M")),
        )



def clean_person_name(name: str | None) -> str:
    """Remove common Thai/English personal honorifics from the beginning of a name."""
    if not name:
        return ""
    value = str(name).strip()

    # Repeatedly remove leading honorifics, including common Thai and English forms.
    honorific_pattern = re.compile(
        r"^(?:"
        r"นาย|นางสาว|นาง|นาง\.สาว|น\.ส\.|น\.ส|"
        r"เด็กชาย|เด็กหญิง|ด\.ช\.|ด\.ญ\.|"
        r"คุณ|คุณหญิง|หม่อมหลวง|หม่อมราชวงศ์|หม่อมเจ้า|"
        r"ศาสตราจารย์|ศ\.|รองศาสตราจารย์|รศ\.|ผู้ช่วยศาสตราจารย์|ผศ\.|"
        r"ดอกเตอร์|ดร\.|"
        r"พลเอก|พลโท|พลตรี|พลจัตวา|พล\.อ\.|พล\.ท\.|พล\.ต\.|"
        r"พันเอก|พันโท|พันตรี|พ\.อ\.|พ\.ท\.|พ\.ต\.|"
        r"นาวาอากาศเอก|นาวาอากาศโท|นาวาอากาศตรี|น\.อ\.อ\.|น\.อ\.ท\.|น\.อ\.ต\.|"
        r"พันตำรวจเอก|พันตำรวจโท|พันตำรวจตรี|พ\.ต\.อ\.|พ\.ต\.ท\.|พ\.ต\.ต\.|"
        r"ร้อยตำรวจเอก|ร้อยตำรวจโท|ร้อยตำรวจตรี|ร\.ต\.อ\.|ร\.ต\.ท\.|ร\.ต\.ต\.|"
        r"ร้อยเอก|ร้อยโท|ร้อยตรี|ร\.อ\.|ร\.ท\.|ร\.ต\.|"
        r"สิบเอก|สิบโท|สิบตรี|ส\.อ\.|ส\.ท\.|ส\.ต\.|"
        r"จ่าสิบเอก|จ่าสิบโท|จ่าสิบตรี|จ\.ส\.อ\.|จ\.ส\.ท\.|จ\.ส\.ต\.|"
        r"พระ|พระภิกษุ|แม่ชี|"
        r"MR\.|MR|Mr\.|Mr|MRS\.|MRS|Mrs\.|Mrs|MS\.|MS|Ms\.|Ms|MISS\.|MISS|Miss\.|Miss|DR\.|DR|Dr\.|Dr"
        r")(?=\s|$)",
        re.IGNORECASE,
    )

    previous = None
    while value != previous:
        previous = value
        value = honorific_pattern.sub("", value, count=1).strip()

    return value


def make_result_card(bank_code: str, account: str, fullname: str) -> BytesIO:
    color = BANK_COLORS.get(bank_code, "#2563EB")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
    *{{box-sizing:border-box}}html,body{{margin:0;width:900px;height:270px;overflow:hidden;
    background:#111827;font-family:Tahoma,Arial,sans-serif}}
    .card{{position:absolute;inset:15px;border-radius:34px;background:#d1d5db;
    display:flex;align-items:center;padding:33px 30px;gap:35px}}
    .logo{{width:160px;height:174px;border-radius:28px;background:{color};color:white;
    display:flex;align-items:center;justify-content:center;font-size:42px;font-weight:bold}}
    .info{{color:#1f2937;line-height:1.35}}.bank{{font-size:39px;font-weight:bold;color:#111827}}
    .name,.account{{font-size:32px;margin-top:8px}}
    </style></head><body><div class="card"><div class="logo">{escape(bank_code)}</div>
    <div class="info"><div class="bank">{escape(BANKS.get(bank_code, bank_code))}</div>
    <div class="name">{escape(fullname)}</div><div class="account">{escape(account)}</div>
    </div></div></body></html>"""
    edge_candidates = [
        shutil.which("msedge"),
        os.path.join(os.getenv("PROGRAMFILES(X86)", ""), "Microsoft/Edge/Application/msedge.exe"),
        os.path.join(os.getenv("PROGRAMFILES", ""), "Microsoft/Edge/Application/msedge.exe"),
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Microsoft/Edge/Application/msedge.exe"),
    ]
    edge = next((path for path in edge_candidates if path and Path(path).exists()), None)
    if not edge:
        raise RuntimeError("Microsoft Edge not found")
    with tempfile.TemporaryDirectory(prefix="bank_card_") as temp:
        html_path = Path(temp) / "card.html"
        png_path = Path(temp) / "card.png"
        html_path.write_text(html, encoding="utf-8")
        completed = subprocess.run(
            [edge, "--headless", "--disable-gpu", "--hide-scrollbars",
             "--window-size=900,270", f"--screenshot={png_path}", html_path.as_uri()],
            capture_output=True, timeout=30, check=False,
        )
        if completed.returncode != 0 or not png_path.exists():
            raise RuntimeError("Edge screenshot failed")
        image_bytes = png_path.read_bytes()
    output = BytesIO()
    output.name = "bank_result.png"
    output.write(image_bytes)
    output.seek(0)
    return output


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


def normalize_bank(value: str) -> str | None:
    clean = " ".join(value.strip().split())
    upper = clean.upper()
    if upper in BANKS:
        return upper
    for code in BANKS:
        if re.search(r"(?<![A-Z0-9])" + re.escape(code) + r"(?![A-Z0-9])", upper):
            return code
    no_brackets = " ".join(re.sub(r"[()\[\]{}]", " ", clean).split())
    return (ALIASES.get(clean.lower()) or ALIASES.get(clean)
            or ALIASES.get(no_brackets.lower()) or ALIASES.get(no_brackets))


def parse_request(text: str) -> tuple[str, str] | None:
    matches = re.findall(r"(?:\d[\s-]*){6,20}", text)
    if not matches:
        return None
    raw_account = max(matches, key=len)
    account = re.sub(r"\D", "", raw_account)
    bank_text = text.replace(raw_account, " ", 1).strip()
    bank = normalize_bank(bank_text)
    if not bank or not (6 <= len(account) <= 20):
        return None
    return bank, account


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 ระบบตรวจบัญชีธนาคาร\n\n"
        "ส่งได้ทั้ง TTB 1234567890 หรือ 1234567890 TTB\n"
        "รองรับวงเล็บ และส่งธนาคารกับเลขบัญชีแยกคนละข้อความได้\n\n"
        "ใช้ /myid เพื่อดู Telegram User ID ของคุณ",
        reply_markup=MENU,
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Telegram User ID ของคุณ: {update.effective_user.id}")


async def help_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text or ""
    if "ธนาคารที่รองรับ" in text:
        lines = [f"{code} — {name}" for code, name in BANKS.items()]
        await update.effective_message.reply_text("🏦 ธนาคารที่รองรับ\n\n" + "\n".join(lines))
    elif "User ID" in text:
        await myid(update, context)
    elif "ประวัติ" in text:
        if not is_allowed(update):
            await update.effective_message.reply_text("บัญชีนี้ยังไม่ได้รับอนุญาตครับ")
            return
        with sqlite3.connect(DB_PATH) as db:
            rows = db.execute(
                "SELECT bank_code,masked_account,fullname,success,checked_at FROM checks "
                "WHERE user_id=? ORDER BY id DESC LIMIT 10", (update.effective_user.id,)
            ).fetchall()
        if not rows:
            await update.effective_message.reply_text("📋 ยังไม่มีประวัติการตรวจครับ")
            return
        lines = ["📋 รายการตรวจสอบล่าสุด"]
        for bank, account, name, success, checked_at in rows:
            clean_name = clean_fullname(name)
            lines.append(
                f"\n{'✅' if success else '❌'} {escape(bank)}\n"
                f"เลขบัญชี: <code>{escape(account)}</code>\n"
                f"ชื่อบัญชี: <code>{escape(clean_name or 'ไม่พบข้อมูล')}</code>\n"
                f"{escape(checked_at)}"
            )
        await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")
    elif "สถิติ" in text:
        if not is_allowed(update):
            await update.effective_message.reply_text("บัญชีนี้ยังไม่ได้รับอนุญาตครับ")
            return
        with sqlite3.connect(DB_PATH) as db:
            total, success = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(success),0) FROM checks WHERE user_id=?",
                (update.effective_user.id,),
            ).fetchone()
        failed = total - success
        rate = success / total * 100 if total else 0
        await update.effective_message.reply_text(
            f"📊 สถิติการตรวจของคุณ\n\nตรวจทั้งหมด: {total} รายการ\n"
            f"สำเร็จ: {success} รายการ\nไม่สำเร็จ: {failed} รายการ\n"
            f"อัตราสำเร็จ: {rate:.1f}%"
        )
    else:
        await update.effective_message.reply_text(
            "ตัวอย่าง:\nTTB 1234567890\n1234567890 TTB\n"
            "ธนาคารกสิกรไทย (Kbank) 1234567890\n\n"
            "หรือส่งธนาคารและเลขบัญชีแยกคนละข้อความ"
        )


async def call_verify_api(bank_code: str, account_number: str) -> dict:
    payload = {"bankCode": bank_code, "accountNumber": account_number, "partnerId": PARTNER_ID}
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://chapo88vip.com",
        "Referer": "https://chapo88vip.com/",
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        response = await client.post(VERIFY_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Invalid response format")
        return result


async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    text = message.text or ""

    if text.startswith("🔎"):
        await message.reply_text("ส่งข้อมูลในรูปแบบนี้ครับ:\nKTB 1234567890")
        return
    if not is_allowed(update):
        await message.reply_text(
            "บัญชีนี้ยังไม่ได้รับอนุญาตครับ\n"
            f"User ID: {user.id}\nให้นำเลขนี้ไปใส่ใน ALLOWED_USER_IDS ของไฟล์ .env"
        )
        return

    parsed = parse_request(text)
    if not parsed:
        bank_only = normalize_bank(text)
        digits_only = re.sub(r"\D", "", text) if re.fullmatch(r"[\d\s-]+", text.strip()) else ""
        account_only = digits_only if 6 <= len(digits_only) <= 20 else None
        if bank_only:
            saved_account = context.user_data.pop("pending_account", None)
            if saved_account:
                parsed = (bank_only, saved_account)
            else:
                context.user_data["pending_bank"] = bank_only
                await message.reply_text(
                    f"เลือก {BANKS.get(bank_only, bank_only)} ({bank_only}) แล้วครับ\n"
                    "ส่งเลขบัญชีมาได้เลย"
                )
                return
        elif account_only:
            saved_bank = context.user_data.pop("pending_bank", None)
            if saved_bank:
                parsed = (saved_bank, account_only)
            else:
                context.user_data["pending_account"] = account_only
                await message.reply_text("รับเลขบัญชีแล้วครับ กรุณาส่งชื่อหรือรหัสธนาคาร")
                return
        else:
            await message.reply_text(
                "รูปแบบไม่ถูกต้องครับ ตัวอย่าง: TTB 1234567890 หรือ 1234567890 TTB"
            )
            return

    bank_code, account_number = parsed
    status = await message.reply_text("🔍 กำลังตรวจสอบบัญชี…")

    try:
        result = await call_verify_api(bank_code, account_number)
        if result.get("success") and result.get("fullname"):
            fullname = clean_fullname(str(result["fullname"]))
            bank_name = BANKS.get(bank_code, bank_code)
            copy_buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 คัดลอกเลขบัญชี",
                                      copy_text=CopyTextButton(text=account_number))],
                [InlineKeyboardButton("👤 คัดลอกชื่อ–นามสกุล",
                                      copy_text=CopyTextButton(text=fullname))],
                [InlineKeyboardButton("🏦 คัดลอกชื่อธนาคาร",
                                      copy_text=CopyTextButton(text=bank_name))],
            ])
            save_history(user.id, bank_code, account_number, fullname, True)

            # ส่งรูปก่อน แล้วจึงแสดงรายละเอียดด้านล่าง
            card = None
            try:
                card = make_result_card(bank_code, account_number, fullname)
                await message.reply_photo(photo=card, caption="🖼 รูปสรุปผลตรวจ")
            except Exception as image_error:
                logging.warning("Result card failed: %s", type(image_error).__name__)

            # <code> คือ Monospace แบบเดียวกับ Ctrl+Shift+M ใน Telegram
            details = (
                "✅ <b>ตรวจสอบสำเร็จ</b>\n\n"
                f"ธนาคาร: {escape(bank_name)} ({escape(bank_code)})\n"
                f"เลขบัญชี: <code>{escape(account_number)}</code>\n"
                f"ชื่อบัญชี: <code>{escape(fullname)}</code>"
            )
            await status.edit_text(details, parse_mode="HTML", reply_markup=copy_buttons)
            if card is None:
                await message.reply_text("⚠️ ตรวจบัญชีสำเร็จ แต่สร้างรูปสรุปไม่สำเร็จครับ")
        else:
            save_history(user.id, bank_code, account_number, None, False)
            await status.edit_text("❌ ไม่พบข้อมูลบัญชี หรือข้อมูลไม่ถูกต้องครับ")
    except httpx.HTTPStatusError as exc:
        save_history(user.id, bank_code, account_number, None, False)
        logging.warning("Bank API HTTP status: %s", exc.response.status_code)
        if exc.response.status_code == 429:
            await status.edit_text("⏳ ระบบจำกัดจำนวนการตรวจ กรุณารอสักครู่แล้วลองใหม่ครับ")
        elif exc.response.status_code == 422:
            await status.edit_text("❌ ธนาคารหรือเลขบัญชีไม่ถูกต้อง (422)")
        else:
            await status.edit_text(f"❌ ระบบตรวจสอบตอบกลับผิดพลาด ({exc.response.status_code})")
    except Exception as exc:
        logging.warning("Bank verification failed: %s", type(exc).__name__)
        await status.edit_text("❌ เชื่อมต่อระบบตรวจสอบไม่สำเร็จ กรุณาลองใหม่ภายหลังครับ")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("กรุณาใส่ TELEGRAM_BOT_TOKEN ในไฟล์ .env")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_message))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(MessageHandler(filters.Regex(r"^(❓|🏦|🪪|📋|📊)"), help_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, verify))
    logging.info("Bank checker bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

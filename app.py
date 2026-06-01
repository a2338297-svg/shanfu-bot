import os, json, base64, re
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, ImageMessageContent
)
import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

app = Flask(__name__)

LINE_SECRET     = os.environ["LINE_CHANNEL_SECRET"]
LINE_TOKEN      = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GEMINI_KEY      = os.environ["GEMINI_API_KEY"]
SHEET_ID        = os.environ["GOOGLE_SHEET_ID"]
SERVICE_ACCOUNT = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

configuration = Configuration(access_token=LINE_TOKEN)
handler       = WebhookHandler(LINE_SECRET)

genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel("gemini-1.5-flash-latest")

def get_sheets():
    creds_data = json.loads(SERVICE_ACCOUNT)
    creds = Credentials.from_service_account_info(
        creds_data,
        scopes=["https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"]
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)

CLASSIFY_PROMPT = """你是山富旅遊的業務助理。
業務會把從LINE或Email收到的旅客資訊貼給你，請分析並回傳JSON。
分類：passport/payment/dietary/room/change/flight/missing/other
只回傳JSON，不要其他文字：
{
  "type": "分類",
  "team": "團別（沒有填null）",
  "passengers": [{"name": "姓名", "data": {"欄位": "值"}}],
  "summary": "一行中文摘要",
  "action": "要更新哪個分頁"
}
護照欄位：passport_no, expiry, birthday, id_no, name_en
付款欄位：deposit_amount, deposit_date, deposit_method, balance_amount, last5digits
餐食欄位：no_beef, no_raw, vegetarian, no_seafood, other_dietary
班機欄位：flight_no, departure, arrival, dep_time, arr_time"""

def classify_text(text):
    resp = model.generate_content(f"{CLASSIFY_PROMPT}\n\n旅客資訊：\n{text}")
    raw  = resp.text.strip()
    raw  = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.MULTILINE)
    return json.loads(raw)

def classify_image(image_b64):
    prompt = """這是旅客傳來的文件圖片。
只回傳JSON，不要其他文字：
{
  "doc_type": "passport 或 taiwan_permit 或 other",
  "name_zh": "中文姓名",
  "name_en": "英文姓名（LAST,FIRST）",
  "doc_no": "號碼",
  "birthday": "YYYY/MM/DD",
  "expiry": "YYYY/MM/DD",
  "id_no": "身分證號（如有）",
  "confidence": "high/medium/low"
}"""
    resp = model.generate_content([
        prompt,
        {"mime_type": "image/jpeg", "data": image_b64}
    ])
    raw = resp.text.strip()
    raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.MULTILINE)
    return json.loads(raw)

COL_MAP = {
    "team":1,"no":2,"name_zh":3,"name_en":4,"group":5,
    "passport_no":6,"expiry":7,"passport_status":8,
    "id_no":9,"birthday":10,"room_type":11,"bus":12,
    "dietary":13,"health":14,
    "deposit":15,"balance":16,"payment_method":17,"last5digits":18,
    "flight":19,"materials":20,"note":21
}

def find_or_create_row(ws, name, team):
    name_col = ws.col_values(3)
    for i, cell in enumerate(name_col[2:], start=3):
        if name and name in str(cell):
            return i
    for i, cell in enumerate(name_col[2:], start=3):
        if not cell:
            return i
    return len(name_col) + 1

def update_passenger(ws, name, team, updates):
    try:
        row = find_or_create_row(ws, name, team)
        if team: ws.update_cell(row, 1, team)
        if name: ws.update_cell(row, 3, name)
        for field, val in updates.items():
            if field in COL_MAP and val:
                ws.update_cell(row, COL_MAP[field], str(val))
        ws.update_cell(row, 21, datetime.now().strftime("%Y/%m/%d %H:%M"))
        return True
    except Exception as e:
        print(f"Sheet error: {e}")
        return False

def append_change(ws, team, person, before, after, impact):
    ws.append_row([
        datetime.now().strftime("%Y/%m/%d %H:%M"),
        team, "改動", person, before, after, impact, "⏳ 待確認"
    ])

def append_payment(ws, team, name, d):
    ws.append_row([
        team, name,
        d.get("deposit_amount",""), d.get("deposit_date",""), d.get("deposit_method",""),
        d.get("balance_amount",""), d.get("balance_date",""), d.get("balance_method",""),
        d.get("last5digits",""), "⏳ 確認中", ""
    ])

def process(result):
    try:
        book       = get_sheets()
        passengers = result.get("passengers", [])
        team       = result.get("team") or "（未指定）"
        rtype      = result.get("type", "other")
        summary    = result.get("summary", "已收到")

        if rtype == "passport":
            ws = book.worksheet("👥 旅客總表")
            for p in passengers:
                d = p.get("data", {})
                update_passenger(ws, p["name"], team, {
                    "passport_no": d.get("passport_no",""),
                    "expiry":      d.get("expiry",""),
                    "birthday":    d.get("birthday",""),
                    "id_no":       d.get("id_no",""),
                    "name_en":     d.get("name_en",""),
                    "passport_status": "✅ 已交",
                })
        elif rtype == "payment":
            ws = book.worksheet("💰 付款追蹤")
            for p in passengers:
                append_payment(ws, team, p["name"], p.get("data",{}))
        elif rtype == "dietary":
            ws = book.worksheet("🍽️ 特殊需求")
            for p in passengers:
                d = p.get("data", {})
                parts = []
                if d.get("no_beef"):    parts.append("不吃牛")
                if d.get("no_raw"):     parts.append("不吃生食")
                if d.get("vegetarian"): parts.append("素食")
                if d.get("no_seafood"): parts.append("不吃海鮮")
                if d.get("other_dietary"): parts.append(d["other_dietary"])
                name_col = ws.col_values(2)
                for i, cell in enumerate(name_col[2:], start=3):
                    if p["name"] in str(cell):
                        ws.update_cell(i, 10, "、".join(parts))
                        break
                else:
                    ws.append_row([team, p["name"],"","","","","","、".join(parts),"",""])
        elif rtype == "change":
            ws = book.worksheet("✏️ 改動記錄")
            for p in passengers:
                d = p.get("data", {})
                append_change(ws, team, p["name"],
                    d.get("before",""), d.get("after",""), d.get("impact",""))

        lines = [f"✅ {summary}", ""]
        for p in passengers:
            lines.append(f"• {p.get('name','未知')} 已更新")
        lines.append(f"\n團別：{team}")
        lines.append(f"時間：{datetime.now().strftime('%H:%M')}")
        return "\n".join(lines)

    except Exception as e:
        return f"⚠️ 寫入錯誤：{str(e)[:80]}\n請稍後再試。"

@app.route("/webhook", methods=["POST"])
def webhook():
    sig  = request.headers.get("X-Line-Signature","")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    text = event.message.text.strip()
    if text in ["狀態","status"]:
        reply = "📊 系統正常運作中\n請貼上旅客資料，我會自動整理到 Google Sheet。"
    elif len(text) < 5:
        reply = "請貼上旅客資料（護照、付款、餐食需求等），我會自動更新試算表。"
    else:
        try:
            result = classify_text(text)
            reply  = process(result)
        except Exception as e:
            reply = f"⚠️ 錯誤：{str(e)[:80]}"

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply)]
            )
        )

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    try:
        with ApiClient(configuration) as api_client:
            blob_api = MessagingApiBlob(api_client)
            img_bytes = blob_api.get_message_content(event.message.id)
            img_b64 = base64.b64encode(img_bytes).decode()

        ocr = classify_image(img_b64)

        if ocr.get("doc_type") in ["passport","taiwan_permit"]:
            label = "護照" if ocr["doc_type"] == "passport" else "台胞證"
            name  = ocr.get("name_zh") or ocr.get("name_en","未知")
            book  = get_sheets()
            ws    = book.worksheet("👥 旅客總表")
            update_passenger(ws, name, None, {
                "passport_no":     ocr.get("doc_no",""),
                "expiry":          ocr.get("expiry",""),
                "birthday":        ocr.get("birthday",""),
                "id_no":           ocr.get("id_no",""),
                "name_en":         ocr.get("name_en",""),
                "passport_status": "✅ 已交",
            })
            reply = (
                f"✅ {label}辨識完成\n\n"
                f"姓名：{ocr.get('name_zh','')} {ocr.get('name_en','')}\n"
                f"號碼：{ocr.get('doc_no','')}\n"
                f"效期：{ocr.get('expiry','')}\n"
                f"生日：{ocr.get('birthday','')}\n\n"
                f"已更新至旅客總表 ✅"
            )
            if ocr.get("expiry"):
                from datetime import date
                try:
                    exp = datetime.strptime(ocr["expiry"], "%Y/%m/%d").date()
                    if (exp - date.today()).days < 180:
                        reply += "\n\n⚠️ 護照效期不足6個月，請確認是否需要切結書！"
                except: pass
        else:
            reply = "📄 無法識別為護照或台胞證，請確認圖片清晰度後重新傳送。"

    except Exception as e:
        reply = f"⚠️ 圖片處理失敗：{str(e)[:80]}"

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

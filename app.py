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
from google import genai
from google.genai import types
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
gemini        = genai.Client(api_key=GEMINI_KEY)

def get_sheets():
    creds = Credentials.from_service_account_info(
        json.loads(SERVICE_ACCOUNT),
        scopes=["https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(creds).open_by_key(SHEET_ID)

def get_memory(book, user_id):
    try:
        ws = book.worksheet("💬 對話記憶")
        ids = ws.col_values(1)
        if user_id in ids:
            row = ids.index(user_id) + 1
            data = ws.row_values(row)
            return {"team": data[1] if len(data) > 1 else "", "context": data[2] if len(data) > 2 else ""}
    except: pass
    return {"team": "", "context": ""}

def save_memory(book, user_id, team, context):
    try:
        try: ws = book.worksheet("💬 對話記憶")
        except:
            ws = book.add_worksheet("💬 對話記憶", rows=200, cols=5)
            ws.append_row(["用戶ID","團別","最近上下文","最後更新"])
        ids = ws.col_values(1)
        now = datetime.now().strftime("%Y/%m/%d %H:%M")
        if user_id in ids:
            row = ids.index(user_id) + 1
            ws.update(f"A{row}:D{row}", [[user_id, team, context, now]])
        else:
            ws.append_row([user_id, team, context, now])
    except Exception as e: print(f"Memory error: {e}")

CLASSIFY_PROMPT = """你是山富旅遊的業務助理。
業務會把從LINE或Email收到的旅客資訊貼給你，請分析並回傳JSON。
分類：passport/payment/dietary/room/change/flight/missing/other
只回傳JSON：
{
  "type": "分類",
  "team": "團別（如沒提到但記憶有，用記憶的；都沒有填null）",
  "passengers": [{"name": "姓名", "data": {"欄位": "值"}}],
  "summary": "一行中文摘要",
  "action": "要更新哪個分頁"
}
護照欄位：passport_no, expiry, birthday, id_no, name_en
付款欄位：deposit_amount, deposit_date, deposit_method, balance_amount, last5digits
餐食欄位：no_beef, no_raw, vegetarian, no_seafood, other_dietary
改動欄位：before, after, impact"""

def keyword_classify(text, memory):
    """Gemini 超額時的備用關鍵字分類"""
    t = text
    rtype = "other"
    if any(k in t for k in ["護照","台胞","PASSPORT","passport","效期","號碼"]): rtype = "passport"
    elif any(k in t for k in ["匯款","訂金","尾款","刷卡","後五碼","付款"]): rtype = "payment"
    elif any(k in t for k in ["不吃","素食","忌口","過敏","餐食"]): rtype = "dietary"
    elif any(k in t for k in ["取消","退出","改","更換","變更"]): rtype = "change"
    team = memory.get("team","") or None
    import re as _re
    names = _re.findall(r'[一-鿿]{2,4}(?=s|護照|台胞|匯款|不吃|取消)', t)
    return {"type":rtype,"team":team,"passengers":[{"name":n,"data":{}} for n in names] if names else [],"summary":f"(備用分類) {rtype}","action":""}

def classify_text(text, memory):
    hint = ""
    if memory.get("team"): hint += f"\n【記憶-團別】{memory['team']}"
    if memory.get("context"): hint += f"\n【記憶-上文】{memory['context']}"
    try:
        resp = gemini.models.generate_content(
            model="gemini-2.0-flash",
            contents=f"{CLASSIFY_PROMPT}{hint}\n\n旅客資訊：\n{text}"
        )
        raw = resp.text.strip()
        raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.MULTILINE)
        return json.loads(raw)
    except Exception as e:
        if "429" in str(e) or "quota" in str(e).lower() or "RESOURCE_EXHAUSTED" in str(e):
            return keyword_classify(text, memory)
        raise

def classify_image(image_b64):
    prompt = """這是旅客傳來的文件圖片。只回傳JSON：
{"doc_type":"passport或taiwan_permit或other","name_zh":"中文姓名","name_en":"英文姓名LAST,FIRST","doc_no":"號碼","birthday":"YYYY/MM/DD","expiry":"YYYY/MM/DD","id_no":"身分證號","confidence":"high/medium/low"}"""
    resp = gemini.models.generate_content(
        model="gemini-2.0-flash",
        contents=[prompt, types.Part.from_bytes(data=base64.b64decode(image_b64), mime_type="image/jpeg")]
    )
    raw = resp.text.strip()
    raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.MULTILINE)
    return json.loads(raw)

COL_MAP = {"team":1,"no":2,"name_zh":3,"name_en":4,"group":5,"passport_no":6,"expiry":7,"passport_status":8,"id_no":9,"birthday":10,"room_type":11,"bus":12,"dietary":13,"health":14,"deposit":15,"balance":16,"payment_method":17,"last5digits":18,"flight":19,"materials":20,"note":21}
COL_LABELS = {"passport_no":"護照號碼","expiry":"護照效期","passport_status":"護照狀態","id_no":"身分證號","birthday":"生日","room_type":"房型","dietary":"餐食需求","deposit":"訂金","balance":"尾款","payment_method":"付款方式","last5digits":"後五碼","flight":"班機","name_en":"英文姓名"}

def find_or_create_row(ws, name, team):
    name_col = ws.col_values(3)
    for i, cell in enumerate(name_col[2:], start=3):
        if name and name in str(cell): return i
    for i, cell in enumerate(name_col[2:], start=3):
        if not cell: return i
    return len(name_col) + 1

def update_passenger(ws, ws_change, name, team, updates):
    try:
        row = find_or_create_row(ws, name, team)
        current = ws.row_values(row)
        def cur(col): return current[col-1] if len(current) >= col else ""
        if team: ws.update_cell(row, 1, team)
        if name: ws.update_cell(row, 3, name)
        changes = []
        for field, new_val in updates.items():
            if field not in COL_MAP or not new_val: continue
            col = COL_MAP[field]
            old_val = cur(col)
            ws.update_cell(row, col, str(new_val))
            if old_val and str(old_val) != str(new_val):
                label = COL_LABELS.get(field, field)
                changes.append(f"{label}：{old_val} → {new_val}")
        ws.update_cell(row, 21, datetime.now().strftime("%Y/%m/%d %H:%M"))
        if changes and ws_change:
            for ch in changes:
                ws_change.append_row([datetime.now().strftime("%Y/%m/%d %H:%M"), team or "（未指定）", "自動偵測", name, ch, "", "系統自動偵測", "✅ 已記錄"])
        return True, changes
    except Exception as e:
        print(f"Sheet error: {e}")
        return False, []

def append_change(ws, team, person, before, after, impact):
    ws.append_row([datetime.now().strftime("%Y/%m/%d %H:%M"), team, "改動", person, before, after, impact, "⏳ 待確認"])

def append_payment(ws, team, name, d):
    ws.append_row([team, name, d.get("deposit_amount",""), d.get("deposit_date",""), d.get("deposit_method",""), d.get("balance_amount",""), d.get("balance_date",""), d.get("balance_method",""), d.get("last5digits",""), "⏳ 確認中", ""])

def process(result, user_id, book):
    try:
        passengers = result.get("passengers", [])
        team = result.get("team") or "（未指定）"
        rtype = result.get("type", "other")
        summary = result.get("summary", "已收到")
        try: ws_change = book.worksheet("✏️ 改動記錄")
        except: ws_change = None
        all_changes = []

        if rtype == "passport":
            ws = book.worksheet("👥 旅客總表")
            for p in passengers:
                d = p.get("data", {})
                _, ch = update_passenger(ws, ws_change, p["name"], team, {"passport_no":d.get("passport_no",""),"expiry":d.get("expiry",""),"birthday":d.get("birthday",""),"id_no":d.get("id_no",""),"name_en":d.get("name_en",""),"passport_status":"✅ 已交"})
                all_changes.extend(ch)
        elif rtype == "payment":
            ws = book.worksheet("💰 付款追蹤")
            for p in passengers: append_payment(ws, team, p["name"], p.get("data",{}))
        elif rtype == "dietary":
            ws_p = book.worksheet("👥 旅客總表")
            for p in passengers:
                d = p.get("data", {})
                parts = []
                if d.get("no_beef"): parts.append("不吃牛")
                if d.get("no_raw"): parts.append("不吃生食")
                if d.get("vegetarian"): parts.append("素食")
                if d.get("no_seafood"): parts.append("不吃海鮮")
                if d.get("other_dietary"): parts.append(d["other_dietary"])
                _, ch = update_passenger(ws_p, ws_change, p["name"], team, {"dietary":"、".join(parts)})
                all_changes.extend(ch)
        elif rtype == "change":
            if ws_change:
                for p in passengers:
                    d = p.get("data", {})
                    append_change(ws_change, team, p["name"], d.get("before",""), d.get("after",""), d.get("impact",""))
            ws_p = book.worksheet("👥 旅客總表")
            for p in passengers:
                d = p.get("data", {})
                upd = {}
                if d.get("room_type"): upd["room_type"] = d["room_type"]
                if d.get("dietary"): upd["dietary"] = d["dietary"]
                if upd:
                    _, ch = update_passenger(ws_p, ws_change, p["name"], team, upd)
                    all_changes.extend(ch)

        save_memory(book, user_id, team, f"{rtype}: {summary}")
        lines = [f"✅ {summary}", ""]
        for p in passengers: lines.append(f"• {p.get('name','未知')} 已更新")
        if all_changes:
            lines.append("\n🔄 自動偵測到改動：")
            for ch in all_changes: lines.append(f"  {ch}")
        lines.append(f"\n團別：{team}")
        lines.append(f"時間：{datetime.now().strftime('%H:%M')}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ 寫入錯誤：{str(e)[:100]}"

@app.route("/webhook", methods=["POST"])
def webhook():
    sig = request.headers.get("X-Line-Signature","")
    body = request.get_data(as_text=True)
    try: handler.handle(body, sig)
    except InvalidSignatureError: abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    if text in ["狀態","status"]:
        reply = "📊 系統正常運作中\n請貼上旅客資料，Bot 會自動判斷類型和團別。\n\n指令：\n• 記憶 — 查看目前記憶\n• 清除記憶 — 重置上下文\n• 測試Sheet — 測試Google Sheet連線"
    elif text == "測試Sheet":
        try:
            book = get_sheets()
            ws = book.worksheet("👥 旅客總表")
            test_row = find_or_create_row(ws, "測試用戶_請刪除", "測試")
            ws.update_cell(test_row, 1, "測試團")
            ws.update_cell(test_row, 3, "測試用戶_請刪除")
            ws.update_cell(test_row, 21, datetime.now().strftime("%Y/%m/%d %H:%M"))
            reply = f"✅ Google Sheet 連線正常！\n已在旅客總表第{test_row}行寫入測試資料\n（請手動刪除那行）\n\n整套系統正常運作 🎉"
        except Exception as e:
            reply = f"❌ Google Sheet 連線失敗：{str(e)[:100]}"
    elif text == "記憶":
        try:
            book = get_sheets()
            m = get_memory(book, user_id)
            reply = f"📝 目前記憶：\n團別：{m.get('team','無')}\n上文：{m.get('context','無')}"
        except: reply = "📝 尚無記憶"
    elif text in ["清除記憶","忘記"]:
        try:
            save_memory(get_sheets(), user_id, "", "")
            reply = "🗑️ 記憶已清除"
        except: reply = "⚠️ 清除失敗"
    elif len(text) < 5:
        reply = "請貼上旅客資料（護照、付款、餐食需求等），我會自動更新試算表。"
    else:
        try:
            book = get_sheets()
            memory = get_memory(book, user_id)
            result = classify_text(text, memory)
            reply = process(result, user_id, book)
        except Exception as e:
            reply = f"⚠️ 錯誤：{str(e)[:100]}"
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply)]))

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    user_id = event.source.user_id
    try:
        with ApiClient(configuration) as api_client:
            img_bytes = MessagingApiBlob(api_client).get_message_content(event.message.id)
        img_b64 = base64.b64encode(img_bytes).decode()
        ocr = classify_image(img_b64)
        book = get_sheets()
        if ocr.get("doc_type") in ["passport","taiwan_permit"]:
            label = "護照" if ocr["doc_type"] == "passport" else "台胞證"
            name = ocr.get("name_zh") or ocr.get("name_en","未知")
            memory = get_memory(book, user_id)
            team = memory.get("team","")
            ws = book.worksheet("👥 旅客總表")
            try: ws_change = book.worksheet("✏️ 改動記錄")
            except: ws_change = None
            _, changes = update_passenger(ws, ws_change, name, team, {"passport_no":ocr.get("doc_no",""),"expiry":ocr.get("expiry",""),"birthday":ocr.get("birthday",""),"id_no":ocr.get("id_no",""),"name_en":ocr.get("name_en",""),"passport_status":"✅ 已交"})
            save_memory(book, user_id, team, f"剛收了{name}的{label}")
            reply = f"✅ {label}辨識完成\n\n姓名：{ocr.get('name_zh','')} {ocr.get('name_en','')}\n號碼：{ocr.get('doc_no','')}\n效期：{ocr.get('expiry','')}\n生日：{ocr.get('birthday','')}\n\n已更新至旅客總表 ✅"
            if changes: reply += "\n\n🔄 偵測到改動：\n" + "\n".join(changes)
            if ocr.get("expiry"):
                from datetime import date
                try:
                    if (datetime.strptime(ocr["expiry"], "%Y/%m/%d").date() - date.today()).days < 180:
                        reply += "\n\n⚠️ 效期不足6個月，請確認切結書！"
                except: pass
        else:
            reply = "📄 無法識別為護照或台胞證，請確認圖片清晰度後重新傳送。"
    except Exception as e:
        reply = f"⚠️ 圖片處理失敗：{str(e)[:100]}"
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply)]))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

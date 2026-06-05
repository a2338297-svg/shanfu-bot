import os, json, base64, re
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, ImageMessageContent
)
from google import genai
from google.genai import types
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from collections import deque

app = Flask(__name__)

# ── 去重：記住最近處理過的 LINE message id，擋 webhook 重送 ──
_seen_ids = deque(maxlen=500)
def already_processed(msg_id):
    """若這則 message id 近期處理過就回 True（避免重複寫入）。"""
    if not msg_id:
        return False
    if msg_id in _seen_ids:
        return True
    _seen_ids.append(msg_id)
    return False

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


# ── 查詢功能（不需要 Gemini）────────────────────────
def query_person(book, name):
    try:
        ws = book.worksheet("👥 旅客總表")
        rows = ws.get_all_values()
        for row in rows[2:]:
            if name in (row[2] if len(row) > 2 else ""):
                r = [f"👤 {row[2]} {row[3] if len(row)>3 else ''}", f"團別：{row[0] or '未指定'}", f"護照：{row[7] if len(row)>7 else '未填'} {row[5] if len(row)>5 else ''}", f"效期：{row[6] if len(row)>6 else '未填'}", f"房型：{row[10] if len(row)>10 else '未填'}", f"餐食：{row[12] if len(row)>12 else '未填'}", f"訂金：{row[14] if len(row)>14 else '未填'}", f"尾款：{row[15] if len(row)>15 else '未填'}"]
                if len(row)>20 and row[20]: r.append(f"更新：{row[20]}")
                return "\n".join(r)
        return f"❌ 找不到「{name}」，請確認姓名"
    except Exception as e: return f"⚠️ 查詢失敗：{str(e)[:80]}"

def query_missing(book, team=None):
    try:
        ws = book.worksheet("👥 旅客總表")
        rows = ws.get_all_values()
        missing = []
        for row in rows[2:]:
            if len(row) < 8: continue
            if team and team not in str(row[0]): continue
            name = row[2] if len(row)>2 else ""
            if not name or "測試" in name: continue
            issues = []
            if not row[7] or "✅" not in row[7]: issues.append("護照未交")
            if len(row)>14 and (not row[14] or "✅" not in row[14]): issues.append("訂金未付")
            if len(row)>15 and (not row[15] or "✅" not in row[15]): issues.append("尾款未付")
            if issues: missing.append(f"• {name}（{row[0] or '未指定'}）：{'、'.join(issues)}")
        if not missing: return "✅ 所有旅客資料完整！"
        hdr = f"⚠️ 缺件名單（{len(missing)}人）" + (f" — {team}" if team else "")
        return hdr + "\n" + "\n".join(missing[:20])
    except Exception as e: return f"⚠️ 查詢失敗：{str(e)[:80]}"

def query_team(book, team):
    try:
        ws = book.worksheet("👥 旅客總表")
        rows = ws.get_all_values()
        total = passport_ok = deposit_ok = balance_ok = 0
        for row in rows[2:]:
            if len(row)<3 or team not in str(row[0]) or not row[2] or "測試" in row[2]: continue
            total += 1
            if len(row)>7 and "✅" in str(row[7]): passport_ok += 1
            if len(row)>14 and "✅" in str(row[14]): deposit_ok += 1
            if len(row)>15 and "✅" in str(row[15]): balance_ok += 1
        if not total: return f"❌ 找不到「{team}」的資料"
        return "\n".join([f"📋 {team} 狀況", f"總人數：{total}人", f"護照：{passport_ok}/{total}（{total-passport_ok}人未交）", f"訂金：{deposit_ok}/{total}（{total-deposit_ok}人未付）", f"尾款：{balance_ok}/{total}（{total-balance_ok}人未付）"])
    except Exception as e: return f"⚠️ 查詢失敗：{str(e)[:80]}"

def is_query(text):
    return any(k in text for k in ["查","缺件","誰沒","未付款","誰缺","狀況","清單","名單"])

def looks_like_data(text):
    if "吃牛" in text and "不吃牛" not in text and not any(k in text for k in ["護照","匯款","訂金","尾款","房"]):
        return False
    return any(k in text for k in [
        "護照","台胞","效期","匯款","訂金","尾款","刷卡","付款","後五碼",
        "不吃","素食","忌口","過敏","餐食","單人房","雙人房","三人房",
        "取消","退出","不去了","不去","JOIN","join","加入","新增","改",
    ])

def has_explicit_team(text):
    text = text.strip()
    return bool(re.match(r"^(這是)?[一-鿿A-Za-z0-9月年月\s_-]{2,20}\s+[一-鿿]{2,4}", text))

def is_team_setup(text):
    return bool(re.match(r"^(這是|新增一團|新增團別|新案件|開一團)\s*[一-鿿A-Za-z0-9月年月\s_-]{2,20}$", text.strip()))

def extract_team_setup(text):
    return re.sub(r"^(這是|新增一團|新增團別|新案件|開一團)\s*", "", text).strip(" ：:-—－")

def chat_answer(text, memory):
    team_hint = f"目前團別：{memory.get('team')}" if memory.get("team") else "目前沒有指定團別"
    prompt = f"""你是 Darren 的山富旅遊團務 AI 助理。
你的工作：
- 可以正常聊天、回答問題、解釋你能做什麼。
- 如果使用者是在問團務資料，提醒他可用：查姓名、缺件、團名狀況。
- 如果使用者是在貼旅客資料，提醒他用「團別 姓名 資料」格式會更準。
- 不要假裝已經寫入資料，除非系統分類流程有處理。

{team_hint}
使用者訊息：{text}
"""
    try:
        resp = gemini.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return resp.text.strip()[:1800]
    except Exception as e:
        return (
            "我現在不是沒智慧，是 AI 模型呼叫失敗，所以只能跑備用規則。\n"
            f"錯誤：{str(e)[:120]}\n\n"
            "可以先用這些不靠 AI 的指令：\n"
            "• 查陳文賓\n"
            "• 缺件\n"
            "• 九月正風狀況\n"
            "• 這是九月正風\n"
            "• 九月正風 陳文賓不吃牛"
        )

def handle_query(text, book, memory):
    import re
    if text.startswith("查") and len(text) < 12:
        name = re.sub(r"查|狀態|的", "", text).strip()
        if name: return query_person(book, name)
    if any(k in text for k in ["缺件","誰缺","誰沒交","護照缺","未付款","誰沒付"]):
        return query_missing(book, memory.get("team") or None)
    if "狀況" in text or "狀態" in text:
        team = memory.get("team","")
        cleaned = re.sub(r"狀況|狀態|整體|查詢|告訴我", "", text).strip()
        if cleaned and len(cleaned)>1: team = cleaned
        if team: return query_team(book, team)
    return None

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

def now_text():
    return datetime.now().strftime("%Y/%m/%d %H:%M")

def get_or_create_ws(book, title, headers, rows=1000, cols=12):
    try:
        return book.worksheet(title)
    except Exception:
        ws = book.add_worksheet(title, rows=rows, cols=cols)
        ws.append_row(headers)
        return ws

def log_raw_inbox(book, user_id, text, note=""):
    """訊息一進來先把原文存進「📥 原始收件」。
    這是最後防線：不管後面 Gemini 超額、解析失敗、Sheet 寫入出錯，
    原始資料都不會消失，最多事後人工補。"""
    try:
        ws = get_or_create_ws(
            book,
            "📥 原始收件",
            ["時間","使用者ID","原始訊息","後續狀態","備註"],
            cols=5,
        )
        ws.append_row([now_text(), user_id, text, "已收原文", note])
    except Exception as e:
        print(f"raw inbox log error: {e}")

def names_text(result):
    names = [p.get("name", "") for p in result.get("passengers", []) if p.get("name")]
    return "、".join(names)

def result_json(result):
    return json.dumps(result, ensure_ascii=False)

def missing_fields_for_result(result):
    rtype = result.get("type", "other")
    team = result.get("team")
    passengers = result.get("passengers", []) or []
    missing = []
    if rtype != "other" and not team:
        missing.append("團別")
    if rtype != "other" and not passengers:
        missing.append("姓名/對象")
    required = {
        "passport": [("passport_no", "護照號碼"), ("expiry", "護照效期")],
        "payment": [("deposit_amount|balance_amount", "付款金額"), ("deposit_method|last5digits", "付款方式/後五碼")],
        "dietary": [("no_beef|no_raw|vegetarian|no_seafood|other_dietary|dietary", "餐食內容")],
        "room": [("room_type", "房型")],
        "change": [("after|impact|dietary|room_type", "變動內容")],
    }
    for p in passengers:
        name = p.get("name") or "未指定"
        if name.startswith("待補") or name in ["未知", "未指定"]:
            missing.append(f"{name}:正確姓名")
        data = p.get("data", {}) or {}
        for keys, label in required.get(rtype, []):
            if not any(data.get(k) for k in keys.split("|")):
                missing.append(f"{name}:{label}")
    return missing

def append_ai_inbox(book, user_id, source_text, result, is_data, write_status, write_tabs="", error="", needs_confirm=False):
    ws = get_or_create_ws(
        book,
        "🤖 AI收件紀錄",
        ["時間","使用者ID","原始訊息","AI判斷類型","團別","涉及人員","解析摘要","AI信心","是否資料","寫入狀態","寫入分頁","錯誤原因","是否需人工確認","AI解析JSON"],
        cols=14,
    )
    ws.append_row([
        now_text(),
        user_id,
        source_text,
        result.get("type", "other"),
        result.get("team", ""),
        names_text(result),
        result.get("summary", ""),
        result.get("confidence", "medium"),
        "是" if is_data else "否",
        write_status,
        write_tabs,
        error,
        "是" if needs_confirm else "否",
        result_json(result),
    ])

def append_pending_confirmation(book, source_text, result, reason):
    ws = get_or_create_ws(
        book,
        "✅ 待確認",
        ["確認碼","時間","團別","姓名/對象","類型","原始訊息","AI解析結果","建議動作","狀態","確認人","確認時間","備註"],
        cols=12,
    )
    confirm_id = "C" + datetime.now().strftime("%m%d%H%M%S")
    ws.append_row([
        confirm_id,
        now_text(),
        result.get("team", ""),
        names_text(result),
        result.get("type", "other"),
        source_text,
        result_json(result),
        "補齊：" + "、".join(reason),
        "待確認",
        "",
        "",
        "",
    ])
    return confirm_id

def append_sync_task(book, team, person, rtype, tabs, source_summary):
    ws = get_or_create_ws(
        book,
        "🔁 同步任務",
        ["時間","團別","姓名/對象","資料類型","Google Sheet狀態","本地紀錄狀態","科威狀態","待處理事項","負責人","截止時間","狀態","來源摘要"],
        cols=12,
    )
    action = {
        "passport": "確認科威證件資料同步",
        "payment": "確認科威收款狀態同步",
        "dietary": "通知供應商並確認特殊餐食",
        "room": "確認房型/分房資料同步",
        "change": "確認人員變動影響",
    }.get(rtype, "人工確認是否需同步")
    ws.append_row([now_text(), team, person, rtype, "已寫入：" + tabs, "未啟用", "待處理", action, "Darren", "今日", "待處理", source_summary])

def append_reminder(book, team, person, task, source_text, priority="中"):
    ws = get_or_create_ws(
        book,
        "🔔 提醒事項",
        ["建立時間","團別","姓名/對象","提醒事項","來源訊息","截止時間","優先級","狀態","完成時間","備註"],
        cols=10,
    )
    ws.append_row([now_text(), team, person, task, source_text, "今日", priority, "待處理", "", ""])

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
    """Gemini 超額時的備用解析。盡量抓團別、姓名與常見欄位，讓系統不中斷。"""
    t = text.strip()
    rtype = "other"
    import re as _re

    if any(k in t for k in ["護照","台胞","PASSPORT","passport","效期","號碼"]):
        rtype = "passport"
    elif any(k in t for k in ["匯款","訂金","尾款","刷卡","後五碼","付款"]):
        rtype = "payment"
    elif any(k in t for k in ["不吃","素食","忌口","過敏","餐食"]):
        rtype = "dietary"
    elif any(k in t for k in ["單人房","雙人房","三人房","兩張床","一大床","房型","膠囊"]):
        rtype = "room"
    elif any(k in t for k in ["取消","退出","不去了","不去","JOIN","join","加入","新增","改","更換","變更"]):
        rtype = "change"

    name_patterns = [
        r'([一-鿿]{2,4})\s*(?=不吃|素食|忌口|過敏)',
        r'([一-鿿]{2,4}).{0,8}(?=護照|台胞|效期|PASSPORT|passport)',
        r'([一-鿿]{2,4}).{0,8}(?=匯款|訂金|尾款|刷卡|付款|後五碼)',
        r'([一-鿿]{2,4}).{0,8}(?=單人房|雙人房|三人房|兩張床|一大床|房型|膠囊)',
        r'([一-鿿]{2,4})\s*(?=取消|退出|不去了|不去)',
        r'(?:新增|加入|JOIN|join|一位JOIN|一位join)\s*([一-鿿]{2,4})'
    ]
    names = []
    for pat in name_patterns:
        for name in _re.findall(pat, t):
            if name and name not in names and name not in ["護照", "台胞", "訂金", "尾款"]:
                names.append(name)

    action_words = ["護照","台胞","效期","匯款","訂金","尾款","刷卡","付款","後五碼","不吃","素食","忌口","過敏","單人房","雙人房","三人房","兩張床","一大床","房型","膠囊","取消","退出","不去了","不去","JOIN","join","加入","新增","改","更換","變更"]
    split_at = len(t)
    for name in names:
        idx = t.find(name)
        if idx >= 0:
            split_at = min(split_at, idx)
    for word in action_words:
        idx = t.find(word)
        if idx >= 0:
            split_at = min(split_at, idx)
    raw_team = t[:split_at].strip(" -—－，,：:")
    raw_team = _re.sub(r'^(這是|團別[:：]?|這團是)', '', raw_team).strip()
    raw_team = _re.sub(r'(的|有|一位|一個)$', '', raw_team).strip()
    team = raw_team or memory.get("team","") or None

    data = {}
    if rtype == "dietary":
        data = {
            "no_beef": "不吃牛" in t,
            "no_raw": any(k in t for k in ["不吃生", "不吃生食", "不吃生魚片"]),
            "vegetarian": "素食" in t,
            "no_seafood": "不吃海鮮" in t,
            "other_dietary": ""
        }
    elif rtype == "payment":
        amount = _re.search(r'(訂金|尾款)?\s*([0-9,]{3,})', t)
        last5 = _re.search(r'後五碼\s*([0-9]{3,5})', t)
        data = {
            "deposit_amount": amount.group(2).replace(",", "") if amount and "尾款" not in t else "",
            "balance_amount": amount.group(2).replace(",", "") if amount and "尾款" in t else "",
            "deposit_method": "匯款" if "匯款" in t else ("刷卡" if "刷卡" in t else ""),
            "last5digits": last5.group(1) if last5 else ""
        }
    elif rtype == "passport":
        passport_no = _re.search(r'([A-Z][0-9]{8}|[A-Z]{1,2}[0-9]{6,9})', t, _re.I)
        expiry = _re.search(r'(20[0-9]{2})[/-年 ]?([0-9]{1,2})?[/-月 ]?([0-9]{1,2})?', t)
        data = {
            "passport_no": passport_no.group(1).upper() if passport_no else "",
            "expiry": "/".join([x for x in (expiry.groups() if expiry else []) if x]) if expiry else ""
        }
    elif rtype == "room":
        room = ""
        for word in ["單人房", "雙人房", "三人房", "兩張床", "一大床", "膠囊"]:
            if word in t:
                room = word
                break
        data = {"room_type": room or "房型待確認"}
    elif rtype == "change":
        if any(k in t for k in ["JOIN", "join", "加入", "新增"]):
            data = {"before": "", "after": "新增/JOIN", "impact": "新增旅客，姓名待補" if not names else "新增旅客"}
            if not names:
                names = ["待補姓名_JOIN"]
        elif any(k in t for k in ["取消", "退出", "不去了", "不去"]):
            data = {"before": "參加", "after": "取消", "impact": "需確認取消費、房型與名單"}

    passengers = [{"name": n, "data": data.copy()} for n in names]
    summary = f"(備用解析) {rtype}"
    if team:
        summary += f"｜團別：{team}"
    if not passengers and rtype != "other":
        summary += "｜未抓到姓名"
    return {"type": rtype, "team": team, "passengers": passengers, "summary": summary, "action": ""}

def normalize_ai_result(text, memory, result):
    """用明確文字線索校正 AI 結果，避免舊記憶或模型誤判覆蓋當下輸入。"""
    guard = keyword_classify(text, memory)

    if guard.get("type") != "other" and result.get("type") == "other":
        return guard

    simple_keywords = ["不吃", "素食", "匯款", "訂金", "尾款", "後五碼", "單人房", "雙人房", "三人房"]
    if guard.get("type") != "other" and any(k in text for k in simple_keywords):
        return guard

    if guard.get("team") and has_explicit_team(text):
        result["team"] = guard["team"]

    guard_names = [p.get("name") for p in guard.get("passengers", []) if p.get("name")]
    result_names = [p.get("name") for p in result.get("passengers", []) if p.get("name")]
    team = str(result.get("team") or guard.get("team") or "")
    bad_result_names = any(name and name in team for name in result_names)
    if guard_names and (not result_names or bad_result_names):
        result["passengers"] = guard["passengers"]
        if guard.get("type") != "other":
            result["type"] = guard["type"]
            result["summary"] = guard.get("summary", result.get("summary", "已收到"))
    return result

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
        return normalize_ai_result(text, memory, json.loads(raw))
    except Exception as e:
        # 任何 Gemini 失敗（超額、JSON 解析錯、網路斷）都改走關鍵字備援，
        # 不再 re-raise 讓資料石沉大海。最差就是進「待確認」由人工補。
        print(f"classify_text fallback ({str(e)[:120]}) -> keyword_classify")
        return keyword_classify(text, memory)

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
    team_col = ws.col_values(1)
    if team:
        for i, cell in enumerate(name_col[2:], start=3):
            row_team = team_col[i - 1] if len(team_col) >= i else ""
            if name and name in str(cell) and team in str(row_team):
                return i
    for i, cell in enumerate(name_col[2:], start=3):
        if name and name in str(cell) and not team:
            return i
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

def process(result, user_id, book, source_text=""):
    try:
        passengers = result.get("passengers", [])
        team = result.get("team") or "（未指定）"
        rtype = result.get("type", "other")
        summary = result.get("summary", "已收到")
        touched_tabs = []
        try: ws_change = book.worksheet("✏️ 改動記錄")
        except: ws_change = None
        all_changes = []

        missing_fields = missing_fields_for_result(result)
        if rtype == "other" or missing_fields:
            confirm_id = append_pending_confirmation(book, source_text, result, missing_fields or ["AI無法判斷是否為資料"])
            append_ai_inbox(book, user_id, source_text, result, rtype != "other", "待確認", "", "缺資料/不明確", True)
            save_memory(book, user_id, team, f"{rtype}: {summary}")
            lines = [
                f"⚠️ 這筆我先放到待確認，沒有寫入主表。",
                f"確認碼：{confirm_id}",
                f"判斷：{rtype}",
                f"團別：{team}",
            ]
            if names_text(result):
                lines.append(f"人員：{names_text(result)}")
            lines.append("原因：" + "、".join(missing_fields or ["AI無法判斷是否為資料"]))
            lines.append("存到：🤖 AI收件紀錄、✅ 待確認")
            lines.append(f"Sheet：https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")
            return "\n".join(lines)

        if rtype == "passport":
            ws = book.worksheet("👥 旅客總表")
            touched_tabs.append("👥 旅客總表")
            for p in passengers:
                d = p.get("data", {})
                _, ch = update_passenger(ws, ws_change, p["name"], team, {"passport_no":d.get("passport_no",""),"expiry":d.get("expiry",""),"birthday":d.get("birthday",""),"id_no":d.get("id_no",""),"name_en":d.get("name_en",""),"passport_status":"✅ 已交"})
                all_changes.extend(ch)
        elif rtype == "payment":
            ws = book.worksheet("💰 付款追蹤")
            touched_tabs.append("💰 付款追蹤")
            for p in passengers: append_payment(ws, team, p["name"], p.get("data",{}))
        elif rtype == "dietary":
            ws_p = book.worksheet("👥 旅客總表")
            touched_tabs.append("👥 旅客總表")
            try:
                ws_special = book.worksheet("🍽️ 特殊需求")
                touched_tabs.append("🍽️ 特殊需求")
            except:
                ws_special = None
            for p in passengers:
                d = p.get("data", {})
                parts = []
                if d.get("no_beef"): parts.append("不吃牛")
                if d.get("no_raw"): parts.append("不吃生食")
                if d.get("vegetarian"): parts.append("素食")
                if d.get("no_seafood"): parts.append("不吃海鮮")
                if d.get("other_dietary"): parts.append(d["other_dietary"])
                dietary_text = "、".join(parts) or d.get("dietary", "") or "餐食需求待確認"
                _, ch = update_passenger(ws_p, ws_change, p["name"], team, {"dietary":dietary_text})
                all_changes.extend(ch)
                if ws_special:
                    ws_special.append_row([team, p["name"], "", "", "", "", "", dietary_text, "", datetime.now().strftime("%Y/%m/%d %H:%M")])
        elif rtype == "room":
            ws_p = book.worksheet("👥 旅客總表")
            touched_tabs.append("👥 旅客總表")
            for p in passengers:
                d = p.get("data", {})
                _, ch = update_passenger(ws_p, ws_change, p["name"], team, {"room_type":d.get("room_type", "房型待確認")})
                all_changes.extend(ch)
        elif rtype == "change":
            if ws_change:
                touched_tabs.append("✏️ 改動記錄")
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
                    if "👥 旅客總表" not in touched_tabs:
                        touched_tabs.append("👥 旅客總表")
                    _, ch = update_passenger(ws_p, ws_change, p["name"], team, upd)
                    all_changes.extend(ch)

        write_tabs = "、".join(dict.fromkeys(touched_tabs))
        append_ai_inbox(book, user_id, source_text, result, True, "已寫入" if touched_tabs else "未寫入", write_tabs, "", False)
        for p in passengers or [{"name": "未指定"}]:
            person = p.get("name", "未指定")
            if touched_tabs:
                append_sync_task(book, team, person, rtype, write_tabs, summary)
            if rtype == "dietary":
                append_reminder(book, team, person, "通知供應商特殊餐食", source_text)
            elif rtype == "payment":
                append_reminder(book, team, person, "確認款項是否入帳", source_text)
            elif rtype == "passport":
                append_reminder(book, team, person, "確認護照效期與缺件狀態", source_text)
            elif rtype == "change":
                append_reminder(book, team, person, "確認變動是否影響房型/機票/報價", source_text, "高")

        save_memory(book, user_id, team, f"{rtype}: {summary}")
        lines = [f"✅ {summary}", ""]
        for p in passengers: lines.append(f"• {p.get('name','未知')} 已更新")
        if not passengers and rtype != "other":
            lines.append("⚠️ 有判斷出資料類型，但沒有抓到姓名；請補姓名再貼一次。")
        if all_changes:
            lines.append("\n🔄 自動偵測到改動：")
            for ch in all_changes: lines.append(f"  {ch}")
        lines.append(f"\n團別：{team}")
        if touched_tabs:
            lines.append(f"存到：{'、'.join(dict.fromkeys(touched_tabs))}")
            lines.append("已記錄：🤖 AI收件紀錄、🔁 同步任務、🔔 提醒事項")
            lines.append(f"Sheet：https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")
        else:
            lines.append("存到：未寫入資料列（只更新記憶/分類）")
        lines.append(f"時間：{datetime.now().strftime('%H:%M')}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ 寫入錯誤：{str(e)[:100]}"

# ════════════════════════════════════════════════════════════
#  主動巡守（agent 的靈魂）：每天掃一次，自己預警，不等人問
# ════════════════════════════════════════════════════════════
import calendar as _calendar

def parse_date_any(s):
    """盡量把各種日期字串轉成 date；失敗回 None。"""
    s = str(s or "").strip()
    if not s:
        return None
    s = s.replace(".", "/").replace("-", "/").replace("年", "/").replace("月", "/").replace("日", "")
    s = re.sub(r"/+", "/", s).strip("/")
    parts = s.split("/")
    try:
        if len(parts) >= 3:
            return datetime(int(parts[0]), int(parts[1]), int(parts[2])).date()
        if len(parts) == 2:
            return datetime(int(parts[0]), int(parts[1]), 1).date()
    except Exception:
        return None
    return None

def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, _calendar.monthrange(y, m)[1])
    return d.replace(year=y, month=m, day=day)

def find_ws(book, keyword):
    """用關鍵字模糊找分頁，避免 emoji/空格差異對不上。"""
    for ws in book.worksheets():
        if keyword in ws.title:
            return ws
    return None

def build_daily_digest(book):
    """掃旅客總表，產出今日預警摘要文字。"""
    from datetime import date
    today = date.today()
    ov = find_ws(book, "案件總覽")
    team_dates = {}
    if ov:
        for row in ov.get_all_values()[2:]:
            t = (row[0] if len(row) > 0 else "").strip()
            if not t:
                continue
            team_dates[t] = {
                "depart": parse_date_any(row[2] if len(row) > 2 else ""),
                "return": parse_date_any(row[3] if len(row) > 3 else ""),
            }
    pax = find_ws(book, "旅客總表")
    urgent, expiry_alerts = [], []
    if pax:
        for row in pax.get_all_values()[2:]:
            name = (row[2] if len(row) > 2 else "").strip()
            if not name or "測試" in name:
                continue
            team = (row[0] if len(row) > 0 else "").strip()
            dates = team_dates.get(team, {})
            depart, ret = dates.get("depart"), dates.get("return")
            exp = parse_date_any(row[6] if len(row) > 6 else "")
            pstat = str(row[7] if len(row) > 7 else "")
            bal = str(row[15] if len(row) > 15 else "")
            # 1) 護照效期不足回程+6個月（只報還沒出發的團；已出發就來不及補救了）
            departed = bool(depart and depart < today)
            if exp and ret and not departed and exp < add_months(ret, 6):
                expiry_alerts.append(f"• {name}（{team}）效期 {row[6]}")
            # 2) 出發前14天仍缺件
            if depart:
                days = (depart - today).days
                if 0 <= days <= 14:
                    issues = []
                    if "✅" not in pstat:
                        issues.append("護照未齊")
                    if "✅" not in bal:
                        issues.append("尾款未結")
                    if issues:
                        urgent.append(f"• {name}（{team}，剩{days}天）：{'、'.join(issues)}")
    lines = [f"🗓️ 山富團務每日巡守 {today.strftime('%Y/%m/%d')}", ""]
    if urgent:
        lines.append(f"🔴 出發前14天缺件（{len(urgent)}人）")
        lines.extend(urgent[:20])
        lines.append("")
    if expiry_alerts:
        lines.append(f"⚠️ 護照效期不足回程+6個月（{len(expiry_alerts)}人）")
        lines.extend(expiry_alerts[:20])
        lines.append("")
    if not urgent and not expiry_alerts:
        lines.append("✅ 今日無緊急預警，將出團資料皆齊全。")
    return "\n".join(lines).strip()

DEFAULT_OWNER = "Ua14134c9ba22c6f73b02afc78091f965"

@app.route("/cron/daily", methods=["GET", "POST"])
def cron_daily():
    """每日巡守端點。外部 cron 每天打一次即可。
    可選用 ?key=... 搭配環境變數 CRON_KEY 防止被亂打。"""
    expected = os.environ.get("CRON_KEY", "")
    if expected and request.args.get("key", "") != expected:
        abort(403)
    try:
        book = get_sheets()
        digest = build_daily_digest(book)
        owner = os.environ.get("OWNER_USER_ID", "").strip() or DEFAULT_OWNER
        if owner and digest:
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).push_message(
                    PushMessageRequest(to=owner, messages=[TextMessage(text=digest)])
                )
        return digest or "（今日無預警）", 200
    except Exception as e:
        return f"error: {str(e)[:200]}", 500

@app.route("/webhook", methods=["POST"])
def webhook():
    sig = request.headers.get("X-Line-Signature","")
    body = request.get_data(as_text=True)
    try: handler.handle(body, sig)
    except InvalidSignatureError: abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    if already_processed(event.message.id):
        return  # 重複的 webhook 重送，略過不再寫入
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
    elif is_team_setup(text) and not looks_like_data(text):
        team = extract_team_setup(text)
        try:
            book = get_sheets()
            save_memory(book, user_id, team, "手動設定團別")
            append_ai_inbox(book, user_id, text, {"type":"team_setup","team":team,"passengers":[],"summary":"手動設定團別"}, False, "已更新記憶", "💬 對話記憶", "", False)
            reply = f"📝 已記住目前團別：{team}\n之後貼資料沒寫團名時，會先歸到這一團。"
        except Exception as e:
            reply = f"⚠️ 記憶設定失敗：{str(e)[:100]}"
    elif len(text) < 5:
        reply = "請貼上旅客資料（護照、付款、餐食需求等），我會自動更新試算表。\n\n查詢：查[姓名] / 缺件 / [團名]狀況"
    else:
        try:
            book = get_sheets()
            memory = get_memory(book, user_id)
            log_raw_inbox(book, user_id, text)  # 先存原文，最後防線
            if is_query(text):
                q_result = handle_query(text, book, memory)
                if q_result:
                    append_ai_inbox(book, user_id, text, {"type":"query","team":memory.get("team",""),"passengers":[],"summary":"查詢指令"}, False, "已回答查詢", "", "", False)
                    reply = q_result
                else:
                    reply = process(classify_text(text, memory), user_id, book, text)
            elif not looks_like_data(text):
                reply = chat_answer(text, memory)
                append_ai_inbox(book, user_id, text, {"type":"chat","team":memory.get("team",""),"passengers":[],"summary":"一般問答/聊天"}, False, "已回答聊天", "", "", False)
            else:
                result = classify_text(text, memory)
                reply = process(result, user_id, book, text)
        except Exception as e:
            reply = f"⚠️ 錯誤：{str(e)[:100]}"
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply)]))

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    if already_processed(event.message.id):
        return  # 重複的 webhook 重送，略過不再寫入
    user_id = event.source.user_id
    try:
        with ApiClient(configuration) as api_client:
            img_bytes = MessagingApiBlob(api_client).get_message_content(event.message.id)
        img_b64 = base64.b64encode(img_bytes).decode()
        book = get_sheets()
        log_raw_inbox(book, user_id, f"[圖片] message_id={event.message.id}", "待OCR")
        ocr = classify_image(img_b64)
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

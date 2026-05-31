# 山富旅遊 LINE Bot 部署指南

## 還需要準備的東西

### 1. Claude API Key
→ 前往 https://console.anthropic.com
→ 登入 → API Keys → Create Key
→ 複製保存（只會顯示一次）

### 2. Google Service Account（讓程式可以寫入 Sheet）

步驟：
1. 前往 https://console.cloud.google.com
2. 建立新專案（名稱：shanfu-bot）
3. 左側選單 → API 和服務 → 啟用 API
   → 搜尋並啟用「Google Sheets API」
   → 搜尋並啟用「Google Drive API」
4. 左側選單 → IAM 和管理員 → 服務帳戶
   → 建立服務帳戶（名稱：shanfu-sheets）
5. 點進剛建的服務帳戶 → 金鑰 → 新增金鑰 → JSON
   → 下載 JSON 檔案
6. 開啟 JSON 檔，複製全部內容

### 3. 把 Sheet 分享給 Service Account
1. 打開你的 Google Sheet
2. 右上角「共用」
3. 貼上 Service Account 的 Email（格式：shanfu-sheets@xxxx.iam.gserviceaccount.com）
4. 權限選「編輯者」

### 4. 找到 Google Sheet ID
打開試算表，網址長這樣：
https://docs.google.com/spreadsheets/d/【這串就是ID】/edit

---

## 部署到 Railway（免費）

1. 前往 https://railway.app
2. 用 GitHub 登入
3. 「New Project」→「Deploy from GitHub repo」
   （先把程式碼 push 到 GitHub，或用「Deploy from local」）
4. 設定環境變數（把 .env.example 的值填入）：
   - LINE_CHANNEL_SECRET
   - LINE_CHANNEL_ACCESS_TOKEN
   - ANTHROPIC_API_KEY
   - GOOGLE_SHEET_ID
   - GOOGLE_SERVICE_ACCOUNT_JSON（把整個 JSON 貼入）
5. 部署完成後，複製你的 Railway URL
   例：https://shanfu-bot-production.up.railway.app

---

## 設定 LINE Webhook

1. 回到 LINE Developer Console
2. Messaging API → Webhook settings
3. Webhook URL 填入：
   https://你的Railway網址/webhook
4. 勾選「Use webhook」
5. 點「Verify」測試連線

---

## 使用方式

開啟 LINE，找到你的 Bot，傳送：
- 「狀態」→ 確認系統正常
- 貼上旅客資料 → 自動分類更新
- 傳護照圖片 → 自動 OCR 更新

### 範例指令：
貼入：「王大明護照 A12345678，效期2029年6月，生日1975/05/20，不吃牛，單人房，意力九州團」
→ Bot 自動更新旅客總表、特殊需求

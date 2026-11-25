# Summery 現行 UI 完整功能規格書

> 文件目的：完整盤點「目前已存在」的 UI/UX 功能、資料契約與互動行為，提供後續改寫 UI 時的功能對照基準。
> 
> 適用範圍：本專案當前 Web 介面（Vite + React + TypeScript）與其對應 API 互動。
> 
> 本文件不限制新的視覺設計方向，只定義現況功能與必要保留能力。

---

## 1. 產品定位與現況摘要

Summery 是影音轉文字與摘要工具。使用者以檔案上傳流程啟動工作（job），經過 ASR、摘要、輸出寫檔後，在同一介面查看摘要/逐字稿/原始 JSON，並下載各種輸出檔案。

### 1.1 核心使用路徑

1. 選擇檔案並設定選項（摘要風格、語言、切塊等）
2. 上傳後建立工作（取得 `job_id`）
3. 觀察進度與狀態（queued/running/...）
4. 完成後檢視結果（摘要、逐字稿、原始 JSON）
5. 視需要播放音訊並點時間戳跳轉
6. 下載輸出文件（md/json/txt/srt/vtt 等）

### 1.2 最近修補（需納入基準）

- **Toast undefined 問題**：上傳成功提示已增加檔名 fallback，避免訊息顯示 `undefined`。
- **同秒重傳撞號問題**：工作 ID 已加入時間序號（nonce）避免碰撞，降低不同模式切換時結果混用風險。

---

## 2. UI 資訊架構（Information Architecture）

主要由以下區塊組成：

1. 上傳與參數設定區（Upload Card）
2. 工作清單/工作詳情切換區
3. 進度卡（Progress Card）
4. 結果卡（Result Card）
   - 摘要頁籤（Summary）
   - 逐字稿頁籤（Transcript）
   - 原始 JSON 頁籤（Raw）
5. 播放器（Audio Player）
6. 下載清單（Download list）
7. 全域通知（Toasts）

---

## 3. 功能模組完整規格

## 3.1 上傳與參數設定（Upload）

### 功能

- 接受使用者選擇影音檔案進行上傳。
- 提供摘要風格選擇（例如：concise / standard / comprehensive / bullets）。
- 提供語言與切塊參數等可調欄位（依前端表單與後端允許參數）。
- 上傳期間回報進度。

### 輸入

- 檔案（File）
- `JobOptionsInput`（前端 options）

### 輸出

- 呼叫 `POST /api/jobs?...` 建立工作
- 取得至少：`job_id`, `status`, `size`，且目前應包含 `name`

### 狀態與錯誤

- 上傳中：顯示百分比/進度
- 成功：建立 active job、顯示成功 toast
- 失敗：顯示錯誤 toast（需有可閱讀錯誤訊息）

### 必保留能力

- 上傳進度可視化
- 建立工作後可立即切換至該工作詳情
- 錯誤提示不可為空字串/undefined

---

## 3.2 工作清單（Jobs List）

### 功能

- 顯示目前與歷史工作列表。
- 以建立時間排序（新到舊）。
- 支援選取工作查看詳細結果。

### 顯示欄位（現況）

- `job_id`
- `name`
- `status`
- `stage`（若有）
- `progress`（若有）
- `message`（若有）

### 狀態

- `queued`
- `running`
- `partial`
- `done`
- `failed`
- `cancelled`
- `cancelling`

### 必保留能力

- 清楚區分執行中與已完成
- 可回看歷史工作結果與下載

---

## 3.3 進度卡（Progress Card）

### 功能

- 即時呈現工作階段與進度。
- 讓使用者知道目前停在 ASR、摘要或輸出階段。

### 主要資訊

- `stage`
- `progress`（0~1）
- `message`
- 可附帶計數（counts）

### 必保留能力

- 進度文本必須可讀（不要只顯示百分比）
- 失敗時必須可見原因

---

## 3.4 結果卡（Result Card）

結果卡包含三個核心頁籤：摘要、逐字稿、原始 JSON。

### 3.4.1 摘要頁籤（Summary）

#### 啟用條件

- `job.result?.summary` 存在時可用

#### 功能

- 顯示摘要標題、TL;DR、段落/章節、重點、開放問題等
- 顯示時間資訊（若有）
- 點擊時間標記可觸發播放器跳轉（seek）

#### 必保留能力

- 摘要內容可讀性（階層、段落、標題）
- 可點擊時間戳定位播放

#### 失效情境

- 無摘要資料時 tab disabled（現況行為）

---

### 3.4.2 逐字稿頁籤（Transcript）

#### 啟用條件

- `job.files` 有 json 檔，或 `job.result?.transcript?.text` 存在

#### 功能

- 顯示逐字稿內容與時間
- 支援依目前播放時間高亮/定位
- 支援點擊跳轉（seek）

#### 必保留能力

- 能看完整內容
- 時間同步與點擊定位仍可用

---

### 3.4.3 原始 JSON 頁籤（Raw）

#### 功能

- 顯示 summary JSON（優先尋找 `*.summary.json`）
- 採 lazy load：進入 tab 才抓取

#### 狀態

- 載入中顯示 skeleton
- 成功顯示 pretty-printed JSON
- 失敗顯示「讀取失敗：...」
- 無摘要 JSON 顯示提示文字

#### 必保留能力

- 錯誤訊息可見
- JSON 格式化可閱讀

---

## 3.5 播放器（Audio Player）

### 功能

- 播放/暫停
- 顯示目前時間與總時長
- 可拖拉進度條
- 向外提供 seek 註冊能力（供摘要/逐字稿點時間時呼叫）

### 互動

- `onTimeUpdate` 回傳目前秒數
- 外部可呼叫 `seek(seconds)`

### 必保留能力

- 拖曳與點擊時間戳互通
- 時間顯示格式一致（mm:ss / hh:mm:ss）

---

## 3.6 下載區（Downloads）

### 功能

- 列出可下載輸出檔
- 顯示格式名稱與大小（KB）

### 可能格式（依工作產出）

- `summary.md`
- `summary.json`
- `transcript.json`
- `transcript.md`
- `txt`
- `srt`
- `vtt`
- `job.json`

### 必保留能力

- 下載連結可直接取得檔案
- 格式標示清楚（不要只有檔名）

---

## 3.7 全域通知（Toasts）

### 功能

- 顯示成功/失敗/提醒訊息
- 位置：右下角（現況）

### 訊息來源

- 上傳成功
- API 失敗
- 其他前端流程異常

### 已知修補

- 上傳成功訊息已避免 `undefined`（檔名 fallback）

### 必保留能力

- 所有 toast 必有可讀文字
- 錯誤訊息需優先顯示可行動資訊

---

## 4. API 與資料契約（UI 依賴）

> 以下是 UI 目前依賴的關鍵欄位，改 API 時需向後相容或同步改前端。

## 4.1 POST `/api/jobs`

### 請求

- Query 包含 options（如 `style`, `summary_language` 等）
- Body 為檔案二進位資料
- Header 含檔名資訊（例如 `X-Filename`）

### 回應（至少）

- `job_id: string`
- `status: string`
- `size: number`
- `name: string`（建議必回）

### UI 用途

- 建立 active job
- 成功提示顯示檔名

---

## 4.2 GET `/api/jobs`

### 回應

- `jobs: JobDetail[]`

### JobDetail 常用欄位

- `job_id`, `name`, `status`, `created_at`
- `stage`, `progress`, `message`
- `options`
- `outputs`, `files`
- `counts`, `errors`
- `historical`

### UI 用途

- 工作列表
- 輪詢更新進度

---

## 4.3 GET `/api/jobs/:id` 或等效明細資料

### 常用欄位

- `result.summary`
- `result.transcript`
- `media_url`
- `files[]`

### UI 用途

- 結果三分頁渲染
- 播放器與時間同步

---

## 4.4 Summary JSON 契約（前端閱讀）

### 典型欄位

- `title`
- `tldr`
- `overview[]`
- `key_points[]`
- `chapters[]`（含時間）
- `open_questions[]`（可含定位資訊）
- `meta`

### UI 用途

- 摘要頁內容顯示與時間定位

---

## 4.5 Transcript JSON 契約（前端閱讀）

### 典型欄位

- `duration`
- `language`
- `segments[]`：`start`, `end`, `text`, `clock`
- `meta`, `stats`

### UI 用途

- 逐字稿顯示、播放定位、高亮

---

## 5. 狀態機規格（UI 行為層）

## 5.1 Job 狀態機（高階）

1. `queued`
2. `running`
3. `partial`（可選）
4. `done` 或 `failed` 或 `cancelled`

## 5.2 結果頁籤狀態

- Summary tab：有 summary 資料才啟用
- Transcript tab：有 transcript 來源才啟用
- Raw tab：永遠可點，內容可為 loading/錯誤/無資料

## 5.3 原始 JSON（Raw）狀態

- idle（未進入 tab）
- loading（進入 tab 後抓取）
- ready（成功）
- error（讀取失敗）
- empty（找不到 summary.json）

---

## 6. 元件責任邊界（重構時的重要依據）

## 6.1 App（容器）

- 管理全域狀態：active job、toast、輪詢
- 協調 Upload / Progress / Result

## 6.2 UploadCard

- 提供上傳與 options 編輯 UI
- 將 options 提交給 API

## 6.3 ProgressCard

- 專責顯示工作執行進度，不承擔資料抓取

## 6.4 ResultCard

- 管理頁籤切換
- 載入 raw JSON
- 組合播放器、摘要、逐字稿、下載區

## 6.5 SummaryDoc

- 專注摘要內容排版與時間戳互動

## 6.6 TranscriptPanel

- 專注逐字稿呈現、時間高亮與跳轉

---

## 7. 目前已知問題與風險清單

## 7.1 已修補

1. **Toast 出現 undefined**：已修。
2. **工作 ID 同秒碰撞**：已修（加入 nonce）。

## 7.2 仍需注意

1. **LLM 模型配置錯誤**可能造成摘要失敗（如 Invalid model name）。
2. 若摘要失敗且無 fallback，摘要頁將不可用（目前方向是保底產出本地摘要）。
3. 歷史工作資料與新工作視覺區分需清楚，避免誤判為「快取重用」。

---

## 8. 後續 UI 改寫時的「不可退化」清單

以下能力在改版後必須維持：

1. 上傳進度可視化。
2. 工作狀態可追蹤（至少 queued/running/done/failed）。
3. 摘要/逐字稿/原始 JSON 三視角至少保留同等資訊可達性。
4. 可透過時間戳控制播放定位。
5. 可下載主要輸出檔（至少 transcript + summary）。
6. 全域錯誤提示不可缺漏、不可顯示 undefined。
7. 同檔重傳不得覆蓋不同工作結果。

---

## 9. 建議的重構驗收規格（可直接用）

## 9.1 功能驗收

- [ ] 能上傳檔案並建立新 job
- [ ] 能看到上傳進度與工作進度
- [ ] 能在完成後看到摘要與逐字稿
- [ ] Raw JSON 可讀且錯誤時有訊息
- [ ] 下載連結可用
- [ ] toast 文案完整

## 9.2 相容性驗收

- [ ] 舊資料結構仍可渲染（或有 migration）
- [ ] API 欄位變更有前端同步
- [ ] 同檔重傳不覆寫舊工作

## 9.3 可用性驗收

- [ ] 重要操作有回饋（上傳、失敗、完成）
- [ ] tab 不會出現可點但無內容的誤導
- [ ] 音訊時間同步行為正確

---

## 10. 檔案/程式區域參考（供開發者）

> 以下為重構時最常影響區域（名稱依現況）：

- `frontend/src/App.tsx`：全域流程與 toast
- `frontend/src/components/UploadCard.tsx`：上傳與參數
- `frontend/src/components/ProgressCard.tsx`：進度顯示
- `frontend/src/components/ResultCard.tsx`：結果三分頁與播放器
- `frontend/src/components/SummaryDoc.tsx`：摘要呈現
- `frontend/src/components/TranscriptPanel.tsx`：逐字稿呈現
- `frontend/src/api.ts`：API 請求與 query/options 組裝
- `frontend/src/types.ts`：前端型別契約
- `src/summery/web.py`：HTTP API 與 job 建立
- `src/summery/pipeline.py`：ASR/摘要/輸出流程

---

## 11. 名詞對照

- **Job**：一次處理工作
- **Stage**：工作內部階段（asr/summarize/write...）
- **Summary**：摘要結構化資料
- **Transcript**：逐字稿資料
- **Raw JSON**：原始摘要 JSON 檢視
- **Historical**：歷史工作（服務啟動後載入的舊紀錄）

---

## 12. 版本註記

- 文件版本：v1.0（現況完整盤點版）
- 用途：UI 全面改寫前基線文件（Baseline Spec）
- 原則：新設計可自由升級視覺與互動，但不得無意移除本文件標示之核心功能能力。

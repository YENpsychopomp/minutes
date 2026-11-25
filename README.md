# summery

把**影片或音訊**轉成**逐字稿 + 摘要**的命令列工具與網頁服務。
ASR 與 LLM 走 **OpenAI 相容 API**（可自行指定 `base_url`），並為**長影音**（數小時講座、研討會、超長會議）重新設計了切塊、併行、續傳與分層摘要機制。

* 執行期**零第三方依賴**：只要 Python 3.11+ 與 `ffmpeg`／`ffprobe`。
* 輸出：Markdown、JSON、純文字、SRT、VTT 逐字稿 + 帶時間戳的章節摘要。
* **會議紀錄**：手上只有筆記或逐字稿（Word、PDF、Markdown、純文字、字幕）也能直接產出會議紀錄；
  再放一份**過往的會議紀錄當範本**，新紀錄會沿用它的格式、段落結構、用語、與會者與專有名詞。
* 介面：命令列為主（可腳本化），另附 Vite 網頁介面（拖放上傳、播放列、可點擊時間戳）。

---

## 目錄

- [安裝](#安裝)
- [快速開始](#快速開始)
- [長影音是怎麼處理的](#長影音是怎麼處理的)
- [會議紀錄：文件輸入與過往紀錄範本](#會議紀錄文件輸入與過往紀錄範本)
- [命令一覽](#命令一覽)
- [輸出檔案](#輸出檔案)
- [設定](#設定)
- [常見 Gateway 設定](#常見-gateway-設定)
- [一鍵啟動（deploy.sh／deploy.ps1）](#一鍵啟動deploysh)
- [網頁介面](#網頁介面)
- [前端開發（Vite）](#前端開發vite)
- [斷點續傳與快取](#斷點續傳與快取)
- [疑難排解](#疑難排解)
- [開發與測試](#開發與測試)
- [已知限制](#已知限制)

---

## 安裝

需要 Python 3.11+ 與 ffmpeg（含 ffprobe）。

### macOS／Linux

```bash
brew install ffmpeg          # macOS
# apt install ffmpeg         # Debian/Ubuntu

git clone https://github.com/YENpsychopomp/minutes.git && cd minutes
pip install -e .             # 安裝 summery 指令
```

不想安裝也可以直接用原始碼執行：

```bash
PYTHONPATH=src python3 -m summery --help
```

### Windows

以下指令在 **PowerShell** 執行，用 conda 一次備齊 Python、ffmpeg（含 ffprobe）與 Git。

**1. 安裝 conda**（已有 Anaconda／Miniconda 可略過）：

```powershell
winget install Anaconda.Miniconda3
conda init powershell              # 讓 PowerShell 認得 conda activate；完成後請重開 PowerShell
```

> 沒有 winget 時：從 <https://www.anaconda.com/download/success> 下載 Miniconda 安裝，或直接改用開始選單裡的「Anaconda PowerShell Prompt」。
> `conda init` 後重開 PowerShell 若出現「因為這個系統上已停用指令碼執行」：`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。

**2. 建立環境**（Python、ffmpeg、Git 都從 conda-forge 裝進這個環境，不動系統 PATH）：

```powershell
conda create -n summery -c conda-forge python=3.12 ffmpeg git -y
conda activate summery

ffmpeg -version                     # 確認兩者都找得到
ffprobe -version
```

> 用 Anaconda Prompt（cmd）也可以照著做；兩者只差在少數語法：cmd 不認得 `;` 分隔指令（要分行或用 `&&`），設定環境變數要用 `set PYTHONPATH=src` 而不是 `$env:PYTHONPATH = "src"`。

**3. 下載專案並安裝**：

```powershell
git clone https://github.com/YENpsychopomp/minutes.git
cd minutes
pip install -e .                    # 安裝到 summery 環境
summery --help
```

不想安裝也可以直接用原始碼執行（同樣要先 `conda activate summery`）：

```powershell
$env:PYTHONPATH = "src"
python -m summery --help
```

之後每次開新的 PowerShell，先 `conda activate summery` 再使用 `summery`。

啟動網頁介面：用 Windows 版一鍵腳本 `deploy.ps1`（參數與 `deploy.sh` 相同，見[一鍵啟動](#一鍵啟動deploysh)）。腳本會自動使用目前已啟用的 conda 環境：

```powershell
conda activate summery
.\deploy.ps1 --open
```

> 直接雙擊 `deploy.cmd` 時不會啟用 conda 環境，可能找不到 Python 或用到別的 Python；請在已 `conda activate summery` 的視窗裡執行 `.\deploy.cmd`。

> 小提醒：在 PowerShell 裡含空白或中文的檔名請加引號，例如 `summery run "C:\錄音\1019 會議.m4a"`。

---

## 快速開始

### 1. 設定金鑰與端點

寫一個 `.env`（或 `my.toml`，見[設定](#設定)）放在執行目錄，**`summery` 會自動載入目前目錄的 `.env`**，不需要 `source`：

```dotenv
ASR_API_KEY=sk-...
LLM_API_KEY=sk-...
SUMMERY__ASR__BASE_URL=https://api.openai.com/v1
SUMMERY__ASR__MODEL=whisper-1
SUMMERY__LLM__BASE_URL=https://api.openai.com/v1
SUMMERY__LLM__MODEL=gpt-4o-mini
SUMMERY_CONFIG=./my.toml
```

```bash
summery run 講座.mp4                    # 自動套用 ./.env
summery --env-file /etc/summery.env run 講座.mp4    # 指定別份（會覆蓋既有環境變數）
summery --no-env-file jobs              # 完全不載入 .env（或用 SUMMERY_DOTENV=0）
```

> 已存在的環境變數優先於 `.env`，方便 CI／臨時覆蓋。

### 2. 跑一個 2 小時講座

```bash
summery run 講座.mp4 --chunk-seconds 900 --concurrency 6 --style comprehensive
```

輸出預設落在來源檔同目錄：

```
講座.transcript.md      # 逐字稿（含 [h:mm:ss] 時間戳與段落編號）
講座.transcript.json    # 結構化逐字稿（segment / word 級時間戳）
講座.txt  .srt  .vtt    # 純文字與字幕
講座.summary.md         # 摘要：TL;DR、重點、章節、決議、待辦、金句
講座.summary.json       # 結構化摘要
講座.job.json           # 工作 metadata（用了哪些檔案、耗時、token 用量）
```

### 3. 沒有 API key 也想先看看流程？

內建**模擬 ASR/LLM 伺服器**與**測試影音產生器**，可完整驗證切塊、併行、續傳、map-reduce：

```bash
bash examples/mock_demo.sh          # 12 分鐘測試影片 → 逐字稿 + 摘要，全程離線
```

或手動：

```bash
summery make-media --out demo.mkv --minutes 60 --segments 200 --video
summery mock-server --port 8899 &
export ASR_API_KEY=mock LLM_API_KEY=mock
export SUMMERY__ASR__BASE_URL=http://127.0.0.1:8899/v1
export SUMMERY__LLM__BASE_URL=http://127.0.0.1:8899/v1
summery run demo.mkv --chunk-seconds 300 --out-dir out
```

---

## 長影音是怎麼處理的

直接將 2 小時音訊丟給 Whisper 的結果通常是：**超过上傳限制**、**時間戳漂移**、**句子被切兩段**、以及**逐字稿長到塞不進摘要模型**。summery 的處理順序：

```
來源媒體
  │ 1. ffprobe：長度、音軌、是否含視訊
  │ 2. ffmpeg：正規化為 16kHz 單聲道（一次完成，後續都從這份切）
  │ 3. silencedetect：找出所有 ≥0.35s 的靜音點
  ▼
4. 切塊計畫：以 target_seconds 為節拍，把每個邊界「吸附」到附近最近的靜音點
   （預設在 ±240s 內找；找不到才硬切）
   每塊往回多讀 overlap_seconds（預設 2s），避免跨邊界的句子被切走
   尾段過短自動合併；若尾段會超過 max_seconds 則繼續切，絕不產生超限塊
  ▼
5. 逐塊轉碼（預設 m4a/AAC 24k，2 小時只需約 21MB）→ 併行上傳 ASR
   單一塊超過 asr.max_upload_bytes 時自動二分遞迴（最多 4 層）
  ▼
6. 依 chunk 起點平移時間戳 → 丟掉重疊區重複內容 → 相鄰邊界去重
   → 產出全域單調、0 空隙、0 重疊的逐字稿
  ▼
7. 摘要：逐字稿 token 數 ≤ max_input_tokens 時單次完成；
   否則 map（區塊筆記）→ 多輪 reduce（筆記合筆記）→ final（全場摘要）
   章節只引用「編號段落對照表」，時間由程式換算，模型無法亂編時間
  ▼
8. 寫出 7 種檔案 + state.json（進度給 CLI/Web 輪詢）
```

實測（本機 mock 服務，重點在管線而非模型品質）：

| 媒體 | 切塊 | 逐字稿 | 摘要模式 |
|---|---|---|---|
| 12 分鐘會議 | 6 塊（全部靜音對齊） | 89 段 | 單次 |
| 2 小時講座 | 24 塊（23 塊靜音對齊） | 893 段 / 43,270 字 | map-reduce（6 區塊、7 次 LLM 呼叫） |

> 真實模型的時間主要花在 ASR 上傳與摘要生成；切塊與合併在本機是秒級。

---

## 會議紀錄：文件輸入與過往紀錄範本

兩種用法可以各自使用，也可以一起用：

| 用法 | 指令 | 產出 |
|---|---|---|
| **文件當輸入**：沒有錄音，只有筆記、逐字稿或舊紀錄 | `summery minutes 筆記.docx 補充.md` | 會議紀錄（標準格式）＋摘要＋原文 |
| **過往紀錄當範本**：影音照常轉錄，紀錄沿用以前的格式 | `summery run 例會.mp4 --template 上次紀錄.docx` | 逐字稿＋摘要＋**依範本格式**的會議紀錄 |
| 兩者一起 | `summery minutes 筆記.md --template 上次紀錄.pdf` | 依範本格式的會議紀錄 |

```bash
# 幾個人的筆記合成一份紀錄，沿用上次的格式（--template 可重複）
summery minutes 王的筆記.docx 李的筆記.md --template 第11次系務會議紀錄.docx -o out

# 影音 + 範本；沒有範本也想要一份正式紀錄就加 --minutes
summery run 例會.mp4 --template 上次紀錄.pdf
summery run 例會.mp4 --minutes

# 既有逐字稿重算摘要時也能套範本
summery summarize out/例會.transcript.json --template 上次紀錄.docx
```

**支援的文件格式**

| 格式 | 怎麼讀 |
|---|---|
| `.docx` | 標準函式庫 `zipfile` + `xml` 解析：保留標題層級、Word 自動編號（「一、」「(一)」「1.」照原樣還原）、清單與表格。舊版 `.doc` 請先另存成 `.docx` |
| `.md`／`.txt` | 直接讀；編碼自動判斷 UTF-8（含 BOM）／UTF-16／Big5（cp950）／GB18030 |
| `.pdf` | 交給 Microsoft 開源的 [markitdown](https://github.com/microsoft/markitdown)（見下方） |
| `.srt`／`.vtt` | 字幕：保留真實時間軸，章節照樣附時間 |
| `.json` | summery 的 `transcript.json`（有時間軸）或 `summary.json` |

**PDF 與 markitdown**：summery 本身仍是零依賴，PDF 才需要 markitdown，依序尋找：

1. 設定 `documents.markitdown_cmd`（或環境變數 `SUMMERY_DOCUMENTS_MARKITDOWN_CMD`）；可以是路徑，也可以是整串指令，例如 `conda run -n markitdown markitdown`
2. 同一個 Python 環境已 `pip install "markitdown[pdf]"` → 直接 import
3. PATH 上的 `markitdown`
4. conda 環境裡的 `markitdown`（自動掃 `anaconda3`／`miniconda3`／`miniforge3` 的 `envs/*/Scripts`，名稱含 markitdown 的環境優先；**不必先 `conda activate`**）

都找不到時會明確說明怎麼裝、或改存 `.docx`／`.txt`。掃描影像的 PDF 沒有文字層，會提示先做 OCR。
不少中文 PDF 會把「工、月、大、一」抽成外觀相同的康熙部首碼位（`⼯ ⽉ ⼤ ⼀`），summery 會自動換回一般漢字，人名與「一、」編號才比對得到（全形標點不受影響）。

**範本怎麼被使用**

1. **範本分析**（1 次 LLM 呼叫）：從過往紀錄抽出段落結構（`outline`）、開頭欄位（時間、地點、主席…）、與會者、專有名詞與版面語氣。結果依內容雜湊快取在工作目錄，同一份範本不會重複分析；模型不可用時改用標題規則推得段落結構。
2. **摘要時校正用語**：與會者與專有名詞會附在 map／reduce／final 每個階段的提示詞裡，逐字稿裡聽錯的人名、單位名稱以範本寫法為準。
3. **撰寫會議紀錄**（1 次 LLM 呼叫）：把結構化摘要依範本的段落順序、編號方式與語氣寫成 `<stem>.minutes.md`。範本裡的**舊日期、舊決議、舊待辦一律不會被沿用**；範本有但本次沒有資訊的欄位寫「（未提及）」。沒有範本時用標準會議紀錄格式（會議資訊／討論事項／決議／待辦表格／待釐清問題）。

文件沒有時間軸，所以文件工作的章節、待釐清問題都以**段落編號**標示（`段落 3–7`），對應 `transcript.md` 裡的 `[3]`；
也不會輸出 `.srt`／`.vtt`。輸入檔絕不會被覆寫：例如輸入 `notes.txt`、輸出目錄又是同一處時，純文字輸出會改名為 `notes.out.txt`。
範本讀不到（例如舊版 `.doc`）時，工作會在送出 ASR **之前**就失敗，不浪費轉錄費用。

---

## 命令一覽

| 命令 | 用途 |
|---|---|
| `summery run <media>` | 完整流程：轉錄 + 摘要（加 `--template 舊紀錄.docx` 另出依範本格式的會議紀錄） |
| `summery minutes <文件…>` | 從一或多份文件（docx／md／txt／pdf／srt／vtt／json）產出會議紀錄，不經 ASR |
| `summery transcribe <media>` | 只要逐字稿（等同 `run --no-summary`） |
| `summery summarize <transcript.json>` | 從既有逐字稿重算摘要（不再打 ASR，換風格/換模型很快）；也吃 docx／md／pdf／srt／vtt |
| `summery plan <media>` | 只顯示切塊計畫與預估流量，**不打 API** |
| `summery probe <media>` | 顯示媒體資訊（`--json` 給腳本） |
| `summery jobs` | 列出已知工作與進度 |
| `summery serve` | 網頁上傳介面 |
| `summery mock-server` | 本機模擬 ASR/LLM（免 key 試跑、跑測試） |
| `summery make-media` | 產生含靜音段的測試影音 |

常用選項（`run`）：

```bash
summery run 會議.mp3 \
  --chunk-seconds 300 --overlap 2 --concurrency 6 \      # 切塊與併行
  --asr-language zh --summary-language zh-TW \           # 語言
  --style concise --instruction "聚焦預算結論" \          # 摘要風格與附加指示
  --formats md,srt --out-dir out \                       # 輸出
  --no-resume --strict --dry-run \                       # 快取／嚴格模式／預演
  --json                                                 # 結尾輸出 JSON（給 CI 用）
```

會議紀錄相關選項（`run`／`minutes`／`summarize` 皆可用）：

```bash
--template 上次紀錄.docx   # 過往會議紀錄當範本，可重複
--minutes                  # 一定輸出 minutes.md（沒有範本就用標準格式）
--no-minutes               # 不要 minutes.md（預設：有範本或輸入是文件時才輸出）
```

任何設定欄位都能用 `--set` 覆蓋：

```bash
summery plan 講座.mp4 --set chunking.silence_snap_window=600 --set llm.model=gpt-4o
```

回傳碼：`0` 成功、`1` 失敗、`2` **部分完成**（某些 chunk 失敗，逐字稿仍可用；重跑會自動補齊）、`130` 使用者取消。

---

## 輸出檔案

| 檔案 | 內容 |
|---|---|
| `<stem>.transcript.md` | 逐字稿，含標題、媒體資訊、段落編號與 `[h:mm:ss]` |
| `<stem>.transcript.json` | `summery.transcript/1`：`segments[]`（start/end/text/words） |
| `<stem>.txt` | 純文字逐字稿 |
| `<stem>.srt` / `<stem>.vtt` | 字幕檔 |
| `<stem>.summary.md` | TL;DR、內容概要、關鍵重點、**章節摘要（含時間）**、關鍵字、決議、待辦（負責人/期限）、**待釐清問題（含時間點）**、金句 |
| `<stem>.summary.json` | `summery.summary/1`：同上結構化版本，章節另附 `clock`；`open_questions[]` 為物件（`text`/`para`/`start`/`clock`/`located`）；另有 `minutes_markdown` |
| `<stem>.minutes.md` | **會議紀錄**：有範本時依範本的段落結構與用語撰寫，沒有範本時用標準格式（文件輸入、`--template` 或 `--minutes` 時產出） |
| `<stem>.job.json` | job_id、輸出對照表、使用格式 |

文件輸入時，`transcript.*` 是上傳文件的原文（以 `[段落編號]` 標示，不輸出 srt／vtt）。
`summary.json` 的 `meta` 會多幾個欄位：`input_kind`（`media`／`document`）、`documents`（輸入文件清單）、
`reference`（範本分析結果：`outline`／`header_fields`／`attendees`／`terms`／`source`）、
`minutes`（`format`：`template`／`default`；`source`：`model`／`local`；`template`：範本檔名）。
文件工作的章節 `clock` 是「段落 a–b」而不是時間。

摘要 JSON 的章節欄位範例：

```json
{
  "start": 3612.4, "end": 4580.9, "clock": "[1:00:12] – [1:16:20]",
  "title": "季度營收與三項擴張提案",
  "summary": "……",
  "points": ["……"],
  "para_start": 61, "para_end": 96
}
```

「待釐清問題」也一律附上時間點，讓你在核對辨識錯誤時可以直接跳回那段音訊：

```json
{
  "text": "「网路咨询」是否應為「網路資訊」？",
  "para": 22, "start": 1026.0, "clock": "0:17:06", "located": "text"
}
```

`located` 說明時間點怎麼來的：`text` 是拿問題裡引號括住的**原詞**回查逐字稿比中的位置（最準）；
`para` 是採用模型回報的段落編號換算而來；`none` 代表無法定位，該筆就不會顯示時間。
比對順序刻意讓原詞優先：map-reduce 模式下模型只看得到幾份分區筆記，段落編號常整排相同。
`meta.open_questions_timed` 會記錄這個工作有幾筆成功定位（例 `"6/8"`）。

---

## 設定

優先序：**`--set` ＞ 環境變數 ＞ TOML 設定檔 ＞ 內建預設值**。

完整可調欄位與說明見 [`config/config.example.toml`](config/config.example.toml)，另外提供兩組現成設定：

* [`config/profile-long-lecture.toml`](config/profile-long-lecture.toml)：2 小時以上講座（900s 大塊、高併行、comprehensive）
* [`config/profile-meeting-quick.toml`](config/profile-meeting-quick.toml)：30–90 分鐘會議（300s、concise、只輸出 md/srt）

```bash
summery run 研討會.mp4 -c config/profile-long-lecture.toml
export SUMMERY_CONFIG=./my.toml        # 不想每次都打 -c
```

環境變數兩種寫法皆可：`SUMMERY_ASR_MODEL` 或 `SUMMERY__ASR__MODEL`（區塊＋欄位）。
列表用逗號分隔、字典用 `key=value,key2=value2`。金鑰可用 `ASR_API_KEY`／`LLM_API_KEY`／`OPENAI_API_KEY`，端點也可用 `OPENAI_BASE_URL`。見 [`.env.example`](.env.example)。

### 長影音重點欄位

| 欄位 | 預設 | 什麼時候調 |
|---|---|---|
| `chunking.target_seconds` | 600 | 內容很長→調大（減少請求數）；要更精準的時間戳→調小 |
| `chunking.silence_snap_window` | 240 | 錄音幾乎沒有靜音（音樂、連續演講）時調大，或 `--no-silence-scan` |
| `chunking.overlap_seconds` | 2.0 | 出現跨邊界重複/漏字時調到 3–5 |
| `chunking.silence_noise_db` | -30 | 環境噪音高（實測都找不到靜音點）時放寬到 -25 |
| `audio.codec` / `bitrate` | m4a / 24k | Gateway 不支援 m4a 就改 `mp3` 或 `wav`；音樂內容用 64k |
| `asr.max_upload_bytes` | 24 MiB | Gateway 有較小上限時調降，超出會自動二分 |
| `summarize.max_input_tokens` | 12000 | 依摘要模型上下文窗口調整；越小越早進入 map-reduce |
| `summarize.map_chunk_tokens` | 7000 | 摘要太空洞→調小（筆記更細）；費用太高→調大 |

### 文件與範本欄位（`[documents]`）

| 欄位 | 預設 | 什麼時候調 |
|---|---|---|
| `documents.markitdown_cmd` | 自動尋找 | PDF 讀不到、或想指定某個 markitdown（路徑或整串指令） |
| `documents.reference_max_chars` | 12000 | 範本很長（多份合計）時，範本分析最多送這麼多字 |
| `documents.template_excerpt_chars` | 4000 | 撰寫紀錄時附上的範本原文節錄；版面模仿得不夠像就調大 |
| `documents.paragraph_max_chars` | 500 | 文件段落過長時依句子切小，章節定位更細 |
| `documents.minutes_max_output_tokens` | 0 | 紀錄很長被截斷時調大（0 = 沿用 `llm.max_output_tokens`） |

---

## 常見 Gateway 設定

```bash
# OpenAI 官方
SUMMERY__ASR__BASE_URL=https://api.openai.com/v1
SUMMERY__ASR__MODEL=whisper-1
SUMMERY__LLM__BASE_URL=https://api.openai.com/v1
SUMMERY__LLM__MODEL=gpt-4o-mini
```

```bash
# Azure OpenAI（deployment 型 URL；金鑰用 ASR_API_KEY / LLM_API_KEY）
SUMMERY__ASR__BASE_URL="https<region>.openai.azure.com/openai/deployments/whisper-large-v3?api-version=2024-10-21"
SUMMERY__LLM__BASE_URL="https<region>.openai.azure.com/openai/deployments/gpt-4o?api-version=2024-10-21"
```

```bash
# vLLM / 自建 Whisper + 本地 LLM（同一個端點給兩者）
SUMMERY__ASR__BASE_URL=http://127.0.0.1:8000/v1
SUMMERY__ASR__MODEL=whisper-large-v3
SUMMERY__LLM__BASE_URL=http://127.0.0.1:8001/v1
SUMMERY__LLM__MODEL=Qwen3-32B
SUMMERY__LLM__SUPPORTS_RESPONSE_FORMAT=false   # 不支援 JSON mode 時
SUMMERY__LLM__MAX_TOKENS_FIELD=max_completion_tokens   # 新版端點需求
```

自簽憑證的內部服務：加 `--no-ssl-verify`。
不支援 `verbose_json` 的端點：`SUMMERY__ASR__RESPONSE_FORMAT=text`（此時逐字稿時間戳會依字數平均分攤）。

---

## 一鍵啟動（deploy.sh）

不想記指令就只用這一條。**開一個終端機視窗、執行它，整個系統就起來了；關掉那個視窗（或按 Ctrl+C），整個系統就停了**——沒有背景精靈、沒有 `nohup`、不會留下孤兒行程。

```bash
./deploy.sh              # 建置前端 → 啟動服務 → http://127.0.0.1:8765/
```

腳本會在啟動前自我檢查，缺少什麼就明講：

| 檢查 | 缺了會怎樣 |
|---|---|
| Python 3.11+ | 直接中止並提示怎麼裝 |
| `summery` 是否已安裝 | 沒裝就用 `PYTHONPATH=src` 直接跑原始碼（同一份程式碼） |
| `ffmpeg`／`ffprobe` | 提示安裝，轉檔無法工作 |
| `.env` 或 `SUMMERY_*_API_KEY` | 提醒沒金鑰，仍會啟動（上傳才會報錯） |
| `frontend/dist` | 有 `node` 就自動建置；沒有就退回內建的陽春介面 |

常用參數：

```bash
./deploy.sh --open                  # 啟動後直接開瀏覽器
./deploy.sh --port 9000 --host 0.0.0.0 --jobs-dir /data/jobs
./deploy.sh --build                 # 強制重建前端
./deploy.sh --no-build              # 直接用現成的 frontend/dist
./deploy.sh --dev                   # 開發模式：Python API + Vite 熱更新（見下方）
./deploy.sh --python .venv/bin/python
```

停止服務：**Ctrl+C**，或直接**關閉終端機視窗**。兩種都會觸發同一個清理程序（TERM → 等待 → KILL），並有測試保障（`tests/test_deploy_script.py`：送 SIGHUP 後埠號必須釋放、不留監聽行程）。

### Windows：deploy.ps1

Windows 版做的檢查與 `deploy.sh` 相同，參數也一樣，寫成 `--port 9000` 或 PowerShell 風格的 `-Port 9000` 都可以：

```powershell
.\deploy.ps1                          # 建置前端 → 啟動服務 → http://127.0.0.1:8765/
.\deploy.ps1 --open
.\deploy.ps1 --port 9000 --host 0.0.0.0 --jobs-dir D:\summery_jobs
.\deploy.ps1 --build                  # 強制重建前端
.\deploy.ps1 --no-build               # 不建置；沒有 frontend\dist 時用內建介面
.\deploy.ps1 --dev                    # 開發模式：Python API + Vite 熱更新
.\deploy.ps1 --python C:\Python312\python.exe
```

* **找 Python 的順序**：`--python` → 環境變數 `PYTHON` → `.venv\Scripts\python.exe` → 已啟用的 conda 環境（`CONDA_PREFIX`，版本不到 3.11 就略過）→ `py -3` 啟動器 → PATH 上的 `python`（會跳過 Windows 那個只會開 Microsoft Store 的假 `python.exe`）。
* **被執行原則擋下**（「因為這個系統上已停用指令碼執行」）：改用 `deploy.cmd`（可直接雙擊，參數相同，例如 `deploy.cmd --port 9000`），或執行 `powershell -ExecutionPolicy Bypass -File .\deploy.ps1`。這兩種都只對這一次執行放行，不會改系統設定。
* **停止**：Ctrl+C 或關閉 PowerShell 視窗。子行程跟腳本共用同一個視窗，關掉視窗就一起結束。開發模式收尾時會用 `taskkill /T` 把 pnpm → node 整串子行程一起關掉。
* 支援 Windows PowerShell 5.1（Windows 10/11 內建）與 PowerShell 7+。`deploy.ps1` 必須以 **UTF-8（含 BOM）** 儲存，否則 5.1 讀中文會變亂碼（`.gitattributes` 已設定換行，編輯器存檔時請保留 BOM）。

---

## 網頁介面

```bash
summery serve --port 8765 --jobs-dir summery_jobs --max-upload-mb 4096
```

後端是純標準函式庫的 HTTP 服務，同時提供 API 與前端靜態檔：

| 路徑 | 說明 |
|---|---|
| `GET /` | 前端頁面（未知路徑一律回前端外殼，重新整理深連結不會 404） |
| `GET /assets/*` | Vite 產出的檔（附 `immutable` 快取標頭） |
| `GET /api/health` `GET /api/config` `GET /api/jobs` | 狀態、可用模型與樣式、工作列表 |
| `POST /api/jobs` | 上傳（位元組直接送 body，檔名放 `X-Filename`，不受表單上限限制）；body 也可以是一份文件 |
| `POST /api/documents` | 暫存一份文件並**立即解析**（同樣 body＋`X-Filename`），回傳 `doc_id`、類型、段落數、字數與預覽；讀不到（例如 PDF 缺 markitdown、掃描檔）回 422 |
| `POST /api/jobs?inputs=<id,id>&references=<id>` | 以已上傳的文件建立會議紀錄工作（body 可為空）；影音上傳也能加 `references=` 套用範本，`minutes=1` 強制輸出會議紀錄 |
| `GET /api/jobs/{id}`、`POST .../cancel` | 進度與結果 |
| `GET /api/jobs/{id}/media` | 原始音訊，支援 `Range` 請求 → 播放器可以直接拖曳跳轉 |
| `GET /files/{id}/{name}` | 各格式輸出下載 |

操作亮點：

* 拖放上傳，即時顯示切塊數、已完成 chunk、當前台階（探測 → 轉碼 → 靜音偵測 → 排程 → 轉錄 → 摘要 → 寫檔）。
* 放置區同時接受影音與文件：放文件（可多份）就是「從文件產出會議紀錄」；另有「過往會議紀錄（範本）」欄位，選了就先上傳解析，讀不到當場顯示原因。
* 分頁：會議紀錄（有的話）/ 摘要 / 逐字稿（文件工作稱「原文」）/ 原始 JSON；會議紀錄可一鍵複製 Markdown 或下載；逐字稿可搜尋，**點任何時間戳會跳播放器對應位置**。
* 摘要含章節大綱，章節時間一樣可點擊跳轉。
* 上傳當下可調：摘要風格、摘要語言、ASR 語言、切塊長度、併行數、是否做章節。
* 結果頁可用 `#/job/<工作編號>` 深連結分享；支援取消與歷史工作（服務重啟後仍可查看與下載）。

前端靜態檔的尋找順序：`--static` 參數 → 環境變數 `SUMMERY_WEB_STATIC` → 同 repo 的 `frontend/dist` → 套件內建的陽春介面（`src/summery/web_static/`）。換言之，**裝好 wheel 之後不再需要 Node.js**：`frontend/dist` 建好後就是一份純靜態檔。

> 預設綁定 `127.0.0.1`。要開放給同事用，請自行放在有 TLS 與身分驗證的反向代理之後。

---

## 前端開發（Vite）

`frontend/` 是 Vite 7 + React 19 + TypeScript 的專案，視覺走「紙張與墨水」方向：米色紙感底色、襯線標題（Lora／Noto Serif TC）、琥珀色重點，沒有深色背景與霓虹光。

```bash
cd frontend
pnpm install          # 或 npm install
pnpm run dev          # 開發伺服器（預設 5173，/api 自動反代到 8765）
pnpm run build        # 產出 frontend/dist
pnpm run typecheck    # TS 型別檢查
```

* 開發模式連非預設埠的後端：`SUMMERY_API_ORIGIN=http://127.0.0.1:9000 pnpm run dev`（`./deploy.sh --dev` 已自動處理）。
* 用 pnpm 時，首次安裝會擋下未核准的建置腳本，這裡只需要 `esbuild`：`pnpm approve-builds` 勾選 esbuild（repo 根目錄的 `pnpm-workspace.yaml` 已寫好 `onlyBuiltDependencies`）。
* 介面品質檢查（需要本機 Chrome，會實際渲染並量測）：

  ```bash
  node frontend/scripts/ui-check.mjs http://127.0.0.1:8765/
  ```

  它會檢查 1440／834／375 三種寬度是否溢出、有無水平捲動、按鈕觸控尺寸、文字對比是否達 WCAG AA，並實際點分頁、打關鍵字搜尋、點時間戳確認 `<audio>` 真的跳轉，截圖存進 `.ui_shots/`。

### 開發模式（前後端分離）

```bash
./deploy.sh --dev --port 8765
# Python API → http://127.0.0.1:8765   （改後端重啟即可）
# Vite       → http://127.0.0.1:5173   （改前端立即熱更新，/api 自動反代）
```

任一端掛掉，腳本會把另一端一起收掉，不會出現「一半還在跑」。

> macOS 小知識：若你的 `node` 是某套裝環境內附的二進位，可能遇到 `Cannot find module @rollup/rollup-darwin-arm64` 或 `ERR_DLOPEN_FAILED`（訊息含 `different Team IDs`）——那是該 node 的碼簽章與 rollup 預編譯檔不同團隊所致，改用官方或 Homebrew 安裝的 `node` 即可；**正式模式（`./deploy.sh`）完全不需要 node**，不會碰到這個問題。

---

## 斷點續傳與快取

所有中間結果放在 `work_dir`（預設 `.summery_work/<job_id>/`）：

```
chunks/    chunk_00000_0.wav …      轉碼後的待傳塊（預設跑完即清）
results/   chunk_0.json …           每塊的 ASR 結果＝快取本體
plan.json  state.json  job.json     切塊計畫、進度、中繼資料
```

* `job_id` 由「來源絕對路徑 + 檔案大小 + 切塊/音訊參數 + ASR 模型」雜湊而來 → 同一檔重跑會自動沿用快取。
* 每個 `chunk_*.json` 內含 `signature = 起點-終點`；切塊參數一改，簽名對不上就自動重跑該塊。
* 因此：**摘要失敗/想換風格重跑，不會重燒一次 ASR**（`summery summarize` 更是不打 ASR）。
* 想要全部重轉：`--no-resume`。想保留 chunk 檔：`--keep-chunks`。
* 部分 chunk 失敗時預設仍輸出可用部分（回傳碼 2，摘要會標註「不完整」）；CI 想要硬失敗就加 `--strict`。

---

## 疑難排解

| 狀況 | 檢查 |
|---|---|
| `找不到 ffmpeg` | 安裝 ffmpeg，或設定 `ffmpeg_bin`／`SUMMERY_FFMPEG_BIN` 為絕對路徑 |
| 401 / 403 | 金鑰與 `base_url` 是否同一個服務；Azure 要帶 `?api-version=` |
| 「超過上傳上限」且反覆拆分仍失敗 | 調高 `audio.bitrate` 反而更糟→調低 `chunking.target_seconds`，或確認 Gateway 的真實上限 |
| `HTTP 413 Request Entity Too Large` | Gateway／nginx 的請求體上限（預設常是 1MB），跟 whisper 寫的 25MB 無關。把 `asr.max_upload_bytes` 設成略低於它（例 `900000`），規劃階段就會自動把每塊縮短；真的撞上了，管線也會把該塊二分重送而不是整個工作失敗 |
| 429 很多 | 降 `--concurrency`、提高 `asr.max_retries`／`retry_backoff`（有 `Retry-After` 會自動遵守） |
| 逐字稿在切塊邊界重複或漏字 | 調大 `--overlap`（3–5），並確認沒有 `--no-silence-scan` |
| 找不到靜音點、全部硬切 | `--set chunking.silence_noise_db=-25` 或加大 `silence_min_duration`；除錯看 `--log-level DEBUG` |
| 摘要只講開頭內容 | 檢查 `summary.json` 的 `meta.chapters_source`：`repaired_from_notes` 表示模型回傳的章節範圍不足，系統已用區塊筆記重建；可調小 `summarize.map_chunk_tokens` 讓筆記更均勻 |
| `HTTP 400 Invalid or unsupported audio file` | 該端點不支援這個 chunk 格式，把 `audio.codec` 改成 `mp3` 或 `wav` |
| 摘要只有標題、TL;DR 是空的 | 推理型模型把 `max_output_tokens` 吃光（`finish_reason=length`）。調高 `llm.max_output_tokens`（例 8000）或改非推理型模型；`summary.json` 的 `meta.warnings` 會寫明 |
| 逐字稿夾雜簡體字 | Whisper 本身的行為：固定 `asr.language = "zh"`、必要時用 `asr.extra_params` 傳 Gateway 支援的語言/繁簡參數 |
| 摘要出現 `mock` 字樣 | 你連到了 `mock-server`，把 `base_url` 換回正式服務 |
| 想看細節 | `--log-level DEBUG`、`summery plan <media>` 先看切塊、`summery jobs` 看狀態 |

---

## 開發與測試

```bash
pip install -e ".[dev]"
python3 -m pytest -q              # 200 多個測試，含端到端（用內建 mock，免聯外網）
python3 -m pytest tests/test_pipeline.py -k resume -q
python3 -m pytest tests/test_deploy_script.py -q     # 一鍵部署／關終端即停止
```

測試涵蓋：切塊邊界與覆蓋率、跨塊時間戳合併與去重、HTTP 重試/429/`response_format` 降級、
map-reduce 觸發與章節時間接地、resume 快取命中、部分失敗與 strict、自適應二分、CLI 各子命令、
Web API 上傳/輪詢/下載/路徑穿越防護、靜態外殼與深連結回退、音訊 `Range` 串流（206/416）、
部署腳本的前臺執行與 SIGHUP 清理、文件解析（docx 標題／自動編號／表格、編碼判斷、字幕、PDF 經 markitdown、
康熙部首正規化）、範本分析快取與保底、依範本撰寫紀錄、文件／範本的 CLI 與 Web 端到端。
前端另有 `frontend/scripts/ui-check.mjs`（渲染級檢查，見「前端開發」）。

原始碼結構：

```
src/summery/
  config.py        TOML + 環境變數 + --set 三層設定
  media.py         ffprobe / ffmpeg（正規化、靜音偵測、切塊）
  chunking.py      靜音對齊切塊計畫與自適應二分
  asr.py           OpenAI 相容 /audio/transcriptions
  transcript.py    解析、跨塊合併、段落化、五種輸出
  llm.py           /chat/completions、JSON 模式降級、token 統計
  prompts.py       繁中提示詞（map / reduce / final / 範本分析 / 會議紀錄）
  summarizer.py    直路 vs map-reduce、章節接地、降級彙整
  documents.py     文件輸入：docx（zipfile+xml）、md/txt、srt/vtt、json、PDF（markitdown）
  minutes.py       過往紀錄範本分析、依範本撰寫會議紀錄（含不靠模型的保底）
  pipeline.py      工作編排、state.json、resume、部分失敗
  web.py           API + 靜態服務（Range 串流、SPA 回退）
  web_static/      內建陽春介面（沒有 frontend/dist 時使用）
  mock_provider.py 免 key 試跑與測試用的模擬 ASR/LLM

frontend/                       Vite + React 前端
  src/App.tsx                   主狀態機（上傳、輪詢、深連結、通知）
  src/components/               上傳卡、進度、摘要文件、會議紀錄（MarkdownView）、逐字稿、播放器、側欄
  src/styles.css                設計系統（紙感配色、字型、陰影、響應式）
  scripts/ui-check.mjs          渲染級品質檢查（需本機 Chrome）
deploy.sh                       一鍵啟動／關閉終端即停止
deploy.ps1 / deploy.cmd         Windows 版一鍵啟動（PowerShell／雙擊）
```

---

## 已知限制

* 只處理**單一音軌；多說話者分離（diarization）不在範圍內，需搭配支援 diarization 的 ASR 端點。
* 逐字稿時間戳精度來自 ASR；端點若不回傳 `verbose_json`，時間會以字數平均分攤。
* 摘要品質取決於模型；系統負責「時間正確、不漏段、不長出原文沒有的章節時間」，不保證內容判斷正確。
* 上傳與轉錄過程的中間檔會留在 `work_dir`/`jobs_dir`，涉及個資的媒體請自行管理保存與清除。
* PDF 需要 markitdown（另外安裝）；掃描影像的 PDF 沒有文字層，需先 OCR。舊版 `.doc`、`.odt`、`.pages` 請先另存成 `.docx`。
* `.docx` 解析涵蓋本文的段落、標題、自動編號、清單與表格；頁首頁尾、註腳、文字方塊內的版面不會保留。
* 網頁上暫存的文件放在 `jobs_dir/_documents/`，不會自動清除。

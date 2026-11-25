"""摘要用的提示詞模板（map / reduce / final / chapters）。"""

from __future__ import annotations

from typing import Sequence

_LANG_NAMES = {
    "zh-TW": "繁體中文",
    "zh-TW ": "繁體中文",
    "zh": "中文",
    "zh-CN": "簡體中文",
    "en": "English",
    "ja": "日本語",
    "auto": "",
}

_STYLE_HINT = {
    "concise": "精簡：TL;DR ≤100 字，overview ≤2 段，key_points ≤5 點。",
    "standard": "標準：TL;DR ≤150 字，overview 3～5 段，key_points 6～10 點。",
    "comprehensive": "完整：保留重要細節、數據、專有名詞與因果關係；overview 可分 5～8 段，key_points 10～18 點。",
    "bullets": "條列：以重點條列為主，overview 最多 1 段。",
}

_JSON_RULE = (
    "只輸出一個合法的 JSON 物件：不要 code fence、不要註解、不要多餘說明。"
    "字串內的引號必須正確跳脫；不可使用註解或尾隨逗號。"
)


def lang_label(language: str | None) -> str:
    if not language:
        return ""
    return _LANG_NAMES.get(language, language)


def _lang_rule(language: str | None) -> str:
    label = lang_label(language)
    if not label:
        return "輸出語言：追隨逐字稿使用的語言。"
    return f"輸出語言：一律使用{label}（專有名詞可保留原文）。"


def _style_rule(style: str) -> str:
    return _STYLE_HINT.get(style, _STYLE_HINT["standard"])


def _extra_rule(custom: str | None) -> str:
    return f"\n其他要求：{custom}" if custom else ""


# --------------------------------------------------------------------------
# map：單一區塊筆記
# --------------------------------------------------------------------------

MAP_SYSTEM_TMPL = """你是勝任的會議與影音內容分析助手。任務是把「逐字稿片段」整理成可供上層摘要使用的結構化筆記。
{lang}
原則：
1. 只使用逐字稿中出現的資訊，不推測、不補寫外部知識。
2. 保留具體的數字、名稱、時間、單位、因果與結論。
3. 口語重覆、贅詞、無意義填充詞不寫入筆記。
4. 若內容聽不清楚或明顯斷裂，在 issues 欄位說明，不要自行腦補。
{_JSON_RULE}
輸出格式：
{{"summary": "3～6 句的重點摘要", "key_points": ["重點 1", "..."], "terms": ["專有名詞或關鍵術語"], "issues": ["疑點或聽不清楚之處"]}}"""

MAP_USER_TMPL = """【時間區間】{clock_start} – {clock_end}（區塊 {block_no}/{block_total}）
【逐字稿】
{text}

請依系統提示輸出 JSON 筆記。"""


def map_prompt(
    text: str, *, clock_start: str, clock_end: str, block_no: int, block_total: int, language: str, style: str, custom: str | None = None
) -> tuple[str, str]:
    system = MAP_SYSTEM_TMPL.format(
        lang=_lang_rule(language), _JSON_RULE=_JSON_RULE
    ) + _extra_rule(custom)
    user = MAP_USER_TMPL.format(
        clock_start=clock_start, clock_end=clock_end, block_no=block_no, block_total=block_total, text=text
    )
    return system, user


# --------------------------------------------------------------------------
# reduce：多份筆記彙整
# --------------------------------------------------------------------------

REDUCE_SYSTEM_TMPL = """你是影音內容彙整助手。任務：把同一區段的「多份區塊筆記」合併成一份較高階的筆記，供最終摘要使用。
{lang}
原則：
1. 去除重覆，合併相近論點，保留細節與數據。
2. 時間順序不可混亂；各小節標示的時間要保留。
3. 不可新增筆記中沒有的資訊。
{_JSON_RULE}
輸出格式：
{{"summary": "整合後摘要（5～10 句）", "key_points": ["合併後的重點"], "terms": ["專有名詞"], "issues": ["仍需確認的疑點"]}}"""

REDUCE_USER_TMPL = """【涵蓋時間】{clock_start} – {clock_end}（彙整第 {round_no} 輪，共 {group_count} 組之 {group_index}）
【區塊筆記】
{notes}

請輸出合併後的 JSON 筆記。"""


def reduce_prompt(
    notes_text: str, *, clock_start: str, clock_end: str, round_no: int, group_index: int, group_count: int, language: str, style: str, custom: str | None = None
) -> tuple[str, str]:
    system = REDUCE_SYSTEM_TMPL.format(lang=_lang_rule(language), _JSON_RULE=_JSON_RULE) + _extra_rule(custom)
    user = REDUCE_USER_TMPL.format(
        clock_start=clock_start, clock_end=clock_end, round_no=round_no,
        group_index=group_index, group_count=group_count, notes=notes_text,
    )
    return system, user


# --------------------------------------------------------------------------
# final：完整摘要
# --------------------------------------------------------------------------

FINAL_SYSTEM_TMPL = """你是資深影音內容編輯。根據提供的逐字稿（或分區筆記）產出可供閱讀的最終摘要。
{lang}
風格：{style_rule}
原則：
1. 忠於原文，不加入外部知識；有爭議或聽不清楚者列入 open_questions。
2. 引用重要原話時放進 quotes（保留說者，若未知寫「未標明」）。
3. 決策（decisions）與待辦（action_items）只寫逐字稿中明確出現者，沒有就回傳空陣列。
4. chapters 必須使用「提供的編號」作為 para_start / para_end（整數，含頭含尾），不可自創編號。
   章節應涵蓋全場、彼此不重疊、時間遞增：第一段章節要從輸入的最前面開始，
   最後一個章節必須涵蓋到輸入的最後一個編號，不可只摘要開頭幾段。
   單章以不超過全場長度的三分之一為原則：長講堂或長會議通常應切出 5–10 章，
   不要因為輸入是幾份「分區筆記」就把整場擠成一兩章（章節編號範圍要細到實際主題）。
5. open_questions 每一筆都標成物件並附上 para（該問題在逐字稿中的段落編號，整數），
   讓讀者可以直接跳回原始音訊／影片核對；只有真的無法定位時才填 null。
{_JSON_RULE}
輸出格式：
{{"title": "簡潔標題",
 "tldr": "一句話總結",
 "overview": ["段落 1", "段落 2"],
 "key_points": ["重點"],
 "chapters": [{{"para_start": 1, "para_end": 4, "title": "小標", "summary": "該章節摘要", "points": ["重點"]}}],
 "keywords": ["關鍵字"],
 "decisions": ["已決定事項"],
 "action_items": [{{"task": "待辦", "owner": "負責人或『未標明』", "due": "期限或『未標明』"}}],
 "open_questions": [{{"text": "待釐清問題", "para": 編號或 null}}],
 "quotes": [{{"text": "原話", "speaker": "說者", "para": 編號或 null}}]}}"""

FINAL_USER_TMPL = """【媒體長度】{duration}（逐字稿約 {chars} 字）
【輸入形態】{input_kind}
【內容】
{text}

請輸出最終摘要 JSON。"""

FINAL_USER_NOTES_TMPL = """【媒體長度】{duration}（逐字稿約 {chars} 字，全部共 {para_total} 個編號段落）
【輸入形態】分區筆記（由長逐字稿逐段彙整而來；每份筆記都標明「段落 a-b」）
【章節編號規則】可引用的段落編號範圍是 1 ～ {para_total}。
  ・chapters 的 para_start / para_end 必須取自各份筆記標示的「段落 a-b」範圍。
  ・章節要依時間順序涵蓋全場：第一章應從接近 1 開始，最後一章的 para_end 必須接近 {para_total}。
【分區筆記】
{notes}

【開頭逐字稿（供校準語氣與主題）】
{head}

【結尾逐字稿（供校準結論）】
{tail}

請輸出最終摘要 JSON。"""


def final_prompt(
    text: str, *, duration: str, chars: int, language: str, style: str, custom: str | None = None
) -> tuple[str, str]:
    system = FINAL_SYSTEM_TMPL.format(
        lang=_lang_rule(language), style_rule=_style_rule(style), _JSON_RULE=_JSON_RULE
    ) + _extra_rule(custom)
    user = FINAL_USER_TMPL.format(duration=duration, chars=chars, text=text, input_kind="完整逐字稿")
    return system, user


def final_prompt_notes(
    notes_text: str, *, duration: str, chars: int, para_total: int, head: str, tail: str,
    language: str, style: str, custom: str | None = None,
) -> tuple[str, str]:
    system = FINAL_SYSTEM_TMPL.format(
        lang=_lang_rule(language), style_rule=_style_rule(style), _JSON_RULE=_JSON_RULE
    ) + _extra_rule(custom)
    user = FINAL_USER_NOTES_TMPL.format(
        duration=duration, chars=chars, para_total=para_total, notes=notes_text,
        head=head or "（無）", tail=tail or "（無）", input_kind="分區筆記",
    )
    return system, user


# --------------------------------------------------------------------------
# 標題（選用，便宜且有用）
# --------------------------------------------------------------------------

TITLE_SYSTEM_TMPL = """根據內容给出 1 個不超過 24 字的標題。{lang}
輸出：{{"title": "..."}}  僅此 JSON。"""


def title_prompt(text: str, *, language: str) -> tuple[str, str]:
    return TITLE_SYSTEM_TMPL.format(lang=_lang_rule(language)), text[:6000]

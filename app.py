import streamlit as st
import google.generativeai as genai
import json
import os
import re
from datetime import datetime, timedelta, timezone
from google.oauth2.service_account import Credentials
import gspread

# ✅ 追加（要約高速化のため）
import io
import hashlib
try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

# --- 画面設定 ---
st.set_page_config(page_title="PDF要約＆クイズ生成ツール", page_icon="🎓", layout="wide")
JST = timezone(timedelta(hours=+9), 'JST')

# --- Googleスプレッドシート連携 ---
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    credentials = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=scopes)
    return gspread.authorize(credentials)

# ✅ 追加：archived列を保証（無ければヘッダーに追加）
def ensure_archived_column(sheet):
    try:
        headers = sheet.row_values(1)
        if "archived" not in headers:
            sheet.update_cell(1, len(headers) + 1, "archived")
    except:
        pass

def load_history_from_gs(user_id):
    try:
        client = get_gspread_client()
        sheet = client.open("study_history_db").sheet1
        ensure_archived_column(sheet)  # ✅ 追加
        records = sheet.get_all_records()

        user_history = []
        for r in records:
            if str(r.get("user_id")) == str(user_id):
                # ✅ 追加：アーカイブはロードはする（表示側でフィルタもできるが一応残す）
                q_data = r.get("quiz_data", "[]")
                if isinstance(q_data, str):
                    try:
                        q_data = json.loads(q_data)
                    except:
                        q_data = []
                user_history.append({
                    "date": r.get("date"),
                    "title": r.get("title", "無題"),
                    "score": r.get("score"),
                    "correct": r.get("correct"),
                    "total": r.get("total"),
                    "quiz_data": q_data,
                    "summary_data": r.get("summary_data"),
                    "archived": r.get("archived", False)  # ✅ 追加
                })
        return user_history
    except:
        return []

def save_history_to_gs(user_id, log_entry):
    try:
        client = get_gspread_client()
        sheet = client.open("study_history_db").sheet1
        ensure_archived_column(sheet)  # ✅ 追加

        row = [
            user_id, log_entry["date"], log_entry.get("title", "無題"),
            log_entry["score"], log_entry["correct"], log_entry["total"],
            json.dumps(log_entry["quiz_data"], ensure_ascii=False),
            log_entry.get("summary_data", "")
        ]

        # ✅ 追加：archived列分を末尾に付与（新規は未アーカイブ）
        row.append(False)

        sheet.append_row(row)
    except Exception as e:
        st.error(f"保存エラー: {e}")

def update_title_in_gs(user_id, date_str, new_title):
    try:
        client = get_gspread_client()
        sheet = client.open("study_history_db").sheet1
        ensure_archived_column(sheet)  # ✅ 追加
        records = sheet.get_all_records()
        for idx, r in enumerate(records):
            if str(r.get("user_id")) == str(user_id) and str(r.get("date")) == str(date_str):
                sheet.update_cell(idx + 2, 3, new_title)
                return True
        return False
    except:
        return False

def clear_history_from_gs(user_id):
    try:
        client = get_gspread_client()
        sheet = client.open("study_history_db").sheet1
        ensure_archived_column(sheet)  # ✅ 追加

        cells = sheet.findall(str(user_id))
        rows_to_delete = sorted(list(set([cell.row for cell in cells])), reverse=True)
        for row_idx in rows_to_delete:
            if str(sheet.cell(row_idx, 1).value) == str(user_id):
                sheet.delete_rows(row_idx)
        return True
    except:
        return False

# ✅ 変更：削除ではなく「アーカイブ」(行は残す)
def archive_one_history_in_gs(user_id, date_str):
    try:
        client = get_gspread_client()
        sheet = client.open("study_history_db").sheet1
        ensure_archived_column(sheet)

        headers = sheet.row_values(1)
        archived_col = headers.index("archived") + 1

        records = sheet.get_all_records()
        for idx, r in enumerate(records):
            if str(r.get("user_id")) == str(user_id) and str(r.get("date")) == str(date_str):
                sheet.update_cell(idx + 2, archived_col, True)
                return True
        return False
    except:
        return False

# --- セッション初期化 ---
for key in ['user_id', 'quiz_history', 'current_quiz', 'results', 'summary', 'current_date', 'edit_mode']:
    if key not in st.session_state:
        st.session_state[key] = None if key != 'quiz_history' and key != 'results' else ([] if key == 'quiz_history' else {})
        if key == 'edit_mode':
            st.session_state[key] = False

if 'current_title' not in st.session_state:
    st.session_state['current_title'] = "無題のクイズ"

# 追加：モデル名キャッシュ、採点後フラグ（表示安定用）
if 'model_name' not in st.session_state:
    st.session_state['model_name'] = None
if 'last_wrong_questions' not in st.session_state:
    st.session_state['last_wrong_questions'] = []
if 'show_retry' not in st.session_state:
    st.session_state['show_retry'] = False

# ✅ 追加：履歴個別アーカイブの誤爆防止用（対象保持）
if 'pending_delete' not in st.session_state:
    st.session_state['pending_delete'] = None

# ✅ 追加：アーカイブ表示ON/OFF（デフォルトOFF）
if 'show_archived' not in st.session_state:
    st.session_state['show_archived'] = False

# --- 🎨 CSS: デザイン設定 (修正版) ---
st.markdown("""
    <style>
    /* サイドバー履歴ボタン */
    div[data-testid="stSidebar"] .stButton button[kind="secondary"] div p {
        white-space: pre-wrap !important;
        line-height: 1.4 !important;
        text-align: left !important;
        font-size: 0.9rem !important;
    }
    div[data-testid="stSidebar"] .stButton button[kind="secondary"] {
        height: auto !important;
        padding: 8px 10px !important;
        border: 1px solid #ddd !important;
        border-left: 5px solid #4CAF50 !important;
        border-radius: 6px !important;
        text-align: left !important;
        background-color: #f9f9f9 !important;
        color: #333 !important;
    }
    div[data-testid="stSidebar"] .stButton button[kind="secondary"]:hover {
        background-color: #e6f7ff !important;
        border-color: #4CAF50 !important;
    }

    /* 💡 問題文ボックス (Flexboxでレイアウト安定化) */
    .question-box {
        display: flex; /* 横並びにする */
        align-items: flex-start; /* 上端で揃える */
        background-color: #f0f8ff;
        border-left: 4px solid #0078d7;
        padding: 12px 15px;
        border-radius: 4px;
        margin-bottom: 8px;
        margin-top: 8px;
        box-shadow: 0 1px 2px rgba(0,0,0,0.05);
    }
    .question-number {
        font-weight: bold;
        color: #0078d7;
        margin-right: 12px; /* 本文との間隔 */
        font-size: 1.1em;
        flex-shrink: 0; /* 番号の幅が縮まないように固定 */
        line-height: 1.6; /* 本文の行間と合わせる */
    }
    .question-text {
        color: #2c3e50;
        white-space: pre-wrap; /* 改行をそのまま表示 */
        line-height: 1.6;
        flex-grow: 1; /* 残りの幅を全部使う */
        word-wrap: break-word; /* 長い単語も折り返す */
    }
    </style>
""", unsafe_allow_html=True)

st.title("🎓 PDF要約＆クイズ生成ツール")

# --- APIキー ---
# 💡 APIキーを直書きしないように修正（ここは雨音の最新版をそのまま）
if "GEMINI_API_KEY" in st.secrets:
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key.strip())
else:
    st.error("APIキーが設定されていません。secrets.tomlにGEMINI_API_KEYを設定してください。")
    st.stop()

# --- サイドバー ---
with st.sidebar:
    st.header("👤 ログイン")
    user_input = st.text_input("ユーザー名", value=st.session_state['user_id'] or "")
    if st.button("ログイン / 切り替え", key="login_btn", type="primary"):
        if user_input:
            st.session_state['user_id'] = user_input
            with st.spinner("同期中..."):
                st.session_state['quiz_history'] = load_history_from_gs(user_input)
            st.session_state['pending_delete'] = None
            st.rerun()

    st.divider()

    # ✅ 入れ替え：先にPDFアップロード
    uploaded_files = st.file_uploader("PDFをアップロード", type=["pdf"], accept_multiple_files=True)

    st.divider()

    # ✅ 入れ替え：後に履歴
    if st.session_state['user_id'] and st.session_state['quiz_history']:
        st.header("📊 履歴")

        # ✅ 追加：アーカイブ表示ON/OFF
        st.checkbox("アーカイブも表示", value=st.session_state.get("show_archived", False), key="show_archived")

        # ✅ 変更：アーカイブはトグルで表示切替
        if st.session_state.get("show_archived"):
            visible_history = list(st.session_state['quiz_history'])
        else:
            visible_history = [h for h in st.session_state['quiz_history'] if not h.get("archived", False)]

        for i, log in enumerate(reversed(visible_history)):
            d = log.get('date', '')
            t = log.get('title', '無題')
            s = log.get('score', 0)
            btn_label = f"📅 {d}\n📝 {t}\n🎯 正解率: {s}%"

            # ✅ 誤爆防止：履歴ボタン + ゴミ箱ボタンを横並び（※UIはそのまま、動作だけアーカイブ）
            c_hist, c_del = st.columns([8, 2])

            with c_hist:
                if st.button(btn_label, key=f"hist_{i}", use_container_width=True, type="secondary"):
                    st.session_state['current_quiz'] = log['quiz_data']
                    st.session_state['summary'] = log['summary_data']
                    st.session_state['current_title'] = t
                    st.session_state['current_date'] = log.get('date')
                    st.session_state['edit_mode'] = False
                    st.session_state['results'] = {}
                    st.session_state['show_retry'] = False
                    st.session_state['last_wrong_questions'] = []
                    st.session_state['pending_delete'] = None
                    st.rerun()

            with c_del:
                # 1段階目：アーカイブ候補にセット
                if st.button("✔️", key=f"del_hist_{i}", use_container_width=True):
                    st.session_state['pending_delete'] = {"date": d, "title": t}
                    st.rerun()

            # 2段階目：確認UI（該当の履歴の直下に表示）
            pending = st.session_state.get('pending_delete')
            if pending and pending.get("date") == d:
                st.warning(f"この履歴をアーカイブしますか？（非表示になりますがデータは残ります）\n\n📅 {d}\n📝 {t}")

                c_yes, c_no = st.columns(2)
                with c_yes:
                    if st.button("アーカイブ", key=f"confirm_del_{i}", use_container_width=True, type="primary"):
                        ok = archive_one_history_in_gs(st.session_state['user_id'], d)
                        st.session_state['pending_delete'] = None
                        if ok:
                            st.session_state['quiz_history'] = load_history_from_gs(st.session_state['user_id'])
                            st.session_state['show_retry'] = False
                            st.session_state['last_wrong_questions'] = []
                            st.rerun()
                        else:
                            st.error("アーカイブに失敗しました。")
                with c_no:
                    if st.button("キャンセル", key=f"cancel_del_{i}", use_container_width=True):
                        st.session_state['pending_delete'] = None
                        st.rerun()

        st.markdown("---")
        if st.button("🗑️ 履歴を全削除", use_container_width=True):
            if clear_history_from_gs(st.session_state['user_id']):
                st.session_state['quiz_history'] = []
                st.session_state['pending_delete'] = None
                st.rerun()

# --- ここから追加の“壊れにくくする”関数（UI/構造は触らない） ---
def parse_json_safely(res_text: str):
    """LLM出力からJSONをできるだけ安全に抽出"""
    t = (res_text or "").strip()
    # コードブロック除去
    t = t.replace("```json", "```").replace("```", "")
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("JSONが見つかりません")
    return json.loads(t[start:end+1])

def norm_answer(s: str) -> str:
    """採点用：表記ゆれを軽減（空白/記号/全角空白など）"""
    s = str(s).strip().lower()
    s = s.replace("　", "")
    s = re.sub(r"\s+", "", s)
    s = s.replace("・", "").replace("、", "").replace("。", "")
    return s

# ✅ 追加：要約の「前置き」や「巨大見出し」を削除
def clean_summary_output(text: str) -> str:
    """要約出力の前置き・不要な見出しを削る（UI/構造に触れない）"""
    t = (text or "").strip()

    if not t:
        return t

    lines = t.splitlines()

    # 1) もし「# 要点」があるなら、そこより前は全部捨てる（最強・確実）
    for i, line in enumerate(lines):
        if re.match(r'^\s*#\s*要点\s*$', line.strip()):
            lines = lines[i:]
            return "\n".join(lines).strip()

    # 2) 「# 要点」が無い場合の保険：先頭の前置き/見出しっぽい行を削る
    def is_preface_or_title(s: str) -> bool:
        s0 = s.strip()
        if not s0:
            return True
        # 例: "要約" だけ / 絵文字付きなど
        if re.fullmatch(r'(📋\s*)?要約', s0):
            return True
        # 承知しました系 + 要約します系
        if re.search(r'承知(いた|し)ました', s0):
            return True
        if re.search(r'PDF資料.*要約', s0):
            return True
        # 「・」区切りの巨大タイトルっぽい1行（箇条書きではない）
        if ("・" in s0) and (not s0.startswith("-")) and (len(s0) >= 12):
            return True
        return False

    # 先頭から、前置き/タイトルっぽい行を連続で削る（最大10行まで）
    cut = 0
    for _ in range(min(10, len(lines))):
        if is_preface_or_title(lines[0]):
            lines.pop(0)
            cut += 1
            continue
        break

    return "\n".join(lines).strip()

    return t

# ✅ 追加：問題削除/追加後に入力ウィジェットをリセット
def reset_quiz_input_widgets():
    for k in list(st.session_state.keys()):
        if k.startswith("r_") or k.startswith("t_"):
            st.session_state.pop(k, None)
    st.session_state['results'] = {}

# --- AI処理 ---
def get_available_model():
    # 💡 指定のモデルリスト（全部入れた版）
    candidates = [
        'gemini-3-pro-preview',
        'gemini-3-flash-preview',
        'gemini-2.5-pro',
        'gemini-2.5-pro-tts',
        'gemini-2.5-flash',
        'gemini-2.5-flash-preview',
        'gemini-2.5-flash-image-preview',
        'gemini-2.5-flash-tts',
        'gemini-2.5-flash-lite',
        'gemini-2.5-flash-lite-preview',
        'gemini-2.0-flash',
        'gemini-2.0-flash-lite',
    ]

    # 追加：前回成功モデルを優先（毎回試行で遅くなるのを防ぐ）
    cached = st.session_state.get("model_name")
    if cached:
        try:
            return genai.GenerativeModel(cached)
        except:
            st.session_state["model_name"] = None

    for m in candidates:
        try:
            mod = genai.GenerativeModel(m)
            mod.generate_content("test", generation_config={"max_output_tokens": 1})
            st.session_state["model_name"] = m
            return mod
        except:
            continue
    return None

# ✅ 追加（要約高速化のため）：要約専用のモデルを固定 + Streamlitでリソースキャッシュ
@st.cache_resource(show_spinner=False)
def get_summary_model():
    # 内容が薄くならない速度×品質のバランス：ここを固定（候補総当たりを回避）
    return genai.GenerativeModel("gemini-2.0-flash")

# ✅ 追加（要約高速化のため）：PDFからテキスト抽出（できる範囲で）
@st.cache_data(show_spinner=False)
def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        texts = []
        for page in reader.pages:
            t = page.extract_text() or ""
            if t.strip():
                texts.append(t)
        return "\n\n".join(texts)
    except:
        return ""

# ✅ 追加：生成が途中で切れたときに「続きを取りに行って結合」する（要約/クイズ共通で使用）
def generate_with_continuation(model, content, generation_config, max_rounds=3):
    text_parts = []
    last_text = ""

    for _ in range(max_rounds):
        res = model.generate_content(content, generation_config=generation_config)
        part = getattr(res, "text", "") or ""
        if part:
            text_parts.append(part)
            last_text = part

        finish_reason = None
        try:
            finish_reason = res.candidates[0].finish_reason
        except:
            finish_reason = None

        if str(finish_reason) not in ("MAX_TOKENS", "FinishReason.MAX_TOKENS"):
            break

        if not last_text.strip():
            break

        content = [
            "今の出力の続きを、重複なしでそのまま出してください。見出しや箇条書きの体裁は維持してください。"
        ]

    # ✅ 変更：最後に要約の前置き/巨大見出しを除去
    return clean_summary_output("\n".join([p.strip() for p in text_parts if p.strip()]).strip())

# ✅ 追加（要約高速化のため）：同じ入力なら要約結果をキャッシュ
@st.cache_data(show_spinner=False)
def summarize_text_cached(text: str) -> str:
    model = get_summary_model()
    prompt = """あなたは学習用の資料要約が得意なアシスタントです。
以下の資料テキストを、復習しやすい形で日本語で要約してください。

【最重要：出力はこの形式に厳密に従う】
## 要約
ご提示いただいた資料は、（資料のテーマを1行で）
主要な要点を（3〜5）項目に整理して分かりやすく要約します。

### 1. （項目名）
（短い説明を1〜2文）
- （要点）
- （要点）
- （要点）

### 2. （項目名）
（短い説明を1〜2文）
- （要点）
- （要点）
- （要点）

### 3. （項目名）
（短い説明を1〜2文）
- （要点）
- （要点）
- （要点）

【ルール】
- 「はい、承知しました」などの前置きは禁止
- 章番号は必ず「### 1.」「### 2.」形式
- 各章は「短い説明1〜2文 + 箇条書き3〜6個」
- 数字・条件・例外・手順は落とさない
- 余計な結論や感想は禁止
"""

    return generate_with_continuation(
        model=model,
        content=[prompt, text],
        generation_config={
            "max_output_tokens": 2400,
            "temperature": 0.25,
        },
        max_rounds=3
    )

def generate_summary(files):
    # ✅ ここだけ改善（他は触らない）
    # 1) PDFをテキスト化できるならテキストで要約（速い＋内容も出せる）
    # 2) テキスト化できないPDFは従来通りPDFを投げる（互換性）
    try:
        texts = []
        pdf_payloads = []
        for f in files:
            b = f.getvalue()
            t = extract_text_from_pdf_bytes(b)
            if t.strip():
                texts.append(t)
            else:
                pdf_payloads.append({"mime_type": "application/pdf", "data": b})

        # テキストが取れた分はまとめてキャッシュ要約
        if texts:
            joined = "\n\n---\n\n".join(texts)

            # テキスト要約（キャッシュ効く）
            with st.spinner("要約中..."):
                base_summary = summarize_text_cached(joined)
        else:
            base_summary = ""

        # 画像PDFなどテキスト化できない分がある場合だけフォールバック
        if pdf_payloads:
            model = get_summary_model()
            content = ["""あなたは学習用の資料要約が得意なアシスタントです。
PDF資料を日本語で要約してください。

【要約ルール】
- 重要点を落とさずに、情報量は“簡潔に”（わかりやすさを重視）
- 見出し + 箇条書き中心で構造化する
- 数字・条件・例外・手順があれば必ず残す
"""] + pdf_payloads

            with st.spinner("要約中..."):
                pdf_summary = generate_with_continuation(
                    model=model,
                    content=content,
                    generation_config={
                        "max_output_tokens": 2400,
                        "temperature": 0.25,
                    },
                    max_rounds=3
                )
            if base_summary and pdf_summary:
                return base_summary + "\n\n---\n\n" + pdf_summary
            return pdf_summary or base_summary

        return base_summary or None
    except:
        return None

# ✅ 追加：クイズ生成も「PDF→テキスト化→テキストで作る」を優先（速い）
@st.cache_data(show_spinner=False)
def build_quiz_cached(text: str) -> dict:
    model = get_summary_model()  # ここも固定モデルで高速化（候補総当たり回避）
    prompt = """あなたは学習用の確認テストを作るのが得意なアシスタントです。
以下の資料テキストからクイズ15問をJSONで出力してください。

【重要】
- 記述式や穴埋め問題の場合、optionsは必ず空リスト[]にすること。
- 出力はJSONのみ。前後に説明文やコードブロックは付けないこと。
- 問題は「暗記」だけでなく「理解」も問う（要件・例外・比較・因果・手順など）。
- explanationは短すぎない（1〜3文）。

【JSON形式】
{"title": "タイトル", "quizzes": [{"question": "..", "options": ["..", ".."], "answer": "..", "explanation": ".."}]}
"""
    res_text = generate_with_continuation(
        model=model,
        content=[prompt, text],
        generation_config={
            "max_output_tokens": 2400,
            "temperature": 0.3,
        },
        max_rounds=2
    )
    return parse_json_safely(res_text)

def start_quiz_generation(files):
    # ✅ ここだけ改善（他は触らない）
    # 1) テキスト抽出できるPDFはテキストでクイズ生成（速い）
    # 2) テキスト抽出できないPDFだけ従来通りPDFを投げる
    try:
        texts = []
        pdf_payloads = []
        for f in files:
            b = f.getvalue()
            t = extract_text_from_pdf_bytes(b)
            if t.strip():
                texts.append(t)
            else:
                pdf_payloads.append({"mime_type": "application/pdf", "data": b})

        if texts:
            joined = "\n\n---\n\n".join(texts)
            with st.spinner("クイズ作成中..."):
                data = build_quiz_cached(joined)
            return data.get("title", "無題"), data.get("quizzes", [])

        # フォールバック：画像PDFなどはPDFを投げる（互換）
        model = get_summary_model()
        prompt = """PDFからクイズ10問をJSONで出力。
【重要】記述式や穴埋め問題の場合、optionsは必ず空リスト[]にすること。
【重要】出力はJSONのみ。前後に説明文やコードブロックは付けないこと。
{"title": "タイトル", "quizzes": [{"question": "..", "options": ["..", ".."], "answer": "..", "explanation": ".."}]}"""
        content = [prompt] + pdf_payloads

        with st.spinner("クイズ作成中..."):
            res_text = generate_with_continuation(
                model=model,
                content=content,
                generation_config={
                    "max_output_tokens": 2400,
                    "temperature": 0.3,
                },
                max_rounds=2
            )
            data = parse_json_safely(res_text)
            return data.get("title", "無題"), data.get("quizzes", [])
    except:
        return "無題", []

# --- メインロジック ---
if uploaded_files:
    c1, c2 = st.columns(2)
    with c1:
        if st.button("📝 資料を要約する", use_container_width=True):
            st.session_state['summary'] = generate_summary(uploaded_files)
    with c2:
        if st.button("🚀 クイズを生成", use_container_width=True, type="primary"):
            t, q = start_quiz_generation(uploaded_files)
            st.session_state.update({"current_title": t, "current_quiz": q, "results": {}, "current_date": None, "edit_mode": False})
            st.session_state['show_retry'] = False
            st.session_state['last_wrong_questions'] = []
            st.rerun()

if st.session_state['summary']:
    st.info(f"### 📋 要約\n{st.session_state['summary']}")

if st.session_state['current_quiz']:
    st.divider()

    # 題名編集エリア
    col_title, col_btn = st.columns([8, 2])
    with col_title:
        if st.session_state['edit_mode']:
            new_title_input = st.text_input("題名編集", value=st.session_state['current_title'], label_visibility="collapsed")
        else:
            st.subheader(f"📖 {st.session_state['current_title']}")
    with col_btn:
        if st.session_state['edit_mode']:
            if st.button("💾 保存", use_container_width=True):
                if st.session_state['current_date'] and st.session_state['user_id']:
                    update_title_in_gs(st.session_state['user_id'], st.session_state['current_date'], new_title_input)
                    st.session_state['quiz_history'] = load_history_from_gs(st.session_state['user_id'])
                st.session_state['current_title'] = new_title_input
                st.session_state['edit_mode'] = False
                st.rerun()
        else:
            if st.button("✏️ 題名を変更", use_container_width=True):
                st.session_state['edit_mode'] = True
                st.rerun()

    # ✅ 追加：問題削除 & 手動追加（ここだけ差し込み。既存は触らない）
    with st.expander("🛠️ 問題の編集（削除 / 手動追加）", expanded=False):
        # --- 削除UI ---
        st.markdown("### 🗑️ 問題を削除（AIがミスった時）")
        options = []
        for i, q in enumerate(st.session_state['current_quiz']):
            qtext = (q.get("question", "") or "").replace("\n", " ")
            if len(qtext) > 30:
                qtext = qtext[:30] + "..."
            options.append(f"Q{i+1}: {qtext}")

        del_selected = st.multiselect("削除する問題を選択", options, key="del_selected")

        if st.button("🗑️ 選択した問題を削除", type="secondary", use_container_width=True, key="del_btn"):
            idxs = []
            for s in del_selected:
                try:
                    n = int(s.split(":")[0].replace("Q", "").strip())
                    idxs.append(n - 1)
                except:
                    pass

            idxs = sorted(set([i for i in idxs if 0 <= i < len(st.session_state['current_quiz'])]), reverse=True)
            for i in idxs:
                st.session_state['current_quiz'].pop(i)

            reset_quiz_input_widgets()
            st.session_state['show_retry'] = False
            st.session_state['last_wrong_questions'] = []
            st.rerun()

        st.markdown("---")

        # ✅ 追加：問題の編集（既存問題を修正）
        st.markdown("### ✏️ 既存の問題を編集")
        if st.session_state['current_quiz']:
            edit_options = []
            for i, q in enumerate(st.session_state['current_quiz']):
                qtext = (q.get("question", "") or "").replace("\n", " ")
                if len(qtext) > 30:
                    qtext = qtext[:30] + "..."
                edit_options.append(f"Q{i+1}: {qtext}")

            selected = st.selectbox("編集する問題を選択", edit_options, key="edit_selectbox")

            # 選択されたインデックス
            try:
                edit_idx = int(selected.split(":")[0].replace("Q", "").strip()) - 1
            except:
                edit_idx = 0

            # 選択が変わったらフォーム値を詰め直す（同一run内で反映）
            if 'edit_last_idx' not in st.session_state:
                st.session_state['edit_last_idx'] = None

            if st.session_state['edit_last_idx'] != edit_idx:
                q0 = st.session_state['current_quiz'][edit_idx]
                st.session_state['edit_q_text'] = q0.get("question", "")
                st.session_state['edit_ans_text'] = q0.get("answer", "")
                st.session_state['edit_exp_text'] = q0.get("explanation", "")

                opts0 = q0.get("options", [])
                is_choice = bool(opts0 and isinstance(opts0, list) and len(opts0) >= 2)
                st.session_state['edit_mode_radio'] = "選択式（optionsあり）" if is_choice else "記述式（optionsなし）"
                st.session_state['edit_opts_text'] = "\n".join([str(x) for x in opts0]) if is_choice else ""
                st.session_state['edit_last_idx'] = edit_idx

            edit_q = st.text_area("問題文（編集）", key="edit_q_text", height=80)
            edit_mode = st.radio("形式（編集）", ["記述式（optionsなし）", "選択式（optionsあり）"], horizontal=True, key="edit_mode_radio")

            edit_opts_raw = ""
            if edit_mode == "選択式（optionsあり）":
                edit_opts_raw = st.text_area(
                    "選択肢（編集）（1行1つ / またはカンマ区切り）",
                    key="edit_opts_text",
                    height=90
                )

            edit_ans = st.text_input("正解（answer）（編集）", key="edit_ans_text")
            edit_exp = st.text_area("解説（explanation）（編集）", key="edit_exp_text", height=80)

            c_save, c_dup, c_cancel = st.columns([4, 3, 3])

            with c_save:
                if st.button("💾 この編集を保存", type="primary", use_container_width=True, key="edit_save_btn"):
                    if not str(edit_q).strip():
                        st.error("問題文が空です。")
                    elif not str(edit_ans).strip():
                        st.error("正解（answer）が空です。")
                    else:
                        opts_list = []
                        if edit_mode == "選択式（optionsあり）":
                            raw = (edit_opts_raw or "").strip()
                            if raw:
                                if "\n" in raw:
                                    opts_list = [x.strip() for x in raw.splitlines() if x.strip()]
                                else:
                                    opts_list = [x.strip() for x in raw.split(",") if x.strip()]

                        # 反映（user_ans / is_correct は一旦クリアして再採点前提にする）
                        qref = st.session_state['current_quiz'][edit_idx]
                        qref["question"] = str(edit_q).strip()
                        qref["options"] = opts_list if opts_list else []
                        qref["answer"] = str(edit_ans).strip()
                        qref["explanation"] = str(edit_exp).strip()
                        qref.pop("user_ans", None)
                        qref.pop("is_correct", None)

                        reset_quiz_input_widgets()
                        st.session_state['show_retry'] = False
                        st.session_state['last_wrong_questions'] = []
                        st.rerun()

            with c_dup:
                if st.button("📄 この問題を複製", use_container_width=True, key="edit_dup_btn"):
                    qref = st.session_state['current_quiz'][edit_idx]
                    copied = {
                        "question": qref.get("question", ""),
                        "options": qref.get("options", []) if isinstance(qref.get("options", []), list) else [],
                        "answer": qref.get("answer", ""),
                        "explanation": qref.get("explanation", "")
                    }
                    st.session_state['current_quiz'].append(copied)

                    reset_quiz_input_widgets()
                    st.session_state['show_retry'] = False
                    st.session_state['last_wrong_questions'] = []
                    st.rerun()

            with c_cancel:
                if st.button("↩️ 編集内容を破棄", use_container_width=True, key="edit_cancel_btn"):
                    # 現在の選択問題の内容でフォームを戻すだけ（保存しない）
                    q0 = st.session_state['current_quiz'][edit_idx]
                    st.session_state['edit_q_text'] = q0.get("question", "")
                    st.session_state['edit_ans_text'] = q0.get("answer", "")
                    st.session_state['edit_exp_text'] = q0.get("explanation", "")
                    opts0 = q0.get("options", [])
                    is_choice = bool(opts0 and isinstance(opts0, list) and len(opts0) >= 2)
                    st.session_state['edit_mode_radio'] = "選択式（optionsあり）" if is_choice else "記述式（optionsなし）"
                    st.session_state['edit_opts_text'] = "\n".join([str(x) for x in opts0]) if is_choice else ""
                    st.rerun()
        else:
            st.info("編集できる問題がありません。")

        st.markdown("---")

        # --- 手動追加UI ---
        st.markdown("### ➕ 手動で問題を追加")
        new_q = st.text_area("問題文", key="add_q_text", placeholder="例：刑法における故意とは何か説明せよ。", height=80)

        mode = st.radio("形式", ["記述式（optionsなし）", "選択式（optionsあり）"], horizontal=True, key="add_mode")

        new_opts = ""
        if mode == "選択式（optionsあり）":
            new_opts = st.text_area(
                "選択肢（1行1つ / またはカンマ区切り）",
                key="add_opts_text",
                placeholder="A\nB\nC\nD\nまたは\nA, B, C, D",
                height=90
            )

        new_ans = st.text_input("正解（answer）", key="add_ans_text", placeholder="例：未必の故意")
        new_exp = st.text_area("解説（explanation）", key="add_exp_text", placeholder="解説を書いておくと復習が楽。", height=80)

        if st.button("➕ この問題を追加", type="primary", use_container_width=True, key="add_btn"):
            if not str(new_q).strip():
                st.error("問題文が空です。")
            elif not str(new_ans).strip():
                st.error("正解（answer）が空です。")
            else:
                opts_list = []
                if mode == "選択式（optionsあり）":
                    raw = (new_opts or "").strip()
                    if raw:
                        if "\n" in raw:
                            opts_list = [x.strip() for x in raw.splitlines() if x.strip()]
                        else:
                            opts_list = [x.strip() for x in raw.split(",") if x.strip()]

                st.session_state['current_quiz'].append({
                    "question": str(new_q).strip(),
                    "options": opts_list if opts_list else [],
                    "answer": str(new_ans).strip(),
                    "explanation": str(new_exp).strip()
                })

                reset_quiz_input_widgets()
                st.session_state['show_retry'] = False
                st.session_state['last_wrong_questions'] = []
                st.rerun()

    # クイズフォーム
    with st.form("quiz_form"):
        for i, q in enumerate(st.session_state['current_quiz']):
            question_text = q.get('question', '')
            st.markdown(f"""
            <div class="question-box">
                <div class="question-number">Q{i+1}.</div>
                <div class="question-text">{question_text}</div>
            </div>
            """, unsafe_allow_html=True)

            opts = q.get('options', [])
            if opts and isinstance(opts, list) and len(opts) >= 2:
                st.session_state['results'][i] = st.radio(
                    f"答えを選択 (Q{i+1})", opts, key=f"r_{i}", label_visibility="collapsed"
                )
            else:
                st.session_state['results'][i] = st.text_input(
                    f"答えを入力 (Q{i+1})", key=f"t_{i}", label_visibility="collapsed", placeholder="回答を入力..."
                )

        submitted = st.form_submit_button("✅ 採点", type="primary")

    # フォーム外処理
    if submitted:
        correct = 0
        wrong_questions = []

        for i, q in enumerate(st.session_state['current_quiz']):
            ans = st.session_state['results'].get(i, "")

            # 追加：表記ゆれ耐性（空白・記号など）
            is_correct = norm_answer(ans) == norm_answer(q.get('answer', ''))

            # 正誤情報の記録（雨音の最新版と同じ）
            st.session_state['current_quiz'][i]['user_ans'] = ans
            st.session_state['current_quiz'][i]['is_correct'] = is_correct

            if is_correct:
                st.success(f"第{i+1}問: 正解")
                correct += 1
            else:
                st.error(f"第{i+1}問: 不正解 (正解: {q.get('answer')})")
                wrong_questions.append(st.session_state['current_quiz'][i])

            # ✅ 解説は常時表示
            st.markdown("#### 解説")
            st.write(q.get('explanation', ''))
            st.markdown("---")

        # 履歴保存
        if st.session_state['user_id']:
            new_log = {
                "date": datetime.now(JST).strftime("%Y/%m/%d %H:%M"),
                "title": st.session_state['current_title'],
                "score": int((correct/len(st.session_state['current_quiz']))*100) if st.session_state['current_quiz'] else 0,
                "correct": correct,
                "total": len(st.session_state['current_quiz']),
                "quiz_data": st.session_state['current_quiz'],
                "summary_data": st.session_state['summary']
            }
            save_history_to_gs(st.session_state['user_id'], new_log)
            st.session_state['quiz_history'] = load_history_from_gs(st.session_state['user_id'])

        # 追加：採点後にその場でリトライを出す（rerunしない）
        st.session_state['last_wrong_questions'] = wrong_questions
        st.session_state['show_retry'] = True

    # 💡【新機能】間違えた問題だけリトライ（採点後に表示して安定化）
    if st.session_state.get('show_retry') and st.session_state.get('last_wrong_questions'):
        wq = st.session_state['last_wrong_questions']
        st.info(f"前回の結果：{len(wq)}問の間違いがありました。")
        if st.button(f"🔥 間違えた{len(wq)}問だけでリベンジする", type="primary", use_container_width=True):
            st.session_state['current_quiz'] = wq
            st.session_state['current_title'] = st.session_state['current_title'] + " (リベンジ)"
            st.session_state['results'] = {}
            st.session_state['current_date'] = None
            st.session_state['show_retry'] = False
            st.session_state['last_wrong_questions'] = []
            st.rerun()
import streamlit as st
import google.generativeai as genai
import json
import os
import re
from datetime import datetime, timedelta, timezone
from google.oauth2.service_account import Credentials
import gspread

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
            log_entry.get("score", ""), log_entry.get("correct", ""), log_entry.get("total", ""),
            json.dumps(log_entry.get("quiz_data", []), ensure_ascii=False),
            log_entry.get("summary_data", "")
        ]

        # ✅ 追加：archived列分を末尾に付与（新規は未アーカイブ）
        row.append("")   # ← False じゃなく空欄にする

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

# 👇 ここを追加（入れ替えじゃない）
def restore_one_history_in_gs(user_id, date_str):
    try:
        client = get_gspread_client()
        sheet = client.open("study_history_db").sheet1
        ensure_archived_column(sheet)

        headers = sheet.row_values(1)
        archived_col = headers.index("archived") + 1

        records = sheet.get_all_records()
        for idx, r in enumerate(records):
            if str(r.get("user_id")) == str(user_id) and str(r.get("date")) == str(date_str):
                sheet.update_cell(idx + 2, archived_col, "")
                return True
        return False
    except:
        return False

# ✅ 追加：生成時点で「作成」、以後は同じ行を「上書き」する（採点もここで上書き）
def upsert_history_in_gs(user_id, date_str, log_entry):
    """
    user_id + date で行を特定し、
    - 存在すれば：タイトル/スコア/正解数/総数/quiz_data/summary_data を上書き
    - 無ければ：append で新規作成
    """
    try:
        client = get_gspread_client()
        sheet = client.open("study_history_db").sheet1
        ensure_archived_column(sheet)

        records = sheet.get_all_records()
        target_row = None
        for idx, r in enumerate(records):
            if str(r.get("user_id")) == str(user_id) and str(r.get("date")) == str(date_str):
                target_row = idx + 2  # header+1
                break

        title = log_entry.get("title", "無題")
        score = log_entry.get("score", "")
        correct = log_entry.get("correct", "")
        total = log_entry.get("total", "")
        quiz_data = json.dumps(log_entry.get("quiz_data", []), ensure_ascii=False)
        summary_data = log_entry.get("summary_data", "")

        if target_row:
            # columns: 1 user_id, 2 date, 3 title, 4 score, 5 correct, 6 total, 7 quiz_data, 8 summary_data
            sheet.update_cell(target_row, 3, title)
            sheet.update_cell(target_row, 4, score)
            sheet.update_cell(target_row, 5, correct)
            sheet.update_cell(target_row, 6, total)
            sheet.update_cell(target_row, 7, quiz_data)
            sheet.update_cell(target_row, 8, summary_data)
            return True
        else:
            # 無ければ新規作成（archivedは空欄）
            row = [user_id, date_str, title, score, correct, total, quiz_data, summary_data, ""]
            sheet.append_row(row)
            return True
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

        show_archived = st.checkbox("アーカイブ表示", value=False)

        if show_archived:
            visible_history = st.session_state['quiz_history']
        else:
            visible_history = [
                h for h in st.session_state['quiz_history']
                if not h.get("archived", False)
            ]

        for i, log in enumerate(reversed(visible_history)):
            d = log.get('date', '')
            t = log.get('title', '無題')
            s = log.get('score', 0)
            archived_flag = log.get("archived", False)

            btn_label = f"📅 {d}\n📝 {t}\n🎯 正解率: {s}%"

            c_hist, c_del = st.columns([8, 2])

            # 履歴読み込み
            with c_hist:
                if st.button(btn_label, key=f"hist_{i}", use_container_width=True, type="secondary"):
                    st.session_state['current_quiz'] = log['quiz_data']
                    st.session_state['summary'] = log['summary_data']
                    st.session_state['current_title'] = t
                    st.session_state['current_date'] = d
                    st.session_state['edit_mode'] = False
                    st.session_state['results'] = {}
                    st.session_state['show_retry'] = False
                    st.session_state['last_wrong_questions'] = []
                    st.session_state['pending_delete'] = None
                    st.rerun()

            # 操作ボタン
            with c_del:
                if st.button("📂", key=f"del_hist_{i}", use_container_width=True):
                    st.session_state['pending_delete'] = {"date": d, "title": t}
                    st.rerun()

            # 確認UI
            pending = st.session_state.get('pending_delete')
            if pending and pending.get("date") == d:
                st.warning(f"この履歴をどうしますか？\n\n📅 {d}\n📝 {t}")

                c_arch, c_delete, c_cancel = st.columns(3)

                # アーカイブ or 復活
                with c_arch:
                    if not archived_flag:
                        if st.button("アーカイブ", key=f"archive_{i}", use_container_width=True):
                            ok = archive_one_history_in_gs(st.session_state['user_id'], d)
                            st.session_state['pending_delete'] = None
                            if ok:
                                st.session_state['quiz_history'] = load_history_from_gs(st.session_state['user_id'])
                                st.rerun()
                            else:
                                st.error("アーカイブに失敗しました。")
                    else:
                        if st.button("復活", key=f"restore_{i}", use_container_width=True):
                            ok = restore_one_history_in_gs(st.session_state['user_id'], d)
                            st.session_state['pending_delete'] = None
                            if ok:
                                st.session_state['quiz_history'] = load_history_from_gs(st.session_state['user_id'])
                                st.rerun()
                            else:
                                st.error("復活に失敗しました。")

                # 完全削除
                with c_delete:
                    if st.button("完全削除", key=f"delete_{i}", use_container_width=True):
                        client = get_gspread_client()
                        sheet = client.open("study_history_db").sheet1
                        records = sheet.get_all_records()

                        for idx2, r2 in enumerate(records):
                            if str(r2.get("user_id")) == str(st.session_state['user_id']) and str(r2.get("date")) == str(d):
                                sheet.delete_rows(idx2 + 2)
                                break

                        st.session_state['pending_delete'] = None
                        st.session_state['quiz_history'] = load_history_from_gs(st.session_state['user_id'])
                        st.rerun()

                # キャンセル
                with c_cancel:
                    if st.button("キャンセル", key=f"cancel_{i}", use_container_width=True):
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

# ✅ 追加：問題削除/追加後に入力ウィジェットをリセット
def reset_quiz_input_widgets():
    for k in list(st.session_state.keys()):
        if k.startswith("r_") or k.startswith("t_"):
            st.session_state.pop(k, None)
    st.session_state['results'] = {}

# --- AI処理 ---
def get_available_model():
    return genai.GenerativeModel("gemini-2.5-pro")

def generate_summary(files):
    model = get_available_model()
    if not model:
        return None
    content = ["資料の要点を、分かりやすく要約してください。"]
    for f in files:
        content.append({"mime_type": "application/pdf", "data": f.getvalue()})
    try:
        with st.spinner("要約中..."):
            return model.generate_content(content).text
    except:
        return None

def start_quiz_generation(files):
    model = get_available_model()
    if not model:
        return "無題", []
    prompt = """PDFからクイズ15問をJSONで出力。
【重要】記述式や穴埋め問題の場合、optionsは必ず空リスト[]にすること。
【重要】出力はJSONのみ。前後に説明文やコードブロックは付けないこと。
{"title": "タイトル", "quizzes": [{"question": "..", "options": ["..", ".."], "answer": "..", "explanation": ".."}]}"""
    content = [prompt] + [{"mime_type": "application/pdf", "data": f.getvalue()} for f in files]
    try:
        with st.spinner("クイズ作成中..."):
            res = model.generate_content(content).text
            data = parse_json_safely(res)
            return data.get("title", "無題"), data.get("quizzes", [])
    except:
        return "無題", []

# --- メインロジック ---
if uploaded_files:
    c1, c2 = st.columns(2)

    # ===== 要約 =====
    with c1:
        if st.button("📝 資料を要約する", use_container_width=True):
            st.session_state['summary'] = generate_summary(uploaded_files)

    # ===== クイズ生成 =====
    with c2:
        if st.button("🚀 クイズを生成", use_container_width=True, type="primary"):

            t, q = start_quiz_generation(uploaded_files)

            # 🔥 毎回必ず新しい履歴として作る（上書き防止）
            st.session_state['current_date'] = datetime.now(JST).strftime("%Y/%m/%d %H:%M")

            st.session_state.update({
                "current_title": t,
                "current_quiz": q,
                "results": {},
                "edit_mode": False
            })

            st.session_state['show_retry'] = False
            st.session_state['last_wrong_questions'] = []

            # ===== 履歴に新規追加 =====
            if st.session_state.get('user_id'):
                init_log = {
                    "date": st.session_state['current_date'],
                    "title": t,
                    "score": "",
                    "correct": "",
                    "total": "",
                    "quiz_data": q,
                    "summary_data": st.session_state.get('summary') or ""
                }

                save_history_to_gs(
                    st.session_state['user_id'],
                    init_log
                )

                st.session_state['quiz_history'] = load_history_from_gs(
                    st.session_state['user_id']
                )

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

    # ===== フォーム外処理 =====
    if submitted:
        correct = 0
        wrong_questions = []

        for i, q in enumerate(st.session_state['current_quiz']):
            ans = st.session_state['results'].get(i, "")

            is_correct = norm_answer(ans) == norm_answer(q.get('answer', ''))

            st.session_state['current_quiz'][i]['user_ans'] = ans
            st.session_state['current_quiz'][i]['is_correct'] = is_correct

            if is_correct:
                st.success(f"第{i+1}問: 正解 (正解: {q.get('answer')})")
                correct += 1
            else:
                st.error(f"第{i+1}問: 不正解 (正解: {q.get('answer')})")
                wrong_questions.append(st.session_state['current_quiz'][i])

            st.markdown("#### 解説")
            st.write(q.get('explanation', ''))
            st.markdown("---")

        # ===== 採点サマリー =====
        total = len(st.session_state['current_quiz'])
        score = int((correct / total) * 100) if total else 0

        st.divider()
        st.subheader("📊 採点結果")

        col1, col2 = st.columns(2)
        with col1:
            st.metric("正解数", f"{correct} / {total}")
        with col2:
            st.metric("正解率", f"{score}%")

        st.progress(score / 100)

        if score == 100:
            st.balloons()
        
        st.divider()

        # ===== 履歴保存（必ず if の中）=====
        if st.session_state['user_id']:

            # 🔥 解き直すたびに日付を「今」に更新
            new_date = datetime.now(JST).strftime("%Y/%m/%d %H:%M")

            new_log = {
                "date": new_date,
                "title": st.session_state['current_title'],
                "score": score,
                "correct": correct,
                "total": total,
                "quiz_data": st.session_state['current_quiz'],
                "summary_data": st.session_state['summary']
            }

            # 以前の日付があればアーカイブ
            if st.session_state.get('current_date'):
                archive_one_history_in_gs(
                    st.session_state['user_id'],
                    st.session_state['current_date']
                )

            # 新しい日付で保存
            save_history_to_gs(
                st.session_state['user_id'],
                new_log
            )

            # セッションの日付も更新
            st.session_state['current_date'] = new_date

            st.session_state['quiz_history'] = load_history_from_gs(
                st.session_state['user_id']
            )

        # ===== リトライ準備も if の中 =====
        st.session_state['last_wrong_questions'] = wrong_questions
        st.session_state['show_retry'] = True


# 💡【間違えた問題だけリトライ】
if st.session_state.get('show_retry') and st.session_state.get('last_wrong_questions'):
    wq = st.session_state['last_wrong_questions']
    st.info(f"前回の結果：{len(wq)}問の間違いがありました。")
    if st.button(
        f"🔥 間違えた{len(wq)}問だけでリベンジする",
        type="primary",
        use_container_width=True
    ):
        st.session_state['current_quiz'] = wq
        st.session_state['current_title'] = (
            st.session_state['current_title'] + " (リベンジ)"
        )
        st.session_state['results'] = {}
        st.session_state['current_date'] = None
        st.session_state['show_retry'] = False
        st.session_state['last_wrong_questions'] = []
        st.rerun()
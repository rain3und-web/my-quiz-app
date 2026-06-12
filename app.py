import streamlit as st
import google.generativeai as genai
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from google.oauth2.service_account import Credentials
import gspread
import hashlib

# --- 画面設定 ---
st.set_page_config(page_title="PDFでクイズ作成", page_icon="🎓", layout="wide")
JST = timezone(timedelta(hours=+9), 'JST')

# --- Googleスプレッドシート連携（高速化・完全上書き版） ---
@st.cache_resource
def get_sheet():
    """シートをキャッシュして毎回認証する無駄を省き、必要な列を自動生成する"""
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    credentials = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=scopes)
    client = gspread.authorize(credentials)
    sheet = client.open("study_history_db").sheet1
    
    headers = sheet.row_values(1)
    needs_update = False
    
    # 不足している列があれば追加
    if "archived" not in headers:
        headers.append("archived")
        needs_update = True
    if "quiz_id" not in headers:
        headers.append("quiz_id")
        needs_update = True
        
    if needs_update:
        # ヘッダー行を1回の通信で上書き
        end_col_letter = gspread.utils.rowcol_to_a1(1, len(headers))[0]
        sheet.update(f'A1:{end_col_letter}1', [headers])
        
    return sheet

def load_history_from_gs(user_id):
    try:
        sheet = get_sheet()
        records = sheet.get_all_records()
        user_history = []
        for r in records:
            if str(r.get("user_id")) == str(user_id):
                q_data = r.get("quiz_data", "[]")
                if isinstance(q_data, str):
                    try:
                        q_data = json.loads(q_data)
                    except:
                        q_data = []
                
                # 過去のデータ(quiz_idがないもの)には一時的にIDを発行して互換性を保つ
                q_id = str(r.get("quiz_id", "")).strip()
                if not q_id:
                    q_id = uuid.uuid4().hex[:8]

                user_history.append({
                    "quiz_id": q_id,
                    "date": r.get("date"),
                    "title": r.get("title", "無題"),
                    "score": r.get("score"),
                    "correct": r.get("correct"),
                    "total": r.get("total"),
                    "quiz_data": q_data,
                    "archived": r.get("archived", "")
                })
        return user_history
    except Exception as e:
        st.error(f"読み込みエラー: {e}")
        return []

def upsert_history_in_gs(user_id, quiz_id, log_entry):
    """quiz_idをキーにしてデータを検索し、存在すれば1行まるごと上書き、なければ新規追加"""
    try:
        sheet = get_sheet()
        headers = sheet.row_values(1)
        records = sheet.get_all_records()
        
        target_row = None
        for idx, r in enumerate(records):
            if str(r.get("user_id")) == str(user_id):
                if quiz_id and str(r.get("quiz_id")) == str(quiz_id):
                    target_row = idx + 2
                    break
                # 古いデータ(quiz_id未登録)への後方互換性（日付でマッチさせる）
                elif not r.get("quiz_id") and str(r.get("date")) == str(log_entry.get("date")):
                    target_row = idx + 2
                    break

        # ヘッダー順に合わせた行データの作成
        row_data = [""] * len(headers)
        for i, h in enumerate(headers):
            if h == "user_id": row_data[i] = user_id
            elif h == "quiz_id": row_data[i] = quiz_id
            elif h == "date": row_data[i] = log_entry.get("date", "")
            elif h == "title": row_data[i] = log_entry.get("title", "無題")
            elif h == "score": row_data[i] = log_entry.get("score", "")
            elif h == "correct": row_data[i] = log_entry.get("correct", "")
            elif h == "total": row_data[i] = log_entry.get("total", "")
            elif h == "quiz_data": row_data[i] = json.dumps(log_entry.get("quiz_data", []), ensure_ascii=False)
            elif h == "archived": row_data[i] = log_entry.get("archived", "")

        if target_row:
            # 1回のAPI通信で一括上書き
            end_col_letter = gspread.utils.rowcol_to_a1(1, len(headers))[0]
            cell_range = f'A{target_row}:{end_col_letter}{target_row}'
            sheet.update(cell_range, [row_data])
        else:
            # 新規追加
            sheet.append_row(row_data)
        return True
    except Exception as e:
        st.error(f"更新エラー: {e}")
        return False

def toggle_archive_in_gs(user_id, quiz_id, date_str, archive_status):
    try:
        sheet = get_sheet()
        headers = sheet.row_values(1)
        archived_col = headers.index("archived") + 1
        records = sheet.get_all_records()
        for idx, r in enumerate(records):
            if str(r.get("user_id")) == str(user_id):
                if quiz_id and str(r.get("quiz_id")) == str(quiz_id):
                    sheet.update_cell(idx + 2, archived_col, archive_status)
                    return True
                elif not r.get("quiz_id") and str(r.get("date")) == str(date_str):
                    sheet.update_cell(idx + 2, archived_col, archive_status)
                    return True
        return False
    except:
        return False

def delete_one_history_in_gs(user_id, quiz_id, date_str):
    try:
        sheet = get_sheet()
        records = sheet.get_all_records()
        for idx, r in enumerate(records):
            if str(r.get("user_id")) == str(user_id):
                if quiz_id and str(r.get("quiz_id")) == str(quiz_id):
                    sheet.delete_rows(idx + 2)
                    return True
                elif not r.get("quiz_id") and str(r.get("date")) == str(date_str):
                    sheet.delete_rows(idx + 2)
                    return True
        return False
    except:
        return False

def clear_history_from_gs(user_id):
    try:
        sheet = get_sheet()
        cells = sheet.findall(str(user_id))
        rows_to_delete = sorted(list(set([cell.row for cell in cells])), reverse=True)
        for row_idx in rows_to_delete:
            if str(sheet.cell(row_idx, 1).value) == str(user_id):
                sheet.delete_rows(row_idx)
        return True
    except:
        return False

def sync_current_quiz_to_db():
    """問題の追加・削除・編集を行った際、データベースに即時反映させる"""
    if st.session_state.get('user_id') and st.session_state.get('current_quiz_id'):
        current_log = next((h for h in st.session_state['quiz_history'] if h['quiz_id'] == st.session_state['current_quiz_id']), None)
        if current_log:
            current_log['quiz_data'] = st.session_state['current_quiz']
            # 問題構成が変わるため採点結果をリセット
            current_log['score'] = ""
            current_log['correct'] = ""
            current_log['total'] = ""
            upsert_history_in_gs(st.session_state['user_id'], st.session_state['current_quiz_id'], current_log)
            st.session_state['quiz_history'] = load_history_from_gs(st.session_state['user_id'])


# --- ユーティリティ系 ---
def norm_answer(s: str) -> str:
    """採点用：表記ゆれを軽減（空白/記号/全角空白など）"""
    s = str(s).strip().lower()
    s = s.replace("　", "")
    s = re.sub(r"\s+", "", s)
    s = s.replace("・", "").replace("、", "").replace("。", "")
    return s

def reset_quiz_input_widgets():
    """問題削除/追加後に入力ウィジェットをリセット"""
    for k in list(st.session_state.keys()):
        if k.startswith("r_") or k.startswith("t_"):
            st.session_state.pop(k, None)
    st.session_state['results'] = {}

def calc_files_hash(files):
    h = hashlib.sha256()
    for f in files:
        h.update(f.getvalue())
    return h.hexdigest()

def clean_title(filename: str) -> str:
    """✅ 新機能：ファイル名から拡張子や不要な文字を削除して美しいタイトルを生成"""
    # 拡張子 (.pdf) を除去
    name = re.sub(r'\.pdf$', '', filename, flags=re.IGNORECASE)
    
    # 削除したいキーワードやパターンを追加・調整できます
    patterns = [
        r"\(.*?\)",        # 半角括弧と中身 例: (1)
        r"（.*?）",        # 全角括弧と中身 例: （１）
        r"テスト",         # 「テスト」という文字
        r"クイズ",         # 「クイズ」という文字
        r"のコピー",       # ダウンロード時の接尾辞など
        r"コピー",
        r"_+",             # アンダースコアの連続
        r"[\s　]+",        # 余計な空白
    ]
    
    cleaned = name
    for p in patterns:
        cleaned = re.sub(p, "", cleaned)
    
    # 整形後、文字が消えすぎて空になった場合のフォールバック
    if not cleaned.strip():
        cleaned = "無題のクイズ"
        
    return cleaned.strip()

@st.cache_resource
def get_available_model():
    return genai.GenerativeModel("gemini-3.1-flash-lite-preview")

def start_quiz_generation(files):
    model = get_available_model()
    if not model:
        return []

    # AIへの指示から「タイトルを付ける」というタスクを削除し、純粋にクイズ作成に集中させます
    prompt = """PDFからクイズを作成してください。
【重要】
・問題数は6問以上15問以下にすること。
・PDF内に既存の確認テストや設問が含まれている場合は優先的に利用すること。
・出力は必ずJSON形式にすること。

{
  "quizzes": [
    {
      "question": "..",
      "options": ["..", ".."],
      "answer": "..",
      "explanation": ".."
    }
  ]
}
"""
    content_files = [{"mime_type": "application/pdf", "data": f.getvalue()} for f in files]

    try:
        with st.spinner("クイズ作成中..."):
            res = model.generate_content(
                [prompt] + content_files,
                generation_config=genai.types.GenerationConfig(response_mime_type="application/json")
            ).text
            
            data = json.loads(res)
            quizzes = data.get("quizzes", [])

            if len(quizzes) < 6:
                res = model.generate_content(
                    ["出力された問題数が6問未満です。もう一度確認し、6問以上15問以下で出力し直してください。"] + content_files,
                    generation_config=genai.types.GenerationConfig(response_mime_type="application/json")
                ).text
                data = json.loads(res)
                quizzes = data.get("quizzes", [])

            return quizzes[:15]
    except Exception as e:
        st.error(f"クイズ生成エラー: {e}")
        return []

# --- セッション初期化 ---
for key in ['user_id', 'quiz_history', 'current_quiz', 'current_quiz_id', 'results', 'current_date', 'edit_mode']:
    if key not in st.session_state:
        st.session_state[key] = None if key != 'quiz_history' and key != 'results' else ([] if key == 'quiz_history' else {})
        if key == 'edit_mode':
            st.session_state[key] = False

if 'current_title' not in st.session_state:
    st.session_state['current_title'] = "無題のクイズ"
if 'last_wrong_questions' not in st.session_state:
    st.session_state['last_wrong_questions'] = []
if 'show_retry' not in st.session_state:
    st.session_state['show_retry'] = False
if 'last_pdf_hash' not in st.session_state:
    st.session_state['last_pdf_hash'] = None
if 'pending_delete' not in st.session_state:
    st.session_state['pending_delete'] = None

# --- 🎨 CSS: デザイン設定 ---
st.markdown("""
    <style>
    div[data-testid="stSidebar"] .stButton button[kind="secondary"] div p { white-space: pre-wrap !important; line-height: 1.4 !important; text-align: left !important; font-size: 0.9rem !important; }
    div[data-testid="stSidebar"] .stButton button[kind="secondary"] { height: auto !important; padding: 8px 10px !important; border: 1px solid #ddd !important; border-left: 5px solid #4CAF50 !important; border-radius: 6px !important; text-align: left !important; background-color: #f9f9f9 !important; color: #333 !important; }
    div[data-testid="stSidebar"] .stButton button[kind="secondary"]:hover { background-color: #e6f7ff !important; border-color: #4CAF50 !important; }
    .question-box { display: flex; align-items: flex-start; background-color: #f0f8ff; border-left: 4px solid #0078d7; padding: 12px 15px; border-radius: 4px; margin-bottom: 8px; margin-top: 8px; box-shadow: 0 1px 2px rgba(0,0,0,0.05); }
    .question-number { font-weight: bold; color: #0078d7; margin-right: 12px; font-size: 1.1em; flex-shrink: 0; line-height: 1.6; }
    .question-text { color: #2c3e50; white-space: pre-wrap; line-height: 1.6; flex-grow: 1; word-wrap: break-word; }
    </style>
""", unsafe_allow_html=True)

st.title("🎓 PDFでクイズ作成")

# --- APIキー ---
if "GEMINI_API_KEY" in st.secrets:
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"].strip())
else:
    st.error("APIキーが設定されていません。secrets.tomlにGEMINI_API_KEYを設定してください。")
    st.stop()

# --- サイドバー ---
with st.sidebar:
    st.header("👤 ログイン")
    user_input = st.text_input("ユーザー名", value=st.session_state['user_id'] or "")
    if st.button("ログイン", key="login_btn", type="primary"):
        if user_input:
            st.session_state['user_id'] = user_input
            with st.spinner("同期中..."):
                st.session_state['quiz_history'] = load_history_from_gs(user_input)
            st.session_state['pending_delete'] = None
            st.rerun()

    st.divider()

    uploaded_files = st.file_uploader("PDFをアップロード", type=["pdf"], accept_multiple_files=True)

    st.divider()

    if st.session_state['user_id'] and st.session_state['quiz_history']:
        st.header("📊 履歴")
        show_archived = st.checkbox("アーカイブ表示", value=False)
        visible_history = st.session_state['quiz_history'] if show_archived else [h for h in st.session_state['quiz_history'] if not h.get("archived", False)]

        for i, log in enumerate(reversed(visible_history)):
            q_id = log.get('quiz_id')
            d = log.get('date', '')
            t = log.get('title', '無題')
            s = log.get('score', 0)
            archived_flag = log.get("archived", False)

            btn_label = f"📅 {d}\n📝 {t}\n🎯 正解率: {s}%"

            c_hist, c_del = st.columns([8, 2])

            with c_hist:
                if st.button(btn_label, key=f"hist_{q_id}_{i}", use_container_width=True, type="secondary"):
                    st.session_state['current_quiz_id'] = q_id
                    st.session_state['current_quiz'] = log['quiz_data']
                    st.session_state['current_title'] = t
                    st.session_state['current_date'] = d
                    st.session_state['edit_mode'] = False
                    st.session_state['results'] = {}
                    st.session_state['show_retry'] = False
                    st.session_state['last_wrong_questions'] = []
                    st.session_state['pending_delete'] = None
                    st.rerun()

            with c_del:
                if st.button("📂", key=f"del_hist_{q_id}_{i}", use_container_width=True):
                    st.session_state['pending_delete'] = {"quiz_id": q_id, "date": d, "title": t}
                    st.rerun()

            pending = st.session_state.get('pending_delete')
            if pending and pending.get("quiz_id") == q_id:
                st.warning(f"この履歴をどうしますか？\n\n📅 {d}\n📝 {t}")
                c_arch, c_delete, c_cancel = st.columns(3)

                with c_arch:
                    if not archived_flag:
                        if st.button("アーカイブ", key=f"archive_{q_id}", use_container_width=True):
                            if toggle_archive_in_gs(st.session_state['user_id'], q_id, d, True):
                                st.session_state['pending_delete'] = None
                                st.session_state['quiz_history'] = load_history_from_gs(st.session_state['user_id'])
                                st.rerun()
                    else:
                        if st.button("復活", key=f"restore_{q_id}", use_container_width=True):
                            if toggle_archive_in_gs(st.session_state['user_id'], q_id, d, ""):
                                st.session_state['pending_delete'] = None
                                st.session_state['quiz_history'] = load_history_from_gs(st.session_state['user_id'])
                                st.rerun()

                with c_delete:
                    if st.button("完全削除", key=f"delete_{q_id}", use_container_width=True):
                        delete_one_history_in_gs(st.session_state['user_id'], q_id, d)
                        st.session_state['pending_delete'] = None
                        st.session_state['quiz_history'] = load_history_from_gs(st.session_state['user_id'])
                        st.rerun()

                with c_cancel:
                    if st.button("キャンセル", key=f"cancel_{q_id}", use_container_width=True):
                        st.session_state['pending_delete'] = None
                        st.rerun()

        st.markdown("---")

        if st.button("🗑️ 履歴を全削除", use_container_width=True):
            if clear_history_from_gs(st.session_state['user_id']):
                st.session_state['quiz_history'] = []
                st.session_state['pending_delete'] = None
                st.rerun()

# --- メインロジック ---
if uploaded_files:
    col1, col2, col3 = st.columns([1, 2, 1])

    with col2:
        if st.button("🚀 クイズを生成", use_container_width=True, type="primary"):
            current_hash = calc_files_hash(uploaded_files)
            
            # ✅ 修正箇所：現在のセッションに問題が存在する時だけブロックする
            if st.session_state['last_pdf_hash'] == current_hash and st.session_state.get('current_quiz'):
                st.info("このPDFのクイズは既に生成・表示されています。")
                st.stop()
            
            # ✅ AIにクイズを作らせる
            q = start_quiz_generation(uploaded_files)
            
            # ✅ 修正箇所：生成に失敗した（空リストが返ってきた）場合の処理
            if not q:
                st.error("⚠️ クイズの生成に失敗しました。時間をおいてもう一度ボタンを押してください。")
                # 失敗時はハッシュをリセットし、再度同じPDFで押せるようにする
                st.session_state['last_pdf_hash'] = None
                st.stop()

            # 成功した時だけハッシュを記録する
            st.session_state['last_pdf_hash'] = current_hash
            
            # 1枚目のPDFのファイル名からタイトルを自動生成
            first_filename = uploaded_files[0].name
            t = clean_title(first_filename)

            new_quiz_id = uuid.uuid4().hex[:8]
            new_date = datetime.now(JST).strftime("%Y/%m/%d %H:%M")

            st.session_state.update({
                "current_quiz_id": new_quiz_id,
                "current_date": new_date,
                "current_title": t,
                "current_quiz": q,
                "results": {},
                "edit_mode": False,
                "show_retry": False,
                "last_wrong_questions": []
            })

            if st.session_state.get('user_id'):
                init_log = {
                    "date": new_date,
                    "title": t,
                    "score": "",
                    "correct": "",
                    "total": "",
                    "quiz_data": q,
                    "archived": ""
                }
                upsert_history_in_gs(st.session_state['user_id'], new_quiz_id, init_log)
                st.session_state['quiz_history'] = load_history_from_gs(st.session_state['user_id'])

            st.rerun()

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
                if st.session_state['current_quiz_id'] and st.session_state['user_id']:
                    # タイトルのみ更新して保存
                    current_log = next((h for h in st.session_state['quiz_history'] if h['quiz_id'] == st.session_state['current_quiz_id']), None)
                    if current_log:
                        current_log['title'] = new_title_input
                        upsert_history_in_gs(st.session_state['user_id'], st.session_state['current_quiz_id'], current_log)
                        st.session_state['quiz_history'] = load_history_from_gs(st.session_state['user_id'])
                
                st.session_state['current_title'] = new_title_input
                st.session_state['edit_mode'] = False
                st.rerun()
        else:
            if st.button("✏️ 題名を変更", use_container_width=True):
                st.session_state['edit_mode'] = True
                st.rerun()

    # 問題の編集（削除 / 手動追加）
    with st.expander("🛠️ 問題の編集（削除 / 手動追加）", expanded=False):
        # 削除UI
        st.markdown("### 🗑️ 問題を削除（AIがミスった時）")
        options = []
        for i, q in enumerate(st.session_state['current_quiz']):
            qtext = (q.get("question", "") or "").replace("\n", " ")
            if len(qtext) > 30: qtext = qtext[:30] + "..."
            options.append(f"Q{i+1}: {qtext}")

        del_selected = st.multiselect("削除する問題を選択", options, key="del_selected")
        if st.button("🗑️ 選択した問題を削除", type="secondary", use_container_width=True, key="del_btn"):
            idxs = []
            for s in del_selected:
                try: idxs.append(int(s.split(":")[0].replace("Q", "").strip()) - 1)
                except: pass

            idxs = sorted(set([i for i in idxs if 0 <= i < len(st.session_state['current_quiz'])]), reverse=True)
            for i in idxs: st.session_state['current_quiz'].pop(i)

            reset_quiz_input_widgets()
            st.session_state['show_retry'] = False
            st.session_state['last_wrong_questions'] = []
            sync_current_quiz_to_db()
            st.rerun()

        st.markdown("---")

        # 既存の問題を編集
        st.markdown("### ✏️ 既存の問題を編集")
        if st.session_state['current_quiz']:
            edit_options = []
            for i, q in enumerate(st.session_state['current_quiz']):
                qtext = (q.get("question", "") or "").replace("\n", " ")
                if len(qtext) > 30: qtext = qtext[:30] + "..."
                edit_options.append(f"Q{i+1}: {qtext}")

            selected = st.selectbox("編集する問題を選択", edit_options, key="edit_selectbox")
            try: edit_idx = int(selected.split(":")[0].replace("Q", "").strip()) - 1
            except: edit_idx = 0

            if 'edit_last_idx' not in st.session_state: st.session_state['edit_last_idx'] = None

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
                edit_opts_raw = st.text_area("選択肢（編集）（1行1つ / またはカンマ区切り）", key="edit_opts_text", height=90)
            edit_ans = st.text_input("正解（answer）（編集）", key="edit_ans_text")
            edit_exp = st.text_area("解説（explanation）（編集）", key="edit_exp_text", height=80)

            c_save, c_dup, c_cancel = st.columns([4, 3, 3])
            with c_save:
                if st.button("💾 この編集を保存", type="primary", use_container_width=True, key="edit_save_btn"):
                    if not str(edit_q).strip(): st.error("問題文が空です。")
                    elif not str(edit_ans).strip(): st.error("正解（answer）が空です。")
                    else:
                        opts_list = []
                        if edit_mode == "選択式（optionsあり）":
                            raw = (edit_opts_raw or "").strip()
                            if raw:
                                if "\n" in raw: opts_list = [x.strip() for x in raw.splitlines() if x.strip()]
                                else: opts_list = [x.strip() for x in raw.split(",") if x.strip()]

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
                        sync_current_quiz_to_db()
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
                    sync_current_quiz_to_db()
                    st.rerun()

            with c_cancel:
                if st.button("↩️ 編集内容を破棄", use_container_width=True, key="edit_cancel_btn"):
                    st.session_state['edit_last_idx'] = None # 強制リセット
                    st.rerun()
        else:
            st.info("編集できる問題がありません。")

        st.markdown("---")

        # 手動追加UI
        st.markdown("### ➕ 手動で問題を追加")
        new_q = st.text_area("問題文", key="add_q_text", placeholder="例：刑法における故意とは何か説明せよ。", height=80)
        mode = st.radio("形式", ["記述式（optionsなし）", "選択式（optionsあり）"], horizontal=True, key="add_mode")
        new_opts = ""
        if mode == "選択式（optionsあり）":
            new_opts = st.text_area("選択肢（1行1つ / またはカンマ区切り）", key="add_opts_text", placeholder="A\nB\nC\nD", height=90)
        new_ans = st.text_input("正解（answer）", key="add_ans_text", placeholder="例：未必の故意")
        new_exp = st.text_area("解説（explanation）", key="add_exp_text", placeholder="解説を書いておくと復習が楽。", height=80)

        if st.button("➕ この問題を追加", type="primary", use_container_width=True, key="add_btn"):
            if not str(new_q).strip(): st.error("問題文が空です。")
            elif not str(new_ans).strip(): st.error("正解（answer）が空です。")
            else:
                opts_list = []
                if mode == "選択式（optionsあり）":
                    raw = (new_opts or "").strip()
                    if raw:
                        if "\n" in raw: opts_list = [x.strip() for x in raw.splitlines() if x.strip()]
                        else: opts_list = [x.strip() for x in raw.split(",") if x.strip()]

                st.session_state['current_quiz'].append({
                    "question": str(new_q).strip(),
                    "options": opts_list if opts_list else [],
                    "answer": str(new_ans).strip(),
                    "explanation": str(new_exp).strip()
                })

                reset_quiz_input_widgets()
                st.session_state['show_retry'] = False
                st.session_state['last_wrong_questions'] = []
                sync_current_quiz_to_db()
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
                st.session_state['results'][i] = st.radio(f"答えを選択 (Q{i+1})", opts, key=f"r_{i}", label_visibility="collapsed")
            else:
                st.session_state['results'][i] = st.text_input(f"答えを入力 (Q{i+1})", key=f"t_{i}", label_visibility="collapsed", placeholder="回答を入力...")

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

        total = len(st.session_state['current_quiz'])
        score = int((correct / total) * 100) if total else 0

        st.divider()
        st.subheader("📊 採点結果")
        col1, col2 = st.columns(2)
        with col1: st.metric("正解数", f"{correct} / {total}")
        with col2: st.metric("正解率", f"{score}%")
        st.progress(score / 100)

        if score == 100: st.balloons()
        st.divider()

        # 履歴保存（上書き）
        if st.session_state['user_id']:
            new_date = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
            new_log = {
                "date": new_date,
                "title": st.session_state['current_title'],
                "score": score,
                "correct": correct,
                "total": total,
                "quiz_data": st.session_state['current_quiz'],
                "archived": ""
            }

            upsert_history_in_gs(st.session_state['user_id'], st.session_state['current_quiz_id'], new_log)
            st.session_state['current_date'] = new_date
            st.session_state['quiz_history'] = load_history_from_gs(st.session_state['user_id'])

        st.session_state['last_wrong_questions'] = wrong_questions
        st.session_state['show_retry'] = True

# 💡【間違えた問題だけリトライ】
if st.session_state.get('show_retry') and st.session_state.get('last_wrong_questions'):
    wq = st.session_state['last_wrong_questions']
    st.info(f"前回の結果：{len(wq)}問の間違いがありました。")
    if st.button(f"🔥 間違えた{len(wq)}問だけでリベンジする", type="primary", use_container_width=True):
        st.session_state['current_quiz'] = wq
        st.session_state['current_title'] = st.session_state['current_title'] + " (リベンジ)"
        st.session_state['results'] = {}
        st.session_state['show_retry'] = False
        st.session_state['last_wrong_questions'] = []
        
        # リベンジ時は「新しいクイズ」としてIDを新規発行する
        new_quiz_id = uuid.uuid4().hex[:8]
        new_date = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
        
        st.session_state['current_quiz_id'] = new_quiz_id
        st.session_state['current_date'] = new_date
        
        if st.session_state.get('user_id'):
            init_log = {
                "date": new_date,
                "title": st.session_state['current_title'],
                "score": "",
                "correct": "",
                "total": "",
                "quiz_data": wq,
                "archived": ""
            }
            upsert_history_in_gs(st.session_state['user_id'], new_quiz_id, init_log)
            st.session_state['quiz_history'] = load_history_from_gs(st.session_state['user_id'])

        st.rerun()
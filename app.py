import streamlit as st
import google.generativeai as genai
from streamlit_gsheets import GSheetsConnection
import edge_tts
import asyncio
import PyPDF2
import pandas as pd
import json
import base64
import io
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from typing import Optional

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================
st.set_page_config(page_title="Sales Dojo", layout="wide", page_icon="🥋")

# ---- Secrets Validation ----
required_top = ["GOOGLE_API_KEY", "drive", "connections"]
missing = [k for k in required_top if k not in st.secrets]

if missing:
    st.error(f"🚨 Setup Needed: Missing secrets keys: {', '.join(missing)}")
    st.stop()

if "folder_id" not in st.secrets["drive"]:
    st.error("🚨 Setup Needed: Missing drive.folder_id in secrets.")
    st.stop()

if "gsheets" not in st.secrets["connections"]:
    st.error("🚨 Setup Needed: Missing connections.gsheets in secrets.")
    st.stop()

# ---- Configure Gemini ----
genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])

# ==========================================
# 2. SESSION STATE
# ==========================================
defaults = {
    "chat_history": [],
    "turn_count": 0,
    "roleplay_active": True,
    "session_started": False,
    "current_tip": None,
    "kb_text": "",
    "file_names": [],
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ==========================================
# 3. ASYNC / TTS HELPERS
# ==========================================
async def text_to_speech(text: str, voice: str):
    try:
        communicate = edge_tts.Communicate(text, voice)
        mp3_data = b""
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                mp3_data += chunk.get("data", b"")
        return mp3_data or None
    except Exception:
        return None

def run_async(coro):
    """
    Streamlit sometimes runs in environments where an event loop may already exist.
    This wrapper avoids RuntimeError from asyncio.run in those cases.
    """
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

def play_audio_autoplay(audio_bytes: Optional[bytes]):
    if not audio_bytes:
        return
    b64 = base64.b64encode(audio_bytes).decode()
    md = f"""
    <audio autoplay="true">
        <source src="data:audio/mp3;base64,{b64}" type="audio/mp3">
    </audio>
    """
    st.markdown(md, unsafe_allow_html=True)

# ==========================================
# 4. GOOGLE DRIVE KB LOADER
# ==========================================
@st.cache_resource
def get_drive_service():
    """
    Uses the same service account info as the Streamlit GSheets connection,
    with a fallback for different secret shapes.
    """
    try:
        gs_cfg = st.secrets["connections"]["gsheets"]

        # Common pattern: connections.gsheets.service_account contains raw SA JSON
        sa_info = gs_cfg.get("service_account", gs_cfg)

        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        st.error(f"Failed to connect to Google Drive: {e}")
        return None

def load_knowledge_base_from_drive(folder_id: str):
    """Downloads all PDFs from the Drive folder and extracts text."""
    service = get_drive_service()
    if not service:
        return "", []

    full_text = ""
    file_list_summary = []

    try:
        results = service.files().list(
            q=f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false",
            fields="files(id, name)"
        ).execute()
        items = results.get("files", [])

        if not items:
            return "", []

        for item in items:
            request = service.files().get_media(fileId=item["id"])
            file_stream = io.BytesIO()
            downloader = MediaIoBaseDownload(file_stream, request)

            done = False
            while done is False:
                _, done = downloader.next_chunk()

            file_stream.seek(0)
            pdf_reader = PyPDF2.PdfReader(file_stream)

            file_text = ""
            for page in pdf_reader.pages:
                page_text = page.extract_text() or ""
                file_text += page_text + "\n"

            full_text += f"\n\n--- SOURCE: {item['name']} ---\n{file_text}"
            file_list_summary.append(item["name"])

    except Exception as e:
        st.error(f"Error reading from Drive: {e}")
        return "", []

    return full_text, file_list_summary

# ==========================================
# 5. INITIALIZE KB (ONCE PER SESSION)
# ==========================================
if not st.session_state.kb_text:
    folder_id = st.secrets["drive"]["folder_id"]
    with st.spinner("Loading Training Materials from Drive..."):
        text, files = load_knowledge_base_from_drive(folder_id)
        st.session_state.kb_text = text or ""
        st.session_state.file_names = files or []

# ==========================================
# 6. HARSH GRADING ENGINE
# ==========================================
def safe_json_loads(s: str):
    try:
        return json.loads(s)
    except Exception:
        return None

def calculate_final_grade_and_save(agent_name: str, kb_context: str):
    try:
        transcript = "\n".join(
            [f"{msg.get('role')}: {msg.get('content')}" for msg in st.session_state.chat_history]
        )

        coach_prompt = f"""
You are a MASTER SALES COACH.

TRAINING CONTEXT (The Correct Answers):
{kb_context[:200000]}

TRANSCRIPT TO GRADE:
{transcript}

INSTRUCTIONS:
1. Give a STRICT Score (0-10).
   - 0-4: If they missed the point, stayed silent, or gave weak one-word answers.
   - 5-8: Good effort but missed key phrases.
   - 9-10: Perfect execution of the "Perspective" close.
2. Identify specific strengths and weaknesses.
3. Provide the exact "Magic Words" they should have used.

OUTPUT JSON:
{{
  "score": 0,
  "feedback_summary": "Detailed feedback.",
  "magic_words": "Phrase 1, Phrase 2"
}}
"""

        model = genai.GenerativeModel("models/gemini-1.5-flash")
        response = model.generate_content(
            coach_prompt,
            generation_config={"response_mime_type": "application/json"}
        )

        result = safe_json_loads(getattr(response, "text", "") or "")
        if not result:
            raise ValueError("Model did not return valid JSON.")

        final_score = int(result.get("score", 0))
        feedback_summary = result.get("feedback_summary", "No feedback returned.")
        magic_words = result.get("magic_words", "")

        final_feedback = f"{feedback_summary}\n\n🔥 MEMORIZE THIS: {magic_words}".strip()

        # Save to Google Sheets
        conn = st.connection("gsheets", type=GSheetsConnection)
        existing_data = conn.read(ttl=0)

        new_row = pd.DataFrame([{
            "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Agent Name": agent_name,
            "Score": final_score,
            "Feedback": final_feedback
        }])

        if existing_data is None or getattr(existing_data, "empty", True):
            updated_df = new_row
        else:
            updated_df = pd.concat([existing_data, new_row], ignore_index=True)

        conn.update(data=updated_df)

        return final_score, final_feedback

    except Exception as e:
        return 0, f"Error generating grade: {e}"

# ==========================================
# 7. SIDEBAR
# ==========================================
with st.sidebar:
    st.title("🥋 Dojo Settings")
    st.success("🟢 AI Connected")

    agent_name = st.text_input("Agent Name", placeholder="Enter your name")

    if st.session_state.file_names:
        st.info(f"📚 {len(st.session_state.file_names)} Files Loaded")
    else:
        st.warning("⚠️ No Files Loaded")
        if st.button("🔄 Force Reload Drive"):
            st.session_state.kb_text = ""
            st.session_state.file_names = []
            st.rerun()

    voice_option = st.selectbox(
        "AI Voice",
        ["en-US-ChristopherNeural", "en-US-JennyNeural", "en-US-GuyNeural", "en-US-AriaNeural"],
        index=0
    )

    mode = st.radio("Select Training Mode", ["Roleplay as Realtor", "Roleplay as Homebuyer"])

    if st.button("Reset Session"):
        st.session_state.chat_history = []
        st.session_state.turn_count = 0
        st.session_state.roleplay_active = True
        st.session_state.session_started = False
        st.session_state.current_tip = None
        st.rerun()

# ==========================================
# 8. MODE 1: ROLEPLAY AS REALTOR
# ==========================================
if mode == "Roleplay as Realtor":
    st.title("🏡 Roleplay as Realtor")
    st.markdown("You are the **Realtor**. The AI is a **Skeptical Buyer**.")
    st.caption("OBJECTIVE: Handle ONE objection perfectly. You have 3 attempts.")

    if not agent_name:
        st.warning("Enter Agent Name in sidebar.")
        st.stop()

    if not st.session_state.kb_text:
        st.error("Knowledge Base Empty. Please click 'Force Reload Drive' in sidebar.")
        st.stop()

    # Progress Bar (Clamped)
    display_turn = min(st.session_state.turn_count, 3)
    prog_value = min(display_turn / 3, 1.0)
    st.progress(prog_value, text=f"Attempt {display_turn}/3")

    context_safe = st.session_state.kb_text[:500000]

    system_persona = f"""
You are a SKEPTICAL HOME BUYER.

CONTEXT:
{context_safe}

INSTRUCTIONS:
1. Start with ONE random objection.
2. STAY ON THIS OBJECTION. Do NOT change the topic.
3. BE STUBBORN. If the agent's answer is generic, push back. Say "I hear you, but..."
4. IMPORTANT: If the agent asks for permission (e.g., "Can I share a perspective?", "Can I ask a question?"),
   YOU MUST SAY "YES".
   - Do NOT say "I hear you but..." to a permission question.
   - Say: "Sure, go ahead." or "Okay, what is it?"
5. Always output JSON.
"""

    # --- START ---
    if not st.session_state.session_started:
        if st.button("🚀 Start Roleplay (Buyer Speaks First)", type="primary"):
            with st.spinner("Buyer is selecting a random objection..."):
                try:
                    model = genai.GenerativeModel(
                        "models/gemini-1.5-flash",
                        system_instruction=system_persona
                    )

                    init_prompt = """
Start the conversation. Pick one random objection from the context and say it to the agent.
Output JSON: {
  "response_text": "The objection you say",
  "strategy_tip": "One concise tip on how the agent should handle this specific objection."
}
"""

                    response = model.generate_content(
                        [init_prompt],
                        generation_config={"response_mime_type": "application/json"}
                    )

                    resp_json = safe_json_loads(getattr(response, "text", "") or "")
                    if not resp_json:
                        raise ValueError("Model did not return valid JSON.")

                    opening_line = resp_json.get("response_text", "I’m not sure this is a good idea.")
                    st.session_state.current_tip = resp_json.get("strategy_tip", "Acknowledge and validate.")

                    st.session_state.chat_history.append({"role": "Buyer", "content": opening_line})
                    st.session_state.session_started = True
                    st.session_state.turn_count = 1

                    tts_audio = run_async(text_to_speech(opening_line, voice_option))
                    play_audio_autoplay(tts_audio)

                    st.rerun()

                except Exception as e:
                    st.error(f"Error starting session: {e}")

    # --- MAIN LOOP ---
    else:
        for msg in st.session_state.chat_history:
            if msg.get("role") == "Buyer":
                st.info(f"**Buyer:** {msg.get('content','')}")
            else:
                st.write(f"**You:** {msg.get('content','')}")

        if st.session_state.current_tip:
            st.warning(f"💡 **Strategy Tip:** {st.session_state.current_tip}")

        audio_key = f"rec_{st.session_state.turn_count}"
        audio_input = st.audio_input("Record your response", key=audio_key)

        if st.button("🛑 Finish & Grade Session"):
            st.session_state.turn_count = 4
            st.rerun()

        if audio_input and st.session_state.roleplay_active and st.session_state.turn_count <= 3:
            with st.spinner("The Buyer is thinking..."):
                try:
                    audio_input.seek(0)
                    audio_bytes = audio_input.read()

                    if not audio_bytes or len(audio_bytes) < 100:
                        st.error("No audio captured.")
                        st.stop()

                    if audio_bytes[:4].startswith(b"RIFF"):
                        mime_type = "audio/wav"
                    else:
                        mime_type = "audio/webm"

                    model = genai.GenerativeModel(
                        "models/gemini-1.5-flash",
                        system_instruction=system_persona
                    )

                    history_context = "\n".join(
                        [f"{x.get('role')}: {x.get('content')}" for x in st.session_state.chat_history]
                    )

                    user_turn_prompt = f"""
HISTORY SO FAR:
{history_context}

INSTRUCTIONS:
1. Listen to the Agent.
2. DECISION:
   - IF they asked permission ("Can I...?", "May I...?"): Say "Yes" or "Go ahead."
   - IF they offered a solution: Critique it. Is it perfect?
     - YES: "Okay, I see your point. [End of objection]"
     - NO/WEAK: "I still don't see how that helps me." (STAY ON TOPIC)
3. Output JSON:
{{
  "response_text": "Spoken response",
  "strategy_tip": "Tip for the NEXT turn.",
  "suggested_response": "The PERFECT script they should have used."
}}
"""

                    response = model.generate_content(
                        [user_turn_prompt, {"mime_type": mime_type, "data": audio_bytes}],
                        generation_config={"response_mime_type": "application/json"}
                    )

                    response_json = safe_json_loads(getattr(response, "text", "") or "")
                    if not response_json:
                        raise ValueError("Model did not return valid JSON.")

                    ai_text = response_json.get("response_text", "")
                    st.session_state.current_tip = response_json.get("strategy_tip", "")

                    tts_audio = run_async(text_to_speech(ai_text, voice_option))

                    st.session_state.chat_history.append({"role": "Agent", "content": "(Audio Input)"})
                    st.session_state.chat_history.append({"role": "Buyer", "content": ai_text})
                    st.session_state.turn_count += 1

                    play_audio_autoplay(tts_audio)
                    st.rerun()

                except Exception as e:
                    st.error(f"AI Error: {e}")

    # --- FINAL GRADING ---
    if st.session_state.turn_count > 3:
        st.divider()
        st.header("🏁 Session Complete")

        if st.session_state.roleplay_active:
            with st.spinner("👨‍🏫 The Master Coach is grading your performance..."):
                score, feedback = calculate_final_grade_and_save(agent_name, st.session_state.kb_text)
                st.session_state.roleplay_active = False

                if score >= 8:
                    st.balloons()
                    color = "green"
                elif score >= 5:
                    color = "orange"
                else:
                    color = "red"

                st.markdown(f"## Final Score: :{color}[{score}/10]")
                st.info(f"**Coach's Feedback:**\n\n{feedback}")
                st.success("✅ Results saved to Google Sheets.")

# ==========================================
# 9. MODE 2: ROLEPLAY AS HOMEBUYER
# ==========================================
elif mode == "Roleplay as Homebuyer":
    st.title("🎓 Roleplay as Homebuyer")
    st.markdown("You act as the **Buyer**. Throw objections!")

    if not st.session_state.kb_text:
        st.error("Knowledge Base Empty. Please click 'Force Reload Drive' in sidebar.")
        st.stop()

    context_safe_mc = st.session_state.kb_text[:500000]
    system_persona_mc = f"""
You are the PERFECT REALTOR.

CONTEXT:
{context_safe_mc}

Output JSON:
{{
  "rebuttal_text": "...",
  "why_it_works": "..."
}}
"""

    audio_key_mc = f"mc_rec_{st.session_state.turn_count}"
    audio_input_mc = st.audio_input("State your objection", key=audio_key_mc)

    if audio_input_mc:
        with st.spinner("Formulating rebuttal..."):
            try:
                audio_input_mc.seek(0)
                audio_bytes_mc = audio_input_mc.read()

                if not audio_bytes_mc or len(audio_bytes_mc) < 100:
                    st.error("No audio captured.")
                    st.stop()

                if audio_bytes_mc[:4].startswith(b"RIFF"):
                    mime_type_mc = "audio/wav"
                else:
                    mime_type_mc = "audio/webm"

                model = genai.GenerativeModel(
                    "models/gemini-1.5-flash",
                    system_instruction=system_persona_mc
                )

                response_mc = model.generate_content(
                    ["Handle this objection perfectly:", {"mime_type": mime_type_mc, "data": audio_bytes_mc}],
                    generation_config={"response_mime_type": "application/json"}
                )

                resp_json_mc = safe_json_loads(getattr(response_mc, "text", "") or "")
                if not resp_json_mc:
                    raise ValueError("Model did not return valid JSON.")

                rebuttal = resp_json_mc.get("rebuttal_text", "")
                explanation = resp_json_mc.get("why_it_works", "")

                tts_audio_mc = run_async(text_to_speech(rebuttal, voice_option))

                st.success(f"**Agent Rebuttal:** {rebuttal}")
                play_audio_autoplay(tts_audio_mc)

                with st.expander("Why this works"):
                    st.write(explanation)

                st.session_state.turn_count += 1
                st.rerun()

            except Exception as e:
                st.error(f"Error: {e}")

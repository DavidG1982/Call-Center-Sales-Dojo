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
import time
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================
st.set_page_config(page_title="Sales Dojo", layout="wide", page_icon="🥋")

# Check for Secrets
required_secrets = ["GOOGLE_API_KEY", "drive"]
if not all(k in st.secrets for k in required_secrets):
    st.error("🚨 Setup Needed: Missing Google API Key or Drive Folder ID in secrets.")
    st.stop()

# Configure Gemini
genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])

# Initialize Session State
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "turn_count" not in st.session_state:
    st.session_state.turn_count = 0
if "roleplay_active" not in st.session_state:
    st.session_state.roleplay_active = True
if "session_started" not in st.session_state:
    st.session_state.session_started = False
if "current_tip" not in st.session_state:
    st.session_state.current_tip = None
if "kb_text" not in st.session_state:
    st.session_state.kb_text = ""
if "file_names" not in st.session_state:
    st.session_state.file_names = []

# ==========================================
# 2. GOOGLE DRIVE HELPER FUNCTIONS
# ==========================================
@st.cache_resource
def get_drive_service():
    try:
        # Convert secrets to a standard dict to avoid type issues
        service_account_info = dict(st.secrets["connections"]["gsheets"])
        
        # Create Credentials
        creds = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=['https://www.googleapis.com/auth/drive.readonly']
        )
        
        # FIX: cache_discovery=False prevents the SSL/Wrong Version error
        return build('drive', 'v3', credentials=creds, cache_discovery=False)
    except Exception as e:
        st.error(f"Failed to connect to Google Drive: {e}")
        return None

def load_knowledge_base_from_drive(folder_id):
    """Downloads all PDFs from the specific Drive folder and extracts text."""
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
        items = results.get('files', [])

        if not items:
            return "", []

        for item in items:
            request = service.files().get_media(fileId=item['id'])
            file_stream = io.BytesIO()
            downloader = MediaIoBaseDownload(file_stream, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
            file_stream.seek(0)
            pdf_reader = PyPDF2.PdfReader(file_stream)
            file_text = ""
            for page in pdf_reader.pages:
                file_text += page.extract_text() + "\n"
            full_text += f"\n\n--- SOURCE: {item['name']} ---\n{file_text}"
            file_list_summary.append(item['name'])
    except Exception as e:
        st.error(f"Error reading from Drive: {e}")
        return "", []

    return full_text, file_list_summary

async def text_to_speech(text, voice):
    try:
        communicate = edge_tts.Communicate(text, voice)
        mp3_data = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_data += chunk["data"]
        return mp3_data
    except Exception:
        return None

def play_audio_autoplay(audio_bytes):
    if audio_bytes:
        b64 = base64.b64encode(audio_bytes).decode()
        md = f"""
            <audio autoplay="true">
            <source src="data:audio/mp3;base64,{b64}" type="audio/mp3">
            </audio>
            """
        st.markdown(md, unsafe_allow_html=True)

# --- GRADING LOGIC ---
def calculate_final_grade_and_save(agent_name, kb_context):
    try:
        transcript = "\n".join([f"{msg['role']}: {msg['content']}" for msg in st.session_state.chat_history])
        
        coach_prompt = f"""
        You are a MASTER SALES COACH.
        
        TRAINING CONTEXT (The Correct Answers):
        {kb_context[:200000]}
        
        TRANSCRIPT TO GRADE:
        {transcript}
        
        INSTRUCTIONS:
        1. Give a STRICT Score (0-10).
           - 0-4: If they missed the point, stayed silent, or gave weak answers.
           - 5-8: Good effort but missed key phrases.
           - 9-10: Perfect execution.
        2. Identify specific strengths and weaknesses.
        3. Provide the exact "Magic Words" they should have used.
        
        OUTPUT JSON:
        {{
            "score": (integer 0-10),
            "feedback_summary": "Detailed feedback.",
            "magic_words": "Phrase 1, Phrase 2"
        }}
        """
        
        model = genai.GenerativeModel("models/gemini-1.5-flash")
        response = model.generate_content(
            coach_prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        result = json.loads(response.text)
        
        final_score = result["score"]
        final_feedback = f"{result['feedback_summary']} \n\n🔥 MEMORIZE THIS: {result['magic_words']}"
        
        conn = st.connection("gsheets", type=GSheetsConnection)
        existing_data = conn.read(ttl=0)
        new_row = pd.DataFrame([{
            "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Agent Name": agent_name,
            "Score": final_score,
            "Feedback": final_feedback
        }])
        updated_df = pd.concat([existing_data, new_row], ignore_index=True)
        conn.update(data=updated_df)
        
        return final_score, final_feedback
        
    except Exception as e:
        return 0, f"Error generating grade: {e}"

# ==========================================
# 3. SIDEBAR
# ==========================================
with st.sidebar:
    st.title("🥋 Dojo Settings")
    st.success("🟢 System Ready")
    
    agent_name = st.text_input("Agent Name", placeholder="Enter your name")
    
    # MANUAL LOAD BUTTON (Fixes Startup Crash)
    if st.button("📂 Load Training Data"):
        folder_id = st.secrets["drive"]["folder_id"]
        with st.spinner("Connecting to Drive..."):
            text, files = load_knowledge_base_from_drive(folder_id)
            if text:
                st.session_state.kb_text = text
                st.session_state.file_names = files
                st.rerun()
            else:
                st.error("Could not load files.")

    if st.session_state.file_names:
        st.info(f"📚 {len(st.session_state.file_names)} Files Loaded")
    else:
        st.warning("⚠️ No Data Loaded")
    
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
# 4. MODE 1: ROLEPLAY AS REALTOR
# ==========================================
if mode == "Roleplay as Realtor":
    st.title("🏡 Roleplay as Realtor")
    st.markdown("You are the **Realtor**. The AI is a **Skeptical Buyer**.")
    st.caption("OBJECTIVE: Handle ONE objection perfectly. You have 3 attempts.")
    
    if not agent_name:
        st.warning("Enter Agent Name in sidebar.")
        st.stop()
        
    if not st.session_state.kb_text:
        st.info("👈 Please click 'Load Training Data' in the sidebar to begin.")
        st.stop()

    # Progress Bar (Clamped to 100% to prevent crash)
    display_turn = min(st.session_state.turn_count, 3)
    prog_value = min(display_turn / 3.0, 1.0)
    st.progress(prog_value, text=f"Attempt {display_turn}/3")

    context_safe = st.session_state.kb_text[:500000]
    
    # --- STUBBORN PERSONA ---
    system_persona = f"""
    You are a SKEPTICAL HOME BUYER.
    
    CONTEXT:
    {context_safe}
    
    INSTRUCTIONS:
    1. Start with ONE random objection.
    2. STAY ON THIS OBJECTION. Do NOT change the topic.
    3. BE STUBBORN. If the agent's answer is generic, push back. Say "I hear you, but..."
    4. IMPORTANT: If the agent asks for permission (e.g., "Can I share a perspective?", "Can I ask a question?"), YOU MUST SAY "YES".
       - Do NOT say "I hear you but..." to a permission question.
       - Say: "Sure, go ahead." or "Okay, what is it?"
    5. Always output JSON.
    """

    # --- START ---
    if not st.session_state.session_started:
        if st.button("🚀 Start Roleplay (Buyer Speaks First)", type="primary"):
            with st.spinner("Buyer is selecting a random objection..."):
                try:
                    # Hardcoded Model for Stability
                    model = genai.GenerativeModel(
                        "models/gemini-1.5-flash",
                        system_instruction=system_persona
                    )
                    
                    init_prompt = ["""
                    Start the conversation. Pick one random objection from the context and say it to the agent.
                    Output JSON: {
                        "response_text": "The objection you say",
                        "strategy_tip": "One concise tip on how the agent should handle this specific objection."
                    }
                    """]
                    
                    response = model.generate_content(
                        init_prompt, 
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    resp_json = json.loads(response.text)
                    opening_line = resp_json.get("response_text", "Hello.")
                    st.session_state.current_tip = resp_json.get("strategy_tip", "Acknowledge and validate.")
                    
                    st.session_state.chat_history.append({"role": "Buyer", "content": opening_line})
                    st.session_state.session_started = True
                    st.session_state.turn_count = 1
                    
                    tts_audio = asyncio.run(text_to_speech(opening_line, voice_option))
                    play_audio_autoplay(tts_audio)
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"Error starting session: {e}")

    # --- MAIN LOOP ---
    else:
        # History
        for msg in st.session_state.chat_history:
            if msg["role"] == "Buyer":
                st.info(f"**Buyer:** {msg['content']}")
            else:
                st.write(f"**You:** {msg['content']}")

        if st.session_state.current_tip:
            st.warning(f"💡 **Strategy Tip:** {st.session_state.current_tip}")

        # DYNAMIC KEY FIX (Prevents Infinite Loop)
        audio_key = f"rec_{st.session_state.turn_count}"
        audio_input = st.audio_input("Record your response", key=audio_key)
        
        # Finish Button
        if st.button("🛑 Finish & Grade Session"):
            st.session_state.turn_count = 4 # Force end
            st.rerun()

        if audio_input and st.session_state.roleplay_active and st.session_state.turn_count <= 3:
             with st.spinner("The Buyer is thinking..."):
                
                audio_input.seek(0)
                audio_bytes = audio_input.read()
                
                if len(audio_bytes) < 100:
                    st.error("No audio captured.")
                    st.stop()
                
                if audio_bytes[:4].startswith(b'RIFF'):
                    mime_type = "audio/wav"
                else:
                    mime_type = "audio/webm"
                
                model = genai.GenerativeModel(
                    "models/gemini-1.5-flash",
                    system_instruction=system_persona
                )
                
                history_context = "\n".join([f"{x['role']}: {x['content']}" for x in st.session_state.chat_history])
                
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
                
                try:
                    response = model.generate_content(
                        [user_turn_prompt, {"mime_type": mime_type, "data": audio_bytes}],
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    response_json = json.loads(response.text)
                    ai_text = response_json.get("response_text", "")
                    
                    st.session_state.current_tip = response_json.get("strategy_tip", "")
                    better_response = response_json.get("suggested_response", "")
                    
                    tts_audio = asyncio.run(text_to_speech(ai_text, voice_option))
                    
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
# 5. MODE 2: ROLEPLAY AS HOMEBUYER
# ==========================================
elif mode == "Roleplay as Homebuyer":
    st.title("🎓 Roleplay as Homebuyer")
    st.markdown("You act as the **Buyer**. Throw objections!")
    
    if not st.session_state.kb_text:
        st.info("👈 Please click 'Load Training Data' in the sidebar to begin.")
        st.stop()

    context_safe_mc = st.session_state.kb_text[:500000]
    system_persona_mc = f"""
    You are the PERFECT REALTOR.
    CONTEXT: {context_safe_mc}
    Output JSON: {{ "rebuttal_text": "...", "why_it_works": "..." }}
    """
    
    # Dynamic Key for MC mode
    audio_key_mc = f"mc_rec_{st.session_state.turn_count}"
    audio_input_mc = st.audio_input("State your objection", key=audio_key_mc)
    
    if audio_input_mc and st.session_state.kb_text:
        with st.spinner("Formulating rebuttal..."):
            audio_input_mc.seek(0)
            audio_bytes_mc = audio_input_mc.read()
            
            if audio_bytes_mc[:4].startswith(b'RIFF'):
                mime_type_mc = "audio/wav"
            else:
                mime_type_mc = "audio/webm"

            try:
                model = genai.GenerativeModel(
                    "models/gemini-1.5-flash",
                    system_instruction=system_persona_mc
                )
                
                response_mc = model.generate_content(
                    ["Handle this objection perfectly:", {"mime_type": mime_type_mc, "data": audio_bytes_mc}],
                    generation_config={"response_mime_type": "application/json"}
                )
                resp_json_mc = json.loads(response_mc.text)
                rebuttal = resp_json_mc["rebuttal_text"]
                explanation = resp_json_mc["why_it_works"]
                
                tts_audio_mc = asyncio.run(text_to_speech(rebuttal, voice_option))
                
                st.success(f"**Agent Rebuttal:** {rebuttal}")
                play_audio_autoplay(tts_audio_mc)
                with st.expander("Why this works"):
                    st.write(explanation)
                
                # Increment to refresh widget
                st.session_state.turn_count += 1
                st.rerun()

            except Exception as e:
                st.error(f"Error: {e}")

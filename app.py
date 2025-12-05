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
import os
import tempfile
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

# ==========================================
# 2. MODEL SELECTOR
# ==========================================
def get_valid_model_name():
    try:
        candidates = [
            "models/gemini-1.5-flash",
            "models/gemini-1.5-flash-001",
            "models/gemini-1.5-pro",
            "models/gemini-2.0-flash-exp"
        ]
        my_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        for cand in candidates:
            if cand in my_models:
                return cand
        if my_models:
            return my_models[0]
        return None
    except Exception as e:
        return None

active_model_name = get_valid_model_name()

# ==========================================
# 3. GOOGLE DRIVE HELPER FUNCTIONS
# ==========================================
@st.cache_resource
def get_drive_service():
    try:
        service_account_info = st.secrets["connections"]["gsheets"]
        creds = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=['https://www.googleapis.com/auth/drive.readonly']
        )
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        st.error(f"Failed to connect to Google Drive: {e}")
        return None

@st.cache_data(ttl=3600) 
def load_knowledge_base_from_drive(folder_id):
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

# ==========================================
# 4. APP LOGIC
# ==========================================
folder_id = st.secrets["drive"]["folder_id"]
with st.spinner("Loading Training Materials..."):
    kb_text, file_names = load_knowledge_base_from_drive(folder_id)

async def text_to_speech(text, voice):
    communicate = edge_tts.Communicate(text, voice)
    mp3_data = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_data += chunk["data"]
    return mp3_data

def play_audio_autoplay(audio_bytes):
    b64 = base64.b64encode(audio_bytes).decode()
    md = f"""
        <audio autoplay="true">
        <source src="data:audio/mp3;base64,{b64}" type="audio/mp3">
        </audio>
        """
    st.markdown(md, unsafe_allow_html=True)

def save_scorecard(agent_name, score, feedback):
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        existing_data = conn.read(ttl=0)
        new_row = pd.DataFrame([{
            "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Agent Name": agent_name,
            "Score": score,
            "Feedback": feedback
        }])
        updated_df = pd.concat([existing_data, new_row], ignore_index=True)
        conn.update(data=updated_df)
        st.success("✅ Scorecard saved!")
    except Exception as e:
        st.error(f"Failed to save score: {e}")

# ==========================================
# 5. SIDEBAR
# ==========================================
with st.sidebar:
    st.title("🥋 Dojo Settings")
    if active_model_name:
        st.success(f"🟢 Brain: {active_model_name}")
    else:
        st.error("🔴 API Key Issue")
    
    agent_name = st.text_input("Agent Name", placeholder="Enter your name")
    
    if file_names:
        st.info(f"📚 {len(file_names)} Training Files Loaded")
    
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
        st.rerun()

# ==========================================
# 6. MODE 1: ROLEPLAY AS REALTOR
# ==========================================
if mode == "Roleplay as Realtor":
    st.title("🏡 Roleplay as Realtor")
    st.markdown("You are the **Realtor**. The AI is a **Skeptical Buyer**.")
    
    if not agent_name:
        st.warning("Enter Agent Name in sidebar.")
        st.stop()
        
    if not kb_text:
        st.error("Knowledge Base Empty.")

    progress = st.session_state.turn_count / 10
    st.progress(progress, text=f"Turn {st.session_state.turn_count}/10")

    audio_input = st.audio_input("Record your pitch/response")

    if audio_input and st.session_state.roleplay_active:
        if st.session_state.turn_count < 10:
            
            with st.spinner("The Buyer is thinking..."):
                # 1. Save Audio to Temp File (Solves format issues)
                audio_input.seek(0)
                audio_bytes = audio_input.read()
                
                if len(audio_bytes) < 100:
                    st.error("No audio captured.")
                    st.stop()

                with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_file:
                    tmp_file.write(audio_bytes)
                    tmp_path = tmp_file.name

                try:
                    # 2. Upload to Gemini
                    uploaded_file = genai.upload_file(tmp_path)
                    
                    # 3. Generate Content
                    model = genai.GenerativeModel(active_model_name)
                    context_safe = kb_text[:900000] 
                    
                    system_prompt = f"""
                    You are a SKEPTICAL HOME BUYER. User is Realtor.
                    CONTEXT: {context_safe}
                    INSTRUCTIONS:
                    1. Select 10 random objections from context.
                    2. Respond naturally using one.
                    3. OUTPUT JSON:
                    {{
                        "response_text": "Spoken response",
                        "coach_hints": ["Hint 1", "Hint 2"],
                        "suggested_response": "Perfect rebuttal script."
                    }}
                    """
                    
                    history_context = "\n".join([f"{x['role']}: {x['content']}" for x in st.session_state.chat_history])
                    full_prompt = f"{system_prompt}\n\nHISTORY:\n{history_context}\n\nRespond to the attached audio."

                    response = model.generate_content(
                        [full_prompt, uploaded_file],
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    response_json = json.loads(response.text)
                    ai_text = response_json["response_text"]
                    hints = response_json["coach_hints"]
                    better_response = response_json.get("suggested_response", "No suggestion.")
                    
                    tts_audio = asyncio.run(text_to_speech(ai_text, voice_option))
                    
                    st.session_state.chat_history.append({"role": "Agent", "content": "(Audio Input)"})
                    st.session_state.chat_history.append({"role": "Buyer", "content": ai_text})
                    st.session_state.turn_count += 1
                    
                    col1, col2 = st.columns([2, 1])
                    with col1:
                        st.info(f"**Buyer says:** {ai_text}")
                        play_audio_autoplay(tts_audio)
                    with col2:
                        with st.expander("💡 Suggested Answer", expanded=True):
                            st.success(better_response)
                        for hint in hints:
                            st.caption(f"• {hint}")
                            
                except Exception as e:
                    st.error(f"AI Error: {e}")
                finally:
                    # Cleanup temp file
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)

    # End Game
    if st.session_state.turn_count >= 10:
        st.header("🏁 Session Over!")
        if st.button("Save Scorecard"):
             save_scorecard(agent_name, 10, "Session Complete")

# ==========================================
# 7. MODE 2: ROLEPLAY AS HOMEBUYER
# ==========================================
elif mode == "Roleplay as Homebuyer":
    st.title("🎓 Roleplay as Homebuyer")
    st.markdown("You act as the **Buyer**. Throw objections!")
    
    audio_input_mc = st.audio_input("State your objection")
    
    if audio_input_mc and kb_text:
        with st.spinner("Formulating rebuttal..."):
            audio_input_mc.seek(0)
            audio_bytes_mc = audio_input_mc.read()
            
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_file:
                tmp_file.write(audio_bytes_mc)
                tmp_path = tmp_file.name

            try:
                uploaded_file = genai.upload_file(tmp_path)
                model = genai.GenerativeModel(active_model_name)
                
                system_prompt_mc = f"""
                You are the PERFECT REALTOR.
                CONTEXT: {kb_text[:900000]}
                Handle the objection.
                Output JSON: {{ "rebuttal_text": "...", "why_it_works": "..." }}
                """
                
                response_mc = model.generate_content(
                    [system_prompt_mc, uploaded_file],
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
            except Exception as e:
                st.error(f"Error: {e}")
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
